"""PostgreSQL database layer for sister (SQLModel + async SQLAlchemy).

Provides persistent storage for visura requests, responses, and structured
result tables (immobili, intestati). Includes cache lookup for deduplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import delete, func, inspect, or_, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import aliased, sessionmaker
from sqlmodel import select

from .db_models import (
    PROPERTY_FIELD_MAP,
    PROPERTY_LOCATION_FIELD_MAP,
    PROPERTY_SUBJECT_FIELD_MAP,
    ActivityLog,
    ActivityLogAttribute,
    ActivityLogPayload,
    CadastralUnit,
    Document,
    DocumentContent,
    DocumentExtraction,
    IstatMunicipality,
    MortgageInspection,
    MortgageInspectionParty,
    MortgageInspectionProperty,
    OwnershipRight,
    PageVisit,
    PageVisitError,
    PageVisitFormElement,
    Property,
    Request,
    RequestPayload,
    RequestResult,
    ResultOwner,
    RightKind,
    Subject,
)
from .provinces import province_code, province_name
from .reference import (
    LOOKUP_TABLES,
    cadastre_type_code,
    clean,
    ensure_lookup,
    fiscal_code_key,
    get_or_create_cadastral_unit,
    get_or_create_comune,
    get_or_create_right,
    get_or_create_subject,
    place_name,
)
from .result_parsers import (
    clean_amount,
    format_amount_it,
    format_consistency,
    format_it_date,
    format_share,
    identifier_kind,
    land_area_m2,
    normalize_category,
    normalize_class,
    normalize_owner,
    parse_amount_it,
    parse_classamento,
    parse_consistency,
    parse_ubicazione,
    parse_vis_imm_sel,
    split_foglio,
)
from .visura_xml_models import DocumentSubject

logger = logging.getLogger("sister")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)
DATA_ROOT = Path(os.getenv("SISTER_DATA_ROOT", str(PROJECT_ROOT))).expanduser().resolve()
DATABASE_DSN = os.getenv("DATABASE_DSN")
DATABASE_SCHEMA = os.getenv("DATABASE_SCHEMA", "sister")
DATABASE_ISTAT_SCHEMA = os.getenv("DATABASE_ISTAT_SCHEMA", "istat")
# raw payloads (response JSON, XML tree, extraction JSON) live in their own schema; tests point it at a throwaway one
DATABASE_ARCHIVE_SCHEMA = os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive")
DATABASE_REVISION = "20261030_request_jobs_fk"

# ---------------------------------------------------------------------------
# Engine and session
# ---------------------------------------------------------------------------

_engine = None
_db_writable: Optional[bool] = None


def is_db_writable() -> bool:
    """Return whether persistence is enabled and configured as writable."""
    global _db_writable
    if _db_writable is not None:
        return _db_writable
    if os.getenv("SISTER_DATABASE_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        _db_writable = False
        return _db_writable
    _db_writable = bool(DATABASE_DSN)
    if not _db_writable:
        logger.warning("Database DSN is missing — persistence is unavailable")
    return _db_writable


def _get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_DSN:
            raise RuntimeError("DATABASE_DSN must be set to a PostgreSQL connection URL")
        url = make_url(DATABASE_DSN)
        if url.get_backend_name() != "postgresql":
            raise RuntimeError("DATABASE_DSN must use PostgreSQL; other database backends are unsupported")
        if url.drivername in {
            "postgres",
            "postgresql",
            "postgresql+asyncpg",
            "postgresql+psycopg2",
            "postgresql+psycopg_async",
        }:
            url = url.set(drivername="postgresql+psycopg")
        _engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            execution_options={
                "schema_translate_map": {
                    "sister_archive": DATABASE_ARCHIVE_SCHEMA,
                    "istat": DATABASE_ISTAT_SCHEMA,
                }
            },
        )
    return _engine


def _get_session_factory():
    return sessionmaker(_get_engine(), class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Check the Postgres connection and require the current Alembic schema revision."""
    engine = _get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        # Sister owns a dedicated schema. Qualifying the schema here avoids a
        # false "incomplete schema" error when the connection's search_path is
        # changed by deployment configuration or a pooled connection.
        schema = DATABASE_SCHEMA
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names(schema=schema))
        )
        required_tables = {
            "cadastral_unit",
            "subject",
            "right_kind",
            "ownership_right",
            "request",
            "request_result",
            "property",
            "result_owner",
            "document",
            "document_subject",
            "property_group",
            "building_unit",
            "land_parcel",
            "ownership_mutation",
            "mutation_owner",
            "page_visit",
            "activity_log",
            "document_extraction",
            "mortgage_inspection",
            "parcel_geometry_cache",
            "sister_request_jobs",
            "sezioni_extraction_jobs",
            *LOOKUP_TABLES,
        }
        missing = sorted(required_tables - table_names)
        if missing:
            raise RuntimeError(
                "PostgreSQL schema is incomplete (missing: " + ", ".join(missing) + "). Run `alembic upgrade head`."
            )
        # We only need the names here. Reflecting full column types makes
        # SQLAlchemy warn about PostGIS's ``geometry`` columns in this shared
        # table, even though the application never maps those columns.
        istat_columns = set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'municipalities'"
                    ),
                    {"schema": DATABASE_ISTAT_SCHEMA},
                )
            ).scalars()
        )
        missing_istat = sorted(
            {
                "numeric_code",
                "name",
                "italian_name",
                "other_language_name",
                "province_abbreviation",
                "cadastral_code",
            }
            - istat_columns
        )
        if missing_istat:
            raise RuntimeError(
                f"Shared ISTAT table {DATABASE_ISTAT_SCHEMA}.municipalities is incomplete (missing: "
                + ", ".join(missing_istat)
                + ")."
            )
        archive_tables = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names(schema=DATABASE_ARCHIVE_SCHEMA))
        )
        missing_archive = sorted({"request_payload", "document_content"} - archive_tables)
        if missing_archive:
            raise RuntimeError(
                f"PostgreSQL archive schema {DATABASE_ARCHIVE_SCHEMA!r} is incomplete (missing: "
                + ", ".join(missing_archive)
                + "). Run `alembic upgrade head`."
            )
        view_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_view_names(schema=schema))
        )
        if "v_visura_cadastral_polygon" not in view_names:
            raise RuntimeError(
                "PostgreSQL schema is missing v_visura_cadastral_polygon. Run `alembic upgrade head`."
            )
        if "alembic_version" not in table_names:
            raise RuntimeError("PostgreSQL migrations are not installed. Run `alembic upgrade head`.")
        quoted_schema = conn.dialect.identifier_preparer.quote_schema(schema)
        version = (
            await conn.execute(text(f"SELECT version_num FROM {quoted_schema}.alembic_version"))
        ).scalar_one_or_none()
        if version != DATABASE_REVISION:
            raise RuntimeError(
                f"PostgreSQL schema is at {version!r}; expected {DATABASE_REVISION!r}. Run `alembic upgrade head`."
            )
    logger.info("Database PostgreSQL inizializzato (writable=%s)", is_db_writable())


# ---------------------------------------------------------------------------
# Durable request worker state
# ---------------------------------------------------------------------------


def _decode_request_job(row) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row[2]
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    return {
        "request_id": row[0],
        "request_type": row[1],
        "payload": payload or {},
        "status": row[3],
        "error_kind": row[4],
        "attempts": row[5],
        "side_effect_started": row[6],
        "lease_until": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


async def create_request_job(request_id: str, request_type: str, payload: dict[str, Any]) -> dict:
    """Persist a queued request and the full payload needed to recover it after restart."""
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        await session.execute(
            text("""INSERT INTO sister_request_jobs
                (request_id, request_type, payload_json, status, error_kind, attempts,
                 side_effect_started, lease_until, created_at, updated_at)
                VALUES (:id, :type, :payload, 'queued', NULL, 0, FALSE, NULL, :now, :now)
                ON CONFLICT (request_id) DO UPDATE SET
                    request_type = EXCLUDED.request_type,
                    payload_json = EXCLUDED.payload_json,
                    status = 'queued', error_kind = NULL, attempts = 0,
                    side_effect_started = FALSE, lease_until = NULL,
                    updated_at = EXCLUDED.updated_at"""),
            {
                "id": request_id,
                "type": request_type,
                "payload": json.dumps(payload, ensure_ascii=False),
                "now": now,
            },
        )
        await session.commit()
    job = await get_request_job(request_id)
    assert job is not None
    return job


async def get_request_job(request_id: str) -> dict[str, Any] | None:
    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                text("""SELECT request_id, request_type, payload_json, status, error_kind,
                             attempts, side_effect_started, lease_until, created_at, updated_at
                      FROM sister_request_jobs WHERE request_id = :id"""),
                {"id": request_id},
            )
        ).first()
    return _decode_request_job(row)


async def update_request_job(
    request_id: str,
    status: str,
    *,
    error_kind: str | None = None,
    side_effect_started: bool | None = None,
    lease_seconds: int = 180,
) -> bool:
    """Update a job state, counting a worker attempt only when it enters running."""
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        result = await session.execute(
            text("""UPDATE sister_request_jobs
                   SET status = CAST(:status AS VARCHAR),
                       error_kind = CASE
                           WHEN CAST(:error_kind AS VARCHAR) IS NOT NULL THEN CAST(:error_kind AS VARCHAR)
                           WHEN CAST(:status AS VARCHAR) IN ('completed', 'no_match', 'cancelled') THEN NULL
                           ELSE error_kind
                       END,
                       side_effect_started = COALESCE(CAST(:side_effect_started AS BOOLEAN), side_effect_started),
                       attempts = attempts + CASE WHEN CAST(:status AS VARCHAR) = 'running' AND status <> 'running' THEN 1 ELSE 0 END,
                       lease_until = CASE
                           WHEN CAST(:status AS VARCHAR) = 'running' THEN :now + (CAST(:lease_seconds AS INTEGER) * INTERVAL '1 second')
                           ELSE NULL
                       END,
                       updated_at = :now
                   WHERE request_id = :id"""),
            {
                "id": request_id,
                "status": status,
                "error_kind": error_kind,
                "side_effect_started": side_effect_started,
                "lease_seconds": lease_seconds,
                "now": now,
            },
        )
        await session.commit()
    return result.rowcount == 1


