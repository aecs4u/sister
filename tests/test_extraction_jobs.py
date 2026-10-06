"""Tests for durable sezioni extraction jobs (extraction_jobs.py + VisuraService lifecycle + routes).

The browser is replaced by a fake whose ``esegui_extract_sezioni`` drives the
progress callback the same way ``extract_all_sezioni`` does, so no portal
automation runs.
"""

import asyncio

import pytest
from fastapi import HTTPException

import sister.extraction_jobs as jobs
import sister.routes as routes
from sister.models import SezioniExtractionRequest
from sister.services import VisuraService

pytestmark = pytest.mark.usefixtures("fresh_db")


def _province(value: str) -> dict:
    return {"value": value, "text": f"Provincia {value}"}


class FakeExtractor:
    """Stands in for BrowserManager.esegui_extract_sezioni."""

    def __init__(self, provinces=("RM", "MI"), total=None, fail_after=None, gate: asyncio.Event | None = None):
        self.provinces = list(provinces)
        self.total = total if total is not None else len(self.provinces)
        self.fail_after = fail_after
        self.gate = gate
        self.calls: list[dict] = []

    async def esegui_extract_sezioni(
        self,
        tipo_catasto,
        max_province=0,
        progress_callback=None,
        completed_province_values=None,
        initial_results=None,
    ):
        self.calls.append(
            {
                "tipo_catasto": tipo_catasto,
                "max_province": max_province,
                "completed": set(completed_province_values or ()),
                "initial_results": list(initial_results or []),
            }
        )
        results = list(initial_results or [])
        done = set(completed_province_values or ())
        for value in self.provinces:
            if value in done:
                continue
            if self.fail_after is not None and len(done) >= self.fail_after:
                raise RuntimeError("portal exploded")
            if self.gate is not None:
                await self.gate.wait()
            records = [{"provincia_value": value, "sezione": f"{value}-A"}]
            done.add(value)
            results = [r for r in results if r.get("provincia_value") != value] + records
            stop = await progress_callback(
                {
                    "province": _province(value),
                    "records": records,
                    "completed": len(done),
                    "total": self.total,
                    "completed_province_values": sorted(done),
                }
            )
            if stop:
                break
        return results


def _service(extractor: FakeExtractor) -> VisuraService:
    service = VisuraService()
    service.browser_manager = extractor
    return service


async def _run_to_end(service: VisuraService, job_id: str) -> dict:
    task = service._extraction_tasks.get(job_id)
    if task is not None:
        await task
    return await jobs.get_extraction_job(job_id)


# ---------------------------------------------------------------------------
# extraction_jobs.py persistence helpers
# ---------------------------------------------------------------------------


async def test_create_job_starts_queued_with_empty_state():
    job = await jobs.create_extraction_job("j1", "T", 5)

    assert job["job_id"] == "j1"
    assert job["cadastre_type"] == "T"
    assert job["max_provinces"] == 5
    assert job["status"] == "queued"
    assert job["progress"] == {}
    assert job["results"] == []
    assert job["error"] is None


async def test_update_job_round_trips_json_fields():
    await jobs.create_extraction_job("j1", "F", 2)

    job = await jobs.update_extraction_job(
        "j1", status="running", progress={"completed": 1, "current_province": "Città"}, results=[{"a": "è"}]
    )

    assert job["status"] == "running"
    assert job["progress"] == {"completed": 1, "current_province": "Città"}
    assert job["results"] == [{"a": "è"}]


async def test_update_job_clears_error_on_terminal_success_status():
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="failed", error="boom")

    job = await jobs.update_extraction_job("j1", status="completed")

    assert job["error"] is None


async def test_update_job_keeps_error_when_status_not_terminal():
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="failed", error="boom")

    job = await jobs.update_extraction_job("j1", progress={"completed": 0})

    assert job["error"] == "boom"


async def test_update_unknown_job_returns_none():
    assert await jobs.update_extraction_job("missing", status="running") is None


async def test_get_unknown_job_returns_none():
    assert await jobs.get_extraction_job("missing") is None


