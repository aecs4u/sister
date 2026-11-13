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
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Date,
    Integer,
    Numeric,
    String,
    and_,
    cast,
    delete,
    distinct,
    func,
    inspect,
    literal,
    null,
    or_,
    text,
    union_all,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import aliased, sessionmaker
from sqlmodel import select

from .database_config import database_dsn_setting, get_database_dsn
from .db_models import (
    PROPERTY_FIELD_MAP,
    PROPERTY_LOCATION_FIELD_MAP,
    PROPERTY_SUBJECT_FIELD_MAP,
    ActivityLog,
    ActivityLogAttribute,
    ActivityLogPayload,
    CadastralUnit,
    CalculationSnapshot,
    Document,
    DocumentContent,
    DocumentExtraction,
    DocumentExtractionPayload,
    DocumentFavorite,
    DocumentPurchase,
    IstatMunicipality,
    ManualPropertyValuation,
    MortgageInspection,
    MortgageInspectionParty,
    MortgageInspectionProperty,
    OwnershipRight,
    PageVisit,
    PageVisitError,
    PageVisitFormElement,
    Property,
    PropertyActualSize,
    PropertyAffrancationAmount,
    PropertyOmiOverride,
    PropertyOmiSelection,
    PropertyValuationPreference,
    Request,
    RequestPayload,
    RequestProvince,
    RequestResult,
    RequestTag,
    RequestType,
    RequestTypeTag,
    ResultOwner,
    RightKind,
    Subject,
    SubjectAccess,
    SubjectFavorite,
    SubjectHolding,
    SubjectNote,
    SubjectPurchase,
    SubjectTag,
    WalletGiftCoupon,
    WalletGiftCouponRedemption,
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
    get_or_create_subject_record,
    place_name,
)
from .request_types import request_type_info
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
DATABASE_DSN = get_database_dsn()
DATABASE_SCHEMA = os.getenv("DATABASE_SCHEMA", "sister")
DATABASE_ISTAT_SCHEMA = os.getenv("DATABASE_ISTAT_SCHEMA", DATABASE_SCHEMA)
# raw payloads (response JSON, XML tree, extraction JSON) live in their own schema; tests point it at a throwaway one
DATABASE_ARCHIVE_SCHEMA = os.getenv("DATABASE_ARCHIVE_SCHEMA", "sister_archive")
DATABASE_REVISION = "20261202_pagopa_payment_history"

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
            raise RuntimeError(f"{database_dsn_setting()} must be set to a PostgreSQL connection URL")
        url = make_url(DATABASE_DSN)
        if url.get_backend_name() != "postgresql":
            raise RuntimeError(f"{database_dsn_setting()} must use PostgreSQL; other database backends are unsupported")
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
            "territory_province",
            "court",
            "court_municipality",
            "conservatoria_office",
            "conservatoria_municipality",
            "conservatoria_section",
            "subject",
            "right_kind",
            "ownership_right",
            "request",
            "request_result",
            "property",
            "result_owner",
            "request_type_tag",
            "document",
            "document_favorite",
            "document_purchase",
            "subject_favorite",
            "subject_note",
            "subject_tag",
            "subject_holding",
            "manual_property_valuation",
            "property_valuation_preference",
            "property_affrancation_amount",
            "property_omi_override",
            "property_omi_selection",
            "property_actual_size",
            "calculation_snapshot",
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
        istat_table_result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = 'municipalities'"
            ),
            {"schema": DATABASE_ISTAT_SCHEMA},
        )
        istat_table_exists = istat_table_result.scalar_one_or_none()
        if istat_table_exists is None:
            raise RuntimeError(
                f"Shared ISTAT table {DATABASE_ISTAT_SCHEMA}.municipalities was not found. "
                "Set DATABASE_ISTAT_SCHEMA to the schema containing the shared municipality table."
            )
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


async def get_recent_activity(
    limit: int = 50, action: Optional[str] = None, action_prefix: Optional[str] = None
) -> list[dict]:
    """The newest activity rows, optionally filtered by an exact action or action prefix."""
    async with _get_session_factory()() as session:
        stmt = (
            select(ActivityLog, ActivityLogPayload)
            .outerjoin(ActivityLogPayload, ActivityLogPayload.activity_id == ActivityLog.id)
            .order_by(ActivityLog.started_at.desc(), ActivityLog.id.desc())
            .limit(limit)
        )
        if action:
            stmt = stmt.where(ActivityLog.action == action)
        if action_prefix:
            stmt = stmt.where(ActivityLog.action.startswith(action_prefix, autoescape=True))
        rows = (await session.execute(stmt)).all()
    return [
        {**row.model_dump(mode="json"), "params": payload.params if payload else None, "result": payload.result if payload else None}
        for row, payload in rows
    ]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _content_request_id():
    """The request that holds a request's content: itself, or the earlier identical answer it points at."""
    return func.coalesce(Request.same_as_request_id, Request.id)


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
            .outerjoin(RequestPayload, RequestPayload.request_id == _content_request_id())
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


async def add_request_tags(request_refs: Iterable[str], tags: Iterable[str] | str) -> int:
    """Attach normalized source tags to persisted requests; return the number of new links."""
    refs = list(dict.fromkeys(str(ref).strip() for ref in request_refs if str(ref).strip()))
    if isinstance(tags, str):
        raw_tags = tags.replace(";", ",").split(",")
    else:
        raw_tags = tags
    normalized_tags = list(dict.fromkeys(str(tag).strip().upper() for tag in raw_tags if str(tag).strip()))
    if not refs or not normalized_tags:
        return 0

    session_factory = _get_session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(select(Request.id, Request.request_ref).where(Request.request_ref.in_(refs)))
        ).all()
        request_ids = {request_ref: request_id for request_id, request_ref in rows}
        missing = sorted(set(refs) - request_ids.keys())
        if missing:
            raise ValueError(f"Cannot tag unknown request references: {', '.join(missing)}")

        values = [
            {"request_id": request_ids[request_ref], "tag": tag}
            for request_ref in refs
            for tag in normalized_tags
        ]
        result = await session.execute(
            pg_insert(RequestTag)
            .values(values)
            .on_conflict_do_nothing(index_elements=["request_id", "tag"])
            .returning(RequestTag.request_id)
        )
        inserted = len(result.scalars().all())
        await session.commit()
        return inserted


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
        loc_fields["section"] = loc_fields["section"] or place.get("section", "")
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
    record: Optional[dict[str, Any]] = None  # the registry record (denominazione + sede) the row belongs to
    owners_complete: bool = False  # the owners come from the unit's own Intestati page: the full list, not just one owner


def _record_fields(data: dict, item: dict) -> Optional[dict[str, Any]]:
    """The registry record of a persona giuridica row, so that entities sharing a fiscal code stay apart.

    A row of the combined ``azienda`` answer carries ``omonimo_denominazione`` / ``omonimo_sede`` (the code is the
    searched one); a row of the records list itself carries ``Denominazione`` / ``Sede`` / ``Codice Fiscale``.
    """
    if item.get("omonimo_denominazione") or item.get("omonimo_sede"):
        code = str(data.get("soggetto") or item.get("Codice Fiscale") or "").strip().upper()
        name, seat = item.get("omonimo_denominazione"), item.get("omonimo_sede")
    elif item.get("omonimoSelezionato"):
        code = str(item.get("Codice Fiscale") or data.get("soggetto") or "").strip().upper()
        name, seat = item.get("Denominazione"), item.get("Sede")
    else:
        return None
    if not identifier_kind(code):
        return None
    return {"fiscal_code": code, "denomination": name, "seat": seat}


def _subject_query_owner(data: dict, item: dict) -> Optional[dict[str, Any]]:
    """The queried person/company as owner of a row of its own property list (rows carry ``Titolarità``)."""
    identifier = str(data.get("soggetto") or "").strip().upper()
    if not identifier_kind(identifier) or not item.get("Titolarità"):
        return None
    return {"codice_fiscale": identifier, "Titolarità": item["Titolarità"]}


_OWNER_IDENTITY_KEYS = ("codice_fiscale", "nome", "sesso", "luogo_nascita", "data_nascita", "sede")


def _project_owner_immobili(
    data: dict, tipo_catasto: str, response_id: str, properties: list[ProjectedProperty], results: list[dict[str, Any]]
) -> None:
    """Add the properties found through an owner's *Immobili* button (``results[].immobili_intestatari``) to ``properties``.

    Each listed immobile is stored like a row of a subject search: the owner is its owner, with the ``Titolarità`` the
    list shows. Only the owner's identity is used: ``titolarita`` / ``quota`` of the source Intestati row belong to
    the immobile the owner was read from, not to these. Rows get positions after every ``immobili`` row and result
    index, so they never tie to a ``visura_results`` row of the searched immobile; a property already listed
    (same unit and owner) is not added twice.
    """
    entries = [
        entry
        for result in data.get("results") or []
        if isinstance(result, dict)
        for entry in result.get("immobili_intestatari") or []
        if isinstance(entry, dict)
    ]
    if not entries:
        return
    position = max([len(data.get("immobili") or []), *(row["result_index"] for row in results), 0])
    seen: set[tuple] = set()
    for entry in entries:
        owner = entry.get("intestato") if isinstance(entry.get("intestato"), dict) else {}
        identity = {key: owner[key] for key in _OWNER_IDENTITY_KEYS if owner.get(key)}
        for item in entry.get("immobili") or []:
            if not isinstance(item, dict):
                continue
            prop, loc, subject = _property_row(response_id, tipo_catasto, item)
            if not loc.get("province"):
                # "MENFI(AG) CONTRADA ...": without it the searched immobile's province would be filled in later
                found = re.match(r"\s*[^()]+\((\w{2})\)", str(item.get("Ubicazione") or ""))
                loc["province"] = found.group(1).upper() if found else ""
            key = (identity.get("codice_fiscale"), tuple(sorted(loc.items())), item.get("Titolarità"))
            if key in seen:
                continue
            seen.add(key)
            position += 1
            projected = ProjectedProperty(position, prop, loc, subject)
            owner_row = {**identity, **({"Titolarità": item["Titolarità"]} if item.get("Titolarità") else {})}
            if identity:
                projected.owners = _owner_pairs([owner_row])
                projected.owners_complete = False
            properties.append(projected)


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
        projected = ProjectedProperty(position, prop, loc, subject, record=_record_fields(data, item))
        result_item = result_items.get(position)
        raw_owners: list[dict] = []
        if result_item is not None:
            projected.result_index = position
            projected.visura_present = bool(result_item.get("visura"))
            raw_owners += [o for o in result_item.get("intestati") or [] if isinstance(o, dict)]
        raw_owners += [o for o in item.get("intestati") or [] if isinstance(o, dict)]
        projected.owners_complete = bool(raw_owners)  # read from the Intestati page (all owners), before the queried one
        queried = _subject_query_owner(data, item)
        if queried and not any(o.get("codice_fiscale") == queried["codice_fiscale"] for o in raw_owners):
            raw_owners.append(queried)
        projected.owners = _owner_pairs(raw_owners)
        properties.append(projected)

    _project_owner_immobili(data, tipo_catasto, response_id, properties, results)

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
        "consistency_sqm": consistency_value if consistency_unit == "MQ" else None,
        "consistency_rooms": consistency_value if consistency_unit == "VANI" else None,
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
    for table_name in ("result_owner", "property", "request_result", "request_province"):
        await session.execute(text(f"DELETE FROM {table_name} WHERE request_id = :rid"), {"rid": request_pk})  # noqa: S608
    if data is None:
        return
    unit = await _request_unit(session, request_pk)
    await _persist_projection(session, request_pk, tipo_catasto, data, unit)
    await _persist_provinces(session, request_pk, data)
    await _link_documents(session, request_pk, data)


async def _identical_previous(session: AsyncSession, request_pk: int, data: Optional[dict]):
    """(content hash, the previous answer of the same query when it has the same content, else None)."""
    from .ownership_history import content_hash

    digest = content_hash(data) if isinstance(data, dict) and not data.get("error") else None
    if digest is None:
        return None, None
    previous = (
        await session.execute(
            text(
                "SELECT p.id, coalesce(p.same_as_request_id, p.id) AS content_id, p.content_hash FROM request r "
                "JOIN request p ON p.cache_key = r.cache_key AND p.id <> r.id "
                "WHERE r.id = :id AND r.cache_key IS NOT NULL AND p.success IS TRUE AND p.content_hash IS NOT NULL "
                "ORDER BY p.responded_at DESC NULLS LAST, p.id DESC LIMIT 1"
            ),
            {"id": request_pk},
        )
    ).first()
    return digest, previous if previous is not None and previous.content_hash == digest else None


def _search_is_national(data: Optional[dict], scope: Optional[str]) -> bool:
    ambito = str((data or {}).get("ambito") or scope or "NAZIONALE").strip().upper()
    return ambito in ("", "NAZIONALE", "NONE")


async def _request_scope(session: AsyncSession, request_pk: int) -> Optional[str]:
    """The office a subject search ran at (``province`` of the queued job; NAZIONALE when it is not known)."""
    raw = (
        await session.execute(
            text("SELECT j.payload_json FROM sister_request_jobs j JOIN request r ON r.request_ref = j.request_id WHERE r.id = :id"),
            {"id": request_pk},
        )
    ).scalar()
    try:
        return (json.loads(raw) or {}).get("province") if raw else None
    except (TypeError, ValueError):
        return None


async def _history(session: AsyncSession, action: str, request_pk: int, observed_at: datetime, **kwargs) -> None:
    """Update the ownership / property-state history; a failure here is logged and never fails the response save."""
    from .ownership_history import (
        collect_observations,
        extend_identical,
        record_observations,
        subject_search_is_complete,
    )

    try:
        async with session.begin_nested():
            if action == "extend":
                await extend_identical(session, kwargs["previous_pk"], request_pk, observed_at)
                return
            data, tipo_catasto = kwargs.get("data"), kwargs.get("tipo_catasto")
            if not isinstance(data, dict):
                return
            code = str(data.get("soggetto") or "").strip().upper()
            national = _search_is_national(data, await _request_scope(session, request_pk)) if code else False
            if data.get("error"):
                # SISTER found nothing for a subject searched everywhere, in both catasti: it owns nothing any more
                if code and national and "NESSUNA CORRISPONDENZA" in str(data["error"]) and (tipo_catasto or "E").upper() == "E":
                    await record_observations(session, request_pk, observed_at, [], code, subject_complete=True)
                return
            observations = await collect_observations(session, tipo_catasto, data, await _request_unit(session, request_pk))
            complete = bool(code) and national and subject_search_is_complete(data, tipo_catasto)
            await record_observations(session, request_pk, observed_at, observations, code or None, subject_complete=complete)
    except Exception:  # noqa: BLE001
        logger.warning("Storico proprieta' non aggiornato per la richiesta %s", request_pk, exc_info=True)