async def claim_request_job(request_id: str, lease_seconds: int = 180) -> bool:
    """Atomically claim one queued job and count exactly one attempt."""
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        result = await session.execute(
            text("""UPDATE sister_request_jobs
                   SET status = 'running', attempts = attempts + 1,
                       side_effect_started = FALSE,
                       lease_until = :now + (:lease_seconds * INTERVAL '1 second'),
                       updated_at = :now
                   WHERE request_id = :id AND status = 'queued'
                   RETURNING request_id"""),
            {"id": request_id, "lease_seconds": lease_seconds, "now": now},
        )
        await session.commit()
    return result.scalar_one_or_none() is not None


async def heartbeat_request_job(request_id: str, lease_seconds: int = 180) -> bool:
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        result = await session.execute(
            text("""UPDATE sister_request_jobs
                   SET lease_until = :now + (:lease_seconds * INTERVAL '1 second'), updated_at = :now
                   WHERE request_id = :id AND status = 'running' AND lease_until > :now
                   RETURNING request_id"""),
            {"id": request_id, "lease_seconds": lease_seconds, "now": now},
        )
        await session.commit()
    return result.scalar_one_or_none() is not None


async def list_request_jobs_for_recovery() -> list[dict[str, Any]]:
    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text("""SELECT request_id, request_type, payload_json, status, error_kind,
                             attempts, side_effect_started, lease_until, created_at, updated_at
                      FROM sister_request_jobs WHERE status IN ('queued', 'running')
                      ORDER BY created_at, request_id""")
            )
        ).fetchall()
    return [_decode_request_job(row) for row in rows]


# ---------------------------------------------------------------------------
# Activity log (traceability of operational actions)
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """A JSON-compatible copy of ``value`` (unknown types become strings) so a summary never fails to serialise."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


def _flatten_json(value: Any, path: str = "") -> list[tuple[str, Optional[str]]]:
    """One ``(path, text)`` row per leaf of a JSON value: ``{"a": [1, {"b": 2}]}`` → ``a.0`` = 1, ``a.1.b`` = 2.

    An empty object or list is a leaf (``{}`` / ``[]``); a bare scalar has the path ``$``.
    """
    if isinstance(value, dict) and value:
        return [leaf for key, item in value.items() for leaf in _flatten_json(item, f"{path}.{key}" if path else str(key))]
    if isinstance(value, list) and value:
        return [leaf for index, item in enumerate(value) for leaf in _flatten_json(item, f"{path}.{index}" if path else str(index))]
    if isinstance(value, (dict, list)):
        return [(path or "$", "{}" if isinstance(value, dict) else "[]")]
    if value is None:
        return [(path or "$", None)]
    if isinstance(value, bool):
        return [(path or "$", "true" if value else "false")]
    return [(path or "$", str(value))]


async def record_activity(
    action: str,
    *,
    actor: Optional[str] = None,
    source: str = "web",
    status: str = "success",
    target: Optional[str] = None,
    params: Optional[dict] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    started_at: Optional[datetime] = None,
    client_ip: Optional[str] = None,
) -> Optional[int]:
    """Append one row to ``activity_log`` and return its id (``None`` when it could not be written).

    Never raises: the audit trail must not break the action it describes. Keep ``params`` and ``result`` to small
    summaries (flags, counts); documents and personal data do not belong here. Each leaf of them is also stored as a
    row of ``activity_log_attribute``; the JSON as given goes to the archive.
    """
    if not is_db_writable():
        return None
    finished = datetime.now(timezone.utc)
    started = started_at or finished
    params = _json_safe(params)
    result = _json_safe(result)
    try:
        async with _get_session_factory()() as session:
            await ensure_lookup(session, "activity_action", action)
            row = ActivityLog(
                started_at=started,
                finished_at=finished,
                duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
                actor=(actor or "system")[:200],
                source=source,
                action=action,
                status=status,
                target=target,
                error=(error or None) and str(error)[:2000],
                client_ip=client_ip,
            )
            session.add(row)
            await session.flush()
            for section, value in (("params", params), ("result", result)):
                if value is None:
                    continue
                for leaf_path, leaf_value in _flatten_json(value):
                    session.add(ActivityLogAttribute(activity_id=row.id, section=section, path=leaf_path, value=leaf_value))
            if params is not None or result is not None:
                session.add(ActivityLogPayload(activity_id=row.id, params=params, result=result))
            await session.commit()
            return row.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Registro attivita' '%s' non scritto: %s", action, exc)
        return None


async def get_recent_activity(limit: int = 50, action: Optional[str] = None) -> list[dict]:
    """The newest ``activity_log`` rows (newest first), optionally filtered by action, with their params/result."""
    async with _get_session_factory()() as session:
        stmt = (
            select(ActivityLog, ActivityLogPayload)
            .outerjoin(ActivityLogPayload, ActivityLogPayload.activity_id == ActivityLog.id)
            .order_by(ActivityLog.started_at.desc(), ActivityLog.id.desc())
            .limit(limit)
        )
        if action:
            stmt = stmt.where(ActivityLog.action == action)
        rows = (await session.execute(stmt)).all()
    return [
        {**row.model_dump(mode="json"), "params": payload.params if payload else None, "result": payload.result if payload else None}
        for row, payload in rows
    ]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def compute_cache_key(request_type: str, **params) -> str:
    """Deterministic cache key from search parameters."""
    # Filter out None values and sort for determinism
    filtered = {k: v for k, v in params.items() if v is not None}
    canonical = json.dumps({"type": request_type, **filtered}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decode_payload(raw: Any) -> Optional[dict]:
    """The archived response JSON (stored as text) back to a dict; None when absent or not an object."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


async def find_cached_response(cache_key: str, ttl_seconds: Optional[int]) -> Optional[dict]:
    """Find a successful response matching the cache key, not older than ``ttl_seconds`` (None: any age)."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        conditions = [Request.cache_key == cache_key, Request.success == True]  # noqa: E712
        if ttl_seconds is not None:
            conditions.append(Request.responded_at >= datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds))
        stmt = (
            select(Request, RequestPayload.data)
            .outerjoin(RequestPayload, RequestPayload.request_id == Request.id)
            .where(*conditions)
            .order_by(Request.responded_at.desc())
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            return None
        request, data = row
        return {
            "request_id": request.request_ref,
            "success": request.success,
            "tipo_catasto": request.cadastre_type or "",
            "data": _decode_payload(data),
            "error": request.error,
            "created_at": request.responded_at.isoformat() if request.responded_at else None,
        }


# ---------------------------------------------------------------------------
# Request operations (same signatures as before)
# ---------------------------------------------------------------------------


async def _upsert_request(session: AsyncSession, request_id: str, request_type: str, unit_id: Optional[int], cache_key: Optional[str]) -> int:
    """Insert the request row (or update its type, unit and cache key); returns its numeric id."""
    request_type = await ensure_lookup(session, "request_type", request_type)
    row = (
        await session.execute(
            text(
                "INSERT INTO request (request_ref, request_type, cadastral_unit_id, cache_key, created_at) "
                "VALUES (:ref, :type, :unit, :key, :now) ON CONFLICT (request_ref) DO UPDATE SET "
                "request_type = EXCLUDED.request_type, cadastral_unit_id = EXCLUDED.cadastral_unit_id, "
                "cache_key = EXCLUDED.cache_key RETURNING id"
            ),
            {"ref": request_id, "type": request_type, "unit": unit_id, "key": cache_key, "now": datetime.now(timezone.utc)},
        )
    ).first()
    return row.id


async def save_request(
    request_id: str,
    request_type: str,
    tipo_catasto: str,
    provincia: str,
    comune: str,
    foglio: str,
    particella: str,
    sezione: Optional[str] = None,
    subalterno: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> None:
    """Persist a new request."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        unit_id = await get_or_create_cadastral_unit(
            session,
            cadastre_type=tipo_catasto,
            province=provincia,
            municipality=comune,
            sheet=foglio,
            parcel=particella,
            subunit=subalterno,
            section=sezione,
        )
        await _upsert_request(session, request_id, request_type, unit_id, cache_key)
        await session.commit()


async def save_requests_batch(requests: list[dict]) -> None:
    """Persist multiple requests atomically."""
    if not requests:
        return
    session_factory = _get_session_factory()
    async with session_factory() as session:
        for req in requests:
            unit_id = await get_or_create_cadastral_unit(
                session,
                cadastre_type=req["tipo_catasto"],
                province=req["provincia"],
                municipality=req["comune"],
                sheet=req["foglio"],
                parcel=req["particella"],
                subunit=req.get("subalterno"),
                section=req.get("sezione"),
            )
            await _upsert_request(session, req["request_id"], req["request_type"], unit_id, req.get("cache_key"))
        await session.commit()


# ---------------------------------------------------------------------------
# Response operations
# ---------------------------------------------------------------------------


_CATASTO_TYPE = {"F": "building", "T": "land", "E": "entity"}


