import json
from datetime import datetime, timedelta

import pytest


def _response(main_module, request_id: str, timestamp: datetime | None = None):
    return main_module.VisuraResponse(
        request_id=request_id,
        success=True,
        tipo_catasto="F",
        data={"ok": True},
        timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_get_response_marks_expired_when_ttl_exceeded(main_module):
    service = main_module.VisuraService()
    service.response_ttl_seconds = 1
    request_id = "req_F_old"
    old_ts = datetime.now() - timedelta(seconds=2)

    service.response_store[request_id] = _response(main_module, request_id, timestamp=old_ts)
    result = await service.get_response(request_id)

    assert result is None
    assert request_id not in service.response_store
    assert request_id in service.expired_request_ids


def test_get_request_state_completed_when_response_present(main_module):
    service = main_module.VisuraService()
    request_id = "req_F_done"
    service.response_store[request_id] = _response(main_module, request_id)

    assert service.get_request_state(request_id) == "completed"


@pytest.mark.asyncio
async def test_store_response_respects_max_items(main_module):
    service = main_module.VisuraService()
    service.response_max_items = 2
    service.response_ttl_seconds = 3600

    service._store_response(_response(main_module, "r1"))
    service._store_response(_response(main_module, "r2"))
    service._store_response(_response(main_module, "r3"))

    assert list(service.response_store.keys()) == ["r2", "r3"]
    assert "r1" in service.expired_request_ids


def test_expired_registry_is_bounded(main_module):
    service = main_module.VisuraService()
    service.response_max_items = 2

    service._mark_request_expired("r1")
    service._mark_request_expired("r2")
    service._mark_request_expired("r3")

    assert list(service.expired_request_ids.keys()) == ["r2", "r3"]


@pytest.mark.asyncio
async def test_ottieni_visura_returns_processing_for_pending(main_module):
    service = main_module.VisuraService()
    request_id = "req_F_pending"
    service.pending_request_ids.add(request_id)

    response = await main_module.ottieni_visura(request_id, service)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "processing"


@pytest.mark.asyncio
async def test_ottieni_visura_returns_expired_status_410(main_module):
    service = main_module.VisuraService()
    request_id = "req_F_expired"
    service.expired_request_ids[request_id] = datetime.now()

    response = await main_module.ottieni_visura(request_id, service)
    payload = json.loads(response.body)

    assert response.status_code == 410
    assert payload["status"] == "expired"


@pytest.mark.asyncio
async def test_ottieni_visura_returns_404_for_unknown(main_module):
    service = main_module.VisuraService()

    with pytest.raises(main_module.HTTPException) as exc_info:
        await main_module.ottieni_visura("missing_request", service)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_richiedi_visura_returns_503_when_service_not_processing(main_module):
    service = main_module.VisuraService()
    service.processing = False
    request = main_module.VisuraInput(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
    )

    with pytest.raises(main_module.HTTPException) as exc_info:
        await main_module.richiedi_visura(request, service)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_richiedi_intestati_returns_503_when_service_not_processing(main_module):
    service = main_module.VisuraService()
    service.processing = False
    request = main_module.VisuraIntestatiInput(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
        subalterno="3",
    )

    with pytest.raises(main_module.HTTPException) as exc_info:
        await main_module.richiedi_intestati_immobile(request, service)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_stop_worker_sentinel_shutdown_clears_state(main_module):
    service = main_module.VisuraService()
    service.pending_request_ids.add("req_F_pending_before_stop")

    await service.initialize()
    assert service.processing is True
    assert service._worker_task is not None
    assert service._worker_task.done() is False

    await service._stop_worker()

    assert service.processing is False
    assert service._worker_task is None
    assert service.pending_request_ids == set()


@pytest.mark.asyncio
async def test_health_check_exposes_runtime_metrics(main_module):
    service = main_module.VisuraService()
    service.pending_request_ids.update({"r1", "r2"})
    service.response_store["r1"] = _response(main_module, "r1")
    service.response_ttl_seconds = 123
    service.response_max_items = 456

    response = await main_module.health_check(service)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "healthy"
    assert payload["pending_requests"] == 2
    assert payload["cached_responses"] == 1
    assert payload["response_ttl_seconds"] == 123
    assert payload["response_max_items"] == 456


def test_invalid_response_env_values_fall_back_to_defaults(monkeypatch, main_module):
    monkeypatch.setenv("RESPONSE_TTL_SECONDS", "invalid")
    monkeypatch.setenv("RESPONSE_MAX_ITEMS", "-7")

    service = main_module.VisuraService()

    assert service.response_ttl_seconds == 6 * 3600
    assert service.response_max_items == 5000


@pytest.mark.asyncio
async def test_get_response_expiry_transitions_state_to_expired(main_module):
    service = main_module.VisuraService()
    service.response_ttl_seconds = 1
    request_id = "req_F_expire_transition"
    old_ts = datetime.now() - timedelta(seconds=2)
    service.response_store[request_id] = _response(main_module, request_id, timestamp=old_ts)

    result = await service.get_response(request_id)

    assert result is None
    assert service.get_request_state(request_id) == "expired"
