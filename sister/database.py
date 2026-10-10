"""PostgreSQL database layer for sister (SQLModel + async SQLAlchemy).

Provides persistent storage for visura requests, responses, and structured
result tables (immobili, intestati). Includes cache lookup for deduplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from sqlalchemy import column, delete, func, inspect, table, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from .db_models import (
    PROPERTY_FIELD_MAP,
    PROPERTY_LOCATION_FIELD_MAP,
    PROPERTY_SUBJECT_FIELD_MAP,
    ActivityLog,
    CadastralLocation,
    CadastralSubject,
    DocumentMetadata,
    GeographicPlace,
    OwnershipRight,
    PageVisit,
    PageVisitError,
    PageVisitFormElement,
    MortgageInspection,
    MortgageInspectionLien,
    MortgageInspectionParty,
    MortgageInspectionProperty,
    MortgageInspectionTitle,
    MortgageInspectionUnit,
    StructuredDocumentExtraction,
    VisuraDocument,
    VisuraOwner,
    VisuraProperty,
    VisuraRequest,
    VisuraResponse,
    VisuraResult,
)

from .result_parsers import (
    clean_amount,
    identifier_kind,
    land_area_m2,
    normalize_owner,
    parse_classamento,
    parse_ubicazione,
    parse_vis_imm_sel,
    split_foglio,
)

logger = logging.getLogger("sister")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)
DATA_ROOT = Path(os.getenv("SISTER_DATA_ROOT", str(PROJECT_ROOT))).expanduser().resolve()
DATABASE_DSN = os.getenv("DATABASE_DSN")
DATABASE_REVISION = "20261010_xml_typed_columns"

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
        _engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    return _engine


def _get_session_factory():
    return sessionmaker(_get_engine(), class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Check the Postgres connection and require the current Alembic schema revision."""
    engine = _get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        table_names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        required_tables = {
            "cadastral_locations",
            "visura_requests",
            "visura_responses",
            "visura_results",
            "visura_properties",
            "visura_owners",
            "visura_documents",
            "document_metadata",
            "document_structured_extractions",
            "mortgage_inspections",
            "mortgage_inspection_titles",
            "mortgage_inspection_liens",
            "mortgage_inspection_units",
            "mortgage_inspection_properties",
            "mortgage_inspection_parties",
        }
        missing = sorted(required_tables - table_names)
        if missing:
            raise RuntimeError(
                "PostgreSQL schema is incomplete (missing: " + ", ".join(missing) + "). Run `alembic upgrade head`."
            )
        if "alembic_version" not in table_names:
            raise RuntimeError("PostgreSQL migrations are not installed. Run `alembic upgrade head`.")
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        if version != DATABASE_REVISION:
            raise RuntimeError(
                f"PostgreSQL schema is at {version!r}; expected {DATABASE_REVISION!r}. Run `alembic upgrade head`."
            )
    logger.info("Database PostgreSQL inizializzato (writable=%s)", is_db_writable())