async def test_list_jobs_respects_limit():
    for i in range(3):
        await jobs.create_extraction_job(f"j{i}", "T", 1)

    assert len(await jobs.list_extraction_jobs(limit=2)) == 2
    assert {j["job_id"] for j in await jobs.list_extraction_jobs()} == {"j0", "j1", "j2"}


async def test_pause_interrupted_only_touches_queued_and_running():
    for job_id, status in [("q", "queued"), ("r", "running"), ("c", "completed"), ("f", "failed")]:
        await jobs.create_extraction_job(job_id, "T", 1)
        await jobs.update_extraction_job(job_id, status=status)

    await jobs.pause_interrupted_extraction_jobs()

    statuses = {j["job_id"]: j["status"] for j in await jobs.list_extraction_jobs()}
    assert statuses == {"q": "paused", "r": "paused", "c": "completed", "f": "failed"}


# ---------------------------------------------------------------------------
# VisuraService job lifecycle
# ---------------------------------------------------------------------------


async def test_extraction_completes_and_persists_results_and_progress():
    extractor = FakeExtractor(provinces=("RM", "MI"))
    service = _service(extractor)

    await service.start_section_extraction("j1", "T", 2)
    job = await _run_to_end(service, "j1")

    assert job["status"] == "completed"
    assert job["results"] == [
        {"provincia_value": "RM", "sezione": "RM-A"},
        {"provincia_value": "MI", "sezione": "MI-A"},
    ]
    assert job["progress"]["completed"] == 2
    assert job["progress"]["completed_province_values"] == ["MI", "RM"]
    assert "j1" not in service._extraction_tasks


async def test_extraction_marks_partial_when_fewer_provinces_than_total():
    service = _service(FakeExtractor(provinces=("RM",), total=3))

    await service.start_section_extraction("j1", "T", 3)
    job = await _run_to_end(service, "j1")

    assert job["status"] == "partial"


async def test_extraction_failure_keeps_progress_and_records_error():
    service = _service(FakeExtractor(provinces=("RM", "MI"), fail_after=1))

    await service.start_section_extraction("j1", "T", 2)
    job = await _run_to_end(service, "j1")

    assert job["status"] == "failed"
    assert job["error"] == "portal exploded"
    assert job["progress"]["completed_province_values"] == ["RM"]
    assert job["results"] == [{"provincia_value": "RM", "sezione": "RM-A"}]


async def test_resume_skips_completed_provinces_and_replaces_retried_chunk():
    extractor = FakeExtractor(provinces=("RM", "MI"), fail_after=1)
    service = _service(extractor)
    await service.start_section_extraction("j1", "T", 2)
    await _run_to_end(service, "j1")

    # Simulate a stale partial chunk for MI left over from the failed attempt.
    await jobs.update_extraction_job(
        "j1",
        results=[{"provincia_value": "RM", "sezione": "RM-A"}, {"provincia_value": "MI", "sezione": "stale"}],
    )
    extractor.fail_after = None

    resumed = await service.resume_section_extraction("j1")
    assert resumed["status"] == "queued"
    job = await _run_to_end(service, "j1")

    assert job["status"] == "completed"
    assert extractor.calls[-1]["completed"] == {"RM"}
    assert {"provincia_value": "MI", "sezione": "stale"} not in job["results"]
    assert {"provincia_value": "MI", "sezione": "MI-A"} in job["results"]
    assert job["error"] is None


async def test_resume_rejects_running_or_completed_jobs():
    service = _service(FakeExtractor())
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="completed")

    with pytest.raises(RuntimeError, match="non riprendibile"):
        await service.resume_section_extraction("j1")


async def test_resume_unknown_job_returns_none():
    assert await _service(FakeExtractor()).resume_section_extraction("missing") is None


async def test_cancel_running_job_stops_after_current_province():
    gate = asyncio.Event()
    extractor = FakeExtractor(provinces=("RM", "MI", "NA"), gate=gate)
    service = _service(extractor)
    await service.start_section_extraction("j1", "T", 3)
    await _wait_until_extracting(extractor)

    cancelling = await service.cancel_section_extraction("j1")
    assert cancelling["status"] == "cancelling"

    gate.set()
    job = await _run_to_end(service, "j1")

    assert job["status"] == "cancelled"
    assert job["progress"]["completed_province_values"] == ["RM"]
    assert "j1" not in service._cancelled_extractions


