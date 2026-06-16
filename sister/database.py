"""SQLite database layer for sister (SQLModel + async SQLAlchemy).

Provides persistent storage for visura requests, responses, and structured
result tables (immobili, intestati). Includes cache lookup for deduplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from .cadastral import (
    CadastralInspection,
    CadastralLegalEntitySearchEntity,
    CadastralLegalEntitySearchGeoSummary,
    CadastralLegalEntitySearchParameter,
    CadastralLegalEntitySearchProperty,
    CadastralLocationParameters,
    CadastralProspectOwner,
    CadastralProspectProperty,
    CadastralPropertyProperty,
    CadastralQuery,
)
from .db_models import (
    OWNER_RIGHT_FIELD_MAP,
    OWNER_SUBJECT_FIELD_MAP,
    PROPERTY_FIELD_MAP,
    PROPERTY_LOCATION_FIELD_MAP,
    CadastralLocation,
    CadastralSubject,
    DocumentMetadata,
    FeedbackConfig,
    FeedbackUnsubscribe,
    OwnershipRight,
    PageVisit,
    VisuraDocument,
    VisuraOwner,
    VisuraProperty,
    VisuraRequest,
    VisuraResponse,
)
from .visura_xml_models import (
    BuildingAddress,
    BuildingClassification,
    BuildingCurrentState,
    BuildingIdentifier,
    BuildingSurface,
    BuildingUnit,
    DocumentSubject,
    LandClassification,
    LandParcel,
    OwnershipMutation,
    PropertyGroup,
    PropertyOwner,
    RelatedParcel,
)

# Collect only sister's tables — avoid creating tables from other packages
# that share the global SQLModel.metadata.
# workflow_runs / workflow_steps are intentionally excluded: they are owned and
# created by the opendata project (see opendata/models/workflow.py and the
# corresponding Alembic migration). Sister queries them via raw SQL but does
# not define or create the schema.
_SISTER_TABLES = [
    CadastralLocation.__table__,  # no FK deps — must be first
    CadastralSubject.__table__,
    OwnershipRight.__table__,
    VisuraRequest.__table__,
    VisuraResponse.__table__,
    VisuraProperty.__table__,
    VisuraOwner.__table__,
    PageVisit.__table__,
    VisuraDocument.__table__,
    DocumentMetadata.__table__,
    DocumentSubject.__table__,
    PropertyGroup.__table__,
    BuildingUnit.__table__,
    BuildingCurrentState.__table__,
    BuildingIdentifier.__table__,
    BuildingClassification.__table__,
    BuildingSurface.__table__,
    BuildingAddress.__table__,
    RelatedParcel.__table__,
    LandParcel.__table__,
    LandClassification.__table__,
    OwnershipMutation.__table__,
    PropertyOwner.__table__,
    FeedbackConfig.__table__,
    FeedbackUnsubscribe.__table__,
    CadastralQuery.__table__,
    CadastralInspection.__table__,
    CadastralLocationParameters.__table__,
    CadastralPropertyProperty.__table__,
    CadastralProspectProperty.__table__,
    CadastralProspectOwner.__table__,
    CadastralLegalEntitySearchParameter.__table__,
    CadastralLegalEntitySearchEntity.__table__,
    CadastralLegalEntitySearchGeoSummary.__table__,
    CadastralLegalEntitySearchProperty.__table__,
]

logger = logging.getLogger("sister")

DB_PATH = os.getenv("SISTER_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sister.sqlite"))

# ---------------------------------------------------------------------------
# Engine and session
# ---------------------------------------------------------------------------

_engine = None
_db_writable: Optional[bool] = None


def is_db_writable() -> bool:
    """Check if the database file is writable. Cached after first call."""
    global _db_writable
    if _db_writable is not None:
        return _db_writable
    db_path = Path(DB_PATH)
    if not db_path.exists():
        _db_writable = os.access(str(db_path.parent), os.W_OK)
    else:
        _db_writable = os.access(str(db_path), os.W_OK)
    if not _db_writable:
        logger.warning("Database is read-only: %s — write operations will be skipped", DB_PATH)
    return _db_writable


def _get_engine():
    global _engine
    if _engine is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        url = f"sqlite+aiosqlite:///{DB_PATH}"
        _engine = create_async_engine(url, echo=False)
    return _engine


def _get_session_factory():
    return sessionmaker(_get_engine(), class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create sister tables if they don't exist."""
    engine = _get_engine()
    writable = is_db_writable()
    async with engine.begin() as conn:
        if writable:

            def _create_sister_tables(sync_conn):
                for table in _SISTER_TABLES:
                    table.create(sync_conn, checkfirst=True)

            await conn.run_sync(_create_sister_tables)
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    logger.info("Database inizializzato: %s (writable=%s)", DB_PATH, writable)


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
        cutoff = datetime.now() - timedelta(seconds=ttl_seconds)
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