async def _request_unit(session: AsyncSession, request_pk: int):
    """The request's own unit: it supplies the province/municipality a row of the list does not carry."""
    return (
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


async def _persist_provinces(session: AsyncSession, request_pk: int, data: Optional[dict]) -> None:
    """Write the "Elenco Province" table of a subject search (``data["province"]``): where the subject has assets."""
    rows = data.get("province") if isinstance(data, dict) else None
    if not rows:
        return
    fiscal_code = str(data.get("soggetto") or "").strip().upper() or None
    subject_id = None
    if fiscal_code:
        subject_id = (await session.execute(select(Subject.id).where(Subject.fiscal_code == fiscal_code))).scalar_one_or_none()
    for index, row in enumerate((r for r in rows if isinstance(r, dict) and r.get("provincia")), start=1):
        code = str(row.get("codice") or "").strip().upper()
        session.add(
            RequestProvince(
                request_id=request_pk,
                province_index=index,
                subject_id=subject_id,
                fiscal_code=fiscal_code,
                province_code=code if re.fullmatch(r"[A-Z]{2}", code) else None,
                province_name=str(row["provincia"]).strip(),
                buildings_count=int(row.get("fabbricati") or 0),
                land_parcels_count=int(row.get("terreni") or 0),
            )
        )
    await session.flush()


async def subject_ownership_history(fiscal_code: str, only_changed: bool = True) -> list[dict]:
    """The ownership story of every unit the subject holds or held, from both sources, newest change first.

    * ``acts``: the official chain of the unit's latest stored visura (``ownership_mutation``: costituzione, atti,
      successioni with their date and the owners each one left), oldest first;
    * ``periods``: what the searches saw (``ownership_history``: owner, right and share, first/last seen, when one stopped
      being listed).

    ``changed``: the visura records more than one act, or a search saw an owner leave. With ``only_changed`` the units
    always in the same hands are left out: the page lists the variations.
    """
    code = fiscal_code_key(fiscal_code)
    if not code:
        return []
    session_factory = _get_session_factory()
    async with session_factory() as session:
        params = {"c": code}
        periods = (
            await session.execute(
                text(
                    "SELECT v.* FROM v_ownership_history v WHERE v.cadastral_unit_id IN ("
                    " SELECT h.cadastral_unit_id FROM ownership_history h JOIN subject s ON s.id = h.subject_id WHERE s.fiscal_code = :c)"
                    " ORDER BY v.cadastral_unit_id, v.first_seen_at, v.id"
                ),
                params,
            )
        ).mappings().all()
        acts = (
            await session.execute(
                text(
                    "WITH acts AS ("
                    " SELECT CASE WHEN m.status = 'predecessore' THEN coalesce((SELECT x.reference_cadastral_unit_id FROM ownership_mutation x"
                    "   WHERE x.document_id = m.document_id AND x.status = 'attuale' AND x.reference_cadastral_unit_id IS NOT NULL"
                    "   AND x.property_group_id IS NOT DISTINCT FROM m.property_group_id ORDER BY x.id LIMIT 1), d.cadastral_unit_id)"
                    "  ELSE coalesce(m.reference_cadastral_unit_id, d.cadastral_unit_id) END AS unit, d.id AS document_id, d.file_name,"
                    "  m.id, m.mutation_index, m.mutation_date, m.source_description, m.is_current"
                    " FROM ownership_mutation m JOIN document d ON d.id = m.document_id),"
                    " mine AS ("
                    "  SELECT h.cadastral_unit_id AS unit FROM ownership_history h JOIN subject s ON s.id = h.subject_id WHERE s.fiscal_code = :c"
                    "  UNION SELECT a.unit FROM acts a JOIN mutation_owner mo ON mo.mutation_id = a.id"
                    "   JOIN subject s ON s.id = mo.subject_id WHERE s.fiscal_code = :c),"
                    " latest AS (SELECT DISTINCT ON (c.unit) c.unit, c.document_id FROM ("
                    "  SELECT a.unit, a.document_id, count(*) AS n FROM acts a JOIN mine USING (unit) GROUP BY a.unit, a.document_id) c"
                    "  ORDER BY c.unit, c.n DESC, c.document_id DESC)"
                    " SELECT a.*, (SELECT string_agg(coalesce(s.name, s.fiscal_code) || coalesce(' ' || nullif(k.description, ''), '')"
                    "   || coalesce(' ' || o.share_numerator || '/' || o.share_denominator, ''), '; ' ORDER BY mo.position, mo.id)"
                    "   FROM mutation_owner mo JOIN subject s ON s.id = mo.subject_id LEFT JOIN ownership_right o ON o.id = mo.right_id"
                    "   LEFT JOIN right_kind k ON k.id = o.right_kind_id WHERE mo.mutation_id = a.id) AS owners,"
                    "  EXISTS (SELECT 1 FROM mutation_owner mo JOIN subject s ON s.id = mo.subject_id"
                    "   WHERE mo.mutation_id = a.id AND s.fiscal_code = :c) AS with_subject"
                    " FROM acts a JOIN latest l ON l.unit = a.unit AND l.document_id = a.document_id"
                    " WHERE a.unit IS NOT NULL ORDER BY a.unit, a.is_current, a.mutation_index NULLS LAST, a.id"
                ),
                params,
            )
        ).mappings().all()
        unit_ids = {r["cadastral_unit_id"] for r in periods} | {r["unit"] for r in acts}
        locations = {}
        if unit_ids:
            locations = {
                row.id: row
                for row in (
                    await session.execute(
                        select(CadastralUnit.id, CadastralUnit.cadastre_type, CadastralUnit.comune_id, CadastralUnit.section,
                               CadastralUnit.sheet, CadastralUnit.parcel, CadastralUnit.subunit)
                        .where(CadastralUnit.id.in_(unit_ids))
                    )
                ).all()
            }
        comuni_ids = {row.comune_id for row in locations.values() if row.comune_id is not None}
        names = {}
        if comuni_ids:
            names = dict(
                (
                    await session.execute(
                        select(IstatMunicipality.numeric_code, func.coalesce(IstatMunicipality.italian_name, IstatMunicipality.name))
                        .where(IstatMunicipality.numeric_code.in_(comuni_ids))
                    )
                ).all()
            )
    units: dict[int, dict] = {}

    def unit_entry(unit_id: int) -> dict:
        if unit_id not in units:
            loc = locations.get(unit_id)
            units[unit_id] = {
                "cadastral_unit_id": unit_id,
                "comune": names.get(loc.comune_id, "") if loc else "",
                "catasto": loc.cadastre_type if loc else "",
                "sezione": loc.section if loc else "",
                "foglio": loc.sheet if loc else "",
                "particella": loc.parcel if loc else "",
                "subalterno": loc.subunit if loc else "",
                "periods": [],
                "acts": [],
                "document": None,
            }
        return units[unit_id]

    for row in periods:
        share = f"{row['share_numerator']}/{row['share_denominator']}" if row["share_numerator"] and row["share_denominator"] else ""
        unit_entry(row["cadastral_unit_id"])["periods"].append(
            {
                "fiscal_code": row["fiscal_code"],
                "name": row["subject_name"],
                "is_subject": row["fiscal_code"] == code,
                "record": " - ".join(x for x in (row["record_denomination"], row["record_seat"]) if x),
                "right": row["right_description"] or row["right_type"] or "",
                "share": share,
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "seen_count": row["seen_count"],
                "ended_at": row["ended_at"],
                "current": row["current"],
            }
        )
    for row in acts:
        unit = unit_entry(row["unit"])
        unit["document"] = {"id": row["document_id"], "file_name": row["file_name"]}
        unit["acts"].append(
            {
                "date": row["mutation_date"],
                "description": row["source_description"] or ("Intestazione attuale" if row["is_current"] else ""),
                "owners": row["owners"] or "",
                "current": row["is_current"],
                "with_subject": row["with_subject"],
            }
        )
    result = []
    for unit in units.values():
        acts_only = [a for a in unit["acts"] if not a["current"]]
        searched_change = len(unit["periods"]) > 1 and any(not p["current"] for p in unit["periods"])
        unit["changed"] = searched_change or len(acts_only) > 1
        dates = [a["date"] for a in acts_only if a["date"]]
        dates += [p["ended_at"].date() for p in unit["periods"] if p["ended_at"]]
        unit["last_change_at"] = max(dates, default=None)
        if unit["changed"] or not only_changed:
            result.append(unit)
    result.sort(key=lambda u: (u["last_change_at"] is None, -(u["last_change_at"].toordinal() if u["last_change_at"] else 0)))
    return result


async def backfill_ownership_history() -> dict[str, int]:
    """Rebuild ``ownership_history`` / ``property_state_history`` from every stored answer, oldest first (idempotent).

    Each answer gets its ``content_hash``; an answer identical to the previous answer of the same query is marked
    ``same_as_request_id`` (its stored copy stays: nothing is deleted) and only extends the periods. Answers are replayed
    in the order SISTER gave them, so a change of owner, share or classamento becomes a new period at the date it was seen.
    """
    from .ownership_history import content_hash

    session_factory = _get_session_factory()
    async with session_factory() as session:
        await session.execute(text("DELETE FROM ownership_history"))
        await session.execute(text("DELETE FROM property_state_history"))
        await session.execute(text("UPDATE request SET content_hash = NULL, same_as_request_id = NULL"))
        await session.commit()
        todo = (
            await session.execute(
                text(
                    "SELECT r.id, r.cache_key, r.cadastre_type, coalesce(r.responded_at, r.created_at) AS at, r.success "
                    "FROM request r JOIN sister_archive.request_payload p ON p.request_id = r.id "
                    "ORDER BY coalesce(r.responded_at, r.created_at), r.id"
                )
            )
        ).all()
    stats = {"answers": 0, "identical": 0, "recorded": 0}
    last_by_key: dict[str, tuple[int, int, str]] = {}  # cache_key -> (request id, content request id, hash)
    for start in range(0, len(todo), 100):
        async with session_factory() as session:
            for row in todo[start : start + 100]:
                payload = await session.get(RequestPayload, row.id)
                data = _decode_payload(payload.data) if payload else None
                stats["answers"] += 1
                digest = content_hash(data) if row.success and isinstance(data, dict) and not data.get("error") else None
                previous = last_by_key.get(row.cache_key) if row.cache_key else None
                if digest and previous and previous[2] == digest:
                    await session.execute(
                        text("UPDATE request SET content_hash = :h, same_as_request_id = :s WHERE id = :id"),
                        {"h": digest, "s": previous[1], "id": row.id},
                    )
                    await _history(session, "extend", row.id, row.at, previous_pk=previous[0])
                    last_by_key[row.cache_key] = (row.id, previous[1], digest)
                    stats["identical"] += 1
                    continue
                await session.execute(text("UPDATE request SET content_hash = :h WHERE id = :id"), {"h": digest, "id": row.id})
                await _history(session, "record", row.id, row.at, tipo_catasto=row.cadastre_type or "E", data=data)
                if digest and row.cache_key:
                    last_by_key[row.cache_key] = (row.id, row.id, digest)
                stats["recorded"] += 1
            await session.commit()
    return stats


async def backfill_projections(
    limit: Optional[int] = None, batch: int = 50, contains: Optional[str] = None
) -> dict[str, int]:
    """Re-project every stored response with the current parsers (idempotent; the archived JSON is the source).

    Fills ``result_id`` / ``owner_index`` on existing properties and owners, splits the composite portal cells and
    ties downloaded documents to their response. ``contains``: only the responses whose archived JSON has this text
    (e.g. ``immobili_intestatari``), so a new projection does not rebuild every response.
    """
    session_factory = _get_session_factory()
    stats = {"responses": 0, "properties": 0, "owners": 0}
    async with session_factory() as session:
        stmt = (
            select(Request.id, Request.cadastre_type)
            .join(RequestPayload, RequestPayload.request_id == Request.id)
            .order_by(Request.responded_at)
        )
        if contains:
            stmt = stmt.where(RequestPayload.data.contains(contains, autoescape=True))
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


async def _persist_listed_subjects(session: AsyncSession, data: Optional[dict]) -> int:
    """Store the persons and companies the *Elenco Omonimi* page listed (``data["soggetti"]``), selected or not.

    A search by code goes through that page, which is the only place that shows the subject: cognome, nome, sesso, data and
    luogo di nascita (persons) or denominazione and sede (companies). A person completes its ``subject`` (name and birth
    data, only where missing); a company gets its registry record, and its name when the code has a single record. Returns
    how many entries were stored.
    """
    entries = [e for e in (data.get("soggetti") if isinstance(data, dict) else None) or [] if isinstance(e, dict)]
    records_per_code: dict[str, int] = {}
    for entry in entries:
        if entry.get("tipo") == "azienda" and (code := fiscal_code_key(entry.get("codice_fiscale"))):
            records_per_code[code] = records_per_code.get(code, 0) + 1
    stored = 0
    for entry in entries:
        code = fiscal_code_key(entry.get("codice_fiscale"))
        if not code or not identifier_kind(code):
            continue
        if entry.get("tipo") == "azienda":
            await get_or_create_subject_record(
                session, fiscal_code=code, denomination=entry.get("denominazione"), seat=entry.get("sede")
            )
            if records_per_code.get(code) == 1 and entry.get("denominazione"):
                await get_or_create_subject(
                    session, fiscal_code=code, display_name=entry["denominazione"], subject_type="legal_entity"
                )
        else:
            await get_or_create_subject(
                session,
                fiscal_code=code,
                last_name=entry.get("cognome"),
                first_name=entry.get("nome"),
                gender=entry.get("sesso"),
                date_of_birth=entry.get("data_nascita"),
                birth_municipality=entry.get("luogo_nascita"),
                birth_province=entry.get("provincia_nascita"),
                subject_type="person",
            )
        stored += 1
    return stored


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
    await _persist_listed_subjects(session, data)
    properties, unlinked, result_rows = _project_response("", tipo_catasto, data)
    result_ids: dict[int, int] = {}
    for result_fields in result_rows:
        row = RequestResult(request_id=request_pk, **result_fields)
        session.add(row)
        await session.flush()
        result_ids[result_fields["result_index"]] = row.id

    seen_owners: set[tuple] = set()
    record_ids: dict[tuple, Optional[int]] = {}

    async def add_owner(
        result_id: Optional[int], owner_index: int, subject_fields: dict, right_fields: dict, record: Optional[dict] = None, record_id: Optional[int] = None
    ) -> None:
        subject_id = await _resolve_subject(session, subject_fields) if subject_fields else None
        # the queried company is the owner of this row *as* this registry record
        if not (record and record_id and fiscal_code_key(subject_fields.get("fiscal_code")) == record["fiscal_code"]):
            record_id = None
        right_id = await get_or_create_right(session, **right_fields) if right_fields else None
        key = (result_id, subject_id, right_id)
        if key in seen_owners:  # (request, result, subject, right) is unique
            return
        seen_owners.add(key)
        session.add(
            ResultOwner(
                request_id=request_pk,
                result_id=result_id,
                owner_index=owner_index,
                subject_id=subject_id,
                subject_record_id=record_id,
                right_id=right_id,
            )
        )

    for projected in properties:
        loc_fields = dict(projected.location)
        if request_unit is not None:
            loc_fields["province"] = loc_fields["province"] or (request_unit.province_code or "")
            loc_fields["municipality"] = loc_fields["municipality"] or (request_unit.municipality or "")
        unit_id = await get_or_create_cadastral_unit(session, **loc_fields)
        subject_id = await _resolve_subject(session, projected.subject) if projected.subject else None
        result_id = result_ids.get(projected.result_index) if projected.result_index is not None else None
        record_id = None
        if projected.record:
            key = tuple(projected.record.items())
            if key not in record_ids:
                record_ids[key] = await get_or_create_subject_record(session, **projected.record)
            record_id = record_ids[key]
        session.add(
            Property(
                request_id=request_pk,
                result_id=result_id,
                cadastral_unit_id=unit_id,
                subject_id=subject_id,
                subject_record_id=record_id,
                **await _property_columns(session, projected.prop),
            )
        )
        for owner_index, (subject_fields, right_fields) in enumerate(projected.owners, start=1):
            await add_owner(result_id, owner_index, subject_fields, right_fields, projected.record, record_id)
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
        # An answer identical to the previous answer of the same query (cache_key) is not stored again: the request keeps its
        # date and points at the request that holds the content; only the history periods are extended.
        observed_at = datetime.now(timezone.utc)
        digest, previous = await _identical_previous(session, request_pk, data if success else None)
        await session.execute(delete(RequestPayload).where(RequestPayload.request_id == request_pk))
        if previous is not None:
            await session.execute(
                text("UPDATE request SET content_hash = :h, same_as_request_id = :s WHERE id = :id"),
                {"h": digest, "s": previous.content_id, "id": request_pk},
            )
            await _replace_projection(session, request_pk, tipo_catasto, None)
            await _history(session, "extend", request_pk, observed_at, previous_pk=previous.id)
        else:
            await session.execute(
                text("UPDATE request SET content_hash = :h, same_as_request_id = NULL WHERE id = :id"), {"h": digest, "id": request_pk}
            )
            # the raw response is kept, as sent, in the archive (nothing is read back from it except by the cache / detail page)
            if data is not None:
                session.add(RequestPayload(request_id=request_pk, data=json.dumps(data, ensure_ascii=False, default=str)))
            await session.flush()
            await _replace_projection(session, request_pk, tipo_catasto, data)
            await session.flush()
            await _history(session, "record", request_pk, observed_at, tipo_catasto=tipo_catasto, data=data)
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

    subject_code = str(summary.get("subject_query") or "").strip().upper()
    if success and len(subject_code) in (11, 16):
        try:
            await refresh_subject_holdings([subject_code])
            if await nameless_subject_codes([subject_code]):
                from .subject_names import names_from_opendata

                await fill_subject_names(await names_from_opendata([subject_code]))
        except Exception:  # noqa: BLE001 - derived ranking data must never fail the response save
            logger.warning("Riepilogo immobili non aggiornato per %s", subject_code, exc_info=True)

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
                .outerjoin(RequestPayload, RequestPayload.request_id == _content_request_id())
                .where(Request.request_ref == request_id)
            )
        ).first()
        tags = []
        if row is not None:
            tags = list(
                (
                    await session.execute(
                        select(RequestTag.tag).where(RequestTag.request_id == row[0].id).order_by(RequestTag.tag)
                    )
                ).scalars()
            )
    if row is None or row[0].responded_at is None:
        return None
    request, data = row
    return {
        "request_id": request.request_ref,
        "tags": tags,
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
            .outerjoin(RequestPayload, RequestPayload.request_id == _content_request_id())
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(Request.request_ref == request_id)
        )
        row = (await session.execute(stmt)).first()
        tags = []
        if row is not None:
            tags = list(
                (
                    await session.execute(
                        select(RequestTag.tag).where(RequestTag.request_id == row[0].id).order_by(RequestTag.tag)
                    )
                ).scalars()
            )
    if row is None:
        return None

    req, raw_data, unit, comune = row
    responded = req.responded_at is not None
    # Same vocabulary as the list (a SISTER "nessuna corrispondenza" answer is ``no_match``, not a failure).
    status = _single_result_status(req.success, req.error) if responded else "pending"
    data = _decode_payload(raw_data)
    return {
        "request_id": req.request_ref,
        "tags": tags,
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


def _join_result_values(values: Any, *, province_labels: bool = False) -> str:
    """Format distinct aggregate values for the result list, including multi-property subject searches."""
    if values is None:
        return ""
    if isinstance(values, str):
        values = [values]
    items = sorted({str(value).strip() for value in values if value is not None and str(value).strip()}, key=str.casefold)
    if province_labels:
        items = [_province_label(value) for value in items]
    return " · ".join(dict.fromkeys(items))


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
            .join(Request, _content_request_id() == Property.request_id)
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
            "consistency_sqm": format_amount_it(prop.consistency_sqm),
            "consistency_rooms": format_amount_it(prop.consistency_rooms),
            "consistency_other": (
                format_consistency(prop.consistency_value, prop.consistency_unit)
                if prop.consistency_value is not None and prop.consistency_unit not in {"MQ", "VANI", "VANO"}
                else None
            ),
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
            "comune_id": unit.comune_id if unit else None,
            "municipality_code": (comune.cadastral_code or "").strip().upper() or None if comune else None,
            "province_code": (comune.province_abbreviation or "").strip().upper() or None if comune else None,
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
            .join(Request, _content_request_id() == ResultOwner.request_id)
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
    filename = doc.file_name or ""
    document_type = doc.document_type
    if filename.casefold().startswith("el_imm_"):
        document_type = "elenco_immobili"
    elif filename.casefold().startswith("isp_") and document_type != "titolo_telematico":
        # a title shares the isp_ file prefix but keeps its own type
        document_type = "ispezione_ipotecaria"
    elif filename.casefold().startswith("el_sub_"):
        document_type = "elenco_subalterni"
    elif filename.casefold().startswith("punti_fiduciali_"):
        document_type = "fiduciali"
    return {
        "id": doc.id,
        "response_id": request_ref,
        "document_type": document_type,
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
        "protocollo": doc.protocol or None,
        "protocollo_anno": doc.protocol_year,
        "subject_search_scope": doc.subject_search_scope,
        "subject_search_province": doc.subject_search_province,
        "xml_content": content or "",
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _document_select(*, include_content: bool = True):
    """Select document metadata and related names; include archived text only for detail views that need it."""
    columns = (Document, CadastralUnit, IstatMunicipality)
    if include_content:
        columns += (DocumentContent.content,)
    columns += (Request.request_ref,)

    stmt = (
        select(*columns)
        .select_from(Document)
        .outerjoin(CadastralUnit, Document.cadastral_unit_id == CadastralUnit.id)
        .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
    )
    if include_content:
        stmt = stmt.outerjoin(DocumentContent, DocumentContent.document_id == Document.id)
    return stmt.outerjoin(Request, Document.request_id == Request.id)


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


async def get_documents_by_ids(document_ids: Iterable[int], *, include_content: bool = False) -> list[dict]:
    """Fetch explicit document IDs in bounded SQL batches; archived content is opt-in."""
    ids = sorted({int(document_id) for document_id in document_ids if document_id is not None})
    if not ids:
        return []
    documents = []
    async with _get_session_factory()() as session:
        for offset in range(0, len(ids), 2000):
            batch = ids[offset : offset + 2000]
            stmt = _document_select(include_content=include_content).where(Document.id.in_(batch)).order_by(Document.id)
            rows = (await session.execute(stmt)).all()
            if include_content:
                documents.extend(
                    _document_record(doc, unit, comune, request_ref=ref, content=content)
                    for doc, unit, comune, content, ref in rows
                )
            else:
                documents.extend(
                    _document_record(doc, unit, comune, request_ref=ref)
                    for doc, unit, comune, ref in rows
                )
    return documents


async def get_document_owner_fiscal_codes(
    document_ids: Iterable[int], *, fiscal_codes: Iterable[str] = ()
) -> dict[int, list[str]]:
    """Fetch owners for selected files and property files owned by selected subjects."""
    ids = sorted({int(document_id) for document_id in document_ids if document_id is not None})
    codes = sorted({str(code).strip().upper() for code in fiscal_codes if str(code or "").strip()})
    if not ids and not codes:
        return {}

    stmt = text(
        """
        WITH selected_owners AS (
            SELECT om.document_id, mo.subject_id
            FROM ownership_mutation om
            JOIN mutation_owner mo ON mo.mutation_id = om.id
            WHERE om.document_id = ANY(CAST(:ids AS bigint[]))

            UNION

            SELECT om.document_id, mo.subject_id
            FROM subject selected_subject
            JOIN mutation_owner mo ON mo.subject_id = selected_subject.id
            JOIN ownership_mutation om ON om.id = mo.mutation_id
            WHERE selected_subject.fiscal_code = ANY(CAST(:codes AS text[]))
        )
        SELECT DISTINCT selected_owners.document_id, s.fiscal_code
        FROM selected_owners
        JOIN subject s ON s.id = selected_owners.subject_id
        WHERE nullif(btrim(s.fiscal_code), '') IS NOT NULL
        ORDER BY selected_owners.document_id, s.fiscal_code
        """
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, {"ids": ids, "codes": codes})).all()

    owners_by_document: dict[int, list[str]] = {}
    for row in rows:
        owners_by_document.setdefault(int(row.document_id), []).append(str(row.fiscal_code))
    return owners_by_document


async def get_document_group_siblings(document_ids: Iterable[int]) -> list[dict]:
    """Fetch metadata for files sharing a request or filename stem with the given documents.

    Subject profiles use this to retain alternate P7M/XML/PDF files when loading
    a subject-scoped archive, without scanning the complete document catalogue.
    The caller must apply its document-visibility policy to the returned rows.
    """
    ids = sorted({int(document_id) for document_id in document_ids if document_id is not None})
    if not ids:
        return []

    request_ids: set[int] = set()
    filename_stems: set[str] = set()
    async with _get_session_factory()() as session:
        for offset in range(0, len(ids), 2000):
            batch = ids[offset : offset + 2000]
            rows = (
                await session.execute(
                    select(Document.request_id, Document.file_name).where(Document.id.in_(batch))
                )
            ).all()
            for row in rows:
                if row.request_id is not None:
                    request_ids.add(int(row.request_id))
                normalized_name = str(row.file_name or "").replace("\\", "/")
                stem = Path(normalized_name).stem.casefold()
                if stem:
                    filename_stems.add(stem)

        normalized_filename = func.replace(Document.file_name, "\\", "/")
        filename = func.regexp_replace(normalized_filename, r"^.*/", "")
        filename_stem = func.lower(func.regexp_replace(filename, r"\.[^.]*$", ""))
        sibling_conditions = [Document.id.in_(ids)]
        if request_ids:
            sibling_conditions.append(Document.request_id.in_(sorted(request_ids)))
        if filename_stems:
            sibling_conditions.append(filename_stem.in_(sorted(filename_stems)))
        sibling_stmt = _document_select(include_content=False).where(or_(*sibling_conditions)).order_by(Document.id)
        rows = (await session.execute(sibling_stmt)).all()

    return [
        _document_record(doc, unit, comune, request_ref=ref)
        for doc, unit, comune, ref in rows
    ]


def parcel_key(comune_id: Any, section: Any, sheet: Any, parcel: Any) -> tuple[int, str, str, str] | None:
    """Comparable key of a parcel: comune, sezione and foglio/particella without leading zeros (``0060`` == ``60``)."""
    if comune_id is None or not str(sheet or "").strip() or not str(parcel or "").strip():
        return None
    return (
        int(comune_id),
        str(section or "").strip().upper(),
        str(sheet).strip().lstrip("0") or "0",
        str(parcel).strip().lstrip("0") or "0",
    )


async def _parcel_key_rows(sql: str, keys: Iterable[tuple[int, str, str, str]]) -> list[Any]:
    """Run ``sql`` joined to the parcel keys (``k.comune_id/section/sheet/parcel``); no keys, no query."""
    unique = sorted(set(keys))
    if not unique:
        return []
    params = {
        "c": [k[0] for k in unique],
        "s": [k[1] for k in unique],
        "f": [k[2] for k in unique],
        "p": [k[3] for k in unique],
    }
    stmt = text(
        "WITH k(comune_id, section, sheet, parcel) AS ("
        "SELECT * FROM unnest(CAST(:c AS integer[]), CAST(:s AS text[]), CAST(:f AS text[]), CAST(:p AS text[]))) " + sql
    )
    async with _get_session_factory()() as session:
        return list((await session.execute(stmt, params)).all())


async def find_parcel_document_ids(keys: Iterable[tuple[int, str, str, str]]) -> dict[tuple, set[int]]:
    """Document ids (visure, planimetrie, ispezioni, …) stored for each parcel key (matched on the unit's parcel)."""
    rows = await _parcel_key_rows(
        "SELECT v.document_id, k.comune_id, k.section, k.sheet, k.parcel FROM ("
        "SELECT document_id, comune_id, section, sheet, parcel, 'document'::text AS link_source "
        "FROM v_document_cadastral_unit "
        "UNION "
        "SELECT de.document_id, cu.comune_id, cu.section, cu.sheet, cu.parcel, 'mortgage_extraction'::text "
        "FROM document_extraction de "
        "JOIN mortgage_inspection_property mip ON mip.inspection_id = de.id "
        "JOIN v_cadastral_unit cu ON cu.cadastral_unit_id = mip.cadastral_unit_id"
        ") v "
        "JOIN k ON v.comune_id = k.comune_id "
        "AND (upper(coalesce(v.section, '')) = k.section "
        "OR (v.link_source = 'mortgage_extraction' AND (coalesce(v.section, '') = '' OR k.section = ''))) "
        "AND coalesce(nullif(ltrim(v.sheet, '0'), ''), '0') = k.sheet "
        "AND coalesce(nullif(ltrim(v.parcel, '0'), ''), '0') = k.parcel",
        keys,
    )
    found: dict[tuple, set[int]] = {}
    for document_id, comune_id, section, sheet, parcel in rows:
        found.setdefault((comune_id, section, sheet, parcel), set()).add(int(document_id))
    return found


async def find_parcel_request_records(keys: Iterable[tuple[int, str, str, str]]) -> dict[tuple, list[dict]]:
    """Completed or pending SISTER requests saved against each parcel key.

    Some useful parcel products, including cadastral inspections and map queries, are stored as
    request responses rather than as ``Document`` rows. Keep those links alongside the archived
    files in the subject parcel view.
    """
    rows = await _parcel_key_rows(
        "SELECT r.request_ref, r.request_type, r.created_at, r.responded_at, r.success, r.total_results, "
        "k.comune_id, k.section, k.sheet, k.parcel "
        "FROM k JOIN cadastral_unit cu ON cu.comune_id = k.comune_id "
        "AND upper(coalesce(cu.section, '')) = k.section "
        "AND coalesce(nullif(ltrim(cu.sheet, '0'), ''), '0') = k.sheet "
        "AND coalesce(nullif(ltrim(cu.parcel, '0'), ''), '0') = k.parcel "
        "JOIN request r ON r.cadastral_unit_id = cu.id "
        "WHERE lower(replace(coalesce(r.request_type, ''), '-', '_')) IN "
        "('ispezioni', 'ispezioni_cart', 'ispezioni_cartacee', 'mappa', 'export_mappa', 'fiduciali') "
        "ORDER BY r.created_at DESC",
        keys,
    )
    found: dict[tuple, list[dict]] = {}
    for request_ref, request_type, created_at, responded_at, success, total_results, comune_id, section, sheet, parcel in rows:
        key = (comune_id, section, sheet, parcel)
        found.setdefault(key, []).append(
            {
                "request_id": request_ref,
                "request_type": request_type or "",
                "requested_at": created_at,
                "responded_at": responded_at,
                "success": success,
                "total_results": total_results,
            }
        )
    return found


async def find_parcels_with_geometry(keys: Iterable[tuple[int, str, str, str]]) -> set[tuple]:
    """Parcel keys that have a cached cadastral geometry (a map can be drawn without downloading anything)."""
    rows = await _parcel_key_rows(
        "SELECT DISTINCT k.comune_id, k.section, k.sheet, k.parcel FROM parcel_geometry_cache g "
        "JOIN k ON g.comune_id = k.comune_id AND upper(coalesce(g.section, '')) = k.section "
        "AND coalesce(nullif(ltrim(g.sheet, '0'), ''), '0') = k.sheet "
        "AND coalesce(nullif(ltrim(g.parcel, '0'), ''), '0') = k.parcel",
        keys,
    )
    return {tuple(row) for row in rows}


async def get_comune_centroids(cadastral_codes: Iterable[str]) -> dict[str, tuple[float, float]]:
    """Approximate (latitude, longitude) of comuni for parcels without their own map geometry.

    Prefer the centre of cached cadastral geometries, then fall back to the ISTAT municipality coordinates. These are
    placement hints for a parcel without an outline, not surveyed parcel positions.
    """
    codes = sorted({str(c).strip().upper() for c in cadastral_codes if str(c or "").strip()})
    if not codes:
        return {}
    schema = postgresql.dialect().identifier_preparer.quote_schema(DATABASE_ISTAT_SCHEMA)
    stmt = text(
        "SELECT upper(btrim(m.cadastral_code)), "
        "COALESCE(avg(ST_Y(ST_Centroid(g.geom))), m.ci_latitude), "
        "COALESCE(avg(ST_X(ST_Centroid(g.geom))), m.ci_longitude) "
        f"FROM {schema}.municipalities m "
        "LEFT JOIN parcel_geometry_cache g ON g.comune_id = m.numeric_code AND g.geom IS NOT NULL "
        "WHERE upper(btrim(m.cadastral_code)) = ANY(CAST(:codes AS text[])) "
        "GROUP BY 1, m.ci_latitude, m.ci_longitude "
        "HAVING COALESCE(avg(ST_Y(ST_Centroid(g.geom))), m.ci_latitude) IS NOT NULL "
        "AND COALESCE(avg(ST_X(ST_Centroid(g.geom))), m.ci_longitude) IS NOT NULL"
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, {"codes": codes})).all()
    return {code: (float(lat), float(lng)) for code, lat, lng in rows if lat is not None and lng is not None}