# ---------------------------------------------------------------------------
# Activity log (traceability of operational actions)
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """A JSON-compatible copy of ``value`` (unknown types become strings) so a summary never fails to serialise."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


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
    summaries (flags, counts); documents and personal data do not belong here.
    """
    if not is_db_writable():
        return None
    finished = datetime.now(timezone.utc)
    started = started_at or finished
    try:
        async with _get_session_factory()() as session:
            row = ActivityLog(
                started_at=started,
                finished_at=finished,
                duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
                actor=(actor or "system")[:200],
                source=source,
                action=action,
                status=status,
                target=target,
                params=_json_safe(params),
                result=_json_safe(result),
                error=(error or None) and str(error)[:2000],
                client_ip=client_ip,
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Registro attivita' '%s' non scritto: %s", action, exc)
        return None


async def get_recent_activity(limit: int = 50, action: Optional[str] = None) -> list[dict]:
    """The newest ``activity_log`` rows (newest first), optionally filtered by action."""
    async with _get_session_factory()() as session:
        stmt = select(ActivityLog).order_by(ActivityLog.started_at.desc(), ActivityLog.id.desc()).limit(limit)
        if action:
            stmt = stmt.where(ActivityLog.action == action)
        rows = (await session.execute(stmt)).scalars().all()
    return [row.model_dump(mode="json") for row in rows]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def compute_cache_key(request_type: str, **params) -> str:
    """Deterministic cache key from search parameters."""
    # Filter out None values and sort for determinism
    filtered = {k: v for k, v in params.items() if v is not None}
    canonical = json.dumps({"type": request_type, **filtered}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def find_cached_response(cache_key: str, ttl_seconds: int) -> Optional[dict]:
    """Find a successful, non-expired response matching the cache key."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        stmt = (
            select(VisuraResponse)
            .join(VisuraRequest)
            .where(
                VisuraRequest.cache_key == cache_key,
                VisuraResponse.success == True,  # noqa: E712
                VisuraResponse.created_at >= cutoff,
            )
            .order_by(VisuraResponse.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "request_id": row.request_id,
            "success": row.success,
            "tipo_catasto": row.cadastre_type,
            "data": row.data,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------


async def get_or_create_location(
    session: AsyncSession,
    cadastre_type: str = "",
    province: str = "",
    municipality: str = "",
    sheet: str = "",
    parcel: str = "",
    subunit: str = "",
    section: str = "",
) -> Optional[int]:
    """Get existing CadastralLocation or create one; return its id. Must run inside an open session."""
    stmt = select(CadastralLocation).where(
        CadastralLocation.cadastre_type == cadastre_type,
        CadastralLocation.province == province,
        CadastralLocation.municipality == municipality,
        CadastralLocation.sheet == sheet,
        CadastralLocation.parcel == parcel,
        CadastralLocation.subunit == subunit,
        CadastralLocation.section == section,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id
    loc = CadastralLocation(
        cadastre_type=cadastre_type,
        province=province,
        municipality=municipality,
        sheet=sheet,
        parcel=parcel,
        subunit=subunit,
        section=section,
    )
    session.add(loc)
    await session.flush()
    return loc.id


async def get_or_create_place(
    session: AsyncSession,
    province: str = "",
    municipality: str = "",
    municipality_code: str = "",
) -> Optional[int]:
    """Get existing GeographicPlace or create one; return its id."""
    stmt = select(GeographicPlace).where(
        GeographicPlace.province == province,
        GeographicPlace.municipality == municipality,
        GeographicPlace.municipality_code == municipality_code,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id
    place = GeographicPlace(province=province, municipality=municipality, municipality_code=municipality_code)
    session.add(place)
    await session.flush()
    return place.id


async def get_or_create_subject(
    session: AsyncSession,
    fiscal_code: Optional[str] = None,
    display_name: Optional[str] = None,
    last_name: Optional[str] = None,
    first_name: Optional[str] = None,
    gender: Optional[str] = None,
    date_of_birth: Optional[str] = None,
    birth_place_id: Optional[int] = None,
    birth_municipality_code: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> int:
    """Get existing CadastralSubject by fiscal_code (when present) or create one; return its id."""
    if fiscal_code:
        result = await session.execute(select(CadastralSubject).where(CadastralSubject.fiscal_code == fiscal_code))
        existing = result.scalar_one_or_none()
        if existing is not None:
            # a later, richer source (birth data from the Intestati page or the XML) completes a bare subject
            known = {
                "display_name": display_name,
                "last_name": last_name,
                "first_name": first_name,
                "gender": gender,
                "date_of_birth": date_of_birth,
                "birth_place_id": birth_place_id,
                "birth_municipality_code": birth_municipality_code,
                "subject_type": subject_type,
            }
            for name, value in known.items():
                if value and not getattr(existing, name):
                    setattr(existing, name, value)
            await session.flush()
            return existing.id
    subj = CadastralSubject(
        fiscal_code=fiscal_code,
        display_name=display_name,
        last_name=last_name,
        first_name=first_name,
        gender=gender,
        date_of_birth=date_of_birth,
        birth_place_id=birth_place_id,
        birth_municipality_code=birth_municipality_code,
        subject_type=subject_type,
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def get_or_create_right(
    session: AsyncSession,
    right_type: Optional[str] = None,
    right_code: Optional[str] = None,
    right_description: Optional[str] = None,
    ownership_share: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Get existing OwnershipRight or create one; return its id.

    Normalizes missing fields to empty strings so equality matches the unique-key representation.
    """
    right_type = right_type or ""
    right_code = right_code or ""
    right_description = right_description or ""
    ownership_share = ownership_share or ""
    start_date = start_date or ""
    end_date = end_date or ""
    conditions = []
    for col, val in [
        (OwnershipRight.right_type, right_type),
        (OwnershipRight.right_code, right_code),
        (OwnershipRight.right_description, right_description),
        (OwnershipRight.ownership_share, ownership_share),
        (OwnershipRight.start_date, start_date),
        (OwnershipRight.end_date, end_date),
    ]:
        conditions.append(col == val)
    result = await session.execute(select(OwnershipRight).where(*conditions))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id
    right = OwnershipRight(
        right_type=right_type,
        right_code=right_code,
        right_description=right_description,
        ownership_share=ownership_share,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(right)
    await session.flush()
    return right.id


# ---------------------------------------------------------------------------
# Request operations (same signatures as before)
# ---------------------------------------------------------------------------


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
        location_id = await get_or_create_location(
            session,
            cadastre_type=tipo_catasto,
            province=provincia,
            municipality=comune,
            sheet=foglio,
            parcel=particella,
            subunit=subalterno or "",
            section=sezione or "",
        )
        row = await session.get(VisuraRequest, request_id)
        if row is None:
            row = VisuraRequest(request_id=request_id, request_type=request_type)
            session.add(row)
        row.request_type = request_type
        row.location_id = location_id
        row.cache_key = cache_key
        await session.commit()


async def save_requests_batch(requests: list[dict]) -> None:
    """Persist multiple requests atomically."""
    if not requests:
        return
    session_factory = _get_session_factory()
    async with session_factory() as session:
        for req in requests:
            location_id = await get_or_create_location(
                session,
                cadastre_type=req["tipo_catasto"],
                province=req["provincia"],
                municipality=req["comune"],
                sheet=req["foglio"],
                parcel=req["particella"],
                subunit=req.get("subalterno") or "",
                section=req.get("sezione") or "",
            )
            row = await session.get(VisuraRequest, req["request_id"])
            if row is None:
                row = VisuraRequest(request_id=req["request_id"], request_type=req["request_type"])
                session.add(row)
            row.request_type = req["request_type"]
            row.location_id = location_id
            row.cache_key = req.get("cache_key")
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


def _parse_page_visits(response_id: str, data: Optional[dict]) -> list[PageVisit]:
    """Parse page_visits from response JSON into structured rows."""
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
                    # SQLModel datetime columns store aware values only; page timestamps are naive local time
                    ts = ts.astimezone()
            except (ValueError, TypeError):
                pass
        rows.append(
            PageVisit(
                response_id=response_id,
                step=item.get("step", ""),
                url=item.get("url"),
                screenshot_url=item.get("screenshot_url"),
                form_elements_json=(
                    json.dumps(item.get("form_elements", []), default=str) if item.get("form_elements") else None
                ),
                errors_json=json.dumps(item.get("errors", []), default=str) if item.get("errors") else None,
                timestamp=ts,
            )
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


async def _link_documents(session: AsyncSession, request_id: str, data: Optional[dict]) -> int:
    """Tie the documents a response downloaded (``downloaded_pdfs``) to it; they are saved before the response."""
    documents = data.get("downloaded_pdfs") if isinstance(data, dict) else None
    linked = 0
    for item in documents if isinstance(documents, list) else []:
        if not isinstance(item, dict):
            continue
        names = [n for n in (item.get("filename"), item.get("original_filename")) if n]
        paths = [p for p in (item.get("path"), item.get("extracted_path")) if p]
        if not names and not paths:
            continue
        result = await session.execute(
            text(
                "UPDATE visura_documents SET response_id = :rid WHERE response_id IS NULL"
                " AND (filename = ANY(:names) OR file_path = ANY(:paths))"
            ),
            {"rid": request_id, "names": names, "paths": paths},
        )
        linked += result.rowcount or 0
    return linked


async def _replace_projection(session: AsyncSession, request_id: str, tipo_catasto: str, data: Optional[dict]) -> None:
    """Rebuild the structured rows of one response from its JSON (results, properties, owners, document links)."""
    for table_name in ("visura_owners", "visura_properties", "visura_results"):
        await session.execute(text(f"DELETE FROM {table_name} WHERE response_id = :rid"), {"rid": request_id})  # noqa: S608
    # the request location supplies the province/municipality a row of the list does not carry
    req_row = await session.get(VisuraRequest, request_id)
    req_loc: Optional[CadastralLocation] = None
    if req_row and req_row.location_id:
        req_loc = await session.get(CadastralLocation, req_row.location_id)
    await _persist_projection(session, request_id, tipo_catasto, data, req_loc)
    await _link_documents(session, request_id, data)


async def backfill_projections(limit: Optional[int] = None, batch: int = 50) -> dict[str, int]:
    """Re-project every stored response with the current parsers (idempotent; the JSON in ``data`` is the source).

    Fills ``result_id`` / ``owner_index`` on existing properties and owners, splits the composite portal cells and
    ties downloaded documents to their response.
    """
    session_factory = _get_session_factory()
    stats = {"responses": 0, "properties": 0, "owners": 0}
    async with session_factory() as session:
        stmt = select(VisuraResponse.request_id, VisuraResponse.cadastre_type).where(VisuraResponse.data.is_not(None))
        if limit:
            stmt = stmt.limit(limit)
        todo = (await session.execute(stmt.order_by(VisuraResponse.created_at))).all()
    for start in range(0, len(todo), batch):
        async with session_factory() as session:
            for request_id, cadastre_type in todo[start : start + batch]:
                resp = await session.get(VisuraResponse, request_id)
                if resp is None or not isinstance(resp.data, dict):
                    continue
                await _replace_projection(session, request_id, cadastre_type, resp.data)
                stats["responses"] += 1
            await session.commit()
    async with session_factory() as session:
        for key, table_name in (("properties", "visura_properties"), ("owners", "visura_owners")):
            stats[key] = (await session.execute(text(f"SELECT count(*) FROM {table_name}"))).scalar_one()  # noqa: S608
    return stats


async def _resolve_subject(session: AsyncSession, fields: dict[str, Any]) -> Optional[int]:
    """Create/enrich the subject for normalised owner fields (birth place → ``geographic_places``)."""
    fields = dict(fields)
    province = fields.pop("birth_province", "") or ""
    municipality = fields.pop("birth_municipality", "") or ""
    fields.pop("registered_office", None)
    if not fields:
        return None
    if municipality:
        fields["birth_place_id"] = await get_or_create_place(session, province=province, municipality=municipality)
    return await get_or_create_subject(session, **fields)


async def _persist_projection(
    session: AsyncSession,
    request_id: str,
    tipo_catasto: str,
    data: Optional[dict],
    req_loc: Optional[CadastralLocation],
) -> None:
    """Write the properties, owners and result rows of a response, keeping each owner tied to its property.

    One ``visura_results`` row per property that has a result (or owners); the property and its owners both carry
    that ``result_id`` (the owner↔property views join on it), and ``owner_index`` keeps the portal's row order.
    """
    properties, unlinked, result_rows = _project_response(request_id, tipo_catasto, data)
    result_ids: dict[int, int] = {}
    for result_fields in result_rows:
        row = VisuraResult(response_id=request_id, **result_fields)
        session.add(row)
        await session.flush()
        result_ids[result_fields["result_index"]] = row.id

    for projected in properties:
        loc_fields = projected.location
        if req_loc:
            loc_fields["province"] = loc_fields["province"] or req_loc.province
            loc_fields["municipality"] = loc_fields["municipality"] or req_loc.municipality
        location_id = await get_or_create_location(session, **loc_fields)
        subject_id = await _resolve_subject(session, projected.subject) if projected.subject else None
        result_id = result_ids.get(projected.result_index) if projected.result_index is not None else None
        session.add(VisuraProperty(**projected.prop, location_id=location_id, subject_id=subject_id, result_id=result_id))
        for owner_index, (subject_fields, right_fields) in enumerate(projected.owners, start=1):
            session.add(
                VisuraOwner(
                    response_id=request_id,
                    result_id=result_id,
                    owner_index=owner_index,
                    subject_id=await _resolve_subject(session, subject_fields) if subject_fields else None,
                    right_id=await get_or_create_right(session, **right_fields) if right_fields else None,
                )
            )
    for owner_index, (subject_fields, right_fields) in enumerate(unlinked, start=1):
        session.add(
            VisuraOwner(
                response_id=request_id,
                owner_index=owner_index,
                subject_id=await _resolve_subject(session, subject_fields) if subject_fields else None,
                right_id=await get_or_create_right(session, **right_fields) if right_fields else None,
            )
        )
    await session.flush()


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
        # Delete existing response + related rows if any (upsert)
        await session.execute(
            text(
                "DELETE FROM page_visit_form_elements WHERE page_visit_id IN "
                "(SELECT id FROM page_visits WHERE response_id = :rid)"
            ),
            {"rid": request_id},
        )
        await session.execute(
            text(
                "DELETE FROM page_visit_errors WHERE page_visit_id IN "
                "(SELECT id FROM page_visits WHERE response_id = :rid)"
            ),
            {"rid": request_id},
        )
        await session.execute(text("DELETE FROM page_visits WHERE response_id = :rid"), {"rid": request_id})
        resp = await session.get(VisuraResponse, request_id)
        if resp is None:
            resp = VisuraResponse(request_id=request_id, success=success, cadastre_type=tipo_catasto)
            session.add(resp)
        resp.success = success
        resp.cadastre_type = tipo_catasto
        resp.data = data
        resp.error = error
        summary = _response_summary(data)
        resp.total_results = summary.get("total_results")
        resp.total_intestati = summary.get("total_intestati")
        resp.skipped_soppresso = summary.get("skipped_soppresso")
        resp.subject_query = summary.get("subject_query")
        resp.created_at = datetime.now(timezone.utc)
        await session.flush()

        await _replace_projection(session, request_id, tipo_catasto, data)
        await session.flush()
        raw_visits = (data or {}).get("page_visits", []) if isinstance(data, dict) else []
        for source_visit in raw_visits if isinstance(raw_visits, list) else []:
            if not isinstance(source_visit, dict):
                continue
            parsed_visits = _parse_page_visits(request_id, {"page_visits": [source_visit]})
            if not parsed_visits:
                continue
            pv = parsed_visits[0]
            session.add(pv)
            await session.flush()
            for element_index, element in enumerate(source_visit.get("form_elements", []) or []):
                if isinstance(element, dict):
                    session.add(
                        PageVisitFormElement(
                            page_visit_id=pv.id,
                            element_index=element_index,
                            tag=str(element.get("tag") or ""),
                            element_type=str(element.get("type") or ""),
                            name=str(element.get("name") or ""),
                            label=str(element.get("label") or ""),
                            value=str(element.get("value") or ""),
                        )
                    )
            for error_index, message in enumerate(source_visit.get("errors", []) or []):
                if isinstance(message, dict):
                    message = message.get("message") or message.get("text") or ""
                session.add(PageVisitError(page_visit_id=pv.id, error_index=error_index, message=str(message or "")))

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


async def get_response(request_id: str) -> Optional[dict]:
    """Fetch a stored response by request_id. Returns None if not found."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        row = await session.get(VisuraResponse, request_id)
        if row is None:
            return None
        return {
            "request_id": row.request_id,
            "success": row.success,
            "tipo_catasto": row.cadastre_type,
            "data": row.data,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
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
            select(VisuraRequest, VisuraResponse, CadastralLocation)
            .outerjoin(VisuraResponse, VisuraRequest.request_id == VisuraResponse.request_id)
            .outerjoin(CadastralLocation, VisuraRequest.location_id == CadastralLocation.id)
            .where(VisuraRequest.request_id == request_id)
        )
        result = await session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None

        req, resp, loc = row
        status = "pending"
        if resp is not None:
            status = "completed" if resp.success else "failed"

        return {
            "request_id": req.request_id,
            "request_type": req.request_type,
            "tipo_catasto": loc.cadastre_type if loc else "",
            "provincia": loc.province if loc else "",
            "comune": loc.municipality if loc else "",
            "foglio": loc.sheet if loc else "",
            "particella": loc.parcel if loc else "",
            "sezione": loc.section if loc else None,
            "subalterno": loc.subunit if loc else None,
            "cost_text": req.cost_text,
            "cost_value": req.cost_value,
            "requested_at": req.created_at.isoformat() if req.created_at else None,
            "responded_at": resp.created_at.isoformat() if resp and resp.created_at else None,
            "success": resp.success if resp else None,
            "status": status,
            "data": resp.data if resp else None,
            "error": resp.error if resp else None,
            "page_visits": (
                resp.data.get("page_visits", [])
                if resp and isinstance(resp.data, dict) and isinstance(resp.data.get("page_visits"), list)
                else []
            ),
        }


async def get_db_properties_for_response(request_id: str) -> list[dict]:
    """Return visura_properties rows with location and subject joins for a response."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(VisuraProperty, CadastralLocation, CadastralSubject)
            .outerjoin(CadastralLocation, VisuraProperty.location_id == CadastralLocation.id)
            .outerjoin(CadastralSubject, VisuraProperty.subject_id == CadastralSubject.id)
            .where(VisuraProperty.response_id == request_id)
            .order_by(VisuraProperty.id)
        )
        result = await session.execute(stmt)
        rows = result.all()
    return [
        {
            "result_id": prop.result_id,
            "property_type": prop.property_type,
            "address": prop.address,
            "partita": prop.partita,
            "category": prop.category,
            "cadastral_class": prop.cadastral_class,
            "consistency": prop.consistency,
            "income": prop.income,
            "census_zone": prop.census_zone,
            "quality": prop.quality,
            "area": prop.area,
            "dominical_income": prop.dominical_income,
            "agricultural_income": prop.agricultural_income,
            "registered_office": prop.registered_office,
            "subject_province": prop.province,
            "subject_municipality": prop.municipality,
            "province": loc.province if loc else None,
            "municipality": loc.municipality if loc else None,
            "sheet": loc.sheet if loc else None,
            "parcel": loc.parcel if loc else None,
            "subunit": loc.subunit if loc else None,
            "section": loc.section if loc else None,
            "cadastre_type": loc.cadastre_type if loc else None,
            "subject_name": subj.display_name if subj else None,
            "subject_fiscal_code": subj.fiscal_code if subj else None,
        }
        for prop, loc, subj in rows
    ]


async def get_db_owners_for_response(request_id: str) -> list[dict]:
    """Return visura_owners rows with subject and right joins for a response."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(VisuraOwner, CadastralSubject, OwnershipRight)
            .outerjoin(CadastralSubject, VisuraOwner.subject_id == CadastralSubject.id)
            .outerjoin(OwnershipRight, VisuraOwner.right_id == OwnershipRight.id)
            .where(VisuraOwner.response_id == request_id)
            .order_by(VisuraOwner.id)
        )
        result = await session.execute(stmt)
        rows = result.all()
    return [
        {
            "nominativo": subj.display_name or (
                f"{subj.last_name or ''} {subj.first_name or ''}".strip() if subj else None
            ),
            "fiscal_code": subj.fiscal_code if subj else None,
            "gender": subj.gender if subj else None,
            "date_of_birth": subj.date_of_birth if subj else None,
            "subject_type": subj.subject_type if subj else None,
            "result_id": owner.result_id,
            "owner_index": owner.owner_index,
            "right_type": right.right_type if right else None,
            "ownership_share": right.ownership_share if right else None,
            "right_code": right.right_code if right else None,
            "right_description": right.right_description if right else None,
            "start_date": right.start_date if right else None,
            "end_date": right.end_date if right else None,
        }
        for owner, subj, right in rows
    ]


async def get_documents_for_response(request_id: str, foglio: str = None, particella: str = None) -> list[dict]:
    """Fetch visura_documents linked to a response_id OR matching foglio/particella."""
    from sqlalchemy import or_

    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(VisuraDocument, DocumentMetadata, CadastralLocation)
            .outerjoin(DocumentMetadata, VisuraDocument.id == DocumentMetadata.id)
            .outerjoin(CadastralLocation, DocumentMetadata.location_id == CadastralLocation.id)
            .order_by(VisuraDocument.created_at.desc())
        )
        if foglio and particella:
            stmt = stmt.where(
                or_(
                    VisuraDocument.response_id == request_id,
                    (CadastralLocation.sheet == foglio) & (CadastralLocation.parcel == particella),
                )
            )
        else:
            stmt = stmt.where(VisuraDocument.response_id == request_id)
        result = await session.execute(stmt)
        rows = result.all()
    docs = []
    for doc_row, meta, loc in rows:
        docs.append({
            "id": doc_row.id,
            "response_id": doc_row.response_id,
            "document_type": (
                "ispezione_ipotecaria"
                if (doc_row.filename or "").casefold().startswith("isp_")
                else doc_row.document_type
            ),
            "file_format": doc_row.file_format,
            "filename": doc_row.filename,
            "file_path": doc_row.file_path,
            "file_size": doc_row.file_size,
            "oggetto": doc_row.subject,
            "richiesta_del": doc_row.requested_at,
            "provincia": loc.province if loc else None,
            "comune": loc.municipality if loc else None,
            "foglio": loc.sheet if loc else None,
            "particella": loc.parcel if loc else None,
            "subalterno": loc.subunit if loc else None,
            "sezione_urbana": loc.section if loc else None,
            "tipo_catasto": loc.cadastre_type if loc else None,
            "visura_subtype": meta.view_subtype if meta else None,
            "situazione_al": meta.reference_date if meta else None,
            "xml_content": (meta.content or "") if meta else "",
            "created_at": doc_row.created_at.isoformat() if doc_row.created_at else None,
        })
    return docs


async def get_document_by_id(doc_id: int) -> dict | None:
    """Fetch a single visura_document by primary key."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(VisuraDocument, DocumentMetadata, CadastralLocation)
            .outerjoin(DocumentMetadata, VisuraDocument.id == DocumentMetadata.id)
            .outerjoin(CadastralLocation, DocumentMetadata.location_id == CadastralLocation.id)
            .where(VisuraDocument.id == doc_id)
        )
        row = result.one_or_none()
    if row is None:
        return None
    doc_row, meta, loc = row
    return {
        "id": doc_row.id,
        "document_type": (
            "ispezione_ipotecaria"
            if (doc_row.filename or "").casefold().startswith("isp_")
            else doc_row.document_type
        ),
        "file_format": doc_row.file_format,
        "filename": doc_row.filename,
        "file_path": doc_row.file_path,
        "file_size": doc_row.file_size,
        "oggetto": doc_row.subject,
        "richiesta_del": doc_row.requested_at,
        "provincia": loc.province if loc else None,
        "comune": loc.municipality if loc else None,
        "foglio": loc.sheet if loc else None,
        "particella": loc.parcel if loc else None,
        "subalterno": loc.subunit if loc else None,
        "sezione_urbana": loc.section if loc else None,
        "tipo_catasto": loc.cadastre_type if loc else None,
        "xml_content": (meta.content or "") if meta else "",
        "created_at": doc_row.created_at.isoformat() if doc_row.created_at else None,
    }


async def get_document_structured_extraction(doc_id: int) -> dict | None:
    """Fetch the latest archived structured payload and its normalized rows."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(StructuredDocumentExtraction)
            .where(StructuredDocumentExtraction.document_id == doc_id)
            .order_by(StructuredDocumentExtraction.created_at.desc(), StructuredDocumentExtraction.id.desc())
            .limit(1)
        )
        extraction = result.scalar_one_or_none()
        if extraction is None:
            return None

        extraction_id = extraction.id
        inspection = (await session.execute(
            select(MortgageInspection).where(MortgageInspection.extraction_id == extraction_id)
        )).scalar_one_or_none()
        title = (await session.execute(
            select(MortgageInspectionTitle).where(MortgageInspectionTitle.extraction_id == extraction_id)
        )).scalar_one_or_none()
        lien = (await session.execute(
            select(MortgageInspectionLien).where(MortgageInspectionLien.extraction_id == extraction_id)
        )).scalar_one_or_none()
        units = (await session.execute(
            select(MortgageInspectionUnit)
            .where(MortgageInspectionUnit.extraction_id == extraction_id)
            .order_by(MortgageInspectionUnit.unit_number, MortgageInspectionUnit.id)
        )).scalars().all()
        parties = (await session.execute(
            select(MortgageInspectionParty)
            .where(MortgageInspectionParty.extraction_id == extraction_id)
            .order_by(MortgageInspectionParty.role, MortgageInspectionParty.ordinal)
        )).scalars().all()
        unit_ids = [unit.id for unit in units]
        properties = []
        if unit_ids:
            properties = (await session.execute(
                select(MortgageInspectionProperty)
                .where(MortgageInspectionProperty.unit_id.in_(unit_ids))
                .order_by(MortgageInspectionProperty.unit_id, MortgageInspectionProperty.property_number)
            )).scalars().all()

        def record(row):
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}

        properties_by_unit: dict[int, list[dict]] = {}
        for property_row in properties:
            properties_by_unit.setdefault(property_row.unit_id, []).append(record(property_row))

        return {
            "extraction": record(extraction),
            "inspection": record(inspection) if inspection else None,
            "title": record(title) if title else None,
            "lien": record(lien) if lien else None,
            "units": [
                {**record(unit), "properties": properties_by_unit.get(unit.id, [])}
                for unit in units
            ],
            "parties": [record(party) for party in parties],
        }


async def get_indexed_file_paths() -> dict[str, int]:
    """Return a mapping of file_path → document id for all indexed documents."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(VisuraDocument.file_path, VisuraDocument.id).where(VisuraDocument.file_path.isnot(None))
        )
        return {row.file_path: row.id for row in result}


async def get_indexed_filenames() -> set[str]:
    """Return the set of filenames already indexed (basename only)."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(select(VisuraDocument.filename).where(VisuraDocument.filename.isnot(None)))
        return {row.filename for row in result}


async def get_indexed_file_metadata() -> dict[str, dict]:
    """Return {file_path: {"id": doc_id, "oggetto": new_name}} for all indexed documents."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(VisuraDocument.file_path, VisuraDocument.id, VisuraDocument.subject).where(
                VisuraDocument.file_path.isnot(None)
            )
        )
        return {row.file_path: {"id": row.id, "oggetto": row.subject or ""} for row in result}


