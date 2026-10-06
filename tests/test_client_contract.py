"""Contract tests: VisuraClient against the real FastAPI app (in-process, via ASGITransport).

Each client method must hit a route the server serves, with a payload the server's
input models accept. The VisuraService is a fake and the DB layer is stubbed by the
``main_module`` fixture, so nothing touches the portal or SQLite.
"""

import asyncio

import httpx
import pytest

from sister.client import VisuraAPIError, VisuraClient
from sister.models import SubmitResult


class FakeService:
    """Accepts any add_* submission and records it."""

    processing = True
    auth_ready = True

    def __init__(self):
        self.submissions: list[tuple[str, object]] = []
        self.options: list[tuple[str, bool, dict]] = []
        self.request_queue: asyncio.Queue = asyncio.Queue()

    def __getattr__(self, name):
        if name.startswith("add_"):

            async def _add(request, force=False, **kwargs):
                self.submissions.append((name, request))
                self.options.append((name, force, kwargs))
                if isinstance(request, list):
                    return [SubmitResult(request_id=r.request_id) for r in request]
                return SubmitResult(request_id=request.request_id)

            return _add
        raise AttributeError(name)


@pytest.fixture()
def fake_service(main_module, monkeypatch):
    service = FakeService()
    monkeypatch.setattr(main_module, "visura_service", service)
    monkeypatch.setattr(main_module, "api_key", None, raising=False)
    return service


@pytest.fixture()
def client(main_module):
    """A client whose every call goes through a fresh in-process ASGI transport."""
    api = VisuraClient(base_url="http://sister.test", timeout=5)

    def _fresh():
        return httpx.AsyncClient(
            base_url=api.base_url, headers=api._headers(), transport=httpx.ASGITransport(app=main_module.app)
        )

    api._get_client = _fresh
    return api


LOCATION = dict(provincia="Trieste", comune="TRIESTE", foglio="9", particella="166")


@pytest.mark.parametrize(
    "call,expected_add",
    [
        (lambda c: c.search(**LOCATION, tipo_catasto="F"), "add_requests_batch"),
        (lambda c: c.intestati(**LOCATION, tipo_catasto="F", subalterno="3"), "add_intestati_request"),
        (lambda c: c.soggetto(codice_fiscale="rssmra85m01h501z"), "add_soggetto_request"),
        (lambda c: c.persona_giuridica(identificativo="02471840997", tipo_catasto="E"), "add_persona_giuridica_request"),
        (lambda c: c.elenco_immobili(provincia="Trieste", comune="TRIESTE", tipo_catasto="T"), "add_elenco_immobili_request"),
        (
            lambda c: c.ispezione_ipotecaria(tipo_ricerca="immobile", provincia="Trieste", comune="TRIESTE",
                                             tipo_catasto="F", foglio="9", particella="166"),
            "add_ispezione_ipotecaria_request",
        ),
        (
            lambda c: c.generic_search(search_type="indirizzo", provincia="Trieste", comune="TRIESTE",
                                       indirizzo="VIA ROMA"),
            "add_generic_request",
        ),
    ],
    ids=["search", "intestati", "soggetto", "persona_giuridica", "elenco", "ipotecaria", "generic"],
)
async def test_client_submissions_are_accepted_by_the_server(fake_service, client, call, expected_add):
    response = await call(client)

    assert response.get("request_id") or response.get("request_ids"), response
    assert [name for name, _ in fake_service.submissions] == [expected_add]


async def test_paid_inspection_cache_reuse_requires_explicit_api_flag(fake_service, client):
    await client.ispezione_ipotecaria(
        tipo_ricerca="immobile",
        provincia="Trieste",
        comune="TRIESTE",
        tipo_catasto="F",
        foglio="9",
        particella="166",
        reuse_cached=True,
    )
    assert fake_service.options[-1][0] == "add_ispezione_ipotecaria_request"
    assert fake_service.options[-1][2]["reuse_cached"] is True


async def test_search_without_tipo_submits_both_cadastres(fake_service, client):
    response = await client.search(**LOCATION)

    ((_, batch),) = fake_service.submissions
    assert sorted(r.cadastre_type for r in batch) == ["F", "T"]
    assert len(response["request_ids"]) == 2


