"""Durable request queue persistence and restart recovery."""

import asyncio

import pytest
from sqlalchemy import text

from sister import database
from sister.models import VisuraRequest
from sister.services import VisuraService

pytestmark = pytest.mark.usefixtures("fresh_db")


def _request(request_id: str) -> VisuraRequest:
    return VisuraRequest(
        request_id=request_id,
        cadastre_type="F",
        province="Roma",
        municipality="ROMA",
        sheet="1",
        parcel="2",
    )


@pytest.mark.asyncio
async def test_request_job_persists_full_payload_and_lifecycle():
    request = _request("r_job")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))

    job = await database.get_request_job("r_job")
    assert job["status"] == "queued"
    assert job["payload"]["parcel"] == "2"

    await database.update_request_job("r_job", "running")
    await database.update_request_job("r_job", "failed", error_kind="interrupted_unknown")
    job = await database.get_request_job("r_job")
    assert job["status"] == "failed"
    assert job["error_kind"] == "interrupted_unknown"
    assert job["attempts"] == 1


@pytest.mark.asyncio
async def test_claim_request_job_is_atomic_and_counts_one_attempt():
    request = _request("r_claim")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))

    results = await asyncio.gather(
        database.claim_request_job(request.request_id),
        database.claim_request_job(request.request_id),
    )
    assert sorted(results) == [False, True]
    job = await database.get_request_job(request.request_id)
    assert job["status"] == "running"
    assert job["attempts"] == 1
    assert job["side_effect_started"] is False


@pytest.mark.asyncio
async def test_worker_marker_does_not_count_a_second_attempt():
    request = _request("r_marker")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    await database.claim_request_job(request.request_id)

    await database.update_request_job(
        request.request_id, "running", side_effect_started=True
    )
    job = await database.get_request_job(request.request_id)
    assert job["attempts"] == 1
    assert job["side_effect_started"] is True


@pytest.mark.asyncio
async def test_restart_requeues_claimed_job_before_side_effect():
    request = _request("r_pre_side_effect")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    await database.claim_request_job(request.request_id)
    async with database._get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE sister_request_jobs SET lease_until = :expired WHERE request_id = :request_id"),
            {"expired": "2000-01-01T00:00:00+00:00", "request_id": request.request_id},
        )

    service = VisuraService()
    await service._restore_request_jobs()

    job = await database.get_request_job(request.request_id)
    assert job["status"] == "queued"
    assert service.request_queue.qsize() == 1
    assert service.request_queue.get_nowait().request_id == request.request_id


@pytest.mark.asyncio
async def test_restart_requeues_queued_jobs_and_does_not_replay_running_jobs():
    queued = _request("r_queued")
    running = _request("r_running")
    for request, status in ((queued, "queued"), (running, "running")):
        await database.save_request(
            request_id=request.request_id,
            request_type="visura",
            tipo_catasto="F",
            provincia="Roma",
            comune="ROMA",
            foglio="1",
            particella="2",
        )
        await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
        if status == "running":
            await database.claim_request_job(request.request_id)
            await database.update_request_job(
                request.request_id, "running", side_effect_started=True
            )
            async with database._get_engine().begin() as conn:
                await conn.execute(
                    text("UPDATE sister_request_jobs SET lease_until = :expired WHERE request_id = :request_id"),
                    {"expired": "2000-01-01T00:00:00+00:00", "request_id": request.request_id},
                )

    service = VisuraService()
    await service._restore_request_jobs()

    assert service.request_queue.qsize() == 1
    assert service.request_queue.get_nowait().request_id == "r_queued"
    interrupted = await database.get_request_job("r_running")
    assert interrupted["status"] == "failed"
    assert interrupted["error_kind"] == "interrupted_unknown"
    assert await database.get_response("r_running") is not None


@pytest.mark.asyncio
async def test_restart_leaves_a_live_leased_job_alone():
    request = _request("r_live_lease")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    assert await database.claim_request_job(request.request_id) is True

    service = VisuraService()
    await service._restore_request_jobs()

    job = await database.get_request_job(request.request_id)
    assert job["status"] == "running"
    assert service.request_queue.empty()


@pytest.mark.asyncio
async def test_heartbeat_extends_live_lease():
    request = _request("r_heartbeat")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    await database.claim_request_job(request.request_id, lease_seconds=1)
    before = (await database.get_request_job(request.request_id))["lease_until"]

    assert await database.heartbeat_request_job(request.request_id, lease_seconds=600) is True
    after = (await database.get_request_job(request.request_id))["lease_until"]
    assert after > before


@pytest.mark.asyncio
async def test_clear_queue_stores_cancelled_result():
    request = _request("r_cancel")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    service = VisuraService()
    service._enqueue_request_nowait(request)

    assert await service.clear_queue() == 1
    assert (await database.get_request_job("r_cancel"))["status"] == "cancelled"
    assert (await service.get_response("r_cancel")).success is False


@pytest.mark.asyncio
async def test_restart_preserves_persisted_no_match_outcome():
    request = _request("r_running_no_match")
    await database.save_request(
        request_id=request.request_id,
        request_type="visura",
        tipo_catasto="F",
        provincia="Roma",
        comune="ROMA",
        foglio="1",
        particella="2",
    )
    await database.create_request_job(request.request_id, "visura", request.model_dump(mode="json"))
    await database.update_request_job(request.request_id, "running")
    await database.save_response(
        request.request_id, False, "F", error="NESSUNA CORRISPONDENZA TROVATA"
    )

    service = VisuraService()
    await service._restore_request_jobs()

    job = await database.get_request_job(request.request_id)
    assert job["status"] == "no_match"
    assert job["error_kind"] == "no_match"
    assert service.request_status[request.request_id] == "no_match"
