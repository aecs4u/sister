"""API route tests that exercise request mapping without a browser or database."""

import json

import pytest
from fastapi import HTTPException

from sister import routes
from sister.models import (
    QueueFullError,
    SubmitResult,
    VisuraInput,
    VisuraIntestatiInput,
    VisuraResponse,
)


class _Queue:
    def qsize(self):
        return 2


class _Service:
    request_queue = _Queue()

    def __init__(self, batch_results=None, single_result=None, error=None):
        self.batch_results = batch_results or []
        self.single_result = single_result
        self.error = error
        self.batch_call = None
        self.single_call = None

    async def add_requests_batch(self, requests, *, force=False):
        self.batch_call = (requests, force)
        if self.error:
            raise self.error
        return self.batch_results

    async def add_intestati_request(self, request, *, force=False):
        self.single_call = (request, force)
        if self.error:
            raise self.error
        return self.single_result


def _body(response):
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_richiedi_visura_defaults_to_both_cadastres_and_normalizes_section():
    service = _Service(batch_results=["request-T", "request-F"])
    request = VisuraInput(
        province="Roma",
        municipality="ROMA",
        sheet="1",
        parcel="2",
        section="_",
        urban_section="A",
        subunit="3",
    )

    response = await routes.richiedi_visura(request, service, force=True)

    requests, force = service.batch_call
    assert force is True
    assert [item.cadastre_type for item in requests] == ["T", "F"]
    assert [(item.section, item.urban_section, item.subunit) for item in requests] == [
        (None, "A", "3"),
        (None, "A", "3"),
    ]
    assert _body(response)["request_ids"] == ["request-T", "request-F"]
    assert _body(response)["status"] == "queued"


@pytest.mark.asyncio
async def test_richiedi_visura_marks_all_cached_batch_results_completed():
    cached = SubmitResult(
        request_id="original-request",
        cached=True,
        response=VisuraResponse(
            request_id="original-request", success=True, cadastre_type="F", data={"immobili": []}
        ),
    )
    service = _Service(batch_results=[cached])
    request = VisuraInput(province="Roma", municipality="ROMA", sheet="1", parcel="2", cadastre_type="F")

    response = await routes.richiedi_visura(request, service)

    payload = _body(response)
    assert payload["status"] == "cached"
    assert payload["request_ids"] == ["original-request"]
    assert payload["cached_results"][0]["data"] == {"immobili": []}


@pytest.mark.asyncio
async def test_richiedi_visura_maps_queue_full_to_http_429():
    service = _Service(error=QueueFullError("coda piena"))
    request = VisuraInput(province="Roma", municipality="ROMA", sheet="1", parcel="2", cadastre_type="F")

    with pytest.raises(HTTPException) as exc_info:
        await routes.richiedi_visura(request, service)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "coda piena"


@pytest.mark.asyncio
async def test_richiedi_intestati_uses_cached_request_id_and_default_cadastre():
    cached = SubmitResult(
        request_id="cached-original",
        cached=True,
        response=VisuraResponse(
            request_id="cached-original", success=False, cadastre_type="T", error="nessun dato"
        ),
    )
    service = _Service(single_result=cached)
    request = VisuraIntestatiInput(province="Roma", municipality="ROMA", sheet="1", parcel="2")

    response = await routes.richiedi_intestati_immobile(request, service, force=True)

    sent_request, force = service.single_call
    assert force is True
    assert sent_request.cadastre_type == "T"
    assert _body(response)["request_id"] == "cached-original"
    assert _body(response)["status"] == "error"


class _GenericService(_Service):
    async def add_generic_request(self, request, *, force=False):
        self.single_call = (request, force)
        return self.single_result


def _stale_result(request_id):
    return SubmitResult(
        request_id=request_id,
        cached=True,
        stale=True,
        response=VisuraResponse(request_id=request_id, success=True, cadastre_type="E", data={"immobili": []}),
    )


@pytest.mark.asyncio
async def test_generic_cached_answer_carries_stale_flag_and_timestamp():
    # soggetto-immobili / soggetto-documento go through the generic route: without "stale" the CLI and the batch never
    # asked before reusing an answer older than the cache window
    service = _GenericService(single_result=_stale_result("soggetto_immobili_E_old"))

    response = await routes.richiedi_generic_sister(
        search_type="soggetto_immobili", provincia="NAZIONALE", service=service, tipo_catasto="E"
    )

    payload = _body(response)
    assert payload["status"] == "cached"
    assert payload["request_id"] == "soggetto_immobili_E_old"
    assert payload["stale"] is True
    assert payload["timestamp"]


@pytest.mark.asyncio
async def test_intestati_cached_answer_carries_stale_flag():
    service = _Service(single_result=_stale_result("intestati_T_old"))
    request = VisuraIntestatiInput(province="Roma", municipality="ROMA", sheet="1", parcel="2")

    payload = _body(await routes.richiedi_intestati_immobile(request, service))

    assert payload["stale"] is True
    assert payload["timestamp"]
