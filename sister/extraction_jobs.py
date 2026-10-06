"""Persistent state helpers for long-running cadastral extraction jobs."""

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from .database import _get_session_factory


def _decode(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "job_id": row[0],
        "cadastre_type": row[1],
        "max_provinces": row[2],
        "status": row[3],
        "progress": json.loads(row[4] or "{}"),
        "results": json.loads(row[5] or "[]"),
        "error": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


async def create_extraction_job(job_id: str, cadastre_type: str, max_provinces: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    async with _get_session_factory()() as session:
        await session.execute(
            text("""INSERT INTO sezioni_extraction_jobs
                (job_id, cadastre_type, max_provinces, status, progress_json, results_json, created_at, updated_at)
                VALUES (:id, :type, :max, 'queued', '{}', '[]', :now, :now)"""),
            {"id": job_id, "type": cadastre_type, "max": max_provinces, "now": now},
        )
        await session.commit()
    return await get_extraction_job(job_id)


async def update_extraction_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: dict | None = None,
    results: list | None = None,
    error: str | None = None,
) -> dict | None:
    fields = ["updated_at = :now"]
    params: dict[str, Any] = {"id": job_id, "now": datetime.now(timezone.utc).isoformat()}
    if status is not None:
        fields.append("status = :status")
        params["status"] = status
    if progress is not None:
        fields.append("progress_json = :progress")
        params["progress"] = json.dumps(progress, ensure_ascii=False)
    if results is not None:
        fields.append("results_json = :results")
        params["results"] = json.dumps(results, ensure_ascii=False)
    if error is not None or status in {"completed", "cancelled", "paused"}:
        fields.append("error = :error")
        params["error"] = error
    async with _get_session_factory()() as session:
        result = await session.execute(
            text(f"UPDATE sezioni_extraction_jobs SET {', '.join(fields)} WHERE job_id = :id"), params
        )
        await session.commit()
        if not result.rowcount:
            return None
    return await get_extraction_job(job_id)


async def get_extraction_job(job_id: str) -> dict | None:
    async with _get_session_factory()() as session:
        row = (await session.execute(
            text("""SELECT job_id, cadastre_type, max_provinces, status, progress_json,
                         results_json, error, created_at, updated_at
                  FROM sezioni_extraction_jobs WHERE job_id = :id"""),
            {"id": job_id},
        )).first()
    return _decode(row)


async def list_extraction_jobs(limit: int = 50) -> list[dict]:
    async with _get_session_factory()() as session:
        rows = (await session.execute(
            text("""SELECT job_id, cadastre_type, max_provinces, status, progress_json,
                         results_json, error, created_at, updated_at
                  FROM sezioni_extraction_jobs ORDER BY created_at DESC LIMIT :limit"""),
            {"limit": limit},
        )).fetchall()
    jobs = [_decode(row) for row in rows]
    for job in jobs:
        job.pop("results", None)
    return jobs


async def pause_interrupted_extraction_jobs() -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with _get_session_factory()() as session:
        await session.execute(
            text("UPDATE sezioni_extraction_jobs SET status = 'paused', updated_at = :now WHERE status IN ('queued', 'running')"),
            {"now": now},
        )
        await session.commit()