async def get_subject_name(fiscal_code: str) -> Optional[str]:
    """Display name stored for a codice fiscale / P.IVA (any query or document that named the subject)."""
    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT name FROM subject WHERE fiscal_code = :code AND coalesce(name, '') <> '' ORDER BY id DESC LIMIT 1"),
                {"code": fiscal_code},
            )
        ).first()
    return row[0] if row else None


async def refresh_subject_holdings(codes: Optional[Iterable[str]] = None) -> int:
    """Recompute ``subject_holding`` for the given codes (all codes with a successful subject search when ``None``).

    Each row comes from the code's latest subject search: provinces and Fabbricati/Terreni counts from its "Elenco
    Province" table, and from the properties it listed (with the subject's own right and quota: in a subject search
    every result row has exactly one owner, the searched subject) the totals of rendita / reddito dominicale and the
    valore catastale, full and pro quota (``cadastral_values``). Returns the number of rows written.
    """
    from .cadastral_values import building_cadastral_value, land_cadastral_value

    wanted = None
    if codes is not None:
        wanted = sorted({str(c or "").strip().upper() for c in codes if str(c or "").strip()})
        if not wanted:
            return 0
    code_filter = "" if wanted is None else " AND upper(btrim(r.subject_query)) = ANY(CAST(:codes AS text[]))"
    params: dict[str, Any] = {} if wanted is None else {"codes": wanted}
    latest = (
        "SELECT DISTINCT ON (upper(btrim(r.subject_query))) upper(btrim(r.subject_query)) AS code, r.id,"
        " r.responded_at AS searched_at"
        " FROM request r WHERE r.success IS TRUE AND length(btrim(r.subject_query)) IN (11, 16)"
        " AND EXISTS (SELECT 1 FROM {table} x WHERE x.request_id = r.id)" + code_filter +
        " ORDER BY upper(btrim(r.subject_query)), r.responded_at DESC NULLS LAST, r.id DESC"
    )
    province_stmt = text(
        f"WITH latest AS ({latest.format(table='request_province')}) "
        "SELECT latest.code, latest.searched_at, sum(rp.buildings_count) AS buildings, sum(rp.land_parcels_count) AS land,"
        " string_agg(DISTINCT coalesce(nullif(rp.province_code, ''), rp.province_name), ', ') AS provinces,"
        " count(DISTINCT coalesce(nullif(rp.province_code, ''), rp.province_name)) AS province_count "
        "FROM latest JOIN request_province rp ON rp.request_id = latest.id GROUP BY latest.code, latest.searched_at"
    )
    property_stmt = text(
        f"WITH latest AS ({latest.format(table='property')}) "
        "SELECT latest.code, latest.id AS request_id, latest.searched_at, p.cadastral_unit_id, p.property_type, p.category, p.income_eur,"
        " p.dominical_income_eur, o.right_type, o.share_numerator, o.share_denominator "
        "FROM latest JOIN property p ON p.request_id = latest.id AND p.property_type IN ('building', 'land') "
        "LEFT JOIN v_result_owner o ON o.result_id = p.result_id AND upper(btrim(o.fiscal_code)) = latest.code"
    )
    async with _get_session_factory()() as session:
        province_rows = (await session.execute(province_stmt, params)).mappings().all()
        property_rows = (await session.execute(property_stmt, params)).mappings().all()

    now = datetime.now(timezone.utc)
    holdings: dict[str, dict[str, Any]] = {}

    def entry(code: str) -> dict[str, Any]:
        return holdings.setdefault(code, {"fiscal_code": code, "computed_at": now})

    for row in province_rows:
        item = entry(str(row["code"]))
        item.update(
            provinces=row["provinces"] or None,
            searched_at=row["searched_at"],
            province_count=row["province_count"],
            buildings=int(row["buildings"] or 0),
            land_parcels=int(row["land"] or 0),
        )
    zero = Decimal(0)
    seen_units: set[tuple[str, Any]] = set()
    for prop in property_rows:
        item = entry(str(prop["code"]))
        if "properties" not in item:
            item.update(
                request_id=prop["request_id"], searched_at=prop["searched_at"], properties=0, full_ownership=0,
                unvalued=0, unknown_share=0,
                cadastral_income_eur=zero, dominical_income_eur=zero,
                cadastral_value_eur=zero, cadastral_value_share_eur=zero,
            )
        # The portal lists a unit once per ownership entry (a different quota each): the whole-asset figures count the
        # unit once, while every row's own quota still adds to the pro quota value.
        unit = (str(prop["code"]), prop["cadastral_unit_id"]) if prop["cadastral_unit_id"] is not None else None
        first_row = unit is None or unit not in seen_units
        if unit is not None:
            seen_units.add(unit)
        if prop["property_type"] == "land":
            income = prop["dominical_income_eur"] or zero
            computed = land_cadastral_value(prop["dominical_income_eur"])
            if first_row:
                item["dominical_income_eur"] += income
        else:
            income = prop["income_eur"] or zero
            computed = building_cadastral_value(prop["income_eur"], prop["category"])
            if first_row:
                item["cadastral_income_eur"] += income
        if first_row:
            item["properties"] += 1
        numerator, denominator = prop["share_numerator"], prop["share_denominator"]
        share = min(Decimal(numerator) / Decimal(denominator), Decimal(1)) if numerator and denominator else Decimal(1)
        if not (numerator and denominator):
            item["unknown_share"] += 1
        if share >= 1 and str(prop["right_type"] or "").lower().startswith("propriet"):
            item["full_ownership"] += 1
        if computed is None:
            if first_row:
                item["unvalued"] += 1
            continue
        if first_row:
            item["cadastral_value_eur"] += computed.value
        item["cadastral_value_share_eur"] += (computed.value * share).quantize(Decimal("0.01"))

    async with _get_session_factory()() as session:
        if wanted is None:
            await session.execute(delete(SubjectHolding))
        else:
            await session.execute(delete(SubjectHolding).where(SubjectHolding.fiscal_code.in_(wanted)))
        session.add_all(SubjectHolding(**item) for item in holdings.values())
        await session.commit()
    return len(holdings)


async def nameless_subject_codes(codes: Optional[Iterable[str]] = None) -> list[str]:
    """Codes (11/16 characters) whose subject has no real name: empty, or the code itself, and no surname."""
    sql = (
        "SELECT DISTINCT upper(btrim(fiscal_code)) FROM subject WHERE length(btrim(fiscal_code)) IN (11, 16) "
        "AND coalesce(last_name, '') = '' AND coalesce(first_name, '') = '' "
        "AND (coalesce(btrim(name), '') = '' OR upper(btrim(name)) = upper(btrim(fiscal_code)))"
    )
    params: dict[str, Any] = {}
    if codes is not None:
        params["codes"] = sorted({str(c or "").strip().upper() for c in codes if str(c or "").strip()})
        if not params["codes"]:
            return []
        sql += " AND upper(btrim(fiscal_code)) = ANY(CAST(:codes AS text[]))"
    async with _get_session_factory()() as session:
        return [row[0] for row in (await session.execute(text(sql), params)).all()]


async def fill_subject_names(names: dict[str, Any]) -> int:
    """Give a name to subjects stored with only their code (a search by code never returns the name).

    ``names`` maps a code to a display name, or to ``(name, last name, first name)`` when the source splits it.
    Fill-only: a subject that already has a real name, or a surname, is left as it is. Returns the rows updated.
    """
    codes: list[str] = []
    full: list[str] = []
    last: list[Optional[str]] = []
    first: list[Optional[str]] = []
    for code, value in names.items():
        name, last_name, first_name = value if isinstance(value, tuple) else (value, None, None)
        name = " ".join(str(name or "").split())[:300]
        if not name:
            continue
        codes.append(code.strip().upper())
        full.append(name)
        last.append(last_name or None)
        first.append(first_name or None)
    if not codes:
        return 0
    stmt = text(
        "UPDATE subject s SET name = n.name, last_name = n.last_name, first_name = n.first_name "
        "FROM unnest(CAST(:codes AS text[]), CAST(:names AS text[]), CAST(:last AS text[]), CAST(:first AS text[])) "
        "AS n(code, name, last_name, first_name) "
        "WHERE upper(btrim(s.fiscal_code)) = n.code AND coalesce(s.last_name, '') = '' AND coalesce(s.first_name, '') = '' "
        "AND (coalesce(btrim(s.name), '') = '' OR upper(btrim(s.name)) = upper(btrim(s.fiscal_code)))"
    )
    async with _get_session_factory()() as session:
        result = await session.execute(stmt, {"codes": codes, "names": full, "last": last, "first": first})
        await session.commit()
    return int(result.rowcount or 0)


async def backfill_subject_names(list_paths: Optional[Iterable[Path]] = None) -> dict[str, int]:
    """Name the subjects known only by code: first from the batch lists (``inputs/**/*.md``), then from opendata."""
    from .subject_names import default_list_paths, names_from_markdown, names_from_opendata

    if list_paths is None:
        list_paths = default_list_paths(Path(__file__).resolve().parents[1])
    nameless = await nameless_subject_codes()
    stats = {"nameless": len(nameless), "from_lists": 0, "from_opendata": 0}
    if not nameless:
        return stats
    listed = names_from_markdown(list_paths)
    stats["from_lists"] = await fill_subject_names({c: listed[c] for c in nameless if c in listed})
    remaining = await nameless_subject_codes(nameless)
    stats["from_opendata"] = await fill_subject_names(await names_from_opendata(remaining))
    stats["still_nameless"] = len(await nameless_subject_codes(nameless))
    return stats


# The name a subject carries itself: its own name (not one that only repeats the code), else surname and first name.
_OWN_NAME_SQL = (
    "CASE WHEN coalesce(btrim(subj.name), '') = '' OR upper(btrim(subj.name)) = c.code"
    " THEN nullif(btrim(coalesce(subj.last_name, '') || ' ' || coalesce(subj.first_name, '')), '')"
    " ELSE btrim(subj.name) END"
)


def _subject_profile_ctes(istat: str) -> str:
    """The CTEs behind the archive table: one row ``p`` per code (a successful subject search or a stored visura soggetto).

    ``:length`` (16 persons / 11 companies) and ``:code`` (one subject, or NULL for all) are bind parameters. Columns of
    ``p``: code, name, gender, birth_date, birth_place, birth_province, age, the stored ``subject_holding`` figures
    (provinces … cadastral_value_share, searched_at), searches, visure, last_seen, ``tags`` (``text[]`` or NULL, see
    :func:`normalize_tag`), ``name_inferred`` / ``name_records`` (see below). A name that only repeats the code is
    NULL; a real name is preferred over such a row, and a missing one is built from surname and first name, else (a code
    with registry records, e.g. a company found by a ``persona_giuridica`` search) the first record's denomination.
    """
    return (
        "req AS ("
        "  SELECT upper(btrim(r.subject_query)) AS code, r.responded_at FROM request r"
        "  WHERE r.success IS TRUE AND r.responded_at IS NOT NULL AND length(btrim(r.subject_query)) = :length"
        "  AND (CAST(:code AS text) IS NULL OR upper(btrim(r.subject_query)) = CAST(:code AS text))"
        "  AND (CAST(:codes AS text[]) IS NULL OR upper(btrim(r.subject_query)) = ANY(CAST(:codes AS text[])))"
        "), docs AS ("
        "  SELECT upper(btrim(s.fiscal_code)) AS code, count(DISTINCT d.id) AS visure,"
        "         max(coalesce(d.requested_at, d.created_at)) AS last_doc"
        "  FROM subject s JOIN document_subject ds ON ds.subject_id = s.id JOIN document d ON d.id = ds.document_id"
        "  WHERE d.document_type = 'visura_soggetto' AND length(btrim(s.fiscal_code)) = :length"
        "  AND (CAST(:code AS text) IS NULL OR upper(btrim(s.fiscal_code)) = CAST(:code AS text))"
        "  AND (CAST(:codes AS text[]) IS NULL OR upper(btrim(s.fiscal_code)) = ANY(CAST(:codes AS text[]))) GROUP BY 1"
        "), codes AS (SELECT code FROM req UNION SELECT code FROM docs"
        "), reqs AS (SELECT code, count(*) AS searches, max(responded_at) AS last_search FROM req GROUP BY code"
        "), subj AS ("
        "  SELECT DISTINCT ON (upper(btrim(s.fiscal_code))) upper(btrim(s.fiscal_code)) AS code, s.name, s.last_name,"
        "         s.first_name, s.gender, s.birth_date,"
        "         coalesce(m.name, s.birth_municipality_text) AS birth_place,"
        "         coalesce(nullif(s.birth_province_code, ''), m.province_abbreviation) AS birth_province"
        "  FROM subject s"
        f"  LEFT JOIN {istat}.municipalities m ON m.numeric_code = s.birth_comune_id"
        "  WHERE length(btrim(s.fiscal_code)) = :length"
        "  AND (CAST(:code AS text) IS NULL OR upper(btrim(s.fiscal_code)) = CAST(:code AS text))"
        "  AND (CAST(:codes AS text[]) IS NULL OR upper(btrim(s.fiscal_code)) = ANY(CAST(:codes AS text[])))"
        "  ORDER BY upper(btrim(s.fiscal_code)),"
        "           (coalesce(s.last_name, '') = '' AND upper(btrim(s.name)) = upper(btrim(s.fiscal_code))),"
        "           (s.birth_date IS NULL), s.id DESC"
        "), rec AS ("
        "  SELECT code, (array_agg(clean ORDER BY id))[1] AS rec_name, count(DISTINCT upper(clean)) AS name_records FROM ("
        "    SELECT upper(btrim(r.fiscal_code)) AS code, r.id,"
        "           nullif(btrim(regexp_replace(regexp_replace(r.denomination, '^[\\s\"]+|[\\s\"]+$', '', 'g'),"
        "                  '\\s+con sede in\\s.*$', '', 'i')), '') AS clean"
        "    FROM subject_record r WHERE length(btrim(r.fiscal_code)) = :length"
        "    AND (CAST(:code AS text) IS NULL OR upper(btrim(r.fiscal_code)) = CAST(:code AS text))"
        "    AND (CAST(:codes AS text[]) IS NULL OR upper(btrim(r.fiscal_code)) = ANY(CAST(:codes AS text[])))"
        "  ) x WHERE clean IS NOT NULL GROUP BY code"
        f"), p AS ("
        f"  SELECT c.code, coalesce({_OWN_NAME_SQL}, rec.rec_name) AS name, subj.last_name, subj.first_name,"
        f"    coalesce(rec.name_records, 0) AS name_records,"
        f"    ({_OWN_NAME_SQL} IS NULL AND rec.rec_name IS NOT NULL) AS name_inferred,"
        "    upper(left(coalesce(subj.gender, ''), 1)) AS gender, subj.birth_date, subj.birth_place,"
        "    upper(subj.birth_province) AS birth_province,"
        "    CASE WHEN subj.birth_date IS NULL THEN NULL"
        "         ELSE date_part('year', age(current_date, subj.birth_date))::int END AS age,"
        "    h.provinces, h.province_count, h.buildings, h.land_parcels AS land, h.properties, h.full_ownership,"
        "    h.unvalued, h.unknown_share, round(h.cadastral_income_eur) AS rendita,"
        "    round(h.dominical_income_eur) AS reddito_dominicale, round(h.cadastral_value_eur) AS cadastral_value,"
        "    round(h.cadastral_value_share_eur) AS cadastral_value_share, h.searched_at,"
        "    coalesce(reqs.searches, 0) AS searches, coalesce(docs.visure, 0) AS visure,"
        "    greatest(reqs.last_search, docs.last_doc) AS last_seen,"
        "    (SELECT array_agg(t.tag ORDER BY t.tag_key) FROM subject_tag t"
        "      WHERE t.subject_type = :stype AND t.subject_code = c.code) AS tags"
        "  FROM codes c LEFT JOIN subj ON subj.code = c.code LEFT JOIN rec ON rec.code = c.code"
        "  LEFT JOIN subject_holding h ON h.fiscal_code = c.code"
        "  LEFT JOIN reqs ON reqs.code = c.code LEFT JOIN docs ON docs.code = c.code"
        ") "
    )


def _subject_kind_params(kind: str, code: Optional[str] = None) -> dict[str, Any]:
    if kind not in ("person", "company"):
        raise ValueError(f"kind must be 'person' or 'company', not {kind!r}")
    return {
        "length": 16 if kind == "person" else 11,
        "code": str(code).strip().upper() if code else None,
        "stype": kind,
        "codes": None,
    }


def _profile_from_row(row: Any, kind: str) -> dict[str, Any]:
    """One archive row as the JSON/dict the table, the CSV and the profile header use."""

    def number(value: Any) -> Any:
        return None if value is None else float(value)

    def stamp(value: Any) -> str:
        return value.isoformat() if value else ""

    profile: dict[str, Any] = {
        "fiscal_code": str(row["code"]),
        "name": row["name"] or "",
        "provinces": row["provinces"] or "",
        "province_count": row["province_count"],
        "buildings": row["buildings"],
        "land": row["land"],
        "properties": row["properties"],
        "full_ownership": row["full_ownership"],
        "unvalued": row["unvalued"],
        "unknown_share": row["unknown_share"],
        "rendita": number(row["rendita"]),
        "reddito_dominicale": number(row["reddito_dominicale"]),
        "cadastral_value": number(row["cadastral_value"]),
        "cadastral_value_share": number(row["cadastral_value_share"]),
        "searched_at": stamp(row["searched_at"]),
        "searches": int(row["searches"] or 0),
        "visure": int(row["visure"] or 0),
        "last_seen": stamp(row["last_seen"]),
        "tags": list(row["tags"] or []),
        # a code with several registry records keeps no name of its own (it would be arbitrary): the list shows the first
        # record's denomination and says how many distinct ones SISTER has
        "name_inferred": bool(row["name_inferred"]),
        "name_records": int(row["name_records"] or 0),
    }
    if kind == "person":
        profile.update(
            last_name=row["last_name"] or "",
            first_name=row["first_name"] or "",
            gender=row["gender"] or "",
            birth_date=row["birth_date"].isoformat() if row["birth_date"] else "",
            age=row["age"],
            birth_place=(row["birth_place"] or "").strip(),
            birth_province=row["birth_province"] or "",
        )
    return profile


async def list_subject_profiles(kind: str = "person", code: Optional[str] = None) -> list[dict[str, Any]]:
    """Every archived person (``kind="person"``, 16-character codice fiscale) or company (``"company"``, 11-digit
    partita IVA), or just ``code``: name, birth data (persons), the stored holdings, searches and visure.

    The archive table pages through :func:`query_subject_profiles` instead; this returns everything.
    """
    params = _subject_kind_params(kind, code)
    istat = postgresql.dialect().identifier_preparer.quote_schema(DATABASE_ISTAT_SCHEMA)
    stmt = text(f"WITH {_subject_profile_ctes(istat)} SELECT * FROM p ORDER BY last_seen DESC NULLS LAST, code")
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, params)).mappings().all()
    return [_profile_from_row(row, kind) for row in rows]


# --- the archive table: search, filters, sort and paging in SQL -----------------------------------------------------

