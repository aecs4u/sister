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

from .db_models import (
    IMMOBILE_FIELD_MAP,
    INTESTATO_FIELD_MAP,
    ImmobileDB,
    IntestatoDB,
    PageVisitDB,
    VisuraDocumentDB,
    VisuraRequestDB,
    VisuraResponseDB,
    WorkflowRunDB,
    WorkflowStepDB,
)

# Collect only sister's tables — avoid creating tables from other packages
# that share the global SQLModel.metadata
_SISTER_TABLES = [
    VisuraRequestDB.__table__,
    VisuraResponseDB.__table__,
    ImmobileDB.__table__,
    IntestatoDB.__table__,
    WorkflowRunDB.__table__,
    WorkflowStepDB.__table__,
    PageVisitDB.__table__,
    VisuraDocumentDB.__table__,
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
            select(VisuraResponseDB)
            .join(VisuraRequestDB)
            .where(
                VisuraRequestDB.cache_key == cache_key,
                VisuraResponseDB.success == True,  # noqa: E712
                VisuraResponseDB.created_at >= cutoff,
            )
            .order_by(VisuraResponseDB.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "request_id": row.request_id,
            "success": row.success,
            "tipo_catasto": row.tipo_catasto,
            "data": row.data,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


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
        row = VisuraRequestDB(
            request_id=request_id,
            request_type=request_type,
            tipo_catasto=tipo_catasto,
            provincia=provincia,
            comune=comune,
            foglio=foglio,
            particella=particella,
            sezione=sezione,
            subalterno=subalterno,
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
            row = VisuraRequestDB(
                request_id=req["request_id"],
                request_type=req["request_type"],
                tipo_catasto=req["tipo_catasto"],
                provincia=req["provincia"],
                comune=req["comune"],
                foglio=req["foglio"],
                particella=req["particella"],
                sezione=req.get("sezione"),
                subalterno=req.get("subalterno"),
                cache_key=req.get("cache_key"),
            )
            session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# Response operations
# ---------------------------------------------------------------------------


def _parse_immobili(response_id: str, tipo_catasto: str, data: Optional[dict]) -> list[ImmobileDB]:
    """Parse immobili from response JSON into structured rows."""
    if not data or not isinstance(data, dict):
        return []
    rows = []
    for item in data.get("immobili", []):
        if not isinstance(item, dict):
            continue
        kwargs: dict[str, Any] = {"response_id": response_id, "tipo_catasto": tipo_catasto}
        for html_key, db_col in IMMOBILE_FIELD_MAP.items():
            if html_key in item:
                kwargs[db_col] = str(item[html_key]).strip() or None
        rows.append(ImmobileDB(**kwargs))
    return rows


def _parse_intestati(response_id: str, data: Optional[dict]) -> list[IntestatoDB]:
    """Parse intestati from response JSON into structured rows."""
    if not data or not isinstance(data, dict):
        return []
    rows = []
    for item in data.get("intestati", []):
        if not isinstance(item, dict):
            continue
        kwargs: dict[str, Any] = {"response_id": response_id}
        for html_key, db_col in INTESTATO_FIELD_MAP.items():
            if html_key in item:
                val = str(item[html_key]).strip() or None
                # For nominativo, concatenate if Cognome+Nome pattern
                if db_col == "nominativo" and kwargs.get("nominativo") and val:
                    kwargs["nominativo"] = f"{kwargs['nominativo']} {val}"
                else:
                    kwargs[db_col] = val
        rows.append(IntestatoDB(**kwargs))
    return rows


def _parse_page_visits(response_id: str, data: Optional[dict]) -> list[PageVisitDB]:
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
        rows.append(PageVisitDB(
            response_id=response_id,
            step=item.get("step", ""),
            url=item.get("url"),
            screenshot_url=item.get("screenshot_url"),
            form_elements_json=json.dumps(item.get("form_elements", []), default=str) if item.get("form_elements") else None,
            errors_json=json.dumps(item.get("errors", []), default=str) if item.get("errors") else None,
            timestamp=ts,
        ))
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
        await session.execute(text("DELETE FROM intestati WHERE response_id = :rid"), {"rid": request_id})
        await session.execute(text("DELETE FROM immobili WHERE response_id = :rid"), {"rid": request_id})
        await session.execute(text("DELETE FROM visura_responses WHERE request_id = :rid"), {"rid": request_id})

        resp = VisuraResponseDB(
            request_id=request_id,
            success=success,
            tipo_catasto=tipo_catasto,
            data=data,
            error=error,
        )
        session.add(resp)

        # Populate structured tables from JSON
        for imm in _parse_immobili(request_id, tipo_catasto, data):
            session.add(imm)
        for intest in _parse_intestati(request_id, data):
            session.add(intest)
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
        row = await session.get(VisuraResponseDB, request_id)
        if row is None:
            return None
        return {
            "request_id": row.request_id,
            "success": row.success,
            "tipo_catasto": row.tipo_catasto,
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
            select(VisuraRequestDB, VisuraResponseDB)
            .outerjoin(VisuraResponseDB, VisuraRequestDB.request_id == VisuraResponseDB.request_id)
            .where(VisuraRequestDB.request_id == request_id)
        )
        result = await session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None

        req, resp = row
        status = "pending"
        if resp is not None:
            status = "completed" if resp.success else "failed"

        return {
            "request_id": req.request_id,
            "request_type": req.request_type,
            "tipo_catasto": req.tipo_catasto,
            "provincia": req.provincia,
            "comune": req.comune,
            "foglio": req.foglio,
            "particella": req.particella,
            "sezione": req.sezione,
            "subalterno": req.subalterno,
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


async def get_workflow_result_record(workflow_id: str) -> Optional[dict]:
    """Fetch workflow run data in the same shape used by result_detail.html."""
    if not os.path.exists(DB_PATH):
        return None

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            """
            SELECT workflow_id, preset, status, input_json, output_json, created_at, updated_at
            FROM workflow_runs
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if run is None:
            return None
        step_rows = conn.execute(
            """
            SELECT step_key, status, result_json, error, started_at, finished_at
            FROM workflow_steps
            WHERE workflow_id = ?
            ORDER BY id
            """,
            (workflow_id,),
        ).fetchall()

    def _loads(raw: Optional[str], fallback):
        if not raw:
            return fallback
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return fallback

    input_data = _loads(run["input_json"], {})
    output_data = _loads(run["output_json"], {})
    step_data = []
    for step in step_rows:
        step_data.append({
            "step_key": step["step_key"],
            "status": step["status"],
            "result": _loads(step["result_json"], None),
            "error": step["error"],
            "started_at": step["started_at"],
            "finished_at": step["finished_at"],
        })

    data: dict[str, Any] = {}
    if isinstance(input_data, dict):
        data["input"] = input_data
    if isinstance(output_data, dict):
        data.update(output_data)
    elif output_data:
        data["output"] = output_data
    if step_data and "persisted_steps" not in data:
        data["persisted_steps"] = step_data

    return {
        "request_id": run["workflow_id"],
        "request_type": f"workflow:{run['preset']}",
        "tipo_catasto": input_data.get("tipo_catasto", "") if isinstance(input_data, dict) else "",
        "provincia": input_data.get("provincia", "") if isinstance(input_data, dict) else "",
        "comune": input_data.get("comune", "") if isinstance(input_data, dict) else "",
        "foglio": input_data.get("foglio", "") if isinstance(input_data, dict) else "",
        "particella": input_data.get("particella", "") if isinstance(input_data, dict) else "",
        "sezione": input_data.get("sezione") if isinstance(input_data, dict) else None,
        "subalterno": input_data.get("subalterno") if isinstance(input_data, dict) else None,
        "cost_text": None,
        "cost_value": None,
        "requested_at": run["created_at"],
        "responded_at": run["updated_at"],
        "success": run["status"] == "completed",
        "status": run["status"],
        "data": data,
        "error": None if run["status"] in ("completed", "partial", "running") else run["status"],
        "page_visits": [],
    }


async def get_documents_for_response(request_id: str, foglio: str = None, particella: str = None) -> list[dict]:
    """Fetch visura_documents linked to a response_id OR matching foglio/particella."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        # Match by response_id OR by property identifiers
        conditions = [VisuraDocumentDB.response_id == request_id]
        if foglio and particella:
            conditions.append(
                (VisuraDocumentDB.foglio == foglio) & (VisuraDocumentDB.particella == particella)
            )
        from sqlalchemy import or_
        stmt = select(VisuraDocumentDB).where(or_(*conditions)).order_by(VisuraDocumentDB.created_at.desc())
        result = await session.execute(stmt)
        rows = result.scalars().all()
    docs = []
    for row in rows:
        doc = {
            "id": row.id,
            "document_type": row.document_type,
            "file_format": row.file_format,
            "filename": row.filename,
            "file_path": row.file_path,
            "file_size": row.file_size,
            "oggetto": row.oggetto,
            "richiesta_del": row.richiesta_del,
            "provincia": row.provincia,
            "comune": row.comune,
            "foglio": row.foglio,
            "particella": row.particella,
            "subalterno": row.subalterno,
            "sezione_urbana": row.sezione_urbana,
            "tipo_catasto": row.tipo_catasto,
            "intestati": json.loads(row.intestati_json) if row.intestati_json else [],
            "dati_immobile": _dati.get("immobile", {}) if (_dati := json.loads(row.dati_immobile_json) if row.dati_immobile_json else {}) else {},
            "classamento": _dati.get("classamento", []),
            "indirizzo": _dati.get("indirizzo", ""),
            "xml_content": row.xml_content or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        docs.append(doc)
    return docs


async def get_all_documents(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch all visura_documents (for browse page)."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = (
            select(VisuraDocumentDB)
            .order_by(VisuraDocumentDB.created_at.desc())
            .limit(limit).offset(offset)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
    docs = []
    for row in rows:
        docs.append({
            "id": row.id,
            "document_type": row.document_type,
            "file_format": row.file_format,
            "filename": row.filename,
            "file_size": row.file_size,
            "oggetto": row.oggetto,
            "richiesta_del": row.richiesta_del,
            "provincia": row.provincia,
            "comune": row.comune,
            "foglio": row.foglio,
            "particella": row.particella,
            "subalterno": row.subalterno,
            "tipo_catasto": row.tipo_catasto,
            "intestati_count": len(json.loads(row.intestati_json)) if row.intestati_json else 0,
            "created_at": row.created_at.isoformat() if row.created_at else None,
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
            select(VisuraRequestDB, VisuraResponseDB)
            .outerjoin(VisuraResponseDB, VisuraRequestDB.request_id == VisuraResponseDB.request_id)
        )
        if provincia:
            stmt = stmt.where(VisuraRequestDB.provincia == provincia)
        if comune:
            stmt = stmt.where(VisuraRequestDB.comune == comune)
        if foglio:
            stmt = stmt.where(VisuraRequestDB.foglio == foglio)
        if particella:
            stmt = stmt.where(VisuraRequestDB.particella == particella)
        if tipo_catasto:
            stmt = stmt.where(VisuraRequestDB.tipo_catasto == tipo_catasto)

        stmt = stmt.order_by(VisuraRequestDB.created_at.desc()).limit(limit).offset(offset)

        result = await session.execute(stmt)
        rows = result.all()

        return [
            {
                "request_id": req.request_id,
                "request_type": req.request_type,
                "tipo_catasto": req.tipo_catasto,
                "provincia": req.provincia,
                "comune": req.comune,
                "foglio": req.foglio,
                "particella": req.particella,
                "sezione": req.sezione,
                "subalterno": req.subalterno,
                "requested_at": req.created_at.isoformat() if req.created_at else None,
                "success": resp.success if resp else None,
                "data": resp.data if resp else None,
                "error": resp.error if resp else None,
                "responded_at": resp.created_at.isoformat() if resp and resp.created_at else None,
            }
            for req, resp in rows
        ]


async def find_workflow_runs(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return workflow runs with step counts for the workflow list page."""
    if not os.path.exists(DB_PATH):
        return []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        where_clause, params = _build_workflow_where(status=status)
        sql = """
            SELECT
                wf.workflow_id, wf.preset, wf.status, wf.input_json,
                wf.created_at, wf.updated_at,
                (SELECT count(*) FROM workflow_steps ws WHERE ws.workflow_id = wf.workflow_id) AS total_steps,
                (SELECT count(*) FROM workflow_steps ws WHERE ws.workflow_id = wf.workflow_id AND ws.status = 'completed') AS completed_steps
            FROM workflow_runs AS wf
        """
        if where_clause:
            sql += f" WHERE {where_clause}"
        sql += " ORDER BY wf.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = []
        for run in conn.execute(sql, params).fetchall():
            input_data = _decode_json_object(run["input_json"])
            rows.append({
                "workflow_id": run["workflow_id"],
                "preset": run["preset"],
                "status": run["status"],
                "provincia": input_data.get("provincia", ""),
                "comune": input_data.get("comune", ""),
                "foglio": input_data.get("foglio", ""),
                "particella": input_data.get("particella", ""),
                "tipo_catasto": input_data.get("tipo_catasto", ""),
                "total_steps": run["total_steps"],
                "completed_steps": run["completed_steps"],
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            })
        return rows


def _decode_json_object(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
                provincia, comune, foglio, particella, tipo_catasto, status,
            )
            sql = """
                SELECT
                    req.request_id,
                    req.request_type,
                    req.tipo_catasto,
                    req.provincia,
                    req.comune,
                    req.foglio,
                    req.particella,
                    req.sezione,
                    req.subalterno,
                    req.created_at AS requested_at,
                    resp.success,
                    resp.error,
                    resp.created_at AS responded_at
                FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            for row in conn.execute(sql, params).fetchall():
                success = bool(row["success"]) if row["success"] is not None else None
                single_rows.append({
                    "request_id": row["request_id"],
                    "request_type": row["request_type"],
                    "source": "single",
                    "tipo_catasto": row["tipo_catasto"],
                    "provincia": row["provincia"],
                    "comune": row["comune"],
                    "foglio": row["foglio"],
                    "particella": row["particella"],
                    "sezione": row["sezione"],
                    "subalterno": row["subalterno"],
                    "requested_at": row["requested_at"],
                    "success": success,
                    "status": _single_result_status(success),
                    "data": None,
                    "error": row["error"],
                    "responded_at": row["responded_at"],
                })

        workflow_rows: list[dict] = []
        if source in (None, "workflow"):
            where_clause, params = _build_workflow_where(
                provincia, comune, foglio, particella, tipo_catasto, status,
            )
            sql = """
                SELECT workflow_id, preset, status, input_json, created_at, updated_at
                FROM workflow_runs AS wf
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            for run in conn.execute(sql, params).fetchall():
                input_data = _decode_json_object(run["input_json"])
                workflow_rows.append({
                    "request_id": run["workflow_id"],
                    "request_type": f"workflow:{run['preset']}",
                    "source": "workflow",
                    "tipo_catasto": input_data.get("tipo_catasto"),
                    "provincia": input_data.get("provincia"),
                    "comune": input_data.get("comune"),
                    "foglio": input_data.get("foglio"),
                    "particella": input_data.get("particella"),
                    "sezione": input_data.get("sezione"),
                    "subalterno": input_data.get("subalterno"),
                    "requested_at": run["created_at"],
                    "success": run["status"] == "completed",
                    "status": run["status"],
                    "data": None,
                    "error": None if run["status"] in ("completed", "partial", "running") else run["status"],
                    "responded_at": run["updated_at"],
                })

    rows = [*single_rows, *workflow_rows]
    rows.sort(key=lambda row: row.get("requested_at") or "", reverse=True)
    return rows[offset:offset + limit]


async def cleanup_old_responses(ttl_seconds: int) -> int:
    """Delete responses older than ttl_seconds. Returns count of deleted rows."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cutoff = datetime.now() - timedelta(seconds=ttl_seconds)

        # Find expired responses
        stmt = select(VisuraResponseDB).where(VisuraResponseDB.created_at < cutoff)
        result = await session.execute(stmt)
        expired = result.scalars().all()
        deleted = len(expired)

        for resp in expired:
            await session.delete(resp)

        if deleted:
            # Also delete orphaned requests
            orphan_stmt = select(VisuraRequestDB).where(
                VisuraRequestDB.created_at < cutoff,
                ~VisuraRequestDB.request_id.in_(
                    select(VisuraResponseDB.request_id)
                ),
            )
            orphan_result = await session.execute(orphan_stmt)
            for req in orphan_result.scalars().all():
                await session.delete(req)

        await session.commit()

        if is_db_writable():
            await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

        return deleted


async def count_responses() -> dict:
    """Return basic stats about stored data."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        total_requests = (await session.execute(
            select(text("count(*)")).select_from(VisuraRequestDB)
        )).scalar() or 0

        total_responses = (await session.execute(
            select(text("count(*)")).select_from(VisuraResponseDB)
        )).scalar() or 0

        successful = (await session.execute(
            select(text("count(*)")).select_from(VisuraResponseDB).where(VisuraResponseDB.success == True)  # noqa: E712
        )).scalar() or 0

        failed = (await session.execute(
            select(text("count(*)")).select_from(VisuraResponseDB).where(VisuraResponseDB.success == False)  # noqa: E712
        )).scalar() or 0

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
    """Build WHERE clause + params for single-result queries on visura_requests/responses."""
    conditions: list[str] = []
    params: list = []
    if provincia:
        conditions.append("req.provincia = ?")
        params.append(provincia)
    if comune:
        conditions.append("req.comune = ?")
        params.append(comune)
    if foglio:
        conditions.append("req.foglio = ?")
        params.append(str(foglio))
    if particella:
        conditions.append("req.particella = ?")
        params.append(str(particella))
    if tipo_catasto:
        conditions.append("req.tipo_catasto = ?")
        params.append(tipo_catasto)
    if status == "completed":
        conditions.append("resp.success = 1")
    elif status in ("failed", "error"):
        conditions.append("resp.success = 0")
    elif status == "pending":
        conditions.append("resp.request_id IS NULL")
    return (" AND ".join(conditions), params)


def _build_workflow_where(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[str, list]:
    """Build WHERE clause + params for workflow_runs queries.

    Property filters match against the JSON stored in input_json via json_extract.
    """
    conditions: list[str] = []
    params: list = []
    if provincia:
        conditions.append("json_extract(input_json, '$.provincia') = ?")
        params.append(provincia)
    if comune:
        conditions.append("json_extract(input_json, '$.comune') = ?")
        params.append(comune)
    if foglio:
        conditions.append("json_extract(input_json, '$.foglio') = ?")
        params.append(str(foglio))
    if particella:
        conditions.append("json_extract(input_json, '$.particella') = ?")
        params.append(str(particella))
    if tipo_catasto:
        conditions.append("json_extract(input_json, '$.tipo_catasto') = ?")
        params.append(tipo_catasto)
    if status == "completed":
        conditions.append("wf.status = 'completed'")
    elif status in ("failed", "error"):
        conditions.append("wf.status IN ('failed', 'error')")
    elif status == "partial":
        conditions.append("wf.status = 'partial'")
    elif status == "pending":
        conditions.append("wf.status = 'pending'")
    elif status == "running":
        conditions.append("wf.status = 'running'")
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
                provincia, comune, foglio, particella, tipo_catasto, status,
            )
            sql = """
                SELECT count(*) FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            total += conn.execute(sql, params).fetchone()[0] or 0

        if source in (None, "workflow"):
            where_clause, params = _build_workflow_where(
                provincia, comune, foglio, particella, tipo_catasto, status,
            )
            sql = "SELECT count(*) FROM workflow_runs AS wf"
            if where_clause:
                sql += f" WHERE {where_clause}"
            total += conn.execute(sql, params).fetchone()[0] or 0

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

    common = dict(provincia=provincia, comune=comune, foglio=foglio, particella=particella, tipo_catasto=tipo_catasto, source=source)

    with sqlite3.connect(DB_PATH) as conn:
        def _count_single(where_clause: str, params: list) -> int:
            sql = """
                SELECT count(*) FROM visura_requests AS req
                LEFT JOIN visura_responses AS resp ON req.request_id = resp.request_id
            """
            if where_clause:
                sql += f" WHERE {where_clause}"
            return conn.execute(sql, params).fetchone()[0] or 0

        def _count_workflow(where_clause: str, params: list) -> int:
            sql = "SELECT count(*) FROM workflow_runs AS wf"
            if where_clause:
                sql += f" WHERE {where_clause}"
            return conn.execute(sql, params).fetchone()[0] or 0

        s_total = s_ok = s_fail = s_pending = 0
        if source in (None, "single"):
            base_where, base_params = _build_single_where(provincia, comune, foglio, particella, tipo_catasto)
            s_total = _count_single(base_where, base_params)
            s_ok = _count_single(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="completed"))
            s_fail = _count_single(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="failed"))
            s_pending = _count_single(*_build_single_where(provincia, comune, foglio, particella, tipo_catasto, status="pending"))

        w_total = w_ok = w_fail = w_partial = w_pending = 0
        if source in (None, "workflow"):
            w_total = _count_workflow(*_build_workflow_where(provincia, comune, foglio, particella, tipo_catasto))
            w_ok = _count_workflow(*_build_workflow_where(provincia, comune, foglio, particella, tipo_catasto, status="completed"))
            w_fail = _count_workflow(*_build_workflow_where(provincia, comune, foglio, particella, tipo_catasto, status="failed"))
            w_partial = _count_workflow(*_build_workflow_where(provincia, comune, foglio, particella, tipo_catasto, status="partial"))
            w_pending = _count_workflow(*_build_workflow_where(provincia, comune, foglio, particella, tipo_catasto, status="pending"))

    return {
        "total_requests": s_total + w_total,
        "total_responses": (s_total - s_pending) + w_total,
        "successful": s_ok + w_ok,
        "failed": s_fail + w_fail,
        "partial": w_partial,
        "pending": s_pending + w_pending,
    }
