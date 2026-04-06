"""SQLite database layer for visura-api.

Provides persistent storage for visura requests and responses,
replacing the in-memory dict cache.
"""

import json
import logging
import os
from typing import Optional

import aiosqlite

logger = logging.getLogger("visura-api")

DB_PATH = os.getenv("VISURA_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "visura_api.sqlite"))


async def get_db() -> aiosqlite.Connection:
    """Open a connection to the SQLite database."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS visura_requests (
                request_id   TEXT PRIMARY KEY,
                request_type TEXT NOT NULL CHECK(request_type IN ('visura', 'intestati')),
                tipo_catasto TEXT NOT NULL CHECK(tipo_catasto IN ('T', 'F')),
                provincia    TEXT NOT NULL,
                comune       TEXT NOT NULL,
                foglio       TEXT NOT NULL,
                particella   TEXT NOT NULL,
                sezione      TEXT,
                subalterno   TEXT,
                created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS visura_responses (
                request_id   TEXT PRIMARY KEY REFERENCES visura_requests(request_id),
                success      INTEGER NOT NULL CHECK(success IN (0, 1)),
                tipo_catasto TEXT NOT NULL CHECK(tipo_catasto IN ('T', 'F')),
                data         TEXT,
                error        TEXT,
                created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_requests_created_at ON visura_requests(created_at);
            CREATE INDEX IF NOT EXISTS idx_responses_created_at ON visura_responses(created_at);
            CREATE INDEX IF NOT EXISTS idx_requests_lookup
                ON visura_requests(provincia, comune, foglio, particella, tipo_catasto);
            """
        )
        await db.commit()
        logger.info(f"Database inizializzato: {DB_PATH}")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Request operations
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
) -> None:
    """Persist a new visura/intestati request."""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO visura_requests
                (request_id, request_type, tipo_catasto, provincia, comune, foglio, particella, sezione, subalterno)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, request_type, tipo_catasto, provincia, comune, foglio, particella, sezione, subalterno),
        )
        await db.commit()
    finally:
        await db.close()


async def save_requests_batch(requests: list[dict]) -> None:
    """Persist multiple visura/intestati requests atomically."""
    if not requests:
        return

    rows = [
        (
            request["request_id"],
            request["request_type"],
            request["tipo_catasto"],
            request["provincia"],
            request["comune"],
            request["foglio"],
            request["particella"],
            request.get("sezione"),
            request.get("subalterno"),
        )
        for request in requests
    ]

    db = await get_db()
    try:
        await db.executemany(
            """
            INSERT INTO visura_requests
                (request_id, request_type, tipo_catasto, provincia, comune, foglio, particella, sezione, subalterno)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Response operations
# ---------------------------------------------------------------------------


async def save_response(
    request_id: str,
    success: bool,
    tipo_catasto: str,
    data: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Persist a visura response (result or error)."""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT OR REPLACE INTO visura_responses
                (request_id, success, tipo_catasto, data, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (request_id, int(success), tipo_catasto, json.dumps(data) if data else None, error),
        )
        await db.commit()
    finally:
        await db.close()


async def get_response(request_id: str) -> Optional[dict]:
    """Fetch a stored response by request_id. Returns None if not found."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT request_id, success, tipo_catasto, data, error, created_at FROM visura_responses WHERE request_id = ?",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "success": bool(row["success"]),
            "tipo_catasto": row["tipo_catasto"],
            "data": json.loads(row["data"]) if row["data"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
        }
    finally:
        await db.close()


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
    conditions = []
    params: list = []

    if provincia:
        conditions.append("r.provincia = ?")
        params.append(provincia)
    if comune:
        conditions.append("r.comune = ?")
        params.append(comune)
    if foglio:
        conditions.append("r.foglio = ?")
        params.append(foglio)
    if particella:
        conditions.append("r.particella = ?")
        params.append(particella)
    if tipo_catasto:
        conditions.append("r.tipo_catasto = ?")
        params.append(tipo_catasto)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    db = await get_db()
    try:
        cursor = await db.execute(
            f"""
            SELECT r.request_id, r.request_type, r.tipo_catasto, r.provincia, r.comune,
                   r.foglio, r.particella, r.sezione, r.subalterno, r.created_at AS requested_at,
                   resp.success, resp.data, resp.error, resp.created_at AS responded_at
            FROM visura_requests r
            LEFT JOIN visura_responses resp ON r.request_id = resp.request_id
            {where}
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [
            {
                "request_id": row["request_id"],
                "request_type": row["request_type"],
                "tipo_catasto": row["tipo_catasto"],
                "provincia": row["provincia"],
                "comune": row["comune"],
                "foglio": row["foglio"],
                "particella": row["particella"],
                "sezione": row["sezione"],
                "subalterno": row["subalterno"],
                "requested_at": row["requested_at"],
                "success": bool(row["success"]) if row["success"] is not None else None,
                "data": json.loads(row["data"]) if row["data"] else None,
                "error": row["error"],
                "responded_at": row["responded_at"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def cleanup_old_responses(ttl_seconds: int) -> int:
    """Delete responses older than ttl_seconds. Returns count of deleted rows."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            DELETE FROM visura_responses
            WHERE (julianday('now', 'localtime') - julianday(created_at)) * 86400 > ?
            """,
            (ttl_seconds,),
        )
        deleted = cursor.rowcount
        if deleted:
            await db.execute(
                """
                DELETE FROM visura_requests
                WHERE request_id NOT IN (SELECT request_id FROM visura_responses)
                  AND (julianday('now', 'localtime') - julianday(created_at)) * 86400 > ?
                """,
                (ttl_seconds,),
            )
        await db.commit()
        return deleted
    finally:
        await db.close()


async def count_responses() -> dict:
    """Return basic stats about stored data."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                (SELECT COUNT(*) FROM visura_responses) AS total_responses,
                (SELECT COUNT(*) FROM visura_responses WHERE success = 1) AS successful,
                (SELECT COUNT(*) FROM visura_responses WHERE success = 0) AS failed
            FROM visura_requests
            """
        )
        row = await cursor.fetchone()
        return {
            "total_requests": row["total_requests"],
            "total_responses": row["total_responses"],
            "successful": row["successful"],
            "failed": row["failed"],
        }
    finally:
        await db.close()