SUBJECT_NUMBER_FIELDS = (
    "age", "province_count", "properties", "buildings", "land", "full_ownership", "rendita", "reddito_dominicale",
    "cadastral_value", "cadastral_value_share", "unvalued", "visure", "searches",
)
SUBJECT_DATE_FIELDS = ("birth_date", "searched_at", "last_seen")
SUBJECT_TEXT_FIELDS = ("name", "fiscal_code", "provinces", "birth_place", "gender", "favorite_label")
SUBJECT_FILTER_FIELDS = SUBJECT_NUMBER_FIELDS + SUBJECT_DATE_FIELDS + SUBJECT_TEXT_FIELDS
_SUBJECT_SORT_SQL = {
    "name": "lower(name)", "fiscal_code": "code", "gender": "gender", "birth_place": "lower(birth_place)",
    "provinces": "provinces", "favorite_label": "(code = ANY(CAST(:favs AS text[])))",
    **{field: field for field in SUBJECT_NUMBER_FIELDS + SUBJECT_DATE_FIELDS},
}
SUBJECT_SORT_FIELDS = tuple(_SUBJECT_SORT_SQL)


def _like_pattern(term: str) -> str:
    return "%" + str(term).strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _number_condition(column: str, term: str, key: str, params: dict[str, Any]) -> Optional[str]:
    """SQL for a numeric header filter: ``10``, ``>10``, ``>=10``, ``<5``, ``<=5``, ``10-50``; None when unparsable
    (an unparsable filter does not filter, like the table's own number filter). Empty cells never match."""
    term = re.sub(r"[\s.]", "", str(term or "")).replace(",", ".")
    if not term:
        return None
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", term)
    if match:
        params[f"{key}a"], params[f"{key}b"] = Decimal(match.group(1)), Decimal(match.group(2))
        return f"{column} BETWEEN :{key}a AND :{key}b"
    match = re.fullmatch(r"(>=|<=|>|<|=)?(-?\d+(?:\.\d+)?)", term)
    if not match:
        return None
    params[key] = Decimal(match.group(2))
    return f"{column} {match.group(1) or '='} :{key}"


SUBJECT_NO_TAG = "-"  # the ``tag`` filter value that selects the subjects without any tag


def _subject_where(
    q: str,
    filters: dict[str, str],
    favorites: Iterable[str],
    name_status: Optional[str] = None,
    tag: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """WHERE clause (over ``p``) and bind parameters for the search box and the column filters."""
    clauses: list[str] = []
    params: dict[str, Any] = {"favs": sorted({str(c).upper() for c in favorites})}
    haystack = (
        "(coalesce(name, '') || ' ' || code || ' ' || coalesce(birth_place, '') || ' ' || "
        "coalesce(birth_province, '') || ' ' || coalesce(provinces, '') || ' ' || "
        "coalesce(array_to_string(tags, ' '), ''))"
    )
    for index, term in enumerate(str(q or "").split()[:8]):
        params[f"q{index}"] = _like_pattern(term)
        clauses.append(f"{haystack} ILIKE :q{index} ESCAPE '\\'")
    if tag == SUBJECT_NO_TAG:
        clauses.append("tags IS NULL")
    elif tag:
        try:
            params["tag_key"] = normalize_tag(tag)[0]
        except ValueError:  # a tag nobody can have matches nothing
            clauses.append("FALSE")
        else:
            clauses.append(
                "EXISTS (SELECT 1 FROM subject_tag st WHERE st.subject_type = :stype AND st.subject_code = code"
                " AND st.tag_key = :tag_key)"
            )
    if name_status == "missing":
        clauses.append("coalesce(btrim(name), '') = ''")
    elif name_status == "named":
        clauses.append("coalesce(btrim(name), '') <> ''")
    for index, (field, value) in enumerate(sorted(filters.items())):
        value = str(value or "").strip()
        if not value or field not in SUBJECT_FILTER_FIELDS:
            continue
        key = f"f{index}"
        if field in SUBJECT_NUMBER_FIELDS:
            clause = _number_condition(field, value, key, params)
        elif field in SUBJECT_DATE_FIELDS:  # typed as dd/mm/yyyy or as an ISO date, or just a year
            params[key] = _like_pattern(value)
            clause = (
                f"(to_char({field}, 'DD/MM/YYYY') ILIKE :{key} ESCAPE '\\' "
                f"OR to_char({field}, 'YYYY-MM-DD') ILIKE :{key} ESCAPE '\\')"
            )
        elif field == "gender":
            params[key] = value.upper()[:1]
            clause = f"gender = :{key}"
        elif field == "favorite_label":
            clause = "code = ANY(CAST(:favs AS text[]))" if value == "si" else (
                "NOT (code = ANY(CAST(:favs AS text[])))" if value == "no" else None
            )
        elif field == "birth_place":
            params[key] = _like_pattern(value)
            clause = f"(coalesce(birth_place, '') || ' ' || coalesce(birth_province, '')) ILIKE :{key} ESCAPE '\\'"
        else:
            column = {"fiscal_code": "code"}.get(field, field)
            params[key] = _like_pattern(value)
            clause = f"coalesce({column}, '') ILIKE :{key} ESCAPE '\\'"
        if clause:
            clauses.append(clause)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


async def query_subject_profiles(
    kind: str = "person",
    *,
    q: str = "",
    filters: Optional[dict[str, str]] = None,
    name_status: Optional[str] = None,
    tag: Optional[str] = None,
    sort: str = "last_seen",
    direction: str = "desc",
    limit: Optional[int] = 50,
    offset: int = 0,
    favorites: Iterable[str] = (),
    allowed_codes: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the archive table and the number of rows that match (all pages).

    ``q`` is the search box (every word must appear in name, code, birthplace or provinces), ``filters`` the column
    filters (field → text; numeric fields take ``>10`` / ``10-50``), ``sort`` one of ``SUBJECT_SORT_FIELDS``
    ``name_status`` optionally selects named or unnamed profiles, ``tag`` one tag (``SUBJECT_NO_TAG``: no tag at all). ``sort`` is one of ``SUBJECT_SORT_FIELDS`` (empty
    values last, ties broken by code). ``limit=None`` returns every match (the CSV export). Rows carry
    ``favorite`` for the codes in ``favorites``.
    """
    params = _subject_kind_params(kind)
    params["codes"] = None if allowed_codes is None else sorted({str(code).strip().upper() for code in allowed_codes})
    where, where_params = _subject_where(q, filters or {}, favorites, name_status, tag)
    params.update(where_params)
    order = _SUBJECT_SORT_SQL.get(sort, "last_seen")
    way = "ASC" if str(direction).lower() == "asc" else "DESC"
    istat = postgresql.dialect().identifier_preparer.quote_schema(DATABASE_ISTAT_SCHEMA)
    paging = ""
    if limit is not None:
        params.update(limit=max(1, int(limit)), offset=max(0, int(offset)))
        paging = " LIMIT :limit OFFSET :offset"
    stmt = text(
        f"WITH {_subject_profile_ctes(istat)} SELECT p.*, count(*) OVER () AS total_rows FROM p{where} "
        f"ORDER BY {order} {way} NULLS LAST, code{paging}"
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, params)).mappings().all()
    favorite_codes = set(params["favs"])
    profiles = []
    for row in rows:
        profile = _profile_from_row(row, kind)
        profile["favorite"] = profile["fiscal_code"] in favorite_codes
        profiles.append(profile)
    return profiles, int(rows[0]["total_rows"]) if rows else 0


async def subject_overview(kind: str = "person", allowed_codes: Iterable[str] | None = None) -> dict[str, Any]:
    """Aggregates over the whole archive in one pass, for the page's summary cards and column widths.

    ``subjects`` / ``with_visura`` / ``buildings`` / ``land`` / ``value_share`` are the card totals; ``widest`` holds the
    longest value of each text column and ``max`` the largest of each numeric one, so the table can size its columns
    to the widest value without loading the rows.
    """
    params = _subject_kind_params(kind)
    params["codes"] = None if allowed_codes is None else sorted({str(code).strip().upper() for code in allowed_codes})
    istat = postgresql.dialect().identifier_preparer.quote_schema(DATABASE_ISTAT_SCHEMA)

    def widest(expression: str) -> str:
        return f"(array_agg({expression} ORDER BY length({expression}) DESC) FILTER (WHERE {expression} IS NOT NULL))[1]"

    place = "(birth_place || coalesce(' (' || nullif(birth_province, '') || ')', ''))"
    maxima = ", ".join(f"max({field}) AS max_{field}" for field in SUBJECT_NUMBER_FIELDS)
    stmt = text(
        f"WITH {_subject_profile_ctes(istat)} SELECT count(*) AS subjects, count(*) FILTER (WHERE visure > 0) AS with_visura,"
        " coalesce(sum(buildings), 0) AS buildings, coalesce(sum(land), 0) AS land,"
        f" coalesce(sum(cadastral_value_share), 0) AS value_share, {widest('name')} AS w_name,"
        f" {widest('code')} AS w_fiscal_code, {widest('provinces')} AS w_provinces, {widest(place)} AS w_birth_place,"
        f" {maxima} FROM p"
    )
    async with _get_session_factory()() as session:
        row = (await session.execute(stmt, params)).mappings().one()
    return {
        "subjects": int(row["subjects"]),
        "with_visura": int(row["with_visura"]),
        "buildings": int(row["buildings"]),
        "land": int(row["land"]),
        "value_share": float(row["value_share"]),
        "widest": {
            field: row[f"w_{field}"] or "" for field in ("name", "fiscal_code", "provinces", "birth_place")
        },
        "max": {
            field: float(row[f"max_{field}"]) for field in SUBJECT_NUMBER_FIELDS if row[f"max_{field}"] is not None
        },
    }


async def get_subject_profile(code: str, kind: str = "person") -> Optional[dict[str, Any]]:
    """The :func:`list_subject_profiles` row of one code, or ``None`` when the archive has nothing for it."""
    rows = await list_subject_profiles(kind, code=code)
    return rows[0] if rows else None


async def list_person_profiles() -> list[dict[str, Any]]:
    """Archived persons (see :func:`list_subject_profiles`)."""
    return await list_subject_profiles("person")


async def search_person_profiles(query: str, limit: int = 50) -> tuple[list[dict[str, str | None]], bool]:
    """Find archived person profiles by a fragment of their name or fiscal code."""
    search_text = " ".join(str(query or "").upper().split())
    tokens = search_text.split()
    code_term = re.sub(r"[^A-Z0-9]", "", search_text)
    if not tokens:
        return [], False

    normalized_code = func.upper(func.trim(Request.subject_query))
    name_terms = []
    for token in tokens:
        escaped_token = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_token}%"
        name_terms.append(
            or_(
                Subject.name.ilike(pattern, escape="\\"),
                Subject.first_name.ilike(pattern, escape="\\"),
                Subject.last_name.ilike(pattern, escape="\\"),
            )
        )
    name_match = (
        select(Subject.id)
        .where(
            func.upper(func.trim(Subject.fiscal_code)) == normalized_code,
            *name_terms,
        )
        .exists()
    )
    matches = [name_match]
    if code_term:
        matches.append(normalized_code.ilike(f"%{code_term}%"))

    result_limit = max(1, int(limit))
    request_candidates = (
        select(
            normalized_code.label("fiscal_code"),
            Request.responded_at.label("last_seen"),
        )
        .where(
            Request.success.is_(True),
            Request.responded_at.is_not(None),
            Request.subject_query.is_not(None),
            func.length(normalized_code) == 16,
            or_(*matches),
        )
        .subquery()
    )
    request_stmt = (
        select(
            request_candidates.c.fiscal_code,
            func.max(request_candidates.c.last_seen).label("last_seen"),
        )
        .group_by(request_candidates.c.fiscal_code)
        .order_by(func.max(request_candidates.c.last_seen).desc(), request_candidates.c.fiscal_code)
        .limit(result_limit + 1)
    )
    document_code = func.upper(func.trim(Subject.fiscal_code))
    document_name_terms = []
    for token in tokens:
        escaped_token = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_token}%"
        document_name_terms.append(
            or_(
                Subject.name.ilike(pattern, escape="\\"),
                Subject.first_name.ilike(pattern, escape="\\"),
                Subject.last_name.ilike(pattern, escape="\\"),
                Document.subject_text.ilike(pattern, escape="\\"),
            )
        )
    document_matches = [and_(*document_name_terms)]
    if code_term:
        document_matches.append(document_code.ilike(f"%{code_term}%"))
    document_stmt = (
        select(
            document_code.label("fiscal_code"),
            func.max(Subject.name).label("name"),
            func.max(func.coalesce(Document.requested_at, Document.created_at)).label("last_seen"),
        )
        .select_from(Subject)
        .join(DocumentSubject, DocumentSubject.subject_id == Subject.id)
        .join(Document, Document.id == DocumentSubject.document_id)
        .where(
            Document.document_type == "visura_soggetto",
            Subject.fiscal_code.is_not(None),
            func.length(document_code) == 16,
            or_(*document_matches),
        )
        .group_by(document_code)
        .order_by(func.max(func.coalesce(Document.requested_at, Document.created_at)).desc(), document_code)
        .limit(result_limit + 1)
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(request_stmt)).all()
        document_rows = (await session.execute(document_stmt)).all()
        request_codes = [str(row.fiscal_code) for row in rows]
        names_by_code: dict[str, str] = {}
        if request_codes:
            subject_code = func.upper(func.trim(Subject.fiscal_code))
            name_stmt = (
                select(subject_code.label("fiscal_code"), Subject.name)
                .where(subject_code.in_(request_codes))
                .order_by(Subject.id.desc())
            )
            for row in (await session.execute(name_stmt)).all():
                names_by_code.setdefault(str(row.fiscal_code), row.name)

    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        fiscal_code = str(row.fiscal_code)
        candidates[fiscal_code] = {
            "fiscal_code": fiscal_code,
            "name": names_by_code.get(fiscal_code),
            "last_seen": row.last_seen,
        }
    for row in document_rows:
        fiscal_code = str(row.fiscal_code)
        candidate = candidates.get(fiscal_code)
        if candidate is None or (row.last_seen and row.last_seen > candidate["last_seen"]):
            candidates[fiscal_code] = {
                "fiscal_code": fiscal_code,
                "name": row.name,
                "last_seen": row.last_seen,
            }
        elif not candidate["name"] and row.name:
            candidate["name"] = row.name
    ordered = sorted(
        candidates.values(),
        key=lambda item: (item["last_seen"] is not None, item["last_seen"], item["fiscal_code"]),
        reverse=True,
    )
    has_more = len(ordered) > result_limit
    return [
        {"fiscal_code": item["fiscal_code"], "name": item["name"]}
        for item in ordered[:result_limit]
    ], has_more


async def find_subject_inspection_document_ids(fiscal_code: str) -> set[int]:
    """Ids of the ispezioni ipotecarie whose parties include the codice fiscale."""
    stmt = text(
        "SELECT de.document_id FROM mortgage_inspection_party p "
        "JOIN subject s ON s.id = p.subject_id JOIN document_extraction de ON de.id = p.inspection_id "
        "WHERE s.fiscal_code = :code "
        # Notes, titles and lists of the same inspection that carry no party rows (signed .p7m titles, PDFs not
        # extracted yet) are tied to the subject through document_subject.
        "UNION SELECT d.id FROM document d JOIN document_subject ds ON ds.document_id = d.id "
        "JOIN subject s ON s.id = ds.subject_id "
        "WHERE s.fiscal_code = :code AND d.document_type IN ('ispezione_ipotecaria', 'titolo_telematico')"
    )
    async with _get_session_factory()() as session:
        return {int(row[0]) for row in (await session.execute(stmt, {"code": fiscal_code})).all()}


async def find_subject_document_associations(fiscal_code: str) -> dict[int, dict[str, Any]]:
    """All document relationships known for a subject and its properties.

    The entity page is an archive, so it must not be limited to the newest
    ``soggetto`` response.  This union deliberately follows every normalized
    provenance path we have: direct document subjects, ownership acts,
    mortgage-inspection parties, documents produced by a subject request, and
    cadastral units found in any saved subject search.  Filename/subject text
    is retained as a compatibility path for older imports that predate the
    normalized relationship tables.
    """
    code = fiscal_code_key(fiscal_code)
    stmt = text(
        """
        WITH owned_documents AS (
            SELECT ds.document_id
            FROM document_subject ds
            JOIN subject s ON s.id = ds.subject_id
            WHERE upper(btrim(s.fiscal_code)) = :code

            UNION
            SELECT om.document_id
            FROM ownership_mutation om
            JOIN mutation_owner mo ON mo.mutation_id = om.id
            JOIN subject s ON s.id = mo.subject_id
            WHERE upper(btrim(s.fiscal_code)) = :code

            UNION
            SELECT d.id
            FROM document d
            WHERE d.document_type = 'visura_soggetto'
              AND (
                    position(:code IN upper(coalesce(d.file_name, ''))) > 0
                 OR position(:code IN upper(coalesce(d.subject_text, ''))) > 0
              )
        ), known_units AS (
            SELECT p.cadastral_unit_id
            FROM property p
            JOIN request r ON r.id = p.request_id
            WHERE upper(btrim(r.subject_query)) = :code
              AND p.cadastral_unit_id IS NOT NULL

            UNION
            SELECT v.cadastral_unit_id
            FROM v_document_cadastral_unit v
            JOIN owned_documents od ON od.document_id = v.document_id
            WHERE v.cadastral_unit_id IS NOT NULL
        ), known_properties AS (
            SELECT DISTINCT
                cu.comune_id,
                coalesce(cu.province_code, '') AS province_code,
                coalesce(cu.comune, '') AS municipality,
                upper(coalesce(cu.section, '')) AS section,
                coalesce(nullif(ltrim(cu.sheet, '0'), ''), '0') AS sheet,
                coalesce(nullif(ltrim(cu.parcel, '0'), ''), '0') AS parcel,
                coalesce(nullif(ltrim(cu.subunit, '0'), ''), '0') AS subunit
            FROM v_cadastral_unit cu
            JOIN known_units ku ON ku.cadastral_unit_id = cu.cadastral_unit_id
            WHERE cu.comune_id IS NOT NULL AND cu.sheet IS NOT NULL AND cu.parcel IS NOT NULL
        ), associations(document_id, relation, province_code, municipality, section, sheet, parcel, subunit) AS (
            SELECT ds.document_id, 'subject', NULL::text, NULL::text, NULL::text,
                   NULL::text, NULL::text, NULL::text
            FROM document_subject ds
            JOIN subject s ON s.id = ds.subject_id
            WHERE upper(btrim(s.fiscal_code)) = :code

            UNION ALL
            SELECT om.document_id, 'ownership', NULL::text, NULL::text, NULL::text,
                   NULL::text, NULL::text, NULL::text
            FROM ownership_mutation om
            JOIN mutation_owner mo ON mo.mutation_id = om.id
            JOIN subject s ON s.id = mo.subject_id
            WHERE upper(btrim(s.fiscal_code)) = :code

            UNION ALL
            SELECT de.document_id, 'mortgage_party', NULL::text, NULL::text, NULL::text,
                   NULL::text, NULL::text, NULL::text
            FROM mortgage_inspection_party mip
            JOIN subject s ON s.id = mip.subject_id
            JOIN document_extraction de ON de.id = mip.inspection_id
            WHERE upper(btrim(s.fiscal_code)) = :code

            UNION ALL
            SELECT d.id, 'subject_request', NULL::text, NULL::text, NULL::text,
                   NULL::text, NULL::text, NULL::text
            FROM document d
            JOIN request r ON r.id = d.request_id
            WHERE upper(btrim(r.subject_query)) = :code

            UNION ALL
            SELECT d.id, 'legacy_identity', NULL::text, NULL::text, NULL::text,
                   NULL::text, NULL::text, NULL::text
            FROM document d
            WHERE position(:code IN upper(coalesce(d.file_name, ''))) > 0
               OR position(:code IN upper(coalesce(d.subject_text, ''))) > 0

            UNION ALL
            SELECT DISTINCT v.document_id, 'property', kp.province_code, kp.municipality,
                   kp.section, kp.sheet, kp.parcel,
                   CASE WHEN kp.subunit = '0' THEN '' ELSE kp.subunit END
            FROM v_document_cadastral_unit v
            JOIN known_properties kp
              ON v.comune_id = kp.comune_id
             AND upper(coalesce(v.section, '')) = kp.section
             AND coalesce(nullif(ltrim(v.sheet, '0'), ''), '0') = kp.sheet
             AND coalesce(nullif(ltrim(v.parcel, '0'), ''), '0') = kp.parcel
             AND (
                    coalesce(v.subunit, '') = ''
                 OR kp.subunit = '0'
                 OR coalesce(nullif(ltrim(v.subunit, '0'), ''), '0') = kp.subunit
             )
            WHERE coalesce(v.document_type, '') <> 'visura_soggetto'

            UNION ALL
            SELECT DISTINCT de.document_id, 'property', kp.province_code, kp.municipality,
                   kp.section, kp.sheet, kp.parcel,
                   CASE WHEN kp.subunit = '0' THEN '' ELSE kp.subunit END
            FROM document_extraction de
            JOIN mortgage_inspection_property mip ON mip.inspection_id = de.id
            JOIN v_cadastral_unit v ON v.cadastral_unit_id = mip.cadastral_unit_id
            JOIN known_properties kp
              ON v.comune_id = kp.comune_id
             AND (
                    upper(coalesce(v.section, '')) = kp.section
                 OR coalesce(v.section, '') = ''
                 OR kp.section = ''
             )
             AND coalesce(nullif(ltrim(v.sheet, '0'), ''), '0') = kp.sheet
             AND coalesce(nullif(ltrim(v.parcel, '0'), ''), '0') = kp.parcel
             AND (
                    coalesce(v.subunit, '') = ''
                 OR kp.subunit = '0'
                 OR coalesce(nullif(ltrim(v.subunit, '0'), ''), '0') = kp.subunit
             )

            UNION ALL
            SELECT DISTINCT d.id, 'property', kp.province_code, kp.municipality,
                   kp.section, kp.sheet, kp.parcel,
                   CASE WHEN kp.subunit = '0' THEN '' ELSE kp.subunit END
            FROM document d
            JOIN v_document_cadastral_unit v ON v.document_id = d.id
            JOIN known_properties kp
              ON v.comune_id IS NULL
             AND position('_' || upper(kp.province_code) || '_' IN upper(d.file_name)) > 0
             AND (
                    upper(coalesce(v.section, '')) = kp.section
                 OR coalesce(v.section, '') = ''
                 OR kp.section = ''
             )
             AND coalesce(nullif(ltrim(v.sheet, '0'), ''), '0') = kp.sheet
             AND coalesce(nullif(ltrim(v.parcel, '0'), ''), '0') = kp.parcel
             AND (
                    coalesce(v.subunit, '') = ''
                 OR kp.subunit = '0'
                 OR coalesce(nullif(ltrim(v.subunit, '0'), ''), '0') = kp.subunit
             )
            WHERE coalesce(d.document_type, '') <> 'visura_soggetto'
        )
        SELECT DISTINCT document_id, relation, province_code, municipality, section, sheet, parcel, subunit
        FROM associations
        ORDER BY document_id, relation, province_code, municipality, section, sheet, parcel, subunit
        """
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, {"code": code})).mappings().all()

    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        document_id = int(row["document_id"])
        entry = found.setdefault(document_id, {"sources": set(), "properties": []})
        entry["sources"].add(str(row["relation"]))
        if row["relation"] != "property":
            continue
        prop = {
            "province": row["province_code"] or "",
            "municipality": row["municipality"] or "",
            "section": row["section"] or "",
            "sheet": row["sheet"] or "",
            "parcel": row["parcel"] or "",
            "subunit": row["subunit"] or "",
        }
        if prop not in entry["properties"]:
            entry["properties"].append(prop)
    return found


async def find_documents_with_extraction(document_ids: Iterable[int]) -> set[int]:
    """Document ids that have at least one persisted structured extraction."""
    ids = sorted({int(document_id) for document_id in document_ids})
    if not ids:
        return set()
    async with _get_session_factory()() as session:
        rows = await session.execute(
            select(DocumentExtraction.document_id)
            .where(DocumentExtraction.document_id.in_(ids))
            .distinct()
        )
        return {int(document_id) for document_id in rows.scalars()}


async def find_subject_query_requests(fiscal_code: str, limit: int = 20) -> list[dict]:
    """Completed subject queries (``soggetto`` / persona giuridica) for a codice fiscale or P.IVA, newest first."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Request)
                .where(Request.subject_query == fiscal_code, Request.success.is_(True))
                .where(Request.responded_at.is_not(None))
                .order_by(Request.responded_at.desc())
                .limit(limit)
            )
        ).scalars()
        return [
            {
                "request_id": req.request_ref,
                "request_type": req.request_type,
                "tipo_catasto": req.cadastre_type or "",
                "total_results": req.total_results,
                "responded_at": req.responded_at.isoformat() if req.responded_at else None,
            }
            for req in rows
        ]