async def test_cancel_completed_job_is_rejected():
    service = _service(FakeExtractor())
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="completed")

    with pytest.raises(RuntimeError, match="già completata"):
        await service.cancel_section_extraction("j1")


async def test_cancel_job_without_live_task_pauses_it():
    service = _service(FakeExtractor())
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="running")

    job = await service.cancel_section_extraction("j1")

    assert job["status"] == "paused"


async def test_cancel_already_stopped_job_is_a_noop():
    service = _service(FakeExtractor())
    await jobs.create_extraction_job("j1", "T", 1)
    await jobs.update_extraction_job("j1", status="failed", error="x")

    job = await service.cancel_section_extraction("j1")

    assert job["status"] == "failed"


async def test_launching_a_job_twice_is_rejected():
    gate = asyncio.Event()
    service = _service(FakeExtractor(gate=gate))
    await service.start_section_extraction("j1", "T", 2)

    with pytest.raises(RuntimeError, match="già in esecuzione"):
        service._launch_section_extraction("j1")

    gate.set()
    await _run_to_end(service, "j1")


async def _wait_until_extracting(extractor: FakeExtractor) -> None:
    for _ in range(200):
        if extractor.calls:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("extraction never started")


async def test_stopping_extraction_tasks_pauses_running_job():
    gate = asyncio.Event()
    extractor = FakeExtractor(gate=gate)
    service = _service(extractor)
    await service.start_section_extraction("j1", "T", 2)
    await _wait_until_extracting(extractor)

    await service._stop_extraction_jobs()

    job = await jobs.get_extraction_job("j1")
    assert job["status"] == "paused"
    assert service._extraction_tasks == {}


async def test_job_stopped_before_it_starts_is_paused_on_next_startup():
    service = _service(FakeExtractor(gate=asyncio.Event()))
    await service.start_section_extraction("j1", "T", 2)

    await service._stop_extraction_jobs()  # cancelled before the worker's try-block
    assert (await jobs.get_extraction_job("j1"))["status"] == "queued"

    await jobs.pause_interrupted_extraction_jobs()  # what initialize() runs at startup
    assert (await jobs.get_extraction_job("j1"))["status"] == "paused"


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def test_extract_route_returns_202_with_job_links():
    service = _service(FakeExtractor())

    response = await routes.extract_sezioni(SezioniExtractionRequest(tipo_catasto="T", max_province=2), service)
    body = __import__("json").loads(response.body)

    assert response.status_code == 202
    assert body["status"] == "queued"
    assert body["status_url"] == f"/sezioni/extract/{body['job_id']}"
    await _run_to_end(service, body["job_id"])


async def test_extract_route_maps_runtime_error_to_503():
    class Refusing(VisuraService):
        async def start_section_extraction(self, *args, **kwargs):
            raise RuntimeError("busy")

    with pytest.raises(HTTPException) as exc:
        await routes.extract_sezioni(SezioniExtractionRequest(tipo_catasto="T", max_province=1), Refusing())

    assert exc.value.status_code == 503


async def test_get_and_list_routes():
    await jobs.create_extraction_job("j1", "T", 1)

    assert (await routes.get_section_extraction("j1")).status_code == 200
    listed = await routes.list_section_extractions(limit=10_000)
    assert __import__("json").loads(listed.body)["count"] == 1
    with pytest.raises(HTTPException) as exc:
        await routes.get_section_extraction("missing")
    assert exc.value.status_code == 404


async def test_cancel_and_resume_routes_map_errors():
    service = _service(FakeExtractor())
    await jobs.create_extraction_job("done", "T", 1)
    await jobs.update_extraction_job("done", status="completed")

    with pytest.raises(HTTPException) as conflict:
        await routes.cancel_section_extraction("done", service)
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as resume_conflict:
        await routes.resume_section_extraction("done", service)
    assert resume_conflict.value.status_code == 409

    for handler in (routes.cancel_section_extraction, routes.resume_section_extraction):
        with pytest.raises(HTTPException) as missing:
            await handler("missing", service)
        assert missing.value.status_code == 404