def _property_row(
    response_id: str, tipo_catasto: str, item: dict
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """One listed immobile → (property fields, location fields, subject fields).

    Besides the plain column mapping this splits the composite cells of the portal lists: ``RA/103`` is sezione +
    foglio, ``Ubicazione`` is comune + provincia + indirizzo, ``Classamento`` is zona + categoria, the money cells
    lose their ``R.Euro:`` prefix, and the radio value (``visImmSel``) supplies the comune/sub the table omits.
    """
    prop_fields: dict[str, Any] = {
        "response_id": response_id,
        "property_type": _CATASTO_TYPE.get(tipo_catasto),
    }
    loc_fields: dict[str, str] = {
        "cadastre_type": tipo_catasto,
        "province": "",
        "municipality": "",
        "sheet": "",
        "parcel": "",
        "subunit": "",
        "section": "",
    }
    subject_fields: dict[str, Any] = {}
    for html_key, db_col in PROPERTY_FIELD_MAP.items():
        if html_key in item:
            prop_fields[db_col] = str(item[html_key]).strip() or None
    for html_key, loc_col in PROPERTY_LOCATION_FIELD_MAP.items():
        if html_key in item:
            loc_fields[loc_col] = str(item[html_key]).strip()
    for html_key, subject_col in PROPERTY_SUBJECT_FIELD_MAP.items():
        if html_key in item:
            subject_fields[subject_col] = str(item[html_key]).strip() or None

    if "Foglio" in item:
        section, loc_fields["sheet"] = split_foglio(item["Foglio"])
        loc_fields["section"] = loc_fields["section"] or section
    income_cell = str(prop_fields.get("income") or "")
    if "R.D." in income_cell:  # land results pack dominical and agricultural income in one cell
        for column_name, marker in (("dominical_income", r"R\.D\.\s*Euro:\s*([0-9.,]+)"), ("agricultural_income", r"R\.A\.\s*Euro:\s*([0-9.,]+)")):
            found = re.search(marker, income_cell)
            if found and not prop_fields.get(column_name):
                prop_fields[column_name] = found.group(1)
        prop_fields["income"] = None
    for column_name in ("income", "dominical_income", "agricultural_income"):
        if column_name in prop_fields:
            prop_fields[column_name] = clean_amount(prop_fields[column_name])
    area = land_area_m2(item)
    if area is not None:
        prop_fields["area"] = str(area)
    if item.get("Ubicazione"):
        place = parse_ubicazione(item["Ubicazione"])
        prop_fields.setdefault("address", place.get("address") or None)
        loc_fields["municipality"] = loc_fields["municipality"] or place.get("municipality", "")
    if item.get("Classamento"):
        for column_name, value in parse_classamento(item["Classamento"]).items():
            prop_fields.setdefault(column_name, value)
    radio = parse_vis_imm_sel(item.get("visImmSel"))
    # the row knows its own catasto: a response covering both (tipo E, workflows) mixes fabbricati and terreni
    row_catasto = next(
        (c for c in (item.get("_tipo_catasto"), item.get("Catasto"), radio.get("catasto")) if c in {"F", "T"}), None
    )
    if row_catasto:
        loc_fields["cadastre_type"] = row_catasto
        prop_fields["property_type"] = _CATASTO_TYPE[row_catasto]
    for loc_col in ("section", "subunit", "municipality"):
        loc_fields[loc_col] = loc_fields[loc_col] or radio.get(loc_col, "")
    loc_fields["province"] = loc_fields["province"] or str(item.get("provincia_nome") or "").strip()
    return prop_fields, loc_fields, subject_fields


def _parse_property_rows(
    response_id: str, tipo_catasto: str, data: Optional[dict]
) -> list[tuple[dict[str, Any], dict[str, str], dict[str, Any]]]:
    """Parse properties from response JSON.

    Returns (property_fields, location_fields, subject_fields) tuples. Location
    and subject fields are resolved to normalized ids by the caller.
    """
    if not data or not isinstance(data, dict):
        return []
    return [
        _property_row(response_id, tipo_catasto, item) for item in data.get("immobili", []) if isinstance(item, dict)
    ]


def _owner_pairs(rows: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Normalise a list of raw owners (Intestati rows, decoded radios, XML Intestato) to (subject, right) pairs."""
    pairs = []
    seen = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        subject_fields, right_fields = normalize_owner(item)
        key = (
            subject_fields.get("fiscal_code") or subject_fields.get("display_name"),
            right_fields.get("right_type"),
            right_fields.get("ownership_share"),
        )
        if key in seen:
            continue
        seen.add(key)
        pairs.append((subject_fields, right_fields))
    return pairs


def _parse_owners(response_id: str, data: Optional[dict]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Parse the top-level owners of a response JSON into (subject_fields, right_fields) pairs."""
    if not data or not isinstance(data, dict):
        return []
    return _owner_pairs(data.get("intestati"))


@dataclass
class ProjectedProperty:
    """A listed immobile with the owners found for it (``result_index`` ties them to one ``visura_results`` row)."""

    index: int  # 1-based position in the response's ``immobili`` list
    prop: dict[str, Any]
    location: dict[str, str]
    subject: dict[str, Any]
    owners: list[tuple[dict[str, Any], dict[str, Any]]] = dc_field(default_factory=list)
    result_index: Optional[int] = None
    visura_present: bool = False


def _subject_query_owner(data: dict, item: dict) -> Optional[dict[str, Any]]:
    """The queried person/company as owner of a row of its own property list (rows carry ``Titolarità``)."""
    identifier = str(data.get("soggetto") or "").strip().upper()
    if not identifier_kind(identifier) or not item.get("Titolarità"):
        return None
    return {"codice_fiscale": identifier, "Titolarità": item["Titolarità"]}


def _project_response(
    response_id: str, tipo_catasto: str, data: Optional[dict]
) -> tuple[list[ProjectedProperty], list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
    """Group a response's properties, owners and result rows so owners stay linked to *their* property.

    Where the owners of a property come from, in order: the per-property ``results[].intestati`` (visura flow), the
    ``intestati`` nested in the row (owner → immobili with owners), the queried subject itself when the row lists its
    ``Titolarità``. Top-level ``intestati`` of a response without per-property data belong to its only property, or
    stay unlinked when there are several. Returns (properties, unlinked owners, visura_results rows).
    """
    if not isinstance(data, dict):
        return [], [], []
    results = _parse_response_results(data)
    result_items = {
        row["result_index"]: item
        for row, item in zip(results, [r for r in data.get("results", []) if isinstance(r, dict)])
    }
    properties: list[ProjectedProperty] = []
    for position, item in enumerate(data.get("immobili", []), start=1):
        if not isinstance(item, dict):
            continue
        prop, loc, subject = _property_row(response_id, tipo_catasto, item)
        projected = ProjectedProperty(position, prop, loc, subject)
        result_item = result_items.get(position)
        raw_owners: list[dict] = []
        if result_item is not None:
            projected.result_index = position
            projected.visura_present = bool(result_item.get("visura"))
            raw_owners += [o for o in result_item.get("intestati") or [] if isinstance(o, dict)]
        raw_owners += [o for o in item.get("intestati") or [] if isinstance(o, dict)]
        queried = _subject_query_owner(data, item)
        if queried and not any(o.get("codice_fiscale") == queried["codice_fiscale"] for o in raw_owners):
            raw_owners.append(queried)
        projected.owners = _owner_pairs(raw_owners)
        properties.append(projected)

    top_level = _owner_pairs(data.get("intestati")) if not result_items else []
    unlinked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if top_level and not any(p.owners for p in properties):
        if len(properties) == 1:
            properties[0].owners = top_level
        else:
            unlinked = top_level

    for projected in properties:
        if projected.owners and projected.result_index is None:
            projected.result_index = projected.index
            results.append({"result_index": projected.index, "visura_present": False})
    return properties, unlinked, results


def _parse_page_visits(response_id: str, data: Optional[dict]) -> list[dict[str, Any]]:
    """Parse page_visits from response JSON into plain dicts (``step``, ``url``, ``screenshot_url``, ``timestamp``,
    ``form_elements``, ``errors``); the writer turns them into ``page_visit`` rows and their children."""
    if not data or not isinstance(data, dict):
        return []
    visits = data.get("page_visits", [])
    if not isinstance(visits, list):
        return []
    rows = []
    for item in visits:
        if not isinstance(item, dict):
            continue
        ts = None
        if item.get("timestamp"):
            try:
                ts = datetime.fromisoformat(item["timestamp"])
                if ts.tzinfo is None:
                    # timestamptz columns store aware values only; page timestamps are naive local time
                    ts = ts.astimezone()
            except (ValueError, TypeError):
                pass
        rows.append(
            {
                "step": item.get("step", ""),
                "url": item.get("url"),
                "screenshot_url": item.get("screenshot_url"),
                "timestamp": ts,
                "form_elements": [e for e in item.get("form_elements") or [] if isinstance(e, dict)],
                "errors": list(item.get("errors") or []),
            }
        )
    return rows


def _response_summary(data: Optional[dict]) -> dict[str, Any]:
    """Extract scalar response summary fields from the legacy payload."""
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("total_results", "total_intestati", "skipped_soppresso"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    if isinstance(data.get("soggetto"), str):
        result["subject_query"] = data["soggetto"]
    return result


def _parse_response_results(data: Optional[dict]) -> list[dict[str, Any]]:
    """Flatten the response ``results`` collection to scalar result rows."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return []
    rows = []
    for position, item in enumerate(data["results"], start=1):
        if not isinstance(item, dict):
            continue
        raw_index = item.get("result_index", position)
        try:
            result_index = int(raw_index)
        except (TypeError, ValueError):
            result_index = position
        rows.append({"result_index": result_index, "visura_present": bool(item.get("visura"))})
    return rows


async def _link_documents(session: AsyncSession, request_pk: int, data: Optional[dict]) -> int:
    """Tie the documents a response downloaded (``downloaded_pdfs``) to it; they are saved before the response."""
    documents = data.get("downloaded_pdfs") if isinstance(data, dict) else None
    linked = 0
    for item in documents if isinstance(documents, list) else []:
        if not isinstance(item, dict):
            continue
        names = {Path(str(v)).name for v in (item.get("filename"), item.get("original_filename"), item.get("path"), item.get("extracted_path")) if v}
        if not names:
            continue
        result = await session.execute(
            text("UPDATE document SET request_id = :rid WHERE request_id IS NULL AND file_name = ANY(:names)"),
            {"rid": request_pk, "names": sorted(names)},
        )
        linked += result.rowcount or 0
    return linked


_OFFICE = re.compile(r"^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$")


def _amount(value: Any) -> Optional[Decimal]:
    return parse_amount_it(value)


async def _property_columns(session: AsyncSession, prop: dict[str, Any]) -> dict[str, Any]:
    """The parsed strings of one listed immobile → the typed columns of ``property`` (lookups are created on the way)."""
    category, description = normalize_category(prop.get("category"))
    if category:
        await ensure_lookup(session, "cadastral_category", category, description)
    quality = (clean(prop.get("quality")) or "").upper() or None
    if quality:
        await ensure_lookup(session, "land_quality", quality)
    consistency_value, consistency_unit = parse_consistency(prop.get("consistency"))
    area = None
    if clean(prop.get("area")):
        try:
            area = Decimal(str(prop["area"]).replace(",", ".").strip())
        except InvalidOperation:
            area = None
    office = _OFFICE.match(str(prop.get("registered_office") or ""))
    if office:
        office_comune = await get_or_create_comune(session, office.group(1), office.group(2))
    else:  # entity results may carry the seat as separate province / municipality cells
        office_comune = await get_or_create_comune(session, prop.get("municipality"), prop.get("province"))
    return {
        "property_type": prop.get("property_type"),
        "address": clean(prop.get("address")),
        "partita": clean(prop.get("partita")),
        "category": category,
        "cadastral_class": normalize_class(prop.get("cadastral_class")),
        "consistency_value": consistency_value,
        "consistency_unit": consistency_unit,
        "census_zone": clean(prop.get("census_zone")),
        "land_quality": quality,
        "area_sqm": area,
        "income_eur": _amount(prop.get("income")),
        "dominical_income_eur": _amount(prop.get("dominical_income")),
        "agricultural_income_eur": _amount(prop.get("agricultural_income")),
        "registered_office_comune_id": office_comune,
    }


async def _replace_projection(session: AsyncSession, request_pk: int, tipo_catasto: str, data: Optional[dict]) -> None:
    """Rebuild the structured rows of one response from its JSON (results, properties, owners, document links)."""
    for table_name in ("result_owner", "property", "request_result"):
        await session.execute(text(f"DELETE FROM {table_name} WHERE request_id = :rid"), {"rid": request_pk})  # noqa: S608
    # the request's own unit supplies the province/municipality a row of the list does not carry
    unit = (
        await session.execute(
            select(
                IstatMunicipality.province_abbreviation.label("province_code"),
                func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name)).label("municipality"),
            )
            .select_from(Request)
            .join(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(Request.id == request_pk)
        )
    ).first()
    await _persist_projection(session, request_pk, tipo_catasto, data, unit)
    await _link_documents(session, request_pk, data)


async def backfill_projections(limit: Optional[int] = None, batch: int = 50) -> dict[str, int]:
    """Re-project every stored response with the current parsers (idempotent; the archived JSON is the source).

    Fills ``result_id`` / ``owner_index`` on existing properties and owners, splits the composite portal cells and
    ties downloaded documents to their response.
    """
    session_factory = _get_session_factory()
    stats = {"responses": 0, "properties": 0, "owners": 0}
    async with session_factory() as session:
        stmt = (
            select(Request.id, Request.cadastre_type)
            .join(RequestPayload, RequestPayload.request_id == Request.id)
            .order_by(Request.responded_at)
        )
        if limit:
            stmt = stmt.limit(limit)
        todo = (await session.execute(stmt)).all()
    for start in range(0, len(todo), batch):
        async with session_factory() as session:
            for request_pk, cadastre_type in todo[start : start + batch]:
                payload = await session.get(RequestPayload, request_pk)
                data = _decode_payload(payload.data) if payload else None
                if data is None:
                    continue
                await _replace_projection(session, request_pk, cadastre_type or "", data)
                stats["responses"] += 1
            await session.commit()
    async with session_factory() as session:
        for key, table_name in (("properties", "property"), ("owners", "result_owner")):
            stats[key] = (await session.execute(text(f"SELECT count(*) FROM {table_name}"))).scalar_one()  # noqa: S608
    return stats


async def _resolve_subject(session: AsyncSession, fields: dict[str, Any]) -> Optional[int]:
    """Create/enrich the subject for normalised owner fields (the birth place becomes a ``comune``)."""
    fields = dict(fields)
    fields.pop("registered_office", None)
    fields.pop("birth_place_id", None)
    if not fields:
        return None
    return await get_or_create_subject(session, **fields)


async def _persist_projection(
    session: AsyncSession,
    request_pk: int,
    tipo_catasto: str,
    data: Optional[dict],
    request_unit: Any,
) -> None:
    """Write the properties, owners and result rows of a response, keeping each owner tied to its property.

    One ``request_result`` row per property that has a result (or owners); the property and its owners both carry
    that ``result_id`` (the owner↔property views join on it), and ``owner_index`` keeps the portal's row order.
    """
    properties, unlinked, result_rows = _project_response("", tipo_catasto, data)
    result_ids: dict[int, int] = {}
    for result_fields in result_rows:
        row = RequestResult(request_id=request_pk, **result_fields)
        session.add(row)
        await session.flush()
        result_ids[result_fields["result_index"]] = row.id

    seen_owners: set[tuple] = set()

    async def add_owner(result_id: Optional[int], owner_index: int, subject_fields: dict, right_fields: dict) -> None:
        subject_id = await _resolve_subject(session, subject_fields) if subject_fields else None
        right_id = await get_or_create_right(session, **right_fields) if right_fields else None
        key = (result_id, subject_id, right_id)
        if key in seen_owners:  # (request, result, subject, right) is unique
            return
        seen_owners.add(key)
        session.add(ResultOwner(request_id=request_pk, result_id=result_id, owner_index=owner_index, subject_id=subject_id, right_id=right_id))

    for projected in properties:
        loc_fields = dict(projected.location)
        if request_unit is not None:
            loc_fields["province"] = loc_fields["province"] or (request_unit.province_code or "")
            loc_fields["municipality"] = loc_fields["municipality"] or (request_unit.municipality or "")
        unit_id = await get_or_create_cadastral_unit(session, **loc_fields)
        subject_id = await _resolve_subject(session, projected.subject) if projected.subject else None
        result_id = result_ids.get(projected.result_index) if projected.result_index is not None else None
        session.add(
            Property(
                request_id=request_pk,
                result_id=result_id,
                cadastral_unit_id=unit_id,
                subject_id=subject_id,
                **await _property_columns(session, projected.prop),
            )
        )
        for owner_index, (subject_fields, right_fields) in enumerate(projected.owners, start=1):
            await add_owner(result_id, owner_index, subject_fields, right_fields)
    for owner_index, (subject_fields, right_fields) in enumerate(unlinked, start=1):
        await add_owner(None, owner_index, subject_fields, right_fields)
    await session.flush()


async def _request_pk(session: AsyncSession, request_id: str) -> Optional[int]:
    return (await session.execute(text("SELECT id FROM request WHERE request_ref = :r"), {"r": request_id})).scalar_one_or_none()


async def save_response(
    request_id: str,
    success: bool,
    tipo_catasto: str,
    data: Optional[dict] = None,
    error: Optional[str] = None,
    export: bool = True,
) -> None:
    """Persist a response and populate structured tables."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        request_pk = await _request_pk(session, request_id)
        if request_pk is None:  # a response saved without its request row: keep it, the request type stays unknown
            request_pk = await _upsert_request(session, request_id, "", None, None)
        # Delete the previous page visits of this request (upsert)
        for child in ("page_visit_form_element", "page_visit_error"):
            await session.execute(
                text(f"DELETE FROM {child} WHERE page_visit_id IN (SELECT id FROM page_visit WHERE request_id = :rid)"),  # noqa: S608
                {"rid": request_pk},
            )
        await session.execute(text("DELETE FROM page_visit WHERE request_id = :rid"), {"rid": request_pk})

        summary = _response_summary(data)
        await session.execute(
            text(
                "UPDATE request SET success = :success, cadastre_type = :ct, error = :error, total_results = :tr, "
                "total_intestati = :ti, skipped_soppresso = :ss, subject_query = :sq, responded_at = :now WHERE id = :id"
            ),
            {
                "success": success,
                "ct": cadastre_type_code(tipo_catasto),
                "error": error,
                "tr": summary.get("total_results"),
                "ti": summary.get("total_intestati"),
                "ss": summary.get("skipped_soppresso"),
                "sq": summary.get("subject_query"),
                "now": datetime.now(timezone.utc),
                "id": request_pk,
            },
        )
        # the raw response is kept, as sent, in the archive (nothing is read back from it except by the cache / detail page)
        await session.execute(delete(RequestPayload).where(RequestPayload.request_id == request_pk))
        if data is not None:
            session.add(RequestPayload(request_id=request_pk, data=json.dumps(data, ensure_ascii=False, default=str)))
        await session.flush()

        await _replace_projection(session, request_pk, tipo_catasto, data)
        await session.flush()
        for visit in _parse_page_visits(request_id, data):
            page_visit = PageVisit(
                request_id=request_pk,
                step=visit["step"],
                url=visit["url"],
                screenshot_url=visit["screenshot_url"],
                visited_at=visit["timestamp"],
            )
            session.add(page_visit)
            await session.flush()
            for element_index, element in enumerate(visit["form_elements"]):
                session.add(
                    PageVisitFormElement(
                        page_visit_id=page_visit.id,
                        element_index=element_index,
                        tag=str(element.get("tag") or ""),
                        element_type=clean(element.get("type")),
                        name=str(element.get("name") or ""),
                        label=str(element.get("label") or ""),
                        value=str(element.get("value") or ""),
                    )
                )
            for error_index, message in enumerate(visit["errors"]):
                if isinstance(message, dict):
                    message = message.get("message") or message.get("text") or ""
                session.add(PageVisitError(page_visit_id=page_visit.id, error_index=error_index, message=str(message or "")))

        await session.commit()

    # Export to outputs/ directory
    if export:
        _export_response_file(request_id, success, tipo_catasto, data, error)


OUTPUTS_DIR = os.getenv("SISTER_OUTPUTS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "outputs"))


def _export_response_file(
    request_id: str,
    success: bool,
    tipo_catasto: str,
    data: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Write response JSON to outputs/ directory."""
    try:
        outputs_dir = Path(OUTPUTS_DIR)
        outputs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "request_id": request_id,
            "success": success,
            "tipo_catasto": tipo_catasto,
            "data": data,
            "error": error,
            "exported_at": datetime.now().isoformat(),
        }
        filename = f"{request_id}_{ts}.json"
        (outputs_dir / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        logger.info("Response exported to outputs/%s", filename)
    except Exception as e:
        logger.warning("Failed to export response file: %s", e)


def document_file_path(file_name: Optional[str]) -> Optional[str]:
    """Full path of a stored document: the documents directory (``SISTER_FILES_BASE``) plus its file name."""
    if not file_name:
        return None
    return str(Path(os.getenv("SISTER_FILES_BASE", str(DATA_ROOT / "documents"))).expanduser() / file_name)


async def get_response(request_id: str) -> Optional[dict]:
    """Fetch a stored response by request_id. Returns None if the request has no response yet."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Request, RequestPayload.data)
                .outerjoin(RequestPayload, RequestPayload.request_id == Request.id)
                .where(Request.request_ref == request_id)
            )
        ).first()
    if row is None or row[0].responded_at is None:
        return None
    request, data = row
    return {
        "request_id": request.request_ref,
        "success": request.success,
        "tipo_catasto": request.cadastre_type or "",
        "data": _decode_payload(data),
        "error": request.error,
        "created_at": request.responded_at.isoformat() if request.responded_at else None,
    }


async def get_result_record(request_id: str) -> Optional[dict]:
    """Fetch joined request/response data for the web results detail page.

    Returns None only when the request itself does not exist. Requests without a
    response are returned with ``status='pending'`` so the UI can distinguish
    pending work from a genuinely unknown request id.
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(Request, RequestPayload.data, CadastralUnit, IstatMunicipality)
            .outerjoin(RequestPayload, RequestPayload.request_id == Request.id)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(Request.request_ref == request_id)
        )
        row = (await session.execute(stmt)).first()
    if row is None:
        return None

    req, raw_data, unit, comune = row
    responded = req.responded_at is not None
    status = "pending"
    if responded:
        status = "completed" if req.success else "failed"
    data = _decode_payload(raw_data)
    return {
        "request_id": req.request_ref,
        "request_type": req.request_type,
        "tipo_catasto": unit.cadastre_type if unit and unit.cadastre_type else "",
        "provincia": _province_label(comune.province_abbreviation if comune else None),
        "comune": _municipality_name(comune) or "",
        "foglio": unit.sheet if unit and unit.sheet else "",
        "particella": unit.parcel if unit and unit.parcel else "",
        "sezione": unit.section if unit else None,
        "subalterno": unit.subunit if unit else None,
        "cost_text": None,
        "cost_value": None,
        "requested_at": req.created_at.isoformat() if req.created_at else None,
        "responded_at": req.responded_at.isoformat() if req.responded_at else None,
        "success": req.success if responded else None,
        "status": status,
        "data": data,
        "error": req.error,
        "page_visits": data.get("page_visits", []) if data and isinstance(data.get("page_visits"), list) else [],
    }


def _province_label(code: Optional[str]) -> str:
    """The province as the pages and forms show it: its name (``Trieste``), the sigla where ISTAT has no name for it."""
    return province_name(code) or code or ""


def _municipality_name(municipality: Optional[IstatMunicipality]) -> Optional[str]:
    """Return the Italian municipality name in the uppercase form used by SISTER results."""
    name = (municipality.italian_name or municipality.name or "").strip() if municipality else ""
    return name.upper() or None


def _birth_place(subject: Subject, municipality: Optional[IstatMunicipality]) -> Optional[str]:
    """Format a resolved ISTAT birthplace or its preserved foreign/historical text."""
    name = _municipality_name(municipality) or subject.birth_municipality_text
    province = municipality.province_abbreviation if municipality else subject.birth_province_code
    if name and province:
        return f"{name} ({province})"
    return name


def _area_text(value: Optional[Decimal]) -> Optional[str]:
    """500.00 → ``500`` (the portal lists whole square metres)."""
    if value is None:
        return None
    return str(int(value)) if value == value.to_integral_value() else f"{value:f}"


async def get_db_properties_for_response(request_id: str) -> list[dict]:
    """Return the ``property`` rows of a request with unit and subject joins (display text, as before the typed schema)."""
    session_factory = _get_session_factory()
    office = aliased(IstatMunicipality)
    async with session_factory() as session:
        stmt = (
            select(Property, CadastralUnit, IstatMunicipality, Subject, office)
            .join(Request, Request.id == Property.request_id)
            .outerjoin(CadastralUnit, Property.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .outerjoin(Subject, Property.subject_id == Subject.id)
            .outerjoin(office, Property.registered_office_comune_id == office.numeric_code)
            .where(Request.request_ref == request_id)
            .order_by(Property.id)
        )
        rows = (await session.execute(stmt)).all()
    return [
        {
            "result_id": prop.result_id,
            "property_type": prop.property_type,
            "address": prop.address,
            "partita": prop.partita,
            "category": prop.category,
            "cadastral_class": prop.cadastral_class,
            "consistency": format_consistency(prop.consistency_value, prop.consistency_unit),
            "income": format_amount_it(prop.income_eur),
            "census_zone": prop.census_zone,
            "quality": prop.land_quality,
            "area": _area_text(prop.area_sqm),
            "dominical_income": format_amount_it(prop.dominical_income_eur),
            "agricultural_income": format_amount_it(prop.agricultural_income_eur),
            "registered_office": (
                f"{_municipality_name(seat)} ({seat.province_abbreviation})"
                if seat and seat.province_abbreviation
                else _municipality_name(seat)
            ),
            "subject_province": seat.province_abbreviation if seat else None,
            "subject_municipality": _municipality_name(seat),
            "province": (_province_label(comune.province_abbreviation) or None) if comune else None,
            "municipality": _municipality_name(comune),
            "sheet": unit.sheet if unit else None,
            "parcel": unit.parcel if unit else None,
            "subunit": unit.subunit if unit else None,
            "section": unit.section if unit else None,
            "cadastre_type": unit.cadastre_type if unit else None,
            "subject_name": subj.name if subj else None,
            "subject_fiscal_code": subj.fiscal_code if subj else None,
        }
        for prop, unit, comune, subj, seat in rows
    ]


async def get_db_owners_for_response(request_id: str) -> list[dict]:
    """Return the ``result_owner`` rows of a request with subject and right joins (display text, as before)."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(ResultOwner, Subject, OwnershipRight, RightKind)
            .join(Request, Request.id == ResultOwner.request_id)
            .outerjoin(Subject, ResultOwner.subject_id == Subject.id)
            .outerjoin(OwnershipRight, ResultOwner.right_id == OwnershipRight.id)
            .outerjoin(RightKind, OwnershipRight.right_kind_id == RightKind.id)
            .where(Request.request_ref == request_id)
            .order_by(ResultOwner.id)
        )
        rows = (await session.execute(stmt)).all()
    return [
        {
            "nominativo": subj.name if subj else None,
            "fiscal_code": subj.fiscal_code if subj else None,
            "gender": subj.gender if subj else None,
            "date_of_birth": format_it_date(subj.birth_date) if subj and subj.birth_date else None,
            "subject_type": subj.subject_type if subj else None,
            "result_id": owner.result_id,
            "owner_index": owner.owner_index,
            "right_type": kind.right_type if kind else None,
            "ownership_share": (format_share(right.share_numerator, right.share_denominator) or None) if right else None,
            "right_code": kind.right_code if kind else None,
            "right_description": kind.description if kind else None,
            "start_date": (format_it_date(right.start_date) or None) if right else None,
            "end_date": (format_it_date(right.end_date) or None) if right else None,
        }
        for owner, subj, right, kind in rows
    ]


_ROME = ZoneInfo("Europe/Rome")


def _format_requested(value: Optional[datetime]) -> Optional[str]:
    """The portal's request timestamp as the Richieste table prints it: ``dd/mm/yyyy HH:MM:SS`` in Italian time."""
    return value.astimezone(_ROME).strftime("%d/%m/%Y %H:%M:%S") if value else None


def _document_record(doc: Document, unit: Optional[CadastralUnit], comune: Optional[IstatMunicipality], *, request_ref: Optional[str] = None,
                     content: Optional[str] = None) -> dict[str, Any]:
    """One document + its cadastral unit as the dict the document pages and the browse list use."""
    return {
        "id": doc.id,
        "response_id": request_ref,
        "document_type": "ispezione_ipotecaria" if (doc.file_name or "").casefold().startswith("isp_") else doc.document_type,
        "file_format": doc.file_format,
        "filename": doc.file_name,
        "file_path": document_file_path(doc.file_name),
        "file_size": doc.file_size,
        "oggetto": doc.subject_text,
        "richiesta_del": _format_requested(doc.requested_at),
        "provincia": (_province_label(comune.province_abbreviation) or None) if comune else None,
        "comune": _municipality_name(comune),
        "foglio": unit.sheet if unit else None,
        "particella": unit.parcel if unit else None,
        "subalterno": unit.subunit if unit else None,
        "sezione_urbana": unit.section if unit else None,
        "tipo_catasto": unit.cadastre_type if unit else None,
        "visura_subtype": doc.view_subtype,
        "situazione_al": format_it_date(doc.reference_date) or None,
        "xml_content": content or "",
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


_DOCUMENT_COLUMNS = (Document, CadastralUnit, IstatMunicipality, DocumentContent.content, Request.request_ref)


def _document_select():
    """``select`` of a document with its unit, comune, archived text and request reference (all outer joins)."""
    return (
        select(*_DOCUMENT_COLUMNS)
        .select_from(Document)
        .outerjoin(CadastralUnit, Document.cadastral_unit_id == CadastralUnit.id)
        .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
        .outerjoin(DocumentContent, DocumentContent.document_id == Document.id)
        .outerjoin(Request, Document.request_id == Request.id)
    )


async def get_documents_for_response(request_id: str, foglio: str = None, particella: str = None) -> list[dict]:
    """Fetch documents linked to a request OR matching foglio/particella."""
    stmt = _document_select().order_by(Document.created_at.desc())
    if foglio and particella:
        stmt = stmt.where(
            or_(Request.request_ref == request_id, (CadastralUnit.sheet == foglio) & (CadastralUnit.parcel == particella))
        )
    else:
        stmt = stmt.where(Request.request_ref == request_id)
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [_document_record(doc, unit, comune, request_ref=ref, content=content) for doc, unit, comune, content, ref in rows]


def _reference_values(value: str) -> set[str]:
    """A sheet / parcel / sub number in the spellings the portal uses: ``12``, ``012``, ``0012`` …"""
    raw = str(value or "").strip().upper()
    values = {raw}
    if raw.isdigit():
        normalized = str(int(raw))
        values.add(normalized)
        values.update(normalized.zfill(width) for width in (2, 3, 4, 5))
    return values


async def get_cadastral_visura_history_documents(properties: list[dict], *, per_location: int = 5) -> list[dict]:
    """Fetch recent XML visure for the exact cadastral units in a subject visura."""
    if not properties:
        return []

    clauses = []
    seen = set()
    for item in properties:
        if not isinstance(item, dict):
            continue
        province = province_code(item.get("province"))
        municipality = place_name(item.get("municipality"))
        sheet = str(item.get("sheet") or "").strip()
        parcel = str(item.get("parcel") or "").strip()
        cadastre_type = str(item.get("cadastre_type") or "").strip().upper()
        subunit = str(item.get("subunit") or "").strip()
        section = str(item.get("section") or "").strip().upper()
        if not all((province, municipality, sheet, parcel)) or cadastre_type not in {"F", "T"}:
            continue
        key = (province, municipality, cadastre_type, section, tuple(sorted(_reference_values(sheet))),
               tuple(sorted(_reference_values(parcel))), tuple(sorted(_reference_values(subunit))))
        if key in seen:
            continue
        seen.add(key)
        clauses.append(
            (func.upper(IstatMunicipality.province_abbreviation) == province)
            & (func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name)) == municipality)
            & (CadastralUnit.cadastre_type == cadastre_type)
            & (func.coalesce(CadastralUnit.section, "") == section)
            & func.upper(func.coalesce(CadastralUnit.sheet, "")).in_(_reference_values(sheet))
            & func.upper(func.coalesce(CadastralUnit.parcel, "")).in_(_reference_values(parcel))
            & func.upper(func.coalesce(CadastralUnit.subunit, "")).in_(_reference_values(subunit))
        )
    if not clauses:
        return []

    async with _get_session_factory()() as session:
        ranked = (
            select(
                Document.id.label("document_id"),
                Document.document_type.label("document_type"),
                Document.file_name.label("filename"),
                Document.created_at.label("created_at"),
                Document.view_subtype.label("view_subtype"),
                Document.reference_date.label("reference_date"),
                DocumentContent.content.label("xml_content"),
                CadastralUnit.cadastre_type.label("cadastre_type"),
                IstatMunicipality.province_abbreviation.label("province"),
                func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name)).label("municipality"),
                CadastralUnit.sheet.label("sheet"),
                CadastralUnit.parcel.label("parcel"),
                CadastralUnit.subunit.label("subunit"),
                CadastralUnit.section.label("section"),
                func.row_number().over(
                    partition_by=CadastralUnit.id,
                    order_by=(Document.created_at.desc(), Document.id.desc()),
                ).label("location_rank"),
            )
            .join(DocumentContent, DocumentContent.document_id == Document.id)
            .join(CadastralUnit, Document.cadastral_unit_id == CadastralUnit.id)
            .join(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(
                or_(*clauses),
                Document.document_type.in_(("visura_fabbricati", "visura_terreni", "visura_storica", "visura")),
            )
            .subquery()
        )
        result = await session.execute(
            select(ranked)
            .where(ranked.c.location_rank <= max(1, min(int(per_location), 10)))
            .order_by(ranked.c.created_at.desc(), ranked.c.document_id.desc())
        )
        rows = result.mappings().all()
    return [
        {
            **dict(row),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "reference_date": format_it_date(row["reference_date"]) or None,
            "province": _province_label(row["province"]) or None,
        }
        for row in rows
    ]


async def get_document_by_id(doc_id: int) -> dict | None:
    """Fetch a single document by primary key."""
    async with _get_session_factory()() as session:
        row = (await session.execute(_document_select().where(Document.id == doc_id))).first()
    if row is None:
        return None
    doc, unit, comune, content, ref = row
    return _document_record(doc, unit, comune, request_ref=ref, content=content)


async def get_subject_document_ids(fiscal_code: str) -> set[int]:
    """Return document IDs linked to a queried subject's canonical fiscal code."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(DocumentSubject.document_id)
            .join(Subject, DocumentSubject.subject_id == Subject.id)
            .where(Subject.fiscal_code == fiscal_code_key(fiscal_code))
        )
        return {int(document_id) for document_id in result.scalars().all() if document_id is not None}


async def get_subject_fiscal_code_for_document(doc_id: int) -> str | None:
    """Return the canonical queried-subject fiscal code for one document, if indexed."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(Subject.fiscal_code)
            .join(DocumentSubject, DocumentSubject.subject_id == Subject.id)
            .where(DocumentSubject.document_id == doc_id)
        )
        fiscal_code = result.scalars().first()
    return str(fiscal_code).strip().upper() if fiscal_code else None