async def get_subject_mortgage_formalities(
    fiscal_code: str, *, include_session_timeline: bool = False
) -> list[dict[str, Any]]:
    """Read archived SISTER mortgage-search summaries associated with a subject code.

    The portal's ``Elenco sintetico delle formalità`` is stored in the raw request payload,
    because it is not a downloaded note or a structured document extraction. Keep that
    provenance and expose only payloads explicitly linked to this subject's tax code.
    """
    code = fiscal_code_key(fiscal_code)
    if not code:
        return []
    stmt = text(
        "SELECT r.id AS request_pk, r.request_ref, coalesce(r.responded_at, r.created_at) AS recorded_at, "
        "       p.data::jsonb #> '{portal_capture,inspection}' AS inspection, "
        "       p.data::jsonb -> 'formalities' AS formalities, "
        "       p.data::jsonb #> '{portal_capture,pdf_note_request}' AS note_request, "
        "       p.data::jsonb #> '{portal_capture,title_requests}' AS title_requests, "
        "       p.data::jsonb #> '{portal_capture,costs}' AS costs, "
        "       p.data::jsonb -> 'cost' AS request_cost "
        "FROM request r "
        "JOIN sister_archive.request_payload p ON p.request_id = r.id "
        "WHERE r.request_type = 'ipotecaria_persona_fisica' "
        "  AND upper(btrim(p.data::jsonb #>> '{portal_capture,inspection,subject_tax_code}')) = :code "
        "  AND CASE WHEN jsonb_typeof(p.data::jsonb -> 'formalities') = 'array' "
        "           THEN jsonb_array_length(p.data::jsonb -> 'formalities') ELSE 0 END > 0 "
        "ORDER BY coalesce(r.responded_at, r.created_at) DESC NULLS LAST, r.id DESC"
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, {"code": code})).mappings().all()

    results: list[dict[str, Any]] = []

    def _decoded(value: Any, fallback: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return fallback
        return value if value is not None else fallback

    def _amount(value: Any) -> Decimal | None:
        if isinstance(value, dict):
            for key in ("value", "amount", "total", "cost", "cost_text", "cost_value", "costo", "fee", "text"):
                if value.get(key) is not None:
                    parsed = _amount(value[key])
                    if parsed is not None:
                        return parsed
            return None
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float, Decimal)):
            try:
                parsed = Decimal(str(value))
                return parsed if parsed.is_finite() else None
            except InvalidOperation:
                return None
        match = re.search(r"-?\d[\d .]*(?:,\d{1,2}|\.\d{1,2})?", str(value))
        if not match:
            return None
        text_value = match.group(0).replace(" ", "")
        if "," in text_value:
            text_value = text_value.replace(".", "").replace(",", ".")
        try:
            parsed = Decimal(text_value)
            return parsed if parsed.is_finite() else None
        except InvalidOperation:
            return None

    def _amount_label(value: Decimal) -> str:
        whole, fraction = f"{value:.2f}".split(".")
        groups = []
        while whole:
            groups.append(whole[-3:])
            whole = whole[:-3]
        return f"{'.'.join(reversed(groups))},{fraction}"

    def _risk_summary(items: list[dict[str, Any]]) -> dict[str, int]:
        seen: set[tuple[str, str]] = set()
        mortgages = pignoramenti = transfers_2026 = 0
        for item in items:
            identity = (
                str(item.get("registro_particolare") or item.get("numero") or "").strip(),
                str(item.get("registro_generale") or item.get("data_formalita") or "").strip(),
            )
            if identity != ("", "") and identity in seen:
                continue
            seen.add(identity)
            kind = str(item.get("tipo") or "").casefold()
            cause = " ".join(
                str(item.get(key) or "") for key in ("causale", "descrizione", "atto")
            ).casefold()
            if "iscrizione" in kind and not item.get("cancellata"):
                mortgages += 1
            if "pignoramento" in cause and not item.get("cancellata"):
                pignoramenti += 1
            date_value = str(item.get("data_formalita") or item.get("data_formalita_display") or "")
            if "decreto di trasferimento" in cause and re.search(r"(?:^2026|/2026$)", date_value):
                transfers_2026 += 1
        return {
            "live_mortgages": mortgages,
            "live_pignoramenti": pignoramenti,
            "decreti_trasferimento_2026": transfers_2026,
        }

    for row in rows:
        formalities = _decoded(row["formalities"], [])
        # Older drivers may return JSONB as text; accept either shape at this boundary.
        if not isinstance(formalities, list):
            continue
        inspection = _decoded(row["inspection"], {})
        if not isinstance(inspection, dict):
            inspection = {}
        note_request = _decoded(row["note_request"], {})
        if not isinstance(note_request, dict):
            note_request = {}
        title_requests = _decoded(row["title_requests"], {})
        single_title = title_requests.get("single_request") if isinstance(title_requests, dict) else None
        title_request_id = ""
        title_number = None
        if isinstance(single_title, dict):
            title_request_id = str(single_title.get("request_id") or "").strip()
            title_number = single_title.get("formalita_numero")
        title_document: dict[str, Any] | None = None
        stored_title = single_title.get("document") if isinstance(single_title, dict) else None
        if isinstance(stored_title, dict) and stored_title.get("document_id") is not None:
            # The archived file recorded against the request (a title downloaded outside Richieste keeps no DOC_ name).
            title_document = {"id": stored_title["document_id"], "file_name": stored_title.get("file_name") or ""}
        elif title_request_id.isdigit():
            # Downloaded titles are stored as ...DOC_<portal request id>...; absent until fetched from Richieste.
            async with _get_session_factory()() as session:
                found = (
                    await session.execute(
                        text(
                            "SELECT id, file_name FROM document WHERE file_name LIKE :pattern "
                            "ORDER BY id DESC LIMIT 1"
                        ),
                        {"pattern": f"%DOC\\_{title_request_id}%"},
                    )
                ).mappings().first()
            if found:
                title_document = {"id": found["id"], "file_name": found["file_name"]}
        note_documents = note_request.get("documents") or []
        if not isinstance(note_documents, list):
            note_documents = []
        note_documents_by_register: dict[tuple[str, str], dict[str, Any]] = {}
        resolved_note_documents = []
        for document in note_documents:
            if not isinstance(document, dict):
                continue
            document = dict(document)
            portal_request_id = next(
                (
                    str(document.get(key) or "").strip()
                    for key in ("request_id", "id_richiesta", "idRichiesta", "portal_request_id")
                    if document.get(key)
                ),
                "",
            )
            if document.get("document_id") is None and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", portal_request_id):
                async with _get_session_factory()() as session:
                    found = (
                        await session.execute(
                            text(
                                "SELECT id, file_name FROM document "
                                "WHERE strpos(file_name, :marker) > 0 ORDER BY id DESC LIMIT 1"
                            ),
                            {"marker": f"_T{portal_request_id}_"},
                        )
                    ).mappings().first()
                if found:
                    document["document_id"] = found["id"]
                    document["file_name"] = found["file_name"]
            key = (
                str(document.get("registro_particolare") or "").strip(),
                str(document.get("registro_generale") or "").strip(),
            )
            resolved_note_documents.append(document)
            if key != ("", "") and document.get("document_id") is not None:
                note_documents_by_register.setdefault(key, document)
        note_documents = resolved_note_documents
        if isinstance(note_request.get("documents"), list):
            note_request["documents"] = note_documents
        downloaded_note_count = sum(1 for document in note_documents if document.get("document_id") is not None)
        if note_request.get("accepted") and note_documents and downloaded_note_count == len(note_documents):
            note_request["downloaded"] = True
        note_request["downloaded_count"] = downloaded_note_count
        parsed_formalities = []
        for item in formalities:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            for field in ("data_formalita", "data_repertorio"):
                value = str(normalized.get(field) or "")
                if len(value) == 10 and value[4] == "-" and value[7] == "-":
                    normalized[f"{field}_display"] = f"{value[8:10]}/{value[5:7]}/{value[:4]}"
            note_document = note_documents_by_register.get(
                (
                    str(normalized.get("registro_particolare") or "").strip(),
                    str(normalized.get("registro_generale") or "").strip(),
                )
            )
            if note_document:
                normalized["note_document_id"] = note_document.get("document_id")
                normalized["note_document_filename"] = note_document.get("file_name") or ""

            related = _decoded(normalized.get("documenti_correlati"), [])
            if isinstance(related, dict):
                related = [related]
            if not isinstance(related, list):
                related = []
            annotation_lines = []
            has_cancellation = False
            fully_cancelled = False
            for annotation in related:
                if isinstance(annotation, str):
                    annotation = {"causale": annotation}
                if not isinstance(annotation, dict):
                    continue
                number = (
                    annotation.get("numero") or annotation.get("numero_annotazione")
                    or annotation.get("registro_particolare") or ""
                )
                annotation_date = (
                    annotation.get("data") or annotation.get("data_formalita")
                    or annotation.get("data_annotazione") or ""
                )
                annotation_date = str(annotation_date)
                if len(annotation_date) == 10 and annotation_date[4] == "-" and annotation_date[7] == "-":
                    annotation_date = f"{annotation_date[8:10]}/{annotation_date[5:7]}/{annotation_date[:4]}"
                cause = (
                    annotation.get("causale") or annotation.get("descrizione")
                    or annotation.get("atto") or annotation.get("tipo") or ""
                )
                annotation_text = " ".join(str(value) for value in annotation.values() if value is not None)
                is_cancellation = bool(re.search(r"cancellazion|cancellat", annotation_text, re.IGNORECASE))
                is_full_cancellation = bool(
                    re.search(r"cancellazion\w*\s+(?:totale|integrale)|(?:totale|integrale)\s+cancellazion\w*", annotation_text, re.IGNORECASE)
                )
                has_cancellation = has_cancellation or is_cancellation
                fully_cancelled = fully_cancelled or is_full_cancellation
                line = "Annotazione"
                if number:
                    line += f" {number}"
                if annotation_date:
                    line += f" del {annotation_date}"
                if cause:
                    line += f" — {cause}"
                annotation_lines.append({
                    "text": line,
                    "is_cancellation": is_cancellation,
                    "is_full_cancellation": is_full_cancellation,
                })
            normalized["documenti_correlati_display"] = annotation_lines
            normalized["has_cancellation_annotation"] = has_cancellation
            normalized["cancellata"] = fully_cancelled
            if title_number is not None and normalized.get("numero") == title_number:
                normalized["title_request_id"] = title_request_id
                normalized["title_status"] = (single_title or {}).get("status") or ""
                if title_document:
                    normalized["title_document_id"] = title_document["id"]
                    normalized["title_document_filename"] = title_document["file_name"]
            parsed_formalities.append(normalized)
        if not parsed_formalities:
            continue
        costs = _decoded(row["costs"], {})
        if not isinstance(costs, dict):
            costs = {}
        request_cost = _decoded(row["request_cost"], row["request_cost"])
        inspection_cost = _amount(
            costs.get("inspection_cost") or costs.get("inspection") or costs.get("ispezione")
            or inspection.get("cost") or inspection.get("cost_value") or inspection.get("fee")
            or request_cost
        )
        notes_cost = _amount(
            costs.get("notes_cost") or costs.get("notes") or costs.get("note") or costs.get("annotazioni")
            or note_request.get("cost") or note_request.get("total_cost") or note_request.get("amount")
        )
        if notes_cost is None and note_documents:
            document_costs = [_amount(document) for document in note_documents if isinstance(document, dict)]
            if document_costs and all(value is not None for value in document_costs):
                notes_cost = sum(document_costs, Decimal("0"))
        if notes_cost is None:
            unit_cost = _amount(
                note_request.get("unit_cost") or note_request.get("cost_per_note")
                or note_request.get("costo_unitario")
            )
            selected_count = note_request.get("selected_count") or len(note_documents)
            try:
                if unit_cost is not None and int(selected_count) > 0:
                    notes_cost = unit_cost * int(selected_count)
            except (TypeError, ValueError):
                pass
        titles_cost = _amount(costs.get("titles_cost") or costs.get("titles") or costs.get("titoli"))
        cost_parts = []
        if inspection_cost is not None:
            cost_parts.append(f"{_amount_label(inspection_cost)} € ispezione")
        if notes_cost is not None:
            cost_parts.append(f"{_amount_label(notes_cost)} € note")
        if titles_cost is not None:
            cost_parts.append(f"{_amount_label(titles_cost)} € titoli")

        pending_request_ids: set[str] = set()
        if note_request.get("accepted") and not note_request.get("downloaded"):
            pending_containers = [
                note_request,
                *(document for document in note_documents if document.get("document_id") is None),
            ]
            for container in pending_containers:
                if not isinstance(container, dict):
                    continue
                for key in ("request_id", "id_richiesta", "idRichiesta", "portal_request_id"):
                    value = str(container.get(key) or "").strip()
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                        pending_request_ids.add(value)
        page_visits = []
        if include_session_timeline:
            async with _get_session_factory()() as session:
                visits = (
                    await session.execute(
                        text(
                            "SELECT pv.step, pv.url, pv.screenshot_url, pv.visited_at, "
                            "       array_agg(pve.message ORDER BY pve.error_index) "
                            "           FILTER (WHERE pve.id IS NOT NULL) AS errors "
                            "FROM page_visit pv LEFT JOIN page_visit_error pve ON pve.page_visit_id = pv.id "
                            "WHERE pv.request_id = :request_id "
                            "GROUP BY pv.id ORDER BY pv.visited_at NULLS LAST, pv.id"
                        ),
                        {"request_id": row["request_pk"]},
                    )
                ).mappings().all()
            page_visits = [
                {
                    "step": visit["step"] or "",
                    "url": visit["url"] or "",
                    "screenshot_url": visit["screenshot_url"] or "",
                    "visited_at": visit["visited_at"],
                    "errors": [str(error) for error in (visit["errors"] or []) if error],
                }
                for visit in visits
            ]

        recorded_at = row["recorded_at"]
        result = {
            "request_ref": row["request_ref"],
            "recorded_at": recorded_at.isoformat() if recorded_at else "",
            "inspection": inspection,
            "formalities": parsed_formalities,
            "note_request": note_request,
            "pending_request_ids": sorted(pending_request_ids),
            "cost_summary": "Costo: " + " + ".join(cost_parts) if cost_parts else "",
            "page_visits": page_visits,
        }
        results.append(result)
    if results:
        results[0]["risk_summary"] = _risk_summary(results[0]["formalities"])
    return results


async def get_subject_document_ids(fiscal_code: str) -> set[int]:
    """Return document IDs linked to a queried subject's canonical fiscal code."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(DocumentSubject.document_id)
            .join(Subject, DocumentSubject.subject_id == Subject.id)
            .where(Subject.fiscal_code == fiscal_code_key(fiscal_code))
        )
        return {int(document_id) for document_id in result.scalars().all() if document_id is not None}


async def get_subject_document_ids_for_fiscal_codes(fiscal_codes: Iterable[str]) -> set[int]:
    """Fetch all subject-visura IDs linked to any of the requested fiscal codes."""
    codes = sorted(
        {
            normalized
            for code in fiscal_codes
            if (normalized := fiscal_code_key(code))
        }
    )
    if not codes:
        return set()
    stmt = text(
        """
        SELECT DISTINCT ds.document_id
        FROM document_subject ds
        JOIN subject s ON s.id = ds.subject_id
        WHERE s.fiscal_code = ANY(CAST(:codes AS text[]))
        """
    )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt, {"codes": codes})).scalars().all()
    return {int(document_id) for document_id in rows if document_id is not None}


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


async def get_document_structured_extraction(
    doc_id: int, *, schema_name: str | None = None
) -> dict | None:
    """Fetch a document's latest structured payload and any normalized inspection rows.

    Pass ``schema_name`` to select a particular parser's extraction. For mortgage inspections, fields the normalized
    layout derives (the original texts of shares, rights and qualities, unit count, interest rate text) are rebuilt
    from the typed columns.
    """
    async with _get_session_factory()() as session:
        statement = select(DocumentExtraction).where(DocumentExtraction.document_id == doc_id)
        if schema_name:
            statement = statement.where(DocumentExtraction.schema_name == schema_name)
        extraction = (
            await session.execute(
                statement.order_by(DocumentExtraction.created_at.desc(), DocumentExtraction.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if extraction is None:
            return None
        extraction_payload = await session.get(DocumentExtractionPayload, extraction.id)
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
            # The inspection template includes the original Ocular JSON. Keep
            # the archived payload alongside the normalized extraction data.
            "structured_data": extraction_payload.structured_data if extraction_payload else {},
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


async def get_all_documents(limit: int | None = 100, offset: int = 0) -> list[dict]:
    """Fetch document metadata for the browse page without transferring archived XML content."""
    stmt = _document_select(include_content=False).order_by(Document.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [_document_record(doc, unit, comune, request_ref=ref) for doc, unit, comune, ref in rows]


async def iter_all_documents(batch_size: int = 500) -> AsyncIterator[dict]:
    """Stream all document metadata in bounded DB batches, without archived XML content."""
    stmt = _document_select(include_content=False).order_by(Document.created_at.desc())
    stmt = stmt.execution_options(yield_per=max(1, int(batch_size)))
    async with _get_session_factory()() as session:
        result = await session.stream(stmt)
        try:
            async for doc, unit, comune, ref in result:
                yield _document_record(doc, unit, comune, request_ref=ref)
        finally:
            await result.close()


async def list_document_options() -> list[dict]:
    """Return only the labels and IDs needed by admin document selectors."""
    stmt = select(Document.id, Document.file_name, Document.subject_text).order_by(Document.id.desc())
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {"id": row.id, "filename": row.file_name, "title": row.subject_text or row.file_name}
        for row in rows
    ]


async def list_admin_documents(query: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Search indexed documents for the admin catalog, including their current owner counts."""
    search = str(query or "").strip()
    predicates = []
    if search:
        pattern = f"%{search}%"
        predicates.append(
            or_(
                cast(Document.id, String).ilike(pattern),
                Document.file_name.ilike(pattern),
                Document.subject_text.ilike(pattern),
                Document.document_type.ilike(pattern),
                Request.request_ref.ilike(pattern),
            )
        )

    owner_counts = (
        select(
            DocumentPurchase.document_id.label("document_id"),
            func.count(DocumentPurchase.user_id).label("owner_count"),
        )
        .group_by(DocumentPurchase.document_id)
        .subquery()
    )
    stmt = (
        select(
            Document,
            CadastralUnit,
            IstatMunicipality,
            Request.request_ref,
            func.coalesce(owner_counts.c.owner_count, 0).label("owner_count"),
        )
        .select_from(Document)
        .outerjoin(CadastralUnit, Document.cadastral_unit_id == CadastralUnit.id)
        .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
        .outerjoin(Request, Document.request_id == Request.id)
        .outerjoin(owner_counts, owner_counts.c.document_id == Document.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    count_stmt = select(func.count(distinct(Document.id))).select_from(Document).outerjoin(
        Request, Document.request_id == Request.id
    )
    if predicates:
        stmt = stmt.where(*predicates)
        count_stmt = count_stmt.where(*predicates)
    stmt = stmt.limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
        total = int((await session.execute(count_stmt)).scalar_one())

    documents = []
    for doc, unit, comune, ref, owner_count in rows:
        record = _document_record(doc, unit, comune, request_ref=ref)
        record["owner_count"] = int(owner_count or 0)
        documents.append(record)
    return documents, total


async def get_document_favorite_ids(user_id: str) -> set[int]:
    """Return the document IDs favorited by one authenticated user."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(DocumentFavorite.document_id).where(DocumentFavorite.user_id == user_id)
        )
        return {int(document_id) for document_id in result.scalars()}


class PurchaseError(ValueError):
    """A purchase operation was refused; ``code`` is a stable key the UI turns into a message."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _purchase_user_keys(user_ids: "str | Iterable[str]") -> list[str]:
    """Normalise one key or several principal keys to the case-insensitive form stored/compared."""
    values = [user_ids] if isinstance(user_ids, str) else list(user_ids)
    return sorted({str(value).strip().casefold() for value in values if str(value or "").strip()})


def _identity_part(user_id: str) -> str:
    """The identifier after the key prefix: ``email:a@b.it`` and ``id:A@B.it`` name the same person."""
    return str(user_id).partition(":")[2].strip().casefold()


def _same_identity_clause(user_id: str):
    return func.lower(func.substr(DocumentPurchase.user_id, func.strpos(DocumentPurchase.user_id, ":") + 1)) == _identity_part(user_id)


def _active_purchase_clause():
    """A purchase gives access while it is not revoked, already started and not expired."""
    return and_(
        DocumentPurchase.revoked_at.is_(None),
        DocumentPurchase.purchased_at <= func.now(),
        or_(DocumentPurchase.expires_at.is_(None), DocumentPurchase.expires_at > func.now()),
    )


async def get_document_purchase_ids(user_ids: "str | Iterable[str]") -> set[int]:
    """Return the indexed documents an identity (any of its key forms) currently has access to."""
    keys = _purchase_user_keys(user_ids)
    if not keys:
        return set()
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(DocumentPurchase.document_id).where(
                func.lower(DocumentPurchase.user_id).in_(keys), _active_purchase_clause()
            )
        )
        return {int(document_id) for document_id in result.scalars()}


async def has_document_purchase(user_ids: "str | Iterable[str]", document_id: int) -> bool:
    """Check whether an identity (any of its key forms) has current access to an indexed document."""
    keys = _purchase_user_keys(user_ids)
    if not keys:
        return False
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(DocumentPurchase.document_id).where(
                func.lower(DocumentPurchase.user_id).in_(keys),
                DocumentPurchase.document_id == document_id,
                _active_purchase_clause(),
            )
        )
        return result.first() is not None