async def get_all_documents(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch all visura_documents (for browse page)."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(VisuraDocument, DocumentMetadata, CadastralLocation)
            .outerjoin(DocumentMetadata, VisuraDocument.id == DocumentMetadata.id)
            .outerjoin(CadastralLocation, DocumentMetadata.location_id == CadastralLocation.id)
            .order_by(VisuraDocument.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        rows = result.all()
    docs = []
    for doc_row, meta, loc in rows:
        docs.append({
            "id": doc_row.id,
            "response_id": doc_row.response_id,
            "document_type": (
                "ispezione_ipotecaria"
                if (doc_row.filename or "").casefold().startswith("isp_")
                else doc_row.document_type
            ),
            "file_format": doc_row.file_format,
            "filename": doc_row.filename,
            "file_size": doc_row.file_size,
            "oggetto": doc_row.subject,
            "richiesta_del": doc_row.requested_at,
            "sezione_urbana": loc.section if loc else None,
            "provincia": loc.province if loc else None,
            "comune": loc.municipality if loc else None,
            "foglio": loc.sheet if loc else None,
            "particella": loc.parcel if loc else None,
            "subalterno": loc.subunit if loc else None,
            "tipo_catasto": loc.cadastre_type if loc else None,
            "visura_subtype": meta.view_subtype if meta else None,
            "situazione_al": meta.reference_date if meta else None,
            "created_at": doc_row.created_at.isoformat() if doc_row.created_at else None,
        })
    return docs


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
            select(VisuraRequest, VisuraResponse, CadastralLocation)
            .outerjoin(VisuraResponse, VisuraRequest.request_id == VisuraResponse.request_id)
            .outerjoin(CadastralLocation, VisuraRequest.location_id == CadastralLocation.id)
        )
        if provincia:
            stmt = stmt.where(CadastralLocation.province == provincia)
        if comune:
            stmt = stmt.where(CadastralLocation.municipality == comune)
        if foglio:
            stmt = stmt.where(CadastralLocation.sheet == foglio)
        if particella:
            stmt = stmt.where(CadastralLocation.parcel == particella)
        if tipo_catasto:
            stmt = stmt.where(CadastralLocation.cadastre_type == tipo_catasto)

        stmt = stmt.order_by(VisuraRequest.created_at.desc()).limit(limit).offset(offset)

        result = await session.execute(stmt)
        rows = result.all()

        return [
            {
                "request_id": req.request_id,
                "request_type": req.request_type,
                "tipo_catasto": loc.cadastre_type if loc else "",
                "provincia": loc.province if loc else "",
                "comune": loc.municipality if loc else "",
                "foglio": loc.sheet if loc else "",
                "particella": loc.parcel if loc else "",
                "sezione": loc.section if loc else None,
                "subalterno": loc.subunit if loc else None,
                "requested_at": req.created_at.isoformat() if req.created_at else None,
                "success": resp.success if resp else None,
                "data": resp.data if resp else None,
                "error": resp.error if resp else None,
                "responded_at": resp.created_at.isoformat() if resp and resp.created_at else None,
            }
            for req, resp, loc in rows
        ]


def _single_result_status(success: Optional[bool]) -> str:
    if success is True:
        return "completed"
    if success is False:
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
    if status not in {"completed", "partial", "failed", "error", "pending", "running"}:
        status = None

    single_rows: list[dict] = []
    if source in (None, "single"):
        property_count = (
            select(func.count(VisuraProperty.id))
            .where(VisuraProperty.response_id == VisuraRequest.request_id)
            .correlate(VisuraRequest)
            .scalar_subquery()
        )
        owner_count = (
            select(func.count(VisuraOwner.id))
            .where(VisuraOwner.response_id == VisuraRequest.request_id)
            .correlate(VisuraRequest)
            .scalar_subquery()
        )
        stmt = (
            select(
                VisuraRequest.request_id.label("request_id"),
                VisuraRequest.request_type.label("request_type"),
                CadastralLocation.cadastre_type.label("cadastre_type"),
                CadastralLocation.province.label("province"),
                CadastralLocation.municipality.label("municipality"),
                CadastralLocation.sheet.label("sheet"),
                CadastralLocation.parcel.label("parcel"),
                CadastralLocation.section.label("section"),
                CadastralLocation.subunit.label("subunit"),
                VisuraRequest.created_at.label("requested_at"),
                VisuraResponse.success.label("success"),
                VisuraResponse.error.label("error"),
                VisuraResponse.created_at.label("responded_at"),
                VisuraResponse.total_results.label("total_results"),
                VisuraResponse.total_intestati.label("total_intestati"),
                property_count.label("property_count"),
                owner_count.label("owner_count"),
            )
            .select_from(VisuraRequest)
            .outerjoin(VisuraResponse, VisuraRequest.request_id == VisuraResponse.request_id)
            .outerjoin(CadastralLocation, VisuraRequest.location_id == CadastralLocation.id)
            .where(
                *_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status)
            )
            .order_by(VisuraRequest.created_at.desc())
        )
        async with _get_session_factory()() as session:
            rows = (await session.execute(stmt)).mappings().all()
        for row in rows:
            success = bool(row["success"]) if row["success"] is not None else None
            single_rows.append(
                {
                    "request_id": row["request_id"],
                    "request_type": row["request_type"],
                    "source": "single",
                    "tipo_catasto": row["cadastre_type"],
                    "provincia": row["province"],
                    "comune": row["municipality"],
                    "foglio": row["sheet"],
                    "particella": row["parcel"],
                    "sezione": row["section"],
                    "subalterno": row["subunit"],
                    "requested_at": row["requested_at"],
                    "success": success,
                    "status": _single_result_status(success),
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


async def cleanup_old_responses(ttl_seconds: int) -> int:
    """Delete responses older than ttl_seconds. Returns count of deleted rows."""
    if not is_db_writable():
        return 0
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)

        expired_ids = (
            await session.execute(
                select(VisuraResponse.request_id).where(VisuraResponse.created_at < cutoff)
            )
        ).scalars().all()
        deleted = len(expired_ids)

        if expired_ids:
            # Ordered bulk deletes avoid ORM autoflush trying to delete a
            # response before its unmapped result rows have been removed.
            page_visit_ids = select(PageVisit.id).where(PageVisit.response_id.in_(expired_ids))
            document_ids = select(VisuraDocument.id).where(VisuraDocument.response_id.in_(expired_ids))
            xml_nodes = table("document_xml_nodes", column("id"), column("document_id"))
            xml_attributes = table("document_xml_attributes", column("node_id"))
            await session.execute(
                delete(PageVisitFormElement).where(PageVisitFormElement.page_visit_id.in_(page_visit_ids))
            )
            await session.execute(
                delete(PageVisitError).where(PageVisitError.page_visit_id.in_(page_visit_ids))
            )
            await session.execute(delete(PageVisit).where(PageVisit.response_id.in_(expired_ids)))
            await session.execute(
                delete(xml_attributes).where(
                    xml_attributes.c.node_id.in_(
                        select(xml_nodes.c.id).where(xml_nodes.c.document_id.in_(document_ids))
                    )
                )
            )
            await session.execute(delete(xml_nodes).where(xml_nodes.c.document_id.in_(document_ids)))
            await session.execute(delete(DocumentMetadata).where(DocumentMetadata.id.in_(document_ids)))
            await session.execute(delete(VisuraDocument).where(VisuraDocument.response_id.in_(expired_ids)))
            await session.execute(delete(VisuraProperty).where(VisuraProperty.response_id.in_(expired_ids)))
            await session.execute(delete(VisuraOwner).where(VisuraOwner.response_id.in_(expired_ids)))
            await session.execute(delete(VisuraResult).where(VisuraResult.response_id.in_(expired_ids)))
            await session.execute(delete(VisuraResponse).where(VisuraResponse.request_id.in_(expired_ids)))

            orphan_ids = (
                await session.execute(
                    select(VisuraRequest.request_id).where(
                        VisuraRequest.created_at < cutoff,
                        ~VisuraRequest.request_id.in_(select(VisuraResponse.request_id)),
                    )
                )
            ).scalars().all()
            if orphan_ids:
                await session.execute(delete(VisuraRequest).where(VisuraRequest.request_id.in_(orphan_ids)))

        await session.commit()
        return deleted


async def count_responses() -> dict:
    """Return basic stats about stored data."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        total_requests = (await session.execute(select(text("count(*)")).select_from(VisuraRequest))).scalar() or 0

        total_responses = (await session.execute(select(text("count(*)")).select_from(VisuraResponse))).scalar() or 0

        successful = (
            await session.execute(
                select(text("count(*)"))
                .select_from(VisuraResponse)
                .where(VisuraResponse.success == True)  # noqa: E712
            )
        ).scalar() or 0

        failed = (
            await session.execute(
                select(text("count(*)"))
                .select_from(VisuraResponse)
                .where(VisuraResponse.success == False)  # noqa: E712
            )
        ).scalar() or 0

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
    """Build portable ORM filters for single-result queries."""
    conditions = []
    if provincia:
        conditions.append(CadastralLocation.province == provincia)
    if comune:
        conditions.append(CadastralLocation.municipality == comune)
    if foglio:
        conditions.append(CadastralLocation.sheet == str(foglio))
    if particella:
        conditions.append(CadastralLocation.parcel == str(particella))
    if tipo_catasto:
        conditions.append(CadastralLocation.cadastre_type == tipo_catasto)
    if status == "completed":
        conditions.append(VisuraResponse.success.is_(True))
    elif status in ("failed", "error"):
        conditions.append(VisuraResponse.success.is_(False))
    elif status == "pending":
        conditions.append(VisuraResponse.request_id.is_(None))
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
    if status not in {"completed", "partial", "failed", "error", "pending", "running"}:
        status = None

    if source not in (None, "single"):
        return 0
    stmt = (
        select(func.count())
        .select_from(VisuraRequest)
        .outerjoin(VisuraResponse, VisuraRequest.request_id == VisuraResponse.request_id)
        .outerjoin(CadastralLocation, VisuraRequest.location_id == CadastralLocation.id)
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

    s_total = s_ok = s_fail = s_pending = 0
    if source in (None, "single"):
        filters = _build_single_where(provincia, comune, foglio, particella, tipo_catasto)
        stmt = select(
            func.count().label("total"),
            func.count().filter(VisuraResponse.success.is_(True)).label("successful"),
            func.count().filter(VisuraResponse.success.is_(False)).label("failed"),
            func.count().filter(VisuraResponse.request_id.is_(None)).label("pending"),
        ).select_from(VisuraRequest).outerjoin(
            VisuraResponse, VisuraRequest.request_id == VisuraResponse.request_id
        ).outerjoin(CadastralLocation, VisuraRequest.location_id == CadastralLocation.id).where(*filters)
        async with _get_session_factory()() as session:
            counts = (await session.execute(stmt)).one()
        s_total, s_ok, s_fail, s_pending = counts

    return {
        "total_requests": s_total,
        "total_responses": s_total - s_pending,
        "successful": s_ok,
        "failed": s_fail,
        "partial": 0,
        "pending": s_pending,
    }