async def get_cadastral_sibling_visuras(
    *,
    province: str,
    municipality: str,
    sheet: str,
    parcel: str,
    subunit: str,
    section: str = "",
) -> list[dict]:
    """Return the latest indexed fabbricati visura for each other subunit."""
    prov, place = province_code(province), place_name(municipality)
    if not all((prov, place, sheet, parcel)):
        return []
    current_subunits = _reference_values(subunit)

    async with _get_session_factory()() as session:
        result = await session.execute(
            select(Document.id, Document.created_at, CadastralUnit.subunit)
            .join(CadastralUnit, Document.cadastral_unit_id == CadastralUnit.id)
            .join(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(
                CadastralUnit.cadastre_type == "F",
                func.upper(IstatMunicipality.province_abbreviation) == prov,
                func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name)) == place,
                func.upper(func.coalesce(CadastralUnit.sheet, "")).in_(_reference_values(sheet)),
                func.upper(func.coalesce(CadastralUnit.parcel, "")).in_(_reference_values(parcel)),
                func.coalesce(CadastralUnit.section, "") == section.strip().upper(),
                CadastralUnit.subunit.is_not(None),
                func.upper(CadastralUnit.subunit).notin_(current_subunits),
                Document.document_type.in_(("visura_fabbricati", "visura_storica", "visura")),
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(200)
        )

        # Multiple visure (e.g. current and historical) can exist per subunit;
        # retain the most recently indexed one for a concise navigation list.
        siblings = {}
        for doc_id, _created_at, sibling_subunit in result.all():
            label = str(sibling_subunit or "").strip()
            siblings.setdefault(label, {"subunit": label, "document_id": doc_id})
    return list(siblings.values())[:25]