def _purchase_state(purchase: DocumentPurchase, now: datetime) -> str:
    if purchase.revoked_at is not None:
        return "revoked"
    if purchase.expires_at is not None and purchase.expires_at <= now:
        return "expired"
    if purchase.purchased_at > now:
        return "scheduled"
    return "active"


async def list_document_purchases(query: str = "", limit: int = 500, status: str = "active") -> list[dict]:
    """List purchase records for the admin ownership manager (``status``: active, revoked or all)."""
    stmt = (
        select(DocumentPurchase, Document.file_name, Document.subject_text, Document.document_type)
        .join(Document, Document.id == DocumentPurchase.document_id)
        .order_by(DocumentPurchase.updated_at.desc(), DocumentPurchase.document_id.desc())
        .limit(max(1, min(int(limit), 1000)))
    )
    if status == "active":
        stmt = stmt.where(DocumentPurchase.revoked_at.is_(None))
    elif status == "revoked":
        stmt = stmt.where(DocumentPurchase.revoked_at.is_not(None))
    search = str(query or "").strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                DocumentPurchase.user_id.ilike(pattern),
                cast(DocumentPurchase.document_id, String).ilike(pattern),
                Document.file_name.ilike(pattern),
                Document.subject_text.ilike(pattern),
            )
        )
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "user_id": purchase.user_id,
            "document_id": int(purchase.document_id),
            "purchased_at": purchase.purchased_at,
            "created_by": purchase.created_by,
            "note": purchase.note or "",
            "updated_at": purchase.updated_at,
            "source": purchase.source or "admin_grant",
            "amount_cents": purchase.amount_cents,
            "currency": purchase.currency or "",
            "payment_ref": purchase.payment_ref or "",
            "expires_at": purchase.expires_at,
            "revoked_at": purchase.revoked_at,
            "revoked_by": purchase.revoked_by or "",
            "revoked_reason": purchase.revoked_reason or "",
            "state": _purchase_state(purchase, now),
            "filename": filename,
            "title": title or filename,
            "document_type": document_type or "",
        }
        for purchase, filename, title, document_type in rows
    ]


async def create_document_purchase(
    user_id: str,
    document_id: int,
    purchased_at: datetime,
    created_by: str,
    note: str | None = None,
    *,
    source: str = "admin_grant",
    amount_cents: int | None = None,
    currency: str | None = None,
    payment_ref: str | None = None,
    wallet_tx_id: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Record a purchase or grant; an already active one is refused, a revoked one is re-activated."""
    async with _get_session_factory()() as session:
        # Serialize equivalent principal keys for this document and duplicate
        # payment references before checking state. The INSERT below still
        # uses ON CONFLICT as the database-level guard for the primary key.
        lock_keys = [f"purchase:{document_id}:{_identity_part(user_id)}"]
        if payment_ref:
            lock_keys.append(f"payment:{payment_ref}")
        for lock_key in sorted(lock_keys):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )

        if await session.get(Document, document_id) is None:
            raise PurchaseError("document_not_found")
        if payment_ref:
            clash = await session.execute(
                select(DocumentPurchase.user_id).where(DocumentPurchase.payment_ref == payment_ref)
            )
            if clash.first() is not None:  # the same Stripe/wallet payment must never fulfil twice
                raise PurchaseError("duplicate_payment")
        purchase = await session.get(DocumentPurchase, (user_id, document_id))
        if purchase is not None and purchase.revoked_at is None:
            raise PurchaseError("duplicate")
        # The same person typed under another key form (email: / username: / id:) already has this document.
        same_person = await session.execute(
            select(DocumentPurchase.user_id).where(
                DocumentPurchase.document_id == document_id,
                DocumentPurchase.revoked_at.is_(None),
                DocumentPurchase.user_id != user_id,
                _same_identity_clause(user_id),
            )
        )
        if same_person.first() is not None:
            raise PurchaseError("duplicate")
        purchase_values = {
            "purchased_at": purchased_at,
            "created_by": created_by,
            "note": note or None,
            "source": source or "admin_grant",
            "amount_cents": amount_cents,
            "currency": (currency or "").upper() or None,
            "payment_ref": payment_ref or None,
            "wallet_tx_id": wallet_tx_id or None,
            "expires_at": expires_at,
            "revoked_at": None,
            "revoked_by": None,
            "revoked_reason": None,
            "updated_at": datetime.now(timezone.utc),
        }
        if purchase is None:
            inserted_user_id = (
                await session.execute(
                    pg_insert(DocumentPurchase)
                    .values(user_id=user_id, document_id=document_id, **purchase_values)
                    .on_conflict_do_nothing()
                    .returning(DocumentPurchase.user_id)
                )
            ).scalar_one_or_none()
            if inserted_user_id is None:
                if payment_ref:
                    duplicate_payment = await session.execute(
                        select(DocumentPurchase.user_id).where(DocumentPurchase.payment_ref == payment_ref)
                    )
                    if duplicate_payment.first() is not None:
                        raise PurchaseError("duplicate_payment")
                raise PurchaseError("duplicate")
            await session.commit()
            return

        for field, value in purchase_values.items():
            setattr(purchase, field, value)
        await session.commit()


async def update_document_purchase(
    old_user_id: str,
    old_document_id: int,
    user_id: str,
    document_id: int,
    purchased_at: datetime | None,
    note: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Edit a purchase record, including moving it to another identity/document pair.

    ``purchased_at=None`` keeps the stored instant (the admin form posts minute precision, so an unchanged
    value must not silently rewrite the seconds).
    """
    async with _get_session_factory()() as session:
        purchase = await session.get(DocumentPurchase, (old_user_id, old_document_id))
        if purchase is None:
            raise PurchaseError("not_found")
        if await session.get(Document, document_id) is None:
            raise PurchaseError("document_not_found")
        if (user_id, document_id) != (old_user_id, old_document_id):
            if await session.get(DocumentPurchase, (user_id, document_id)) is not None:
                raise PurchaseError("duplicate")
            same_person = await session.execute(
                select(DocumentPurchase.user_id).where(
                    DocumentPurchase.document_id == document_id,
                    DocumentPurchase.revoked_at.is_(None),
                    DocumentPurchase.user_id != old_user_id,
                    _same_identity_clause(user_id),
                )
            )
            if same_person.first() is not None:
                raise PurchaseError("duplicate")
            purchase.user_id = user_id
            purchase.document_id = document_id
        if purchased_at is not None:
            purchase.purchased_at = purchased_at
        purchase.note = note or None
        purchase.expires_at = expires_at
        purchase.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def revoke_document_purchase(user_id: str, document_id: int, revoked_by: str, reason: str) -> bool:
    """End an access without deleting its history; return whether an active record was revoked."""
    async with _get_session_factory()() as session:
        purchase = await session.get(DocumentPurchase, (user_id, document_id))
        if purchase is None or purchase.revoked_at is not None:
            return False
        now = datetime.now(timezone.utc)
        purchase.revoked_at = now
        purchase.revoked_by = revoked_by
        purchase.revoked_reason = reason or None
        purchase.updated_at = now
        await session.commit()
        return True


async def delete_document_purchase(user_id: str, document_id: int) -> bool:
    """Physically remove a purchase record (maintenance/tests); the UI revokes softly instead."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            delete(DocumentPurchase).where(
                DocumentPurchase.user_id == user_id,
                DocumentPurchase.document_id == document_id,
            )
        )
        await session.commit()
        return bool(result.rowcount)


async def set_document_favorite(user_id: str, document_id: int, favorite: bool) -> None:
    """Idempotently add or remove one user's favorite document."""
    async with _get_session_factory()() as session:
        if favorite:
            await session.execute(
                text(
                    "INSERT INTO document_favorite (user_id, document_id, created_at) "
                    "VALUES (:user_id, :document_id, :created_at) ON CONFLICT (user_id, document_id) DO NOTHING"
                ),
                {
                    "user_id": user_id,
                    "document_id": document_id,
                    "created_at": datetime.now(timezone.utc),
                },
            )
        else:
            await session.execute(
                text("DELETE FROM document_favorite WHERE user_id = :user_id AND document_id = :document_id"),
                {"user_id": user_id, "document_id": document_id},
            )
        await session.commit()


async def has_subject_access(user_ids: "str | Iterable[str]", subject_type: str, subject_code: str) -> bool:
    """Match an active, explicit subject-search grant against the signed-in principal's account keys."""
    keys = _purchase_user_keys(user_ids)
    if not keys:
        return False
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectAccess.subject_code).where(
                func.lower(SubjectAccess.user_id).in_(keys),
                SubjectAccess.subject_type == subject_type,
                SubjectAccess.subject_code == subject_code.strip().upper(),
                SubjectAccess.granted_at <= func.now(),
                SubjectAccess.revoked_at.is_(None),
            ).limit(1)
        )
        return result.first() is not None


async def get_subject_access_codes(user_ids: "str | Iterable[str]", subject_type: str) -> set[str]:
    """Codes whose archived person or company searches belong to this active user principal."""
    keys = _purchase_user_keys(user_ids)
    if not keys:
        return set()
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectAccess.subject_code).where(
                func.lower(SubjectAccess.user_id).in_(keys),
                SubjectAccess.subject_type == subject_type,
                SubjectAccess.granted_at <= func.now(),
                SubjectAccess.revoked_at.is_(None),
            )
        )
        return {str(code) for code in result.scalars()}


async def grant_subject_access(
    user_id: str, subject_type: str, subject_code: str, created_by: str, note: str | None = None
) -> bool:
    """Grant a saved search without changing document ownership; preserve existing active grants."""
    code = subject_code.strip().upper()
    pattern = r"\d{11}" if subject_type == "company" else r"[A-Z0-9]{16}"
    keys = _purchase_user_keys(user_id)
    if subject_type not in {"company", "person"} or not re.fullmatch(pattern, code) or not keys:
        raise ValueError("Invalid subject access grant")
    values = {
        "granted_at": datetime.now(timezone.utc),
        "created_by": created_by,
        "note": note or None,
        "revoked_at": None,
        "revoked_by": None,
        "revoked_reason": None,
    }
    async with _get_session_factory()() as session:
        result = await session.execute(
            pg_insert(SubjectAccess)
            .values(user_id=keys[0], subject_type=subject_type, subject_code=code, **values)
            .on_conflict_do_update(
                index_elements=[SubjectAccess.user_id, SubjectAccess.subject_type, SubjectAccess.subject_code],
                set_=values,
                where=SubjectAccess.revoked_at.is_not(None),
            )
            .returning(SubjectAccess.subject_code)
        )
        changed = result.scalar_one_or_none() is not None
        await session.commit()
        return changed


async def complete_subject_purchase(
    user_id: str,
    subject_type: str,
    subject_code: str,
    *,
    source: str,
    payment_ref: str,
    created_by: str,
    amount: Decimal | None = None,
    currency: str | None = None,
    wallet_tx_id: str | None = None,
    note: str | None = None,
) -> str:
    """Record one paid entity purchase and grant access atomically; payment refs are replay-safe."""
    keys = _purchase_user_keys(user_id)
    code = str(subject_code or "").strip().upper()
    pattern = r"\d{11}" if subject_type == "company" else r"[A-Z0-9]{16}"
    payment_key = str(payment_ref or "").strip()
    source_value = str(source or "").strip().lower()
    if (
        not keys
        or subject_type not in {"person", "company"}
        or not re.fullmatch(pattern, code)
        or source_value not in {"stripe", "wallet"}
        or not payment_key
    ):
        raise PurchaseError("bad_subject_purchase")
    values = {
        "user_id": keys[0],
        "subject_type": subject_type,
        "subject_code": code,
        "source": source_value,
        "payment_ref": payment_key,
        "created_by": str(created_by or source_value)[:320],
        "amount": amount,
        "currency": (str(currency or "").upper() or None),
        "wallet_tx_id": wallet_tx_id,
        "status": "completed",
        "note": str(note or "")[:2000] or None,
    }
    async with _get_session_factory()() as session:
        lock_keys = [f"subject:{subject_type}:{code}:{_identity_part(keys[0])}", f"subject-payment:{payment_key}"]
        for lock_key in sorted(lock_keys):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
        existing = await session.execute(
            select(SubjectPurchase.id).where(SubjectPurchase.payment_ref == payment_key).limit(1)
        )
        if existing.first() is not None:
            return "replay"
        owned = await session.execute(
            select(SubjectAccess.user_id).where(
                func.lower(SubjectAccess.user_id).in_(keys),
                SubjectAccess.subject_type == subject_type,
                SubjectAccess.subject_code == code,
                SubjectAccess.granted_at <= func.now(),
                SubjectAccess.revoked_at.is_(None),
            ).limit(1)
        )
        if owned.first() is not None:
            values["status"] = "refund_required"
            await session.execute(pg_insert(SubjectPurchase).values(**values))
            await session.commit()
            return "already_owned"
        await session.execute(pg_insert(SubjectPurchase).values(**values))
        grant_values = {
            "granted_at": datetime.now(timezone.utc),
            "created_by": source_value,
            "note": note or f"Pagamento {payment_key}",
            "revoked_at": None,
            "revoked_by": None,
            "revoked_reason": None,
        }
        await session.execute(
            pg_insert(SubjectAccess)
            .values(user_id=keys[0], subject_type=subject_type, subject_code=code, **grant_values)
            .on_conflict_do_update(
                index_elements=[SubjectAccess.user_id, SubjectAccess.subject_type, SubjectAccess.subject_code],
                set_=grant_values,
                where=SubjectAccess.revoked_at.is_not(None),
            )
        )
        await session.commit()
        return "created"


async def list_subject_purchases(query: str = "", *, limit: int = 500) -> list[dict[str, Any]]:
    """Return recent paid subject purchases and refund follow-ups for the admin audit view."""
    stmt = select(SubjectPurchase).order_by(SubjectPurchase.purchased_at.desc(), SubjectPurchase.id.desc())
    search = str(query or "").strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            SubjectPurchase.user_id.ilike(pattern)
            | SubjectPurchase.subject_code.ilike(pattern)
            | SubjectPurchase.payment_ref.ilike(pattern)
        )
    stmt = stmt.limit(max(1, min(int(limit), 1000)))
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "subject_type": row.subject_type,
            "subject_code": row.subject_code,
            "purchased_at": row.purchased_at,
            "source": row.source,
            "amount": row.amount,
            "currency": row.currency or "",
            "payment_ref": row.payment_ref or "",
            "status": row.status,
            "note": row.note or "",
        }
        for row in rows
    ]


async def mark_subject_purchase_refunded(payment_ref: str) -> bool:
    """Mark a purchase row refunded after the wallet credit reversal succeeds."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            update(SubjectPurchase)
            .where(SubjectPurchase.payment_ref == str(payment_ref or "").strip())
            .values(status="refunded")
        )
        await session.commit()
        return bool(result.rowcount)


async def create_wallet_gift_coupon(
    code_hash: str,
    code_hint: str,
    amount: Decimal,
    max_redemptions: int,
    created_by: str,
    *,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> int:
    """Persist a wallet gift coupon without retaining its redeemable plaintext code."""
    coupon = WalletGiftCoupon(
        code_hash=code_hash,
        code_hint=code_hint,
        amount=amount,
        max_redemptions=max_redemptions,
        redeemed_count=0,
        status="active",
        created_by=str(created_by or "admin")[:320],
        expires_at=expires_at,
        note=str(note or "")[:500] or None,
    )
    async with _get_session_factory()() as session:
        session.add(coupon)
        await session.commit()
        await session.refresh(coupon)
    return int(coupon.id)


async def count_wallet_gift_coupons() -> int:
    async with _get_session_factory()() as session:
        result = await session.execute(select(func.count(WalletGiftCoupon.id)))
        return int(result.scalar_one() or 0)


async def list_wallet_gift_coupons(*, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(WalletGiftCoupon).order_by(WalletGiftCoupon.created_at.desc(), WalletGiftCoupon.id.desc())
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    stmt = stmt.offset(max(0, int(offset)))
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": row.id,
            "code_hint": row.code_hint,
            "amount": row.amount,
            "max_redemptions": row.max_redemptions,
            "redeemed_count": row.redeemed_count,
            "status": row.status,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "note": row.note or "",
        }
        for row in rows
    ]


async def set_wallet_gift_coupon_status(coupon_id: int, status: str) -> bool:
    if status not in {"active", "frozen", "revoked"}:
        raise ValueError("invalid_gift_coupon_status")
    async with _get_session_factory()() as session:
        result = await session.execute(
            update(WalletGiftCoupon).where(WalletGiftCoupon.id == int(coupon_id)).values(status=status)
        )
        await session.commit()
        return bool(result.rowcount)


async def reserve_wallet_gift_coupon_redemption(code_hash: str, user_id: str) -> dict[str, Any]:
    """Reserve one code redemption under an advisory lock; repeated submissions resume safely."""
    user_key = str(user_id or "").strip().casefold()
    if not code_hash or not user_key:
        return {"status": "invalid"}
    async with _get_session_factory()() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"wallet-gift-coupon:{code_hash}"},
        )
        coupon = (
            await session.execute(select(WalletGiftCoupon).where(WalletGiftCoupon.code_hash == code_hash).limit(1))
        ).scalar_one_or_none()
        if coupon is None:
            return {"status": "invalid"}
        existing = (
            await session.execute(
                select(WalletGiftCouponRedemption).where(
                    WalletGiftCouponRedemption.coupon_id == coupon.id,
                    func.lower(WalletGiftCouponRedemption.user_id) == user_key,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status == "pending":
                return {"status": "pending", "redemption_id": existing.id, "amount": existing.amount}
            return {"status": "already_redeemed"}
        if coupon.status != "active":
            return {"status": "frozen" if coupon.status == "frozen" else "invalid"}
        if coupon.expires_at is not None and coupon.expires_at <= datetime.now(timezone.utc):
            return {"status": "expired"}
        if coupon.redeemed_count >= coupon.max_redemptions:
            return {"status": "exhausted"}
        redemption = WalletGiftCouponRedemption(
            coupon_id=coupon.id,
            user_id=user_key,
            amount=coupon.amount,
            status="pending",
            note="Gift coupon redemption",
        )
        coupon.redeemed_count += 1
        session.add(redemption)
        await session.commit()
        await session.refresh(redemption)
        return {"status": "pending", "redemption_id": redemption.id, "amount": redemption.amount}


async def complete_wallet_gift_coupon_redemption(redemption_id: int, wallet_tx_id: str) -> bool:
    async with _get_session_factory()() as session:
        result = await session.execute(
            update(WalletGiftCouponRedemption)
            .where(
                WalletGiftCouponRedemption.id == int(redemption_id),
                WalletGiftCouponRedemption.status == "pending",
            )
            .values(status="completed", wallet_tx_id=str(wallet_tx_id), completed_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return bool(result.rowcount)


async def get_wallet_gift_coupon_redemption(redemption_id: int) -> dict[str, Any] | None:
    stmt = (
        select(WalletGiftCouponRedemption, WalletGiftCoupon)
        .join(WalletGiftCoupon, WalletGiftCoupon.id == WalletGiftCouponRedemption.coupon_id)
        .where(WalletGiftCouponRedemption.id == int(redemption_id))
        .limit(1)
    )
    async with _get_session_factory()() as session:
        row = (await session.execute(stmt)).first()
    if row is None:
        return None
    redemption, coupon = row
    return {
        "id": redemption.id,
        "coupon_id": coupon.id,
        "code_hint": coupon.code_hint,
        "user_id": redemption.user_id,
        "amount": redemption.amount,
        "status": redemption.status,
        "wallet_tx_id": redemption.wallet_tx_id,
        "reversed_tx_id": redemption.reversed_tx_id,
        "redeemed_at": redemption.redeemed_at,
    }


def _wallet_gift_redemption_search_filter(query: str):
    search = str(query or "").strip()
    if search:
        pattern = f"%{search}%"
        wallet_pattern = f"%sister:{_identity_part(search)}%"
        return (
            WalletGiftCouponRedemption.user_id.ilike(pattern)
            | WalletGiftCouponRedemption.user_id.ilike(wallet_pattern)
            | WalletGiftCoupon.code_hint.ilike(pattern)
        )
    return None


async def count_wallet_gift_coupon_redemptions(*, query: str = "") -> int:
    stmt = select(func.count(WalletGiftCouponRedemption.id)).join(
        WalletGiftCoupon, WalletGiftCoupon.id == WalletGiftCouponRedemption.coupon_id
    )
    search_filter = _wallet_gift_redemption_search_filter(query)
    if search_filter is not None:
        stmt = stmt.where(search_filter)
    async with _get_session_factory()() as session:
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


async def list_wallet_gift_coupon_redemptions(
    *, query: str = "", limit: int = 300, offset: int = 0
) -> list[dict[str, Any]]:
    stmt = (
        select(WalletGiftCouponRedemption, WalletGiftCoupon)
        .join(WalletGiftCoupon, WalletGiftCoupon.id == WalletGiftCouponRedemption.coupon_id)
        .order_by(WalletGiftCouponRedemption.redeemed_at.desc(), WalletGiftCouponRedemption.id.desc())
    )
    search_filter = _wallet_gift_redemption_search_filter(query)
    if search_filter is not None:
        stmt = stmt.where(search_filter)
    stmt = stmt.limit(max(1, min(int(limit), 1000)))
    stmt = stmt.offset(max(0, int(offset)))
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {
            "id": redemption.id,
            "coupon_id": coupon.id,
            "code_hint": coupon.code_hint,
            "user_id": redemption.user_id,
            "amount": redemption.amount,
            "status": redemption.status,
            "wallet_tx_id": redemption.wallet_tx_id or "",
            "reversed_tx_id": redemption.reversed_tx_id or "",
            "redeemed_at": redemption.redeemed_at,
        }
        for redemption, coupon in rows
    ]


async def mark_wallet_gift_coupon_redemption_reversed(redemption_id: int, reversed_tx_id: str) -> bool:
    async with _get_session_factory()() as session:
        result = await session.execute(
            update(WalletGiftCouponRedemption)
            .where(
                WalletGiftCouponRedemption.id == int(redemption_id),
                WalletGiftCouponRedemption.status.in_(["completed", "reversal_required"]),
            )
            .values(status="reversed", reversed_tx_id=str(reversed_tx_id))
        )
        await session.commit()
        return bool(result.rowcount)


async def list_subject_access(query: str = "", *, status: str = "active", limit: int = 500) -> list[dict[str, Any]]:
    """List subject grants for the admin manager without returning document or credential data."""
    stmt = select(SubjectAccess).order_by(SubjectAccess.granted_at.desc(), SubjectAccess.subject_code)
    if status == "active":
        stmt = stmt.where(SubjectAccess.revoked_at.is_(None), SubjectAccess.granted_at <= func.now())
    elif status == "revoked":
        stmt = stmt.where(SubjectAccess.revoked_at.is_not(None))
    search = str(query or "").strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(SubjectAccess.user_id.ilike(pattern) | SubjectAccess.subject_code.ilike(pattern))
    stmt = stmt.limit(max(1, min(int(limit), 1000)))
    async with _get_session_factory()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "user_id": row.user_id,
            "subject_type": row.subject_type,
            "subject_code": row.subject_code,
            "granted_at": row.granted_at,
            "created_by": row.created_by,
            "note": row.note or "",
            "revoked_at": row.revoked_at,
            "revoked_by": row.revoked_by or "",
            "revoked_reason": row.revoked_reason or "",
        }
        for row in rows
    ]


async def revoke_subject_access(
    user_id: str, subject_type: str, subject_code: str, revoked_by: str, reason: str | None = None
) -> bool:
    """Soft-revoke one subject grant while retaining its history."""
    keys = _purchase_user_keys(user_id)
    code = str(subject_code or "").strip().upper()
    if subject_type not in {"person", "company"} or not code or not keys:
        raise ValueError("Invalid subject access revocation")
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        result = await session.execute(
            update(SubjectAccess)
            .where(
                func.lower(SubjectAccess.user_id).in_(keys),
                SubjectAccess.subject_type == subject_type,
                SubjectAccess.subject_code == code,
                SubjectAccess.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_by=revoked_by, revoked_reason=reason or None)
        )
        await session.commit()
        return bool(result.rowcount)


async def get_subject_favorite_codes(user_id: str, subject_type: str) -> set[str]:
    """Return the profile codes of one kind saved by an authenticated user."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectFavorite.subject_code).where(
                SubjectFavorite.user_id == user_id,
                SubjectFavorite.subject_type == subject_type,
            )
        )
        return {str(code) for code in result.scalars()}