async def test_soggetto_fiscal_code_is_uppercased_before_submission(fake_service, client):
    await client.soggetto(codice_fiscale="rssmra85m01h501z")

    ((_, request),) = fake_service.submissions
    assert request.fiscal_code == "RSSMRA85M01H501Z"


async def test_generic_search_forwards_foglio_to_the_request_params(fake_service, client):
    await client.generic_search(search_type="mappa", provincia="Trieste", comune="TRIESTE", foglio="9")

    ((_, request),) = fake_service.submissions
    assert request.search_type == "mappa"
    assert request.params == {"foglio": "9"}


async def test_intestati_document_request_is_opt_in_and_forwarded(fake_service, client):
    await client.intestati(**LOCATION, tipo_catasto="F", subalterno="3", request_documents=True)

    ((_, request),) = fake_service.submissions
    assert request.request_documents is True


async def test_unknown_generic_search_type_is_a_404(fake_service, client):
    with pytest.raises(VisuraAPIError) as exc:
        await client.generic_search(search_type="teletrasporto", provincia="Trieste")

    assert exc.value.status_code == 404
    assert fake_service.submissions == []


async def test_invalid_payload_is_rejected_with_422(fake_service, client):
    with pytest.raises(VisuraAPIError) as exc:
        await client.intestati(provincia="Trieste", comune="TRIESTE", foglio="9", particella="", tipo_catasto="T")

    assert exc.value.status_code == 422


async def test_history_and_health_routes_exist(fake_service, client, main_module, monkeypatch):
    async def _health(_service):
        return {"status": "healthy"}

    monkeypatch.setattr(main_module, "health_check", _health)

    assert (await client.history(provincia="Trieste", limit=5))["count"] == 0
    assert (await client.health())["status"] == "healthy"


async def test_custom_workflow_submission_uses_persisted_workflow_route(fake_service, client, main_module, monkeypatch):
    async def fake_backend(payload):
        assert payload["preset"] == "custom"
        assert payload["custom_steps"] == ["search", "drill_intestati", "risk_score"]
        assert payload["numero_nota"] == "123"
        return {"workflow_id": "wf_custom", "steps": [], "summary": {"completed": 0}}

    monkeypatch.setattr(main_module, "_run_workflow_backend", fake_backend)
    result = await client.workflow(
        preset="custom",
        custom_steps=["search", "drill_intestati", "risk_score"],
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        numero_nota="123",
    )
    assert result["workflow_id"] == "wf_custom"


async def test_workflow_run_lifecycle_routes_proxy_to_owner(fake_service, client, main_module, monkeypatch):
    calls = []

    async def fake_proxy(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"workflow_id": "wf_1", "status": "completed"}

    monkeypatch.setattr(main_module, "_workflow_backend_request", fake_proxy)
    assert (await client.workflow_status("wf_1"))["status"] == "completed"
    assert (await client.cancel_workflow("wf_1"))["workflow_id"] == "wf_1"
    assert (await client.resume_workflow("wf_1", preset="custom", custom_steps=["risk_score"]))["status"] == "completed"
    assert [call[0:2] for call in calls] == [
        ("GET", "/catasto/workflow/runs/wf_1"),
        ("POST", "/catasto/workflow/runs/wf_1/cancel"),
        ("POST", "/catasto/workflow/run"),
    ]
    assert calls[-1][2]["params"] == {"resume": "true", "workflow_id": "wf_1"}
    assert calls[-1][2]["json"]["custom_steps"] == ["risk_score"]


async def test_workflow_submission_reaches_a_workflow_route(fake_service, client, main_module, monkeypatch):
    async def fake_backend(payload):
        assert payload["preset"] == "due-diligence"
        assert payload["foglio"] == "9"
        return {"workflow_id": "wf_test", "steps": [], "summary": {"completed": 0}}

    monkeypatch.setattr(main_module, "_run_workflow_backend", fake_backend)
    result = await client.workflow(preset="due-diligence", **LOCATION, tipo_catasto="F")

    assert result["workflow_id"] == "wf_test"