def _iso(value: Any) -> Optional[str]:
    """Dates and timestamps as ISO text, the form the inspection page formats."""
    return value.isoformat() if value is not None else None


async def get_document_structured_extraction(doc_id: int) -> dict | None:
    """Fetch the latest structured extraction of a document with its inspection, title, lien, units and parties.

    The shape is the one the inspection page was written for; fields the normalised layout derives (the original
    texts of shares, rights and qualities, the unit count, the interest rate text) are rebuilt from the typed columns.
    """
    async with _get_session_factory()() as session:
        extraction = (
            await session.execute(
                select(DocumentExtraction)
                .where(DocumentExtraction.document_id == doc_id)
                .order_by(DocumentExtraction.created_at.desc(), DocumentExtraction.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if extraction is None:
            return None
        inspection = (
            await session.execute(select(MortgageInspection).where(MortgageInspection.extraction_id == extraction.id))
        ).scalar_one_or_none()
        party_comune = aliased(IstatMunicipality)
        parties = (
            await session.execute(
                select(MortgageInspectionParty, Subject, party_comune)
                .join(Subject, MortgageInspectionParty.subject_id == Subject.id)
                .outerjoin(party_comune, Subject.birth_comune_id == party_comune.numeric_code)
                .where(MortgageInspectionParty.inspection_id == extraction.id)
                .order_by(MortgageInspectionParty.role, MortgageInspectionParty.ordinal)
            )
        ).all()
        properties = (
            await session.execute(
                select(MortgageInspectionProperty, CadastralUnit, IstatMunicipality)
                .outerjoin(CadastralUnit, MortgageInspectionProperty.cadastral_unit_id == CadastralUnit.id)
                .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
                .where(MortgageInspectionProperty.inspection_id == extraction.id)
                .order_by(MortgageInspectionProperty.unit_number, MortgageInspectionProperty.property_number)
            )
        ).all()

    def number(text_value: Any) -> Optional[int]:
        return int(text_value) if str(text_value or "").isdigit() else None

    units: dict[Optional[int], dict[str, Any]] = {}
    for prop, unit, comune in properties:
        entry = units.setdefault(prop.unit_number, {"extraction_id": extraction.id, "unit_number": prop.unit_number, "properties": []})
        entry["properties"].append(
            {
                "property_number": prop.property_number,
                "municipality_code": comune.cadastral_code if comune else None,
                "municipality": _municipality_name(comune),
                "cadastre_type": {"F": "fabbricati", "T": "terreni"}.get(unit.cadastre_type) if unit and unit.cadastre_type else None,
                "urban_section": unit.section if unit else None,
                "sheet": number(unit.sheet) if unit else None,
                "parcel": number(unit.parcel) if unit else None,
                "subunit": number(unit.subunit) if unit else None,
                "nature": prop.nature,
                "nature_description": prop.nature_description,
                "room_count": prop.room_count,
                "area_sqm": float(prop.area_sqm) if prop.area_sqm is not None else None,
                "floor": prop.floor,
                "address": prop.address,
                "civic_number": prop.civic_number,
            }
        )
    inspection_record = title_record = lien_record = None
    if inspection is not None:
        inspection_record = {
            "extraction_id": extraction.id,
            "inspection_date": _iso(inspection.inspection_date),
            "inspection_time": _iso(inspection.inspection_time),
            "inspection_number": inspection.inspection_number,
            "inspection_date_number": _iso(inspection.inspection_number_date),
            "inspection_start": _iso(inspection.inspection_started_at),
            "requester": inspection.requester,
            "tax_paid_euro": inspection.tax_paid_eur,
            "page_count": inspection.page_count,
            "office": inspection.office,
            "service": inspection.service,
            "note_type": inspection.note_type,
            "note_timestamp": _iso(inspection.note_at),
            "registro_generale": inspection.registro_generale,
            "registro_particolare": inspection.registro_particolare,
            "presentazione_numero": inspection.presentazione_numero,
            "presentazione_data": _iso(inspection.presentazione_data),
            "section_a_other_data": inspection.section_a_other_data,
            "section_d_text": inspection.section_d_text,
            "unit_count": len(units),
            "party_favore_count": sum(1 for p, _s, _c in parties if p.role == "a_favore"),
            "party_contro_count": sum(1 for p, _s, _c in parties if p.role == "contro"),
        }
        if inspection.title_type or inspection.title_description or inspection.title_date:
            title_record = {
                "extraction_id": extraction.id,
                "title_type": inspection.title_type,
                "description": inspection.title_description,
                "title_date": _iso(inspection.title_date),
                "repertory_number": inspection.title_repertory_number,
                "notary_name": inspection.title_notary_name,
                "notary_fiscal_code": inspection.title_notary_fiscal_code,
                "notary_location": inspection.title_notary_location,
            }
        if inspection.lien_type or inspection.lien_principal_eur is not None or inspection.lien_total_eur is not None:
            rate = inspection.lien_annual_interest_rate_pct
            lien_record = {
                "extraction_id": extraction.id,
                "lien_type": inspection.lien_type,
                "lien_type_original": inspection.lien_type,
                "derived_from": inspection.lien_derived_from,
                "derived_from_code": inspection.lien_derived_from_code,
                "derived_from_description": inspection.lien_derived_from_description,
                "principal_euro": inspection.lien_principal_eur,
                "annual_interest_rate": f"{str(rate.normalize()).replace('.', ',')}%" if rate is not None else None,
                "annual_interest_rate_pct": float(rate) if rate is not None else None,
                "semiannual_interest_rate": inspection.lien_semiannual_interest_rate,
                "interest_euro": inspection.lien_interest_eur,
                "expenses_euro": inspection.lien_expenses_eur,
                "total_euro": inspection.lien_total_eur,
                "variable_amounts": inspection.lien_variable_amounts,
                "foreign_currency": inspection.lien_foreign_currency,
                "automatic_increase": inspection.lien_automatic_increase,
                "resolutive_condition": inspection.lien_resolutive_condition,
                "duration_years": inspection.lien_duration_years,
                "duration_description": inspection.lien_duration_description,
                "mortgage_rank": inspection.lien_mortgage_rank,
            }
    party_records = []
    for party, subject, birthplace in parties:
        is_entity = subject.subject_type == "legal_entity"
        share = format_share(party.share_numerator, party.share_denominator) or None
        party_records.append(
            {
                "id": party.id,
                "extraction_id": extraction.id,
                "role": party.role,
                "ordinal": party.ordinal,
                "subject_number": party.subject_number,
                "quality": party.quality,
                "surname": subject.last_name,
                "given_name": subject.first_name,
                "birth_date": _iso(subject.birth_date),
                "birth_place": _birth_place(subject, birthplace),
                "gender": subject.gender,
                "fiscal_code": subject.fiscal_code,
                "entity_name": subject.name if is_entity else None,
                "registered_office": party.registered_office,
                "mortgage_domicile": party.mortgage_domicile,
                "negotiation_unit_reference": party.negotiation_unit_reference,
                "right_type": party.right_type,
                "share_numerator": party.share_numerator,
                "share_denominator": party.share_denominator,
                "raw_quality": party.quality,
                "raw_share": share,
                "raw_right_type": party.right_type,
            }
        )
    return {
        "extraction": {
            "id": extraction.id,
            "document_id": extraction.document_id,
            "provider": extraction.provider,
            "model": extraction.model,
            "schema_name": extraction.schema_name,
            "schema_version": extraction.schema_version,
            "run_id": extraction.run_id,
            "workflow_id": extraction.workflow_id,
            "output_path": extraction.output_path,
            "extracted_at": extraction.extracted_at,
            "created_at": extraction.created_at,
        },
        "inspection": inspection_record,
        "title": title_record,
        "lien": lien_record,
        "units": [units[key] for key in sorted(units, key=lambda k: (k is None, k or 0))],
        "parties": party_records,
    }


async def get_indexed_file_paths() -> dict[str, int]:
    """Return a mapping of full file path → document id for all indexed documents."""
    async with _get_session_factory()() as session:
        result = await session.execute(select(Document.file_name, Document.id))
        return {document_file_path(row.file_name): row.id for row in result}


async def get_indexed_filenames() -> set[str]:
    """Return the set of filenames already indexed (basename only)."""
    async with _get_session_factory()() as session:
        result = await session.execute(select(Document.file_name))
        return {row.file_name for row in result}


async def get_indexed_file_metadata() -> dict[str, dict]:
    """Return {file_path: {"id": doc_id, "oggetto": new_name}} for all indexed documents."""
    async with _get_session_factory()() as session:
        result = await session.execute(select(Document.file_name, Document.id, Document.subject_text))
        return {document_file_path(row.file_name): {"id": row.id, "oggetto": row.subject_text or ""} for row in result}


async def get_all_documents(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch all documents (for the browse page)."""
    stmt = _document_select().order_by(Document.created_at.desc()).limit(limit).offset(offset)
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [_document_record(doc, unit, comune, request_ref=ref) for doc, unit, comune, _content, ref in rows]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def find_responses(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Search stored responses by cadastral coordinates."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(Request, RequestPayload.data, CadastralUnit, IstatMunicipality)
            .outerjoin(RequestPayload, RequestPayload.request_id == Request.id)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto))
            .order_by(Request.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await session.execute(stmt)).all()

    return [
        {
            "request_id": req.request_ref,
            "request_type": req.request_type,
            "tipo_catasto": unit.cadastre_type if unit and unit.cadastre_type else "",
            "provincia": _province_label(town.province_abbreviation if town else None),
            "comune": _municipality_name(town) or "",
            "foglio": unit.sheet if unit and unit.sheet else "",
            "particella": unit.parcel if unit and unit.parcel else "",
            "sezione": unit.section if unit else None,
            "subalterno": unit.subunit if unit else None,
            "requested_at": req.created_at.isoformat() if req.created_at else None,
            "success": req.success if req.responded_at else None,
            "data": _decode_payload(data),
            "error": req.error,
            "responded_at": req.responded_at.isoformat() if req.responded_at else None,
        }
        for req, data, unit, town in rows
    ]


_NO_MATCH_ERROR = "nessuna corrispondenza trovata"


def _is_no_match_error(error: Optional[str]) -> bool:
    normalized = " ".join(str(error or "").casefold().split())
    return _NO_MATCH_ERROR in normalized or normalized == "no_match"


def _no_match_sql_condition():
    normalized_error = func.lower(func.coalesce(Request.error, ""))
    return or_(normalized_error.like(f"%{_NO_MATCH_ERROR}%"), normalized_error == "no_match")


def _single_result_status(success: Optional[bool], error: Optional[str] = None) -> str:
    if success is True:
        return "completed"
    if success is False:
        if _is_no_match_error(error):
            return "no_match"
        return "failed"
    return "pending"


async def find_result_rows(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Search single-query responses and workflow runs for the web results page."""
    if source not in {"single", "workflow"}:
        source = None
    if status not in {"completed", "partial", "failed", "error", "pending", "running", "no_match"}:
        status = None

    single_rows: list[dict] = []
    if source in (None, "single"):
        property_count = (
            select(func.count(Property.id)).where(Property.request_id == Request.id).correlate(Request).scalar_subquery()
        )
        owner_count = (
            select(func.count(ResultOwner.id)).where(ResultOwner.request_id == Request.id).correlate(Request).scalar_subquery()
        )
        stmt = (
            select(
                Request.request_ref.label("request_id"),
                Request.request_type.label("request_type"),
                CadastralUnit.cadastre_type.label("cadastre_type"),
                IstatMunicipality.province_abbreviation.label("province"),
                func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name)).label("municipality"),
                CadastralUnit.sheet.label("sheet"),
                CadastralUnit.parcel.label("parcel"),
                CadastralUnit.section.label("section"),
                CadastralUnit.subunit.label("subunit"),
                Request.created_at.label("requested_at"),
                Request.success.label("success"),
                Request.error.label("error"),
                Request.responded_at.label("responded_at"),
                Request.total_results.label("total_results"),
                Request.total_intestati.label("total_intestati"),
                property_count.label("property_count"),
                owner_count.label("owner_count"),
            )
            .select_from(Request)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status))
            .order_by(Request.created_at.desc())
        )
        async with _get_session_factory()() as session:
            rows = (await session.execute(stmt)).mappings().all()
        for row in rows:
            success = bool(row["success"]) if row["success"] is not None and row["responded_at"] is not None else None
            single_rows.append(
                {
                    "request_id": row["request_id"],
                    "request_type": row["request_type"],
                    "source": "single",
                    "tipo_catasto": row["cadastre_type"] or "",
                    "provincia": _province_label(row["province"]),
                    "comune": row["municipality"] or "",
                    "foglio": row["sheet"] or "",
                    "particella": row["parcel"] or "",
                    "sezione": row["section"],
                    "subalterno": row["subunit"],
                    "requested_at": row["requested_at"],
                    "success": success,
                    "status": _single_result_status(success, row["error"]),
                    "data": None,
                    "error": row["error"],
                    "responded_at": row["responded_at"],
                    "total_results": row["total_results"],
                    "total_intestati": row["total_intestati"],
                    "property_count": row["property_count"] or 0,
                    "owner_count": row["owner_count"] or 0,
                }
            )

    # workflow_runs no longer live in sister's DB — owned by opendata
    return single_rows[offset : offset + limit]