async def is_subject_favorite(user_id: str, subject_type: str, subject_code: str) -> bool:
    """Check whether one person or company profile is saved by a user."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectFavorite.subject_code).where(
                SubjectFavorite.user_id == user_id,
                SubjectFavorite.subject_type == subject_type,
                SubjectFavorite.subject_code == subject_code,
            )
        )
        return result.scalar_one_or_none() is not None


async def set_subject_favorite(user_id: str, subject_type: str, subject_code: str, favorite: bool) -> None:
    """Idempotently add or remove a user's person or company profile favorite."""
    async with _get_session_factory()() as session:
        if favorite:
            await session.execute(
                text(
                    "INSERT INTO subject_favorite (user_id, subject_type, subject_code, created_at) "
                    "VALUES (:user_id, :subject_type, :subject_code, :created_at) "
                    "ON CONFLICT (user_id, subject_type, subject_code) DO NOTHING"
                ),
                {
                    "user_id": user_id,
                    "subject_type": subject_type,
                    "subject_code": subject_code,
                    "created_at": datetime.now(timezone.utc),
                },
            )
        else:
            await session.execute(
                text(
                    "DELETE FROM subject_favorite WHERE user_id = :user_id "
                    "AND subject_type = :subject_type AND subject_code = :subject_code"
                ),
                {"user_id": user_id, "subject_type": subject_type, "subject_code": subject_code},
            )
        await session.commit()


# --- tags: shared labels that group persons and companies -----------------------------------------------------------

SUBJECT_TAG_MAX_LENGTH = 40
SUBJECT_TAGS_PER_SUBJECT = 20


def normalize_tag(value: Any) -> tuple[str, str]:
    """``(key, label)`` of a tag as typed: whitespace squeezed, at most 40 characters, case-insensitive identity.

    Raises ``ValueError`` for an empty or too long tag, one with control characters, or one without a letter/digit
    (``-`` is reserved for "no tag" in the table filter).
    """
    label = " ".join(str(value or "").split())
    if not label:
        raise ValueError("il tag è vuoto")
    if len(label) > SUBJECT_TAG_MAX_LENGTH:
        raise ValueError(f"il tag può avere al massimo {SUBJECT_TAG_MAX_LENGTH} caratteri")
    if any(unicodedata.category(ch).startswith("C") for ch in label):
        raise ValueError("il tag contiene caratteri non validi")
    if not any(ch.isalnum() for ch in label):
        raise ValueError("il tag deve contenere almeno una lettera o un numero")
    return label.casefold()[:SUBJECT_TAG_MAX_LENGTH], label


def _subject_tag_kind(kind: str) -> str:
    if kind not in ("person", "company"):
        raise ValueError(f"kind must be 'person' or 'company', not {kind!r}")
    return kind


async def get_subject_tags(kind: str, code: str) -> list[str]:
    """The tags of one person or company, in alphabetical order."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectTag.tag)
            .where(SubjectTag.subject_type == _subject_tag_kind(kind), SubjectTag.subject_code == code)
            .order_by(SubjectTag.tag_key)
        )
        return [str(tag) for tag in result.scalars()]


async def set_subject_tags(kind: str, code: str, tags: Iterable[Any], user: Optional[str] = None) -> list[str]:
    """Replace the tags of one subject with ``tags`` (case-insensitive duplicates collapse, the first spelling wins).

    A tag that is already on the subject keeps its stored spelling and author. Returns the tags now stored.
    """
    kind = _subject_tag_kind(kind)
    wanted: dict[str, str] = {}
    for value in tags:
        key, label = normalize_tag(value)
        wanted.setdefault(key, label)
    if len(wanted) > SUBJECT_TAGS_PER_SUBJECT:
        raise ValueError(f"al massimo {SUBJECT_TAGS_PER_SUBJECT} tag per profilo")
    async with _get_session_factory()() as session:
        await session.execute(
            text(
                "DELETE FROM subject_tag WHERE subject_type = :kind AND subject_code = :code "
                "AND NOT (tag_key = ANY(CAST(:keys AS text[])))"
            ),
            {"kind": kind, "code": code, "keys": sorted(wanted)},
        )
        for key, label in wanted.items():
            await session.execute(
                text(
                    "INSERT INTO subject_tag (subject_type, subject_code, tag_key, tag, created_by, created_at) "
                    "VALUES (:kind, :code, :key, :tag, :user, :now) ON CONFLICT DO NOTHING"
                ),
                {"kind": kind, "code": code, "key": key, "tag": label, "user": user, "now": datetime.now(timezone.utc)},
            )
        await session.commit()
    return await get_subject_tags(kind, code)


async def tag_subjects(kind: str, codes: Iterable[str], tag: str, user: Optional[str] = None) -> int:
    """Add ``tag`` to every code (those that already have it are left alone); returns how many were newly tagged."""
    kind = _subject_tag_kind(kind)
    key, label = normalize_tag(tag)
    codes = sorted({str(c).strip().upper() for c in codes if str(c).strip()})
    if not codes:
        return 0
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO subject_tag (subject_type, subject_code, tag_key, tag, created_by, created_at) "
                "SELECT :kind, c, :key, :tag, :user, :now FROM unnest(CAST(:codes AS text[])) AS c "
                "ON CONFLICT DO NOTHING"
            ),
            {"kind": kind, "key": key, "tag": label, "user": user, "now": datetime.now(timezone.utc), "codes": codes},
        )
        await session.commit()
        return int(result.rowcount or 0)


async def untag_subjects(kind: str, codes: Iterable[str], tag: str) -> int:
    """Remove ``tag`` from every code that has it; returns how many lost it."""
    kind = _subject_tag_kind(kind)
    key, _label = normalize_tag(tag)
    codes = sorted({str(c).strip().upper() for c in codes if str(c).strip()})
    if not codes:
        return 0
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(
                "DELETE FROM subject_tag WHERE subject_type = :kind AND tag_key = :key "
                "AND subject_code = ANY(CAST(:codes AS text[]))"
            ),
            {"kind": kind, "key": key, "codes": codes},
        )
        await session.commit()
        return int(result.rowcount or 0)


async def list_subject_tags(kind: str, allowed_codes: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Tags in use for one kind, optionally scoped to records owned by one account."""
    kind = _subject_tag_kind(kind)
    codes = None if allowed_codes is None else sorted({str(code).strip().upper() for code in allowed_codes})
    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT (array_agg(tag ORDER BY created_at))[1] AS tag, count(*) AS subjects FROM subject_tag "
                    "WHERE subject_type = :kind AND (CAST(:codes AS text[]) IS NULL "
                    "OR subject_code = ANY(CAST(:codes AS text[]))) GROUP BY tag_key ORDER BY count(*) DESC, tag_key"
                ),
                {"kind": kind, "codes": codes},
            )
        ).all()
    return [{"tag": str(tag), "count": int(count)} for tag, count in rows]


async def get_subject_note(user_id: str, subject_type: str, subject_code: str) -> str | None:
    """Return the signed-in user's private note for one person or company profile."""
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(SubjectNote.note).where(
                SubjectNote.user_id == user_id,
                SubjectNote.subject_type == subject_type,
                SubjectNote.subject_code == subject_code,
            )
        )
        return result.scalar_one_or_none()


async def set_subject_note(user_id: str, subject_type: str, subject_code: str, note: str) -> None:
    """Save or clear the signed-in user's private note for one subject profile."""
    normalized_note = note.strip()
    async with _get_session_factory()() as session:
        if normalized_note:
            await session.execute(
                text(
                    "INSERT INTO subject_note "
                    "(user_id, subject_type, subject_code, note, created_at, updated_at) "
                    "VALUES (:user_id, :subject_type, :subject_code, :note, now(), now()) "
                    "ON CONFLICT (user_id, subject_type, subject_code) DO UPDATE "
                    "SET note = EXCLUDED.note, updated_at = now()"
                ),
                {
                    "user_id": user_id,
                    "subject_type": subject_type,
                    "subject_code": subject_code,
                    "note": normalized_note,
                },
            )
        else:
            await session.execute(
                text(
                    "DELETE FROM subject_note WHERE user_id = :user_id "
                    "AND subject_type = :subject_type AND subject_code = :subject_code"
                ),
                {"user_id": user_id, "subject_type": subject_type, "subject_code": subject_code},
            )
        await session.commit()


async def get_calculation_snapshot(
    *,
    scope_type: str,
    scope_id: str,
    viewer_key: str,
    calculation_type: str,
    input_hash: str,
    algorithm_version: str,
    max_age_seconds: int,
) -> dict[str, Any] | None:
    """Return a fresh persisted result only when its source fingerprint and algorithm still match."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(max_age_seconds)))
    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                select(CalculationSnapshot).where(
                    CalculationSnapshot.scope_type == scope_type,
                    CalculationSnapshot.scope_id == scope_id,
                    CalculationSnapshot.viewer_key == viewer_key,
                    CalculationSnapshot.calculation_type == calculation_type,
                    CalculationSnapshot.input_hash == input_hash,
                    CalculationSnapshot.algorithm_version == algorithm_version,
                    CalculationSnapshot.calculated_at >= cutoff,
                )
            )
        ).scalar_one_or_none()
    return row.result if row and isinstance(row.result, dict) else None


async def set_calculation_snapshot(
    *,
    scope_type: str,
    scope_id: str,
    viewer_key: str,
    calculation_type: str,
    input_hash: str,
    algorithm_version: str,
    result: dict[str, Any],
) -> None:
    """Persist or replace the latest calculated result for one source, viewer and calculation."""
    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    async with _get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO calculation_snapshot "
                "(scope_type, scope_id, viewer_key, calculation_type, input_hash, algorithm_version, result, calculated_at) "
                "VALUES (:scope_type, :scope_id, :viewer_key, :calculation_type, :input_hash, :algorithm_version, "
                "CAST(:result AS jsonb), now()) "
                "ON CONFLICT (scope_type, scope_id, viewer_key, calculation_type) DO UPDATE SET "
                "input_hash = EXCLUDED.input_hash, algorithm_version = EXCLUDED.algorithm_version, "
                "result = EXCLUDED.result, calculated_at = now()"
            ),
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "viewer_key": viewer_key,
                "calculation_type": calculation_type,
                "input_hash": input_hash,
                "algorithm_version": algorithm_version,
                "result": result_json,
            },
        )
        await session.commit()


async def get_user_manual_property_valuations(user_id: str, asset_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Load one user's property valuations, source preferences, and saved OMI and affrancazione overrides."""
    keys = sorted({str(key) for key in asset_keys if re.fullmatch(r"[a-f0-9]{64}", str(key))})
    if not keys:
        return {}

    empty_text = cast(null(), String())
    empty_numeric = cast(null(), Numeric(14, 2))
    empty_date = cast(null(), Date())
    empty_integer = cast(null(), Integer())
    empty_boolean = cast(null(), Boolean())
    rows_by_kind = union_all(
        select(
            ManualPropertyValuation.asset_key.label("asset_key"),
            literal("valuation", type_=String()).label("record_type"),
            ManualPropertyValuation.provider.label("provider"),
            ManualPropertyValuation.value_min.label("value_min"),
            ManualPropertyValuation.value_max.label("value_max"),
            ManualPropertyValuation.source_date.label("source_date"),
            ManualPropertyValuation.source_url.label("source_url"),
            ManualPropertyValuation.notes.label("notes"),
            empty_text.label("preferred_source"),
            empty_numeric.label("amount"),
            empty_text.label("zone_code"),
            empty_integer.label("typology_code"),
            empty_boolean.label("is_manual"),
        ).where(ManualPropertyValuation.user_id == user_id, ManualPropertyValuation.asset_key.in_(keys)),
        select(
            PropertyValuationPreference.asset_key,
            literal("preference", type_=String()),
            empty_text,
            empty_numeric,
            empty_numeric,
            empty_date,
            empty_text,
            empty_text,
            PropertyValuationPreference.preferred_source,
            empty_numeric,
            empty_text,
            empty_integer,
            empty_boolean,
        ).where(PropertyValuationPreference.user_id == user_id, PropertyValuationPreference.asset_key.in_(keys)),
        select(
            PropertyAffrancationAmount.asset_key,
            literal("affrancation", type_=String()),
            empty_text,
            empty_numeric,
            empty_numeric,
            empty_date,
            empty_text,
            empty_text,
            empty_text,
            PropertyAffrancationAmount.amount,
            empty_text,
            empty_integer,
            empty_boolean,
        ).where(PropertyAffrancationAmount.user_id == user_id, PropertyAffrancationAmount.asset_key.in_(keys)),
        select(
            PropertyOmiOverride.asset_key,
            literal("omi_override", type_=String()),
            empty_text,
            PropertyOmiOverride.value_min,
            PropertyOmiOverride.value_max,
            empty_date,
            empty_text,
            empty_text,
            empty_text,
            empty_numeric,
            empty_text,
            empty_integer,
            empty_boolean,
        ).where(PropertyOmiOverride.user_id == user_id, PropertyOmiOverride.asset_key.in_(keys)),
        select(
            PropertyOmiSelection.asset_key,
            literal("omi_selection", type_=String()),
            empty_text,
            empty_numeric,
            empty_numeric,
            empty_date,
            empty_text,
            empty_text,
            empty_text,
            empty_numeric,
            PropertyOmiSelection.zone_code,
            PropertyOmiSelection.typology_code,
            PropertyOmiSelection.is_manual,
        ).where(PropertyOmiSelection.user_id == user_id, PropertyOmiSelection.asset_key.in_(keys)),
    ).subquery("user_property_valuation_rows")

    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                select(rows_by_kind).order_by(rows_by_kind.c.record_type, rows_by_kind.c.provider)
            )
        ).mappings().all()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = result.setdefault(row["asset_key"], {"valuations": [], "preferred_source": None})
        if row["record_type"] == "valuation":
            values["valuations"].append({
                "provider": row["provider"],
                "value_min": row["value_min"],
                "value_max": row["value_max"],
                "source_date": row["source_date"],
                "source_url": row["source_url"],
                "notes": row["notes"],
            })
        elif row["record_type"] == "preference":
            values["preferred_source"] = row["preferred_source"]
        elif row["record_type"] == "affrancation":
            values["affrancation_amount"] = row["amount"]
        elif row["record_type"] == "omi_override":
            values["omi_override"] = {"value_min": row["value_min"], "value_max": row["value_max"]}
        elif row["record_type"] == "omi_selection":
            values["omi_selection"] = {
                "zone_code": row["zone_code"],
                "typology_code": row["typology_code"],
                "is_manual": row["is_manual"],
            }
    return result


async def get_user_property_omi_selections(user_id: str, asset_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Load saved OMI zone and typology choices for one user's cadastral properties."""
    keys = sorted({str(key) for key in asset_keys if re.fullmatch(r"[a-f0-9]{64}", str(key))})
    if not keys:
        return {}
    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PropertyOmiSelection).where(
                    PropertyOmiSelection.user_id == user_id,
                    PropertyOmiSelection.asset_key.in_(keys),
                )
            )
        ).scalars().all()
    return {
        row.asset_key: {
            "zone_code": row.zone_code,
            "typology_code": row.typology_code,
            "is_manual": row.is_manual,
        }
        for row in rows
    }


async def get_user_property_actual_sizes(user_id: str, asset_keys: Iterable[str]) -> dict[str, Decimal]:
    """Load one user's saved effective size for each cadastral property."""
    keys = sorted({str(key) for key in asset_keys if re.fullmatch(r"[a-f0-9]{64}", str(key))})
    if not keys:
        return {}
    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PropertyActualSize).where(
                    PropertyActualSize.user_id == user_id,
                    PropertyActualSize.asset_key.in_(keys),
                )
            )
        ).scalars().all()
    return {row.asset_key: row.actual_size_sqm for row in rows}


async def ensure_user_property_omi_selections(
    user_id: str, defaults: Iterable[dict[str, Any]]
) -> None:
    """Persist or refresh automatic OMI guesses without replacing explicit user choices."""
    values = []
    for item in defaults:
        asset_key = str(item.get("asset_key") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
            continue
        zone_code = str(item.get("zone_code") or "").strip().upper() or None
        try:
            typology_code = int(item["typology_code"]) if item.get("typology_code") is not None else None
        except (TypeError, ValueError):
            typology_code = None
        if not zone_code and typology_code is None:
            continue
        values.append({
            "user_id": user_id,
            "asset_key": asset_key,
            "zone_code": zone_code,
            "typology_code": typology_code,
            "is_manual": False,
            "legacy_zone_code": str(item.get("legacy_zone_code") or "").strip().upper() or None,
        })
    if not values:
        return
    async with _get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO property_omi_selection "
                "(user_id, asset_key, zone_code, typology_code, is_manual, updated_at) "
                "VALUES (:user_id, :asset_key, :zone_code, :typology_code, :is_manual, now()) "
                "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                "zone_code = EXCLUDED.zone_code, typology_code = EXCLUDED.typology_code, "
                "is_manual = FALSE, updated_at = now() "
                "WHERE property_omi_selection.is_manual IS FALSE OR "
                "(property_omi_selection.is_manual IS NULL "
                "AND CAST(:legacy_zone_code AS VARCHAR) IS NOT NULL "
                "AND property_omi_selection.zone_code IS NOT DISTINCT FROM CAST(:legacy_zone_code AS VARCHAR))"
            ),
            values,
        )
        await session.commit()


async def set_user_property_omi_selection(
    user_id: str, asset_key: str, zone_code: str | None, typology_code: int | None
) -> None:
    """Save a user's selected OMI zone and typology and invalidate their cached totals."""
    async with _get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO property_omi_selection "
                "(user_id, asset_key, zone_code, typology_code, is_manual, updated_at) "
                "VALUES (:user_id, :asset_key, :zone_code, :typology_code, TRUE, now()) "
                "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                "zone_code = EXCLUDED.zone_code, typology_code = EXCLUDED.typology_code, "
                "is_manual = TRUE, updated_at = now()"
            ),
            {
                "user_id": user_id,
                "asset_key": asset_key,
                "zone_code": zone_code,
                "typology_code": typology_code,
            },
        )
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()


async def set_user_property_actual_size(
    user_id: str, asset_key: str, actual_size_sqm: Decimal | None
) -> None:
    """Save a user's effective property size, or clear it to use the cadastral default again."""
    async with _get_session_factory()() as session:
        if actual_size_sqm is None:
            await session.execute(
                text("DELETE FROM property_actual_size WHERE user_id = :user_id AND asset_key = :asset_key"),
                {"user_id": user_id, "asset_key": asset_key},
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO property_actual_size "
                    "(user_id, asset_key, actual_size_sqm, updated_at) "
                    "VALUES (:user_id, :asset_key, :actual_size_sqm, now()) "
                    "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                    "actual_size_sqm = EXCLUDED.actual_size_sqm, updated_at = now()"
                ),
                {"user_id": user_id, "asset_key": asset_key, "actual_size_sqm": actual_size_sqm},
            )
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()