async def get_or_create_subject(
    session: AsyncSession,
    fiscal_code: Optional[str] = None,
    display_name: Optional[str] = None,
    last_name: Optional[str] = None,
    first_name: Optional[str] = None,
    gender: Optional[str] = None,
    date_of_birth: Optional[str] = None,
    subject_type: Optional[str] = None,
) -> int:
    """Get existing CadastralSubject by fiscal_code (when present) or create one; return its id."""
    if fiscal_code:
        result = await session.execute(select(CadastralSubject).where(CadastralSubject.fiscal_code == fiscal_code))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing.id
    subj = CadastralSubject(
        fiscal_code=fiscal_code,
        display_name=display_name,
        last_name=last_name,
        first_name=first_name,
        gender=gender,
        date_of_birth=date_of_birth,
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

    Matches NULL fields explicitly (SQLite does not deduplicate NULLs via UNIQUE constraints).
    """
    conditions = []
    for col, val in [
        (OwnershipRight.right_type, right_type),
        (OwnershipRight.right_code, right_code),
        (OwnershipRight.right_description, right_description),
        (OwnershipRight.ownership_share, ownership_share),
        (OwnershipRight.start_date, start_date),
        (OwnershipRight.end_date, end_date),
    ]:
        conditions.append(col.is_(None) if val is None else col == val)
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
        row = VisuraRequest(
            request_id=request_id,
            request_type=request_type,
            location_id=location_id,
            cache_key=cache_key,
        )
        session.add(row)
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
            row = VisuraRequest(
                request_id=req["request_id"],
                request_type=req["request_type"],
                location_id=location_id,
                cache_key=req.get("cache_key"),
            )
            session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# Response operations
# ---------------------------------------------------------------------------


def _parse_property_rows(
    response_id: str, tipo_catasto: str, data: Optional[dict]
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """Parse properties from response JSON.

    Returns (property_fields, location_fields) pairs.  Location fields are
    resolved to a CadastralLocation id by the caller (save_response).
    """
    if not data or not isinstance(data, dict):
        return []
    _CATASTO_TYPE = {"F": "building", "T": "land", "E": "entity"}
    rows = []
    for item in data.get("immobili", []):
        if not isinstance(item, dict):
            continue
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
        for html_key, db_col in PROPERTY_FIELD_MAP.items():
            if html_key in item:
                prop_fields[db_col] = str(item[html_key]).strip() or None
        for html_key, loc_col in PROPERTY_LOCATION_FIELD_MAP.items():
            if html_key in item:
                loc_fields[loc_col] = str(item[html_key]).strip()
        rows.append((prop_fields, loc_fields))
    return rows


def _parse_owners(response_id: str, data: Optional[dict]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Parse owners from response JSON into (subject_fields, right_fields) pairs."""
    if not data or not isinstance(data, dict):
        return []
    rows = []
    for item in data.get("intestati", []):
        if not isinstance(item, dict):
            continue
        subject_fields: dict[str, Any] = {}
        right_fields: dict[str, Any] = {}
        for html_key, db_col in OWNER_SUBJECT_FIELD_MAP.items():
            if html_key in item:
                subject_fields[db_col] = str(item[html_key]).strip() or None
        for html_key, db_col in OWNER_RIGHT_FIELD_MAP.items():
            if html_key in item:
                right_fields[db_col] = str(item[html_key]).strip() or None
        rows.append((subject_fields, right_fields))
    return rows


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


async def save_response(
    request_id: str,
    success: bool,
    tipo_catasto: str,
    data: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Persist a response and populate structured tables."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        # Delete existing response + related rows if any (upsert)
        await session.execute(text("DELETE FROM page_visits WHERE response_id = :rid"), {"rid": request_id})
        await session.execute(text("DELETE FROM visura_owners WHERE response_id = :rid"), {"rid": request_id})
        await session.execute(text("DELETE FROM visura_properties WHERE response_id = :rid"), {"rid": request_id})
        await session.execute(text("DELETE FROM visura_responses WHERE request_id = :rid"), {"rid": request_id})

        resp = VisuraResponse(
            request_id=request_id,
            success=success,
            cadastre_type=tipo_catasto,
            data=data,
            error=error,
        )
        session.add(resp)

        # Look up request location to inherit province/municipality for property locations
        req_row = await session.get(VisuraRequest, request_id)
        req_loc: Optional[CadastralLocation] = None
        if req_row and req_row.location_id:
            req_loc = await session.get(CadastralLocation, req_row.location_id)

        # Populate structured tables from JSON
        for prop_fields, loc_fields in _parse_property_rows(request_id, tipo_catasto, data):
            if req_loc:
                loc_fields["province"] = loc_fields["province"] or req_loc.province
                loc_fields["municipality"] = loc_fields["municipality"] or req_loc.municipality
            location_id = await get_or_create_location(session, **loc_fields)
            session.add(VisuraProperty(**prop_fields, location_id=location_id))
        for subject_fields, right_fields in _parse_owners(request_id, data):
            subject_id = await get_or_create_subject(session, **subject_fields) if subject_fields else None
            right_id = await get_or_create_right(session, **right_fields) if right_fields else None
            session.add(VisuraOwner(response_id=request_id, subject_id=subject_id, right_id=right_id))
        for pv in _parse_page_visits(request_id, data):
            session.add(pv)

        await session.commit()

    # Export to outputs/ directory
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
            "right_type": right.right_type if right else None,
            "ownership_share": right.ownership_share if right else None,
            "right_code": right.right_code if right else None,
            "right_description": right.right_description if right else None,
            "start_date": right.start_date if right else None,
            "end_date": right.end_date if right else None,
        }
        for _, subj, right in rows
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
            "document_type": doc_row.document_type,
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
        "document_type": doc_row.document_type,
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
            "document_type": doc_row.document_type,
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
    if not os.path.exists(DB_PATH):
        return []

    if source not in {"single", "workflow"}:
        source = None
    if status not in {"completed", "partial", "failed", "error", "pending", "running"}:
        status = None

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        single_rows: list[dict] = []
        if source in (None, "single"):
            where_clause, params = _build_single_where(
                provincia,
                comune,
                foglio,
                particella,
                tipo_catasto,
                status,
            )
            sql = """
                SELECT
                    req.request_id,
                    req.request_type,
                    loc.cadastre_type,
                    loc.province,
                    loc.municipality,
                    loc.sheet,
                    loc.parcel,
                    loc.section,
                    loc.subunit,
                    req.created_at AS requested_at,
                    resp.success,
                    resp.error,
                    resp.created_at AS responded_at,
                    (SELECT COUNT(*) FROM visura_properties WHERE response_id = req.request_id) AS property_count,
                    (SELECT COUNT(*) FROM visura_owners WHERE response_id = req.request_id) AS owner_count
                FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
                LEFT JOIN cadastral_locations AS loc ON req.location_id = loc.id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            for row in conn.execute(sql, params).fetchall():
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
                        "property_count": row["property_count"] or 0,
                        "owner_count": row["owner_count"] or 0,
                    }
                )

    # workflow_runs no longer live in sister's DB — owned by opendata
    rows = list(single_rows)
    rows.sort(key=lambda row: row.get("requested_at") or "", reverse=True)
    return rows[offset : offset + limit]


async def cleanup_old_responses(ttl_seconds: int) -> int:
    """Delete responses older than ttl_seconds. Returns count of deleted rows."""
    if not is_db_writable():
        return 0
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cutoff = datetime.now() - timedelta(seconds=ttl_seconds)

        stmt = select(VisuraResponse).where(VisuraResponse.created_at < cutoff)
        result = await session.execute(stmt)
        expired = result.scalars().all()
        deleted = len(expired)

        for resp in expired:
            await session.delete(resp)

        if deleted:
            orphan_stmt = select(VisuraRequest).where(
                VisuraRequest.created_at < cutoff,
                ~VisuraRequest.request_id.in_(select(VisuraResponse.request_id)),
            )
            orphan_result = await session.execute(orphan_stmt)
            for req in orphan_result.scalars().all():
                await session.delete(req)

        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
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
) -> tuple[str, list]:
    """Build WHERE clause + params for single-result queries on visura_requests/responses/cadastral_locations."""
    conditions: list[str] = []
    params: list = []
    if provincia:
        conditions.append("loc.province = ?")
        params.append(provincia)
    if comune:
        conditions.append("loc.municipality = ?")
        params.append(comune)
    if foglio:
        conditions.append("loc.sheet = ?")
        params.append(str(foglio))
    if particella:
        conditions.append("loc.parcel = ?")
        params.append(str(particella))
    if tipo_catasto:
        conditions.append("loc.cadastre_type = ?")
        params.append(tipo_catasto)
    if status == "completed":
        conditions.append("resp.success = 1")
    elif status in ("failed", "error"):
        conditions.append("resp.success = 0")
    elif status == "pending":
        conditions.append("resp.request_id IS NULL")
    return (" AND ".join(conditions), params)


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
    if not os.path.exists(DB_PATH):
        return 0

    if source not in {"single", "workflow"}:
        source = None
    if status not in {"completed", "partial", "failed", "error", "pending", "running"}:
        status = None

    total = 0
    with sqlite3.connect(DB_PATH) as conn:
        if source in (None, "single"):
            where_clause, params = _build_single_where(
                provincia,
                comune,
                foglio,
                particella,
                tipo_catasto,
                status,
            )
            sql = """
                SELECT count(*) FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
                LEFT JOIN cadastral_locations AS loc ON req.location_id = loc.id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            total += conn.execute(sql, params).fetchone()[0] or 0

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
    if not os.path.exists(DB_PATH):
        return {
            "total_requests": 0,
            "total_responses": 0,
            "successful": 0,
            "failed": 0,
            "partial": 0,
            "pending": 0,
        }

    if source not in {"single", "workflow"}:
        source = None

    with sqlite3.connect(DB_PATH) as conn:

        def _count_single(where_clause: str, params: list) -> int:
            sql = """
                SELECT count(*) FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
                LEFT JOIN cadastral_locations AS loc ON req.location_id = loc.id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            return conn.execute(sql, params).fetchone()[0] or 0

        # workflow_runs no longer in sister's DB — owned by opendata
        s_total = s_ok = s_fail = s_pending = 0
        if source in (None, "single"):
            base_where, base_params = _build_single_where(provincia, comune, foglio, particella, tipo_catasto)
            s_total = _count_single(base_where, base_params)
            s_ok = _count_single(
                *_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="completed")
            )
            s_fail = _count_single(
                *_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="failed")
            )
            s_pending = _count_single(
                *_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="pending")
            )

    return {
        "total_requests": s_total,
        "total_responses": s_total - s_pending,
        "successful": s_ok,
        "failed": s_fail,
        "partial": 0,
        "pending": s_pending,
    }