async def _delete_documents(session: AsyncSession, document_ids: list[int]) -> None:
    """Delete documents with everything that hangs off them (typed visura rows, extractions, archived text/XML).

    Files on disk are not touched. Children first, because every relation has a foreign key.
    """
    if not document_ids:
        return
    archive = f'"{DATABASE_ARCHIVE_SCHEMA}"'
    statements = (
        "DELETE FROM mutation_owner WHERE mutation_id IN (SELECT id FROM ownership_mutation WHERE document_id = ANY(:ids))",
        "DELETE FROM ownership_mutation WHERE document_id = ANY(:ids)",
        "DELETE FROM land_parcel_classification WHERE land_parcel_id IN (SELECT id FROM land_parcel WHERE document_id = ANY(:ids))",
        "DELETE FROM land_parcel WHERE document_id = ANY(:ids)",
        "DELETE FROM building_unit_related_parcel WHERE building_unit_id IN (SELECT id FROM building_unit WHERE document_id = ANY(:ids))",
        "DELETE FROM building_unit WHERE document_id = ANY(:ids)",
        "DELETE FROM property_group WHERE document_id = ANY(:ids)",
        "DELETE FROM document_address WHERE document_id = ANY(:ids)",
        "DELETE FROM document_subject WHERE document_id = ANY(:ids)",
        "DELETE FROM mortgage_inspection_property WHERE inspection_id IN (SELECT id FROM document_extraction WHERE document_id = ANY(:ids))",
        "DELETE FROM mortgage_inspection_party WHERE inspection_id IN (SELECT id FROM document_extraction WHERE document_id = ANY(:ids))",
        "DELETE FROM mortgage_inspection WHERE extraction_id IN (SELECT id FROM document_extraction WHERE document_id = ANY(:ids))",
        f"DELETE FROM {archive}.document_extraction_payload WHERE extraction_id IN (SELECT id FROM document_extraction WHERE document_id = ANY(:ids))",
        "DELETE FROM document_extraction WHERE document_id = ANY(:ids)",
        f"DELETE FROM {archive}.document_xml_attribute WHERE node_id IN (SELECT id FROM {archive}.document_xml_node WHERE document_id = ANY(:ids))",
        f"DELETE FROM {archive}.document_xml_node WHERE document_id = ANY(:ids)",
        f"DELETE FROM {archive}.document_content WHERE document_id = ANY(:ids)",
        "DELETE FROM document WHERE id = ANY(:ids)",
    )
    for statement in statements:
        await session.execute(text(statement), {"ids": list(document_ids)})  # noqa: S608 (fixed statements, quoted schema)