async def set_user_manual_property_valuation(
    user_id: str,
    asset_key: str,
    provider: str,
    value_min: Decimal,
    value_max: Decimal | None,
    source_date: date | None,
    source_url: str | None,
    notes: str | None,
) -> None:
    """Create or replace one user's provider valuation for one cadastral property."""
    async with _get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO manual_property_valuation "
                "(user_id, asset_key, provider, value_min, value_max, source_date, source_url, notes, created_at, updated_at) "
                "VALUES (:user_id, :asset_key, :provider, :value_min, :value_max, :source_date, :source_url, :notes, now(), now()) "
                "ON CONFLICT (user_id, asset_key, provider) DO UPDATE SET "
                "value_min = EXCLUDED.value_min, value_max = EXCLUDED.value_max, source_date = EXCLUDED.source_date, "
                "source_url = EXCLUDED.source_url, notes = EXCLUDED.notes, updated_at = now()"
            ),
            {
                "user_id": user_id,
                "asset_key": asset_key,
                "provider": provider,
                "value_min": value_min,
                "value_max": value_max,
                "source_date": source_date,
                "source_url": source_url,
                "notes": notes,
            },
        )
        # The final KPI snapshot includes this user's manual values and source choice.
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()


async def set_user_property_valuation_preference(user_id: str, asset_key: str, source: str) -> bool:
    """Set a user's preferred source; return False if an external provider has no saved estimate."""
    async with _get_session_factory()() as session:
        if source != "omi":
            exists = (
                await session.execute(
                    select(ManualPropertyValuation.asset_key).where(
                        ManualPropertyValuation.user_id == user_id,
                        ManualPropertyValuation.asset_key == asset_key,
                        ManualPropertyValuation.provider == source,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
        await session.execute(
            text(
                "INSERT INTO property_valuation_preference (user_id, asset_key, preferred_source, updated_at) "
                "VALUES (:user_id, :asset_key, :preferred_source, now()) "
                "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                "preferred_source = EXCLUDED.preferred_source, updated_at = now()"
            ),
            {"user_id": user_id, "asset_key": asset_key, "preferred_source": source},
        )
        # Source preference changes affect the ownership-right allocation as well as the displayed range.
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()
    return True


async def set_user_property_affrancation_amount(
    user_id: str, asset_key: str, amount: Decimal | None
) -> None:
    """Save an account-specific amount, or remove it to restore the automatic estimate."""
    async with _get_session_factory()() as session:
        if amount is None:
            await session.execute(
                text(
                    "DELETE FROM property_affrancation_amount "
                    "WHERE user_id = :user_id AND asset_key = :asset_key"
                ),
                {"user_id": user_id, "asset_key": asset_key},
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO property_affrancation_amount "
                    "(user_id, asset_key, amount, updated_at) VALUES (:user_id, :asset_key, :amount, now()) "
                    "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                    "amount = EXCLUDED.amount, updated_at = now()"
                ),
                {"user_id": user_id, "asset_key": asset_key, "amount": amount},
            )
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()


async def set_user_property_omi_override(
    user_id: str,
    asset_key: str,
    value_min: Decimal | None,
    value_max: Decimal | None,
) -> None:
    """Save or clear a user's custom OMI range for one cadastral property."""
    async with _get_session_factory()() as session:
        if value_min is None:
            await session.execute(
                text("DELETE FROM property_omi_override WHERE user_id = :user_id AND asset_key = :asset_key"),
                {"user_id": user_id, "asset_key": asset_key},
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO property_omi_override "
                    "(user_id, asset_key, value_min, value_max, updated_at) "
                    "VALUES (:user_id, :asset_key, :value_min, :value_max, now()) "
                    "ON CONFLICT (user_id, asset_key) DO UPDATE SET "
                    "value_min = EXCLUDED.value_min, value_max = EXCLUDED.value_max, updated_at = now()"
                ),
                {
                    "user_id": user_id,
                    "asset_key": asset_key,
                    "value_min": value_min,
                    "value_max": value_max,
                },
            )
        await session.execute(
            text("DELETE FROM calculation_snapshot WHERE viewer_key = :viewer_key"),
            {"viewer_key": user_id},
        )
        await session.commit()


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
    status: Optional[str] = None,
) -> list[dict]:
    """Search stored responses by cadastral coordinates.

    ``status`` (``completed`` | ``failed`` | ``pending``) filters in SQL, so a page holds ``limit`` matching rows.
    """
    status_where = {
        "completed": [Request.responded_at.is_not(None), Request.success.is_(True)],
        "failed": [Request.responded_at.is_not(None), Request.success.is_(False)],
        "pending": [Request.responded_at.is_(None)],
    }.get((status or "").lower(), [])
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(Request, RequestPayload.data, CadastralUnit, IstatMunicipality)
            .outerjoin(RequestPayload, RequestPayload.request_id == _content_request_id())
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .outerjoin(IstatMunicipality, CadastralUnit.comune_id == IstatMunicipality.numeric_code)
            .where(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto), *status_where)
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


def _request_subject_aggregate(value, *subject_conditions, joins=()):
    """Aggregate normalized subjects linked through owners or the queried subject's fiscal code."""
    owner_match = (
        select(ResultOwner.id)
        .where(ResultOwner.request_id == _content_request_id(), ResultOwner.subject_id == Subject.id)
        .correlate(Request, Subject)
        .exists()
    )
    property_match = (
        select(Property.id)
        .where(Property.request_id == _content_request_id(), Property.subject_id == Subject.id)
        .correlate(Request, Subject)
        .exists()
    )
    stmt = select(func.string_agg(distinct(value), " · ")).select_from(Subject)
    for target, on_clause in joins:
        stmt = stmt.outerjoin(target, on_clause)
    return (
        stmt.where(
            or_(owner_match, property_match, Subject.fiscal_code == Request.subject_query),
            *subject_conditions,
        )
        .correlate(Request)
        .scalar_subquery()
    )


async def find_result_rows(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    request_type: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    fiscal_code: Optional[str] = None,
    person: Optional[str] = None,
    company: Optional[str] = None,
) -> list[dict]:
    """Search single-query responses and workflow runs for the web results page."""
    if source not in {"single", "workflow"}:
        source = None
    if status not in {"completed", "partial", "failed", "error", "pending", "running", "no_match"}:
        status = None

    single_rows: list[dict] = []
    if source in (None, "single"):
        property_count = (
            select(func.count(Property.id)).where(Property.request_id == _content_request_id()).correlate(Request).scalar_subquery()
        )
        owner_count = (
            select(func.count(ResultOwner.id)).where(ResultOwner.request_id == _content_request_id()).correlate(Request).scalar_subquery()
        )
        subject_fiscal_codes = _request_subject_aggregate(
            Subject.fiscal_code,
            Subject.fiscal_code.is_not(None),
        )
        birth_municipality = aliased(IstatMunicipality)
        person_details = _request_subject_aggregate(
            func.concat_ws(
                " · ",
                Subject.name,
                Subject.fiscal_code,
                cast(Subject.birth_date, String),
                func.coalesce(
                    birth_municipality.italian_name,
                    birth_municipality.name,
                    Subject.birth_municipality_text,
                ),
                func.coalesce(birth_municipality.province_abbreviation, Subject.birth_province_code),
                Subject.gender,
            ),
            Subject.subject_type == "person",
            joins=((birth_municipality, Subject.birth_comune_id == birth_municipality.numeric_code),),
        )
        company_details = _request_subject_aggregate(
            func.concat_ws(" · ", Subject.name, Subject.fiscal_code),
            Subject.subject_type == "legal_entity",
        )
        # Page on the request ids alone, then compute the per-row aggregates (counts, subjects, tags) for those rows:
        # evaluated in one statement they also ran for every row skipped by OFFSET (9-40 s at the end of the list).
        page_ids = (
            select(Request.id.label("id"))
            .select_from(Request)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .where(
                *_build_single_where(
                    provincia,
                    comune,
                    foglio,
                    particella,
                    tipo_catasto,
                    status,
                    request_type,
                    tag,
                    fiscal_code,
                    person,
                    company,
                )
            )
            .order_by(Request.created_at.desc(), Request.id.desc())
            .limit(limit)
            .offset(offset)
            .subquery("page_ids")
        )
        stmt = (
            select(
                Request.id.label("request_pk"),
                Request.request_ref.label("request_id"),
                Request.request_type.label("request_type"),
                RequestType.label.label("request_type_label"),
                RequestType.description.label("request_type_description"),
                (
                    select(func.array_agg(RequestTypeTag.tag))
                    .where(RequestTypeTag.request_type == Request.request_type)
                    .correlate(Request)
                    .scalar_subquery()
                ).label("request_type_tags"),
                CadastralUnit.cadastre_type.label("cadastre_type"),
                CadastralUnit.comune_id.label("comune_id"),
                CadastralUnit.sheet.label("sheet"),
                CadastralUnit.parcel.label("parcel"),
                CadastralUnit.section.label("section"),
                CadastralUnit.subunit.label("subunit"),
                Request.created_at.label("requested_at"),
                Request.success.label("success"),
                Request.error.label("error"),
                Request.responded_at.label("responded_at"),
                Request.subject_query.label("subject_query"),
                Request.total_results.label("total_results"),
                Request.total_intestati.label("total_intestati"),
                property_count.label("property_count"),
                owner_count.label("owner_count"),
                subject_fiscal_codes.label("subject_fiscal_codes"),
                person_details.label("person_details"),
                company_details.label("company_details"),
            )
            .select_from(Request)
            .join(page_ids, page_ids.c.id == Request.id)
            .outerjoin(RequestType, Request.request_type == RequestType.code)
            .outerjoin(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .order_by(Request.created_at.desc(), Request.id.desc())
        )
        async with _get_session_factory()() as session:
            rows = (await session.execute(stmt)).mappings().all()
            request_ids = [row["request_pk"] for row in rows]
            property_locations: dict[int, dict[str, Any]] = {}
            provinces_by_request: dict[int, list[str]] = {}
            if request_ids:
                property_unit = aliased(CadastralUnit)
                property_summary_stmt = (
                    select(
                        Property.request_id,
                        func.array_agg(distinct(property_unit.comune_id))
                        .filter(property_unit.comune_id.is_not(None))
                        .label("municipality_ids"),
                        func.array_agg(distinct(property_unit.cadastre_type))
                        .filter(property_unit.cadastre_type.is_not(None))
                        .label("types"),
                        func.array_agg(distinct(property_unit.sheet))
                        .filter(property_unit.sheet.is_not(None))
                        .label("sheets"),
                        func.array_agg(distinct(property_unit.parcel))
                        .filter(property_unit.parcel.is_not(None))
                        .label("parcels"),
                    )
                    .select_from(Property)
                    .outerjoin(property_unit, Property.cadastral_unit_id == property_unit.id)
                    .where(Property.request_id.in_(request_ids))
                    .group_by(Property.request_id)
                )
                property_rows = (await session.execute(property_summary_stmt)).mappings().all()
                property_locations = {item["request_id"]: dict(item) for item in property_rows}
                municipality_ids = {
                    int(comune_id)
                    for row in rows
                    for comune_id in (
                        [row["comune_id"]]
                        + (property_locations.get(row["request_pk"], {}).get("municipality_ids") or [])
                    )
                    if comune_id is not None
                }
                municipalities_by_id: dict[int, IstatMunicipality] = {}
                if municipality_ids:
                    municipality_rows = (
                        await session.execute(
                            select(IstatMunicipality).where(IstatMunicipality.numeric_code.in_(municipality_ids))
                        )
                    ).scalars().all()
                    municipalities_by_id = {int(item.numeric_code): item for item in municipality_rows}
                province_rows = (
                    await session.execute(
                        select(
                            RequestProvince.request_id,
                            RequestProvince.province_code,
                            RequestProvince.province_name,
                        )
                        .where(RequestProvince.request_id.in_(request_ids))
                        .order_by(RequestProvince.province_index)
                    )
                ).all()
                for request_pk, code, name in province_rows:
                    value = str(code or name or "").strip().upper()
                    if value:
                        provinces_by_request.setdefault(request_pk, []).append(value)
        for row in rows:
            success = bool(row["success"]) if row["success"] is not None and row["responded_at"] is not None else None
            type_info = request_type_info(row["request_type"])
            fiscal_codes = [
                value.strip()
                for value in str(row["subject_fiscal_codes"] or "").split(" · ")
                if value.strip()
            ]
            queried_code = str(row["subject_query"] or "").strip().upper()
            if re.fullmatch(r"(?:[A-Z0-9]{16}|\d{11})", queried_code) and queried_code not in fiscal_codes:
                fiscal_codes.append(queried_code)
            locations = property_locations.get(row["request_pk"], {})
            asset_municipalities = [
                municipalities_by_id.get(int(comune_id))
                for comune_id in (locations.get("municipality_ids") or [])
                if int(comune_id) in municipalities_by_id
            ]
            request_municipality = municipalities_by_id.get(row["comune_id"]) if row["comune_id"] is not None else None
            province_values = (
                [item.province_abbreviation for item in asset_municipalities if item.province_abbreviation]
                or provinces_by_request.get(row["request_pk"])
                or (
                    [request_municipality.province_abbreviation]
                    if request_municipality and request_municipality.province_abbreviation
                    else []
                )
            )
            municipality_values = (
                [item.italian_name or item.name for item in asset_municipalities if item.italian_name or item.name]
                or (
                    [request_municipality.italian_name or request_municipality.name]
                    if request_municipality and (request_municipality.italian_name or request_municipality.name)
                    else []
                )
            )
            sheet_values = locations.get("sheets") or ([row["sheet"]] if row["sheet"] else [])
            parcel_values = locations.get("parcels") or ([row["parcel"]] if row["parcel"] else [])
            type_values = locations.get("types") or ([row["cadastre_type"]] if row["cadastre_type"] else [])
            single_rows.append(
                {
                    "request_id": row["request_id"],
                    "request_type": row["request_type"],
                    "request_type_label": row["request_type_label"] or type_info.label,
                    "request_type_description": row["request_type_description"] or type_info.description,
                    "request_type_tags": list(row["request_type_tags"] or type_info.tags),
                    "source": "single",
                    "tipo_catasto": row["cadastre_type"] or _join_result_values(type_values),
                    "provincia": _join_result_values(province_values, province_labels=True),
                    "comune": _join_result_values(municipality_values),
                    "foglio": _join_result_values(sheet_values),
                    "particella": _join_result_values(parcel_values),
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
                    "fiscal_code": " · ".join(fiscal_codes),
                    "person_details": row["person_details"] or "",
                    "company_details": row["company_details"] or "",
                }
            )

    # workflow_runs no longer live in sister's DB — owned by opendata
    return single_rows


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
    """Return request totals using the same failed/no-match definitions as the results page."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        counts = (
            await session.execute(
                select(
                    func.count().label("requests"),
                    func.count().filter(Request.responded_at.is_not(None)).label("responses"),
                    func.count().filter(Request.success.is_(True)).label("successful"),
                    func.count()
                    .filter(Request.success.is_(False), ~_no_match_sql_condition())
                    .label("failed"),
                    func.count()
                    .filter(Request.success.is_(False), _no_match_sql_condition())
                    .label("no_match"),
                ).select_from(Request)
            )
        ).one()
    total_requests, total_responses, successful, failed, no_match = counts
    return {
        "total_requests": total_requests,
        "total_responses": total_responses,
        "successful": successful,
        "failed": failed,
        "no_match": no_match,
        "pending": max(total_requests - total_responses, 0),
    }


def _build_single_where(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    status: Optional[str] = None,
    request_type: Optional[str] = None,
    tag: Optional[str] = None,
    fiscal_code: Optional[str] = None,
    person: Optional[str] = None,
    company: Optional[str] = None,
) -> list:
    """Build filters for requests and their linked cadastral subjects."""
    conditions = []
    property_unit = aliased(CadastralUnit)

    def property_match(*property_conditions):
        return (
            select(Property.id)
            .select_from(Property)
            .outerjoin(property_unit, Property.cadastral_unit_id == property_unit.id)
            .where(Property.request_id == _content_request_id(), *property_conditions)
            .correlate(Request)
            .exists()
        )

    if provincia:
        code = province_code(provincia) or str(provincia).strip().upper()
        name = place_name(provincia) or str(provincia).strip().upper()
        province_municipality_ids = select(IstatMunicipality.numeric_code).where(
            func.upper(func.trim(IstatMunicipality.province_abbreviation)) == code
        )
        request_unit_matches = (
            select(Request.id)
            .join(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .where(CadastralUnit.comune_id.in_(province_municipality_ids))
        )
        property_request_matches = (
            select(Property.request_id)
            .join(CadastralUnit, Property.cadastral_unit_id == CadastralUnit.id)
            .where(CadastralUnit.comune_id.in_(province_municipality_ids))
        )
        subject_province_matches = select(RequestProvince.request_id).where(
            or_(
                func.upper(func.trim(RequestProvince.province_code)) == code,
                func.upper(func.trim(RequestProvince.province_name)).ilike(f"%{name}%"),
            )
        )
        conditions.append(
            or_(
                Request.id.in_(request_unit_matches),
                Request.id.in_(property_request_matches),
                Request.id.in_(subject_province_matches),
            )
        )
    if comune:
        name = place_name(comune) or str(comune).strip().upper()
        municipality_ids = select(IstatMunicipality.numeric_code).where(
            or_(
                func.upper(func.trim(IstatMunicipality.italian_name)) == name,
                func.upper(func.trim(IstatMunicipality.name)) == name,
                func.upper(func.trim(IstatMunicipality.other_language_name)) == name,
            )
        )
        request_unit_matches = (
            select(Request.id)
            .join(CadastralUnit, Request.cadastral_unit_id == CadastralUnit.id)
            .where(CadastralUnit.comune_id.in_(municipality_ids))
        )
        property_request_matches = (
            select(Property.request_id)
            .join(CadastralUnit, Property.cadastral_unit_id == CadastralUnit.id)
            .where(CadastralUnit.comune_id.in_(municipality_ids))
        )
        conditions.append(
            or_(Request.id.in_(request_unit_matches), Request.id.in_(property_request_matches))
        )
    if foglio:
        value = str(foglio).strip()
        conditions.append(or_(CadastralUnit.sheet == value, property_match(property_unit.sheet == value)))
    if particella:
        value = str(particella).strip()
        conditions.append(or_(CadastralUnit.parcel == value, property_match(property_unit.parcel == value)))
    if tipo_catasto:
        value = str(tipo_catasto).strip().upper()
        conditions.append(
            or_(CadastralUnit.cadastre_type == value, property_match(property_unit.cadastre_type == value))
        )
    if request_type:
        conditions.append(Request.request_type == request_type)
    if tag:
        conditions.append(
            select(RequestTypeTag.request_type)
            .where(RequestTypeTag.request_type == Request.request_type, RequestTypeTag.tag == tag)
            .exists()
        )

    def subject_match(*subject_conditions):
        owner_match = (
            select(ResultOwner.id)
            .where(ResultOwner.request_id == _content_request_id(), ResultOwner.subject_id == Subject.id)
            .correlate(Request, Subject)
            .exists()
        )
        property_match = (
            select(Property.id)
            .where(Property.request_id == _content_request_id(), Property.subject_id == Subject.id)
            .correlate(Request, Subject)
            .exists()
        )
        return (
            select(Subject.id)
            .select_from(Subject)
            .where(
                or_(
                    owner_match,
                    property_match,
                    Subject.fiscal_code == Request.subject_query,
                ),
                *subject_conditions,
            )
            .correlate(Request)
            .exists()
        )

    def matching_terms(value: str, columns, *, include_birthplace=False):
        tokens = [token for token in re.split(r"\s+", value.strip()) if token]
        if not tokens:
            return None
        token_conditions = []
        for token in tokens:
            pattern = f"%{token}%"
            alternatives = [column.ilike(pattern) for column in columns]
            if include_birthplace:
                municipality_ids = select(IstatMunicipality.numeric_code).where(
                    or_(
                        IstatMunicipality.name.ilike(pattern),
                        IstatMunicipality.italian_name.ilike(pattern),
                        IstatMunicipality.other_language_name.ilike(pattern),
                    )
                )
                alternatives.append(Subject.birth_comune_id.in_(municipality_ids))
            token_conditions.append(or_(*alternatives))
        return and_(*token_conditions)

    if fiscal_code and fiscal_code.strip():
        # Fiscal codes and VAT numbers are alphanumeric; discard presentation
        # separators on both sides so partial input also matches formatted values.
        code = re.sub(r"[^A-Z0-9]", "", fiscal_code.upper())
        if code:
            code_pattern = f"%{code}%"

            def normalized_code(column):
                return func.regexp_replace(
                    func.upper(func.coalesce(column, "")),
                    r"[^A-Z0-9]",
                    "",
                    "g",
                )

            conditions.append(
                or_(
                    normalized_code(Request.subject_query).ilike(code_pattern),
                    subject_match(normalized_code(Subject.fiscal_code).ilike(code_pattern)),
                )
            )
    if person and person.strip():
        person_search = matching_terms(
            person,
            (
                Subject.name,
                Subject.first_name,
                Subject.last_name,
                Subject.gender,
                cast(Subject.birth_date, String),
                Subject.birth_municipality_text,
                Subject.birth_province_code,
            ),
            include_birthplace=True,
        )
        if person_search is not None:
            conditions.append(subject_match(Subject.subject_type == "person", person_search))
    if company and company.strip():
        company_search = matching_terms(company, (Subject.name, Subject.fiscal_code))
        if company_search is not None:
            conditions.append(subject_match(Subject.subject_type == "legal_entity", company_search))
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
    request_type: Optional[str] = None,
    tag: Optional[str] = None,
    fiscal_code: Optional[str] = None,
    person: Optional[str] = None,
    company: Optional[str] = None,
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
        .where(
            *_build_single_where(
                provincia,
                comune,
                foglio,
                particella,
                tipo_catasto,
                status,
                request_type,
                tag,
                fiscal_code,
                person,
                company,
            )
        )
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
    request_type: Optional[str] = None,
    tag: Optional[str] = None,
    fiscal_code: Optional[str] = None,
    person: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Return web result stats including single-query requests and workflows."""
    if source not in {"single", "workflow"}:
        source = None

    s_total = s_ok = s_fail = s_no_match = s_pending = 0
    if source in (None, "single"):
        filters = _build_single_where(
            provincia,
            comune,
            foglio,
            particella,
            tipo_catasto,
            request_type=request_type,
            tag=tag,
            fiscal_code=fiscal_code,
            person=person,
            company=company,
        )
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