async def cleanup_old_responses(ttl_seconds: int) -> int:
    """Delete responses older than ttl_seconds. Returns count of deleted rows."""
    if not is_db_writable():
        return 0
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)

        expired_ids = (await session.execute(select(Request.id).where(Request.responded_at < cutoff))).scalars().all()
        deleted = len(expired_ids)

        if expired_ids:
            visit_ids = select(PageVisit.id).where(PageVisit.request_id.in_(expired_ids))
            await session.execute(delete(PageVisitFormElement).where(PageVisitFormElement.page_visit_id.in_(visit_ids)))
            await session.execute(delete(PageVisitError).where(PageVisitError.page_visit_id.in_(visit_ids)))
            await session.execute(delete(PageVisit).where(PageVisit.request_id.in_(expired_ids)))
            document_ids = (await session.execute(select(Document.id).where(Document.request_id.in_(expired_ids)))).scalars().all()
            await _delete_documents(session, list(document_ids))
            await session.execute(delete(ResultOwner).where(ResultOwner.request_id.in_(expired_ids)))
            await session.execute(delete(Property).where(Property.request_id.in_(expired_ids)))
            await session.execute(delete(RequestResult).where(RequestResult.request_id.in_(expired_ids)))
            await session.execute(delete(RequestPayload).where(RequestPayload.request_id.in_(expired_ids)))
            await session.execute(delete(Request).where(Request.id.in_(expired_ids)))

        # requests that never got a response
        await session.execute(delete(Request).where(Request.created_at < cutoff, Request.responded_at.is_(None)))
        await session.commit()
        return deleted


async def count_responses() -> dict:
    """Return basic stats about stored data."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        counts = (
            await session.execute(
                select(
                    func.count().label("requests"),
                    func.count().filter(Request.responded_at.is_not(None)).label("responses"),
                    func.count().filter(Request.success.is_(True)).label("successful"),
                    func.count().filter(Request.success.is_(False)).label("failed"),
                ).select_from(Request)
            )
        ).one()
    total_requests, total_responses, successful, failed = counts
    return {
        "total_requests": total_requests,
        "total_responses": total_responses,
        "successful": successful,
        "failed": failed,
        "pending": max(total_requests - total_responses, 0),
    }


def _build_single_where(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    status: Optional[str] = None,
) -> list:
    """Build portable ORM filters for single-result queries (province by sigla or name, comune by name)."""
    conditions = []
    if provincia:
        conditions.append(
            func.upper(IstatMunicipality.province_abbreviation) == (province_code(provincia) or str(provincia).strip().upper())
        )
    if comune:
        conditions.append(
            func.upper(func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name))
            == (place_name(comune) or str(comune).strip().upper())
        )
    if foglio:
        conditions.append(CadastralUnit.sheet == str(foglio))
    if particella:
        conditions.append(CadastralUnit.parcel == str(particella))
    if tipo_catasto:
        conditions.append(CadastralUnit.cadastre_type == tipo_catasto)
    if status == "completed":
        conditions.append(Request.success.is_(True))
    elif status == "no_match":
        conditions.extend((Request.success.is_(False), _no_match_sql_condition()))
    elif status in ("failed", "error"):
        conditions.extend((Request.success.is_(False), ~_no_match_sql_condition()))
    elif status == "pending":
        conditions.append(Request.responded_at.is_(None))
    return conditions


async def count_total_result_rows(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
) -> int:
    """Return total count of result rows matching filters, using SQL COUNT(*)."""
    if source not in {"single", "workflow"}:
        source = None
    if status not in {"completed", "partial", "failed", "error", "pending", "running", "no_match"}:
        status = None

    if source not in (None, "single"):
        return 0
    stmt = (
        select(func.count())
        .select_from(Request)
        .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
        .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
        .where(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status))
    )
    async with _get_session_factory()() as session:
        total = (await session.execute(stmt)).scalar_one()

    # workflow_runs no longer in sister's DB — owned by opendata
    return total


async def count_result_rows(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    """Return web result stats including single-query requests and workflows."""
    if source not in {"single", "workflow"}:
        source = None

    s_total = s_ok = s_fail = s_no_match = s_pending = 0
    if source in (None, "single"):
        filters = _build_single_where(provincia, comune, foglio, particella, tipo_catasto)
        stmt = (
            select(
                func.count().label("total"),
                func.count().filter(Request.success.is_(True)).label("successful"),
                func.count().filter(Request.success.is_(False), ~_no_match_sql_condition()).label("failed"),
                func.count().filter(Request.success.is_(False), _no_match_sql_condition()).label("no_match"),
                func.count().filter(Request.responded_at.is_(None)).label("pending"),
            )
            .select_from(Request)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(*filters)
        )
        async with _get_session_factory()() as session:
            counts = (await session.execute(stmt)).one()
        s_total, s_ok, s_fail, s_no_match, s_pending = counts

    return {
        "total_requests": s_total,
        "total_responses": s_total - s_pending,
        "successful": s_ok,
        "failed": s_fail,
        "no_match": s_no_match,
        "partial": 0,
        "pending": s_pending,
    }
