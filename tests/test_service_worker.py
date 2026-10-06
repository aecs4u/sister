"""Tests for VisuraService: request worker dispatch, cache hits, batching, and auth/browser control.

The browser manager is a fake; the database layer is stubbed by the ``main_module``
fixture (see conftest.py), so no portal or SQLite I/O happens here.
"""

import asyncio
from contextlib import suppress

import pytest

import sister.services as services_mod
from sister.services import _request_job_outcome
from sister.models import (
    AuthenticationError,
    BrowserError,
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaRequest,
    VisuraIntestatiRequest,
    VisuraPersonaGiuridicaRequest,
    VisuraRequest,
    VisuraResponse,
    VisuraSoggettoRequest,
    SubmitResult,
)

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """The worker sleeps 2 s between requests and 5 s while waiting for auth; skip that."""

    async def _sleep(_delay=0, *args, **kwargs):
        await _real_sleep(0)

    monkeypatch.setattr(services_mod.asyncio, "sleep", _sleep)


class FakeBrowser:
    """Records which esegui_* method handled each request."""

    def __init__(self, fail_on: set[str] | None = None):
        self.handled: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()
        self.closed = False
        self.initialized = False
        self.logged_in = False
        self.keepalive = False

    def _respond(self, method: str, request) -> VisuraResponse:
        self.handled.append((method, request.request_id))
        if request.request_id in self.fail_on:
            raise RuntimeError(f"crash in {method}")
        return VisuraResponse(
            request_id=request.request_id,
            success=True,
            cadastre_type=getattr(request, "cadastre_type", "E"),
            data={"handled_by": method},
        )

    async def esegui_visura(self, request):
        return self._respond("visura", request)

    async def esegui_visura_intestati(self, request):
        return self._respond("intestati", request)

    async def esegui_visura_soggetto(self, request):
        return self._respond("soggetto", request)

    async def esegui_visura_persona_giuridica(self, request):
        return self._respond("persona_giuridica", request)

    async def esegui_elenco_immobili(self, request):
        return self._respond("elenco", request)

    async def esegui_ispezione_ipotecaria(self, request):
        return self._respond("ipotecaria", request)

    async def esegui_generic(self, request):
        return self._respond("generic", request)

    # auth lifecycle
    async def initialize(self):
        self.initialized = True

    async def login(self):
        self.logged_in = True

    async def start_keep_alive(self):
        self.keepalive = True

    async def close(self):
        self.closed = True

    @property
    def is_cdp(self):
        return True

    @property
    def session_origin(self):
        return "authenticated" if self.logged_in else None


def _service(main_module, browser: FakeBrowser | None = None, *, auth_ready: bool = True):
    service = main_module.VisuraService()
    service.browser_manager = browser or FakeBrowser()
    service._auth_ready = auth_ready
    service.processing = True
    return service


async def _drain(service) -> None:
    """Run the worker until everything queued so far is processed, then stop it."""
    worker = asyncio.create_task(service._process_requests())
    await asyncio.wait_for(service.request_queue.join(), timeout=5)
    service.request_queue.put_nowait(None)
    await asyncio.wait_for(worker, timeout=5)


def _loc(**extra):
    return dict(cadastre_type="F", province="Roma", municipality="ROMA", sheet="1", parcel="2", **extra)


ALL_REQUESTS = [
    ("visura", lambda: VisuraRequest(request_id="r_visura", **_loc())),
    ("intestati", lambda: VisuraIntestatiRequest(request_id="r_intestati", **_loc())),
    ("soggetto", lambda: VisuraSoggettoRequest(request_id="r_soggetto", fiscal_code="RSSMRA85M01H501Z")),
    (
        "persona_giuridica",
        lambda: VisuraPersonaGiuridicaRequest(request_id="r_pnf", identifier="01234567890", cadastre_type="E"),
    ),
    (
        "elenco",
        lambda: ElencoImmobiliRequest(request_id="r_elenco", cadastre_type="T", province="Roma", municipality="ROMA"),
    ),
    ("ipotecaria", lambda: IspezioneIpotecariaRequest(request_id="r_ipo", search_type="immobile", **_loc())),
    (
        "generic",
        lambda: GenericSisterRequest(request_id="r_gen", search_type="indirizzo", cadastre_type="T", province="Roma"),
    ),
]


# ---------------------------------------------------------------------------
# Worker dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,factory", ALL_REQUESTS, ids=[m for m, _ in ALL_REQUESTS])
async def test_worker_routes_each_request_type_to_its_handler(main_module, method, factory):
    browser = FakeBrowser()
    service = _service(main_module, browser)
    request = factory()
    service._enqueue_request_nowait(request)
    assert service.get_request_state(request.request_id) == "processing"

    await _drain(service)

    assert browser.handled == [(method, request.request_id)]
    assert service.get_request_state(request.request_id) == "completed"
    assert (await service.get_response(request.request_id)).data == {"handled_by": method}


async def test_worker_processes_requests_in_fifo_order(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser)
    for i in range(3):
        service._enqueue_request_nowait(VisuraRequest(request_id=f"r{i}", **_loc()))

    await _drain(service)

    assert [rid for _, rid in browser.handled] == ["r0", "r1", "r2"]


async def test_worker_does_not_call_handler_if_side_effect_marker_is_not_durable(main_module, monkeypatch):
    browser = FakeBrowser()
    service = _service(main_module, browser)
    original_update = services_mod.update_request_job

    async def _fail_side_effect_marker(request_id, status, **kwargs):
        if kwargs.get("side_effect_started"):
            return False
        return await original_update(request_id, status, **kwargs)

    monkeypatch.setattr(services_mod, "update_request_job", _fail_side_effect_marker)
    request = VisuraRequest(request_id="r_unmarked", **_loc())
    service._enqueue_request_nowait(request)

    await _drain(service)

    assert browser.handled == []
    response = await service.get_response("r_unmarked")
    assert "Could not persist the portal side-effect boundary" in response.error


async def test_worker_survives_a_crashing_request_and_continues(main_module):
    browser = FakeBrowser(fail_on={"r_bad"})
    service = _service(main_module, browser)
    service._enqueue_request_nowait(VisuraRequest(request_id="r_bad", **_loc()))
    service._enqueue_request_nowait(VisuraRequest(request_id="r_good", **_loc()))

    await _drain(service)

    assert service.get_request_state("r_good") == "completed"
    assert "r_bad" not in service.pending_request_ids


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_kind"),
    [
        ("SessionLockedError: busy", "failed", "session_locked"),
        ("BrowserNavigationError: redirect failed", "failed", "session_expired"),
        ("SelectorDrift: result form changed", "failed", "selector_drift"),
        ("ConnectTimeout: portal unavailable", "failed", "portal_unavailable"),
        ("InvalidInput: bad identifier", "failed", "invalid_input"),
        ("CaptchaRequired: CAPTCHA_REQUIRED", "needs_human", "captcha_required"),
    ],
)
def test_request_outcomes_have_stable_categories(error, expected_status, expected_kind, main_module):
    response = main_module.VisuraResponse(
        request_id="r_category", success=False, cadastre_type="F", error=error
    )
    assert _request_job_outcome(response) == (expected_status, expected_kind)


async def test_no_match_payload_is_reported_as_no_match_not_success(main_module):
    service = _service(main_module)
    response = main_module.VisuraResponse(
        request_id="r_none",
        success=True,
        cadastre_type="F",
        data={"immobili": [], "error": "NESSUNA CORRISPONDENZA TROVATA"},
    )

    await service._store_response(response)

    stored = await service.get_response("r_none")
    assert stored.success is False
    assert stored.error == "NESSUNA CORRISPONDENZA TROVATA"


async def test_crashing_request_still_gets_an_error_outcome(main_module):
    service = _service(main_module, FakeBrowser(fail_on={"r_bad"}))
    service._enqueue_request_nowait(VisuraRequest(request_id="r_bad", **_loc()))

    await _drain(service)

    assert service.get_request_state("r_bad") == "completed"
    assert (await service.get_response("r_bad")).success is False


async def test_worker_skips_unknown_request_types(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser)
    service.request_queue.put_nowait(object())
    service._enqueue_request_nowait(VisuraRequest(request_id="r_ok", **_loc()))

    await _drain(service)

    assert browser.handled == [("visura", "r_ok")]


async def test_worker_waits_for_auth_then_processes(main_module, monkeypatch):
    browser = FakeBrowser()
    service = _service(main_module, browser, auth_ready=False)
    waits = []

    async def _sleep_then_authenticate(_delay=0, *args, **kwargs):
        waits.append(_delay)
        if sum(delay == 5 for delay in waits) == 3:  # auth completes after three wait intervals
            service._auth_ready = True
        await _real_sleep(0)

    monkeypatch.setattr(services_mod.asyncio, "sleep", _sleep_then_authenticate)
    service._enqueue_request_nowait(VisuraRequest(request_id="r1", **_loc()))

    await _drain(service)

    assert browser.handled == [("visura", "r1")]
    assert [delay for delay in waits if delay == 5][:3] == [5, 5, 5]


async def test_worker_survives_auth_timeout_and_keeps_serving(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser, auth_ready=False)
    service._enqueue_request_nowait(VisuraRequest(request_id="dropped", **_loc()))
    worker = asyncio.create_task(service._process_requests())

    # The first request receives a persisted failure after the auth wait; the worker stays alive.
    await asyncio.wait_for(service.request_queue.join(), timeout=5)
    assert not worker.done()
    assert "dropped" not in service.pending_request_ids
    failed = await service.get_response("dropped")
    assert failed is not None
    assert failed.success is False
    assert "AuthenticationError" in failed.error

    service._auth_ready = True
    service._enqueue_request_nowait(VisuraRequest(request_id="later", **_loc()))
    await asyncio.wait_for(service.request_queue.join(), timeout=5)
    service.request_queue.put_nowait(None)
    await asyncio.wait_for(worker, timeout=5)

    assert browser.handled == [("visura", "later")]


async def test_worker_stops_on_sentinel_and_clears_processing_flag(main_module):
    service = _service(main_module)
    service.request_queue.put_nowait(None)

    await asyncio.wait_for(service._process_requests(), timeout=5)

    assert service.processing is False


# ---------------------------------------------------------------------------
# Cache and submission
# ---------------------------------------------------------------------------


def _cached_record(request_id="cached_1"):
    return {
        "request_id": request_id,
        "success": True,
        "tipo_catasto": "F",
        "data": {"from": "cache"},
        "error": None,
        "created_at": "2026-09-01T10:00:00",
    }


async def test_cache_hit_returns_cached_result_without_enqueuing(main_module, monkeypatch):
    async def _hit(_key, _ttl):
        return _cached_record()

    monkeypatch.setattr(services_mod, "find_cached_response", _hit)
    service = _service(main_module)

    result = await service.add_request(VisuraRequest(request_id="new", **_loc()))

    assert result.cached is True
    assert result.request_id == "cached_1"
    assert result.response.data == {"from": "cache"}
    assert service.request_queue.qsize() == 0


async def test_cache_ttl_can_be_overridden_per_request_type(main_module, monkeypatch):
    observed_ttls = []

    async def _miss(_key, ttl):
        observed_ttls.append(ttl)
        return None

    monkeypatch.setenv("RESPONSE_TTL_SECONDS", "21600")
    monkeypatch.setenv("RESPONSE_TTL_SECONDS_INTESTATI", "900")
    monkeypatch.setattr(services_mod, "find_cached_response", _miss)
    service = _service(main_module)

    await service.add_request(VisuraRequest(request_id="ttl_search", **_loc()))
    await service.add_intestati_request(VisuraIntestatiRequest(request_id="ttl_intestati", **_loc()))

    assert observed_ttls == [21600, 900]


async def test_force_bypasses_cache(main_module, monkeypatch):
    async def _hit(_key, _ttl):
        return _cached_record()

    monkeypatch.setattr(services_mod, "find_cached_response", _hit)
    service = _service(main_module)

    result = await service.add_request(VisuraRequest(request_id="new", **_loc()), force=True)

    assert result.cached is False
    assert result.request_id == "new"
    assert service.request_queue.qsize() == 1


async def test_batch_enqueues_only_cache_misses(main_module, monkeypatch):
    async def _hit_for_T(key, _ttl):
        return _cached_record("cached_T") if key == "key_T" else None

    monkeypatch.setattr(services_mod, "compute_cache_key", lambda _type, **p: f"key_{p['cadastre_type']}")
    monkeypatch.setattr(services_mod, "find_cached_response", _hit_for_T)
    service = _service(main_module)

    results = await service.add_requests_batch(
        [
            VisuraRequest(request_id="req_T", **{**_loc(), "cadastre_type": "T"}),
            VisuraRequest(request_id="req_F", **_loc()),
        ]
    )

    assert [(r.request_id, r.cached) for r in results] == [("cached_T", True), ("req_F", False)]
    assert service.request_queue.qsize() == 1
    assert service.pending_request_ids == {"req_F"}


async def test_empty_batch_is_a_noop(main_module):
    assert await _service(main_module).add_requests_batch([]) == []


def test_cache_params_include_identifiers_and_generic_params(main_module):
    service = _service(main_module)

    pnf = service._request_cache_params(
        "persona_giuridica",
        VisuraPersonaGiuridicaRequest(request_id="x", identifier="01234567890", cadastre_type="E"),
    )
    generic = service._request_cache_params(
        "indirizzo",
        GenericSisterRequest(
            request_id="y", search_type="indirizzo", cadastre_type="T", province="Roma", params={"indirizzo": "VIA X"}
        ),
    )

    assert pnf["identifier"] == "01234567890"
    assert generic["indirizzo"] == "VIA X"
    assert generic["search_type"] == "indirizzo"


@pytest.mark.parametrize(
    "method,request_type",
    [
        ("add_intestati_request", "intestati"),
        ("add_soggetto_request", "soggetto"),
        ("add_persona_giuridica_request", "persona_giuridica"),
        ("add_elenco_immobili_request", "elenco_immobili"),
        ("add_ispezione_ipotecaria_request", "ipotecaria_immobile"),
        ("add_generic_request", "indirizzo"),
    ],
)
async def test_add_methods_use_the_expected_cache_namespace(main_module, monkeypatch, method, request_type):
    seen = []

    def _key(req_type, **_params):
        seen.append(req_type)
        return "k"

    monkeypatch.setattr(services_mod, "compute_cache_key", _key)
    service = _service(main_module)
    request = {
        "add_intestati_request": lambda: VisuraIntestatiRequest(request_id="a", **_loc()),
        "add_soggetto_request": lambda: VisuraSoggettoRequest(request_id="a", fiscal_code="RSSMRA85M01H501Z"),
        "add_persona_giuridica_request": lambda: VisuraPersonaGiuridicaRequest(
            request_id="a", identifier="01234567890", cadastre_type="E"
        ),
        "add_elenco_immobili_request": lambda: ElencoImmobiliRequest(
            request_id="a", cadastre_type="T", province="Roma", municipality="ROMA"
        ),
        "add_ispezione_ipotecaria_request": lambda: IspezioneIpotecariaRequest(
            request_id="a", search_type="immobile", **_loc()
        ),
        "add_generic_request": lambda: GenericSisterRequest(
            request_id="a", search_type="indirizzo", cadastre_type="T", province="Roma"
        ),
    }[method]()

    result = await getattr(service, method)(request)

    assert result.request_id == "a"
    assert seen and set(seen) == {request_type}


async def test_paid_inspection_cache_requires_explicit_reuse(main_module, monkeypatch):
    calls = []
    cached = {
        "request_id": "paid_previous",
        "success": True,
        "tipo_catasto": "F",
        "data": {"cached": True},
        "error": None,
        "created_at": "2026-09-26T10:00:00+00:00",
    }

    async def fake_check_cache(*args, **kwargs):
        calls.append((args, kwargs))
        return cached

    monkeypatch.setattr(services_mod, "find_cached_response", fake_check_cache)
    service = _service(main_module)
    request = IspezioneIpotecariaRequest(
        request_id="paid_new", search_type="immobile", province="Roma", municipality="ROMA",
        cadastre_type="F", sheet="1", parcel="2", auto_confirm=False,
    )

    queued = await service.add_ispezione_ipotecaria_request(request)
    assert queued.request_id == "paid_new"
    assert queued.cached is False
    assert calls == []
    assert service.request_queue.qsize() == 1

    reused = await service.add_ispezione_ipotecaria_request(request, reuse_cached=True)
    assert reused.request_id == "paid_previous"
    assert reused.cached is True
    assert reused.response.data == {"cached": True}
    assert len(calls) == 1


async def test_paid_inspection_force_overrides_cache_reuse(main_module, monkeypatch):
    calls = []

    async def fake_check_cache(*args, **kwargs):
        calls.append((args, kwargs))
        return SubmitResult(request_id="paid_previous", cached=True)

    monkeypatch.setattr(services_mod, "find_cached_response", fake_check_cache)
    service = _service(main_module)
    request = IspezioneIpotecariaRequest(
        request_id="paid_forced", search_type="nota", province="Roma", note_number="4"
    )

    queued = await service.add_ispezione_ipotecaria_request(request, force=True, reuse_cached=True)
    assert queued.request_id == "paid_forced"
    assert queued.cached is False
    assert calls == []


async def test_durable_submission_fails_closed_when_database_is_read_only(main_module, monkeypatch):
    import sister.database as database

    monkeypatch.setattr(database, "is_db_writable", lambda: False)
    service = _service(main_module)
    with pytest.raises(RuntimeError, match="durable request submission is unavailable"):
        await service.add_request(VisuraRequest(request_id="read_only", **_loc()), force=True)
    assert service.request_queue.empty()


async def test_submission_rejected_when_worker_not_running(main_module):
    service = _service(main_module)
    service.processing = False

    with pytest.raises(RuntimeError, match="non in esecuzione"):
        await service.add_request(VisuraRequest(request_id="x", **_loc()))


# ---------------------------------------------------------------------------
# Auth status and background auth
# ---------------------------------------------------------------------------


def test_auth_status_ready(main_module):
    assert _service(main_module).auth_status["state"] == "ready"


def test_auth_status_reports_failure_message(main_module):
    service = _service(main_module, auth_ready=False)
    service._auth_failed_message = "nope"

    assert service.auth_status == {
        "state": "unavailable",
        "mode": "cdp",
        "message": "nope",
        "error_kind": None,
        "session_origin": None,
    }


async def test_auth_status_connecting_while_auth_task_runs(main_module):
    service = _service(main_module, auth_ready=False)
    gate = asyncio.Event()
    service._auth_task = asyncio.create_task(gate.wait())

    assert service.auth_status["state"] == "connecting"
    gate.set()
    await service._auth_task


def test_auth_status_idle_and_unavailable(main_module):
    service = _service(main_module, auth_ready=False)
    assert service.auth_status["state"] == "idle"
    service.processing = False
    assert service.auth_status["state"] == "unavailable"


async def test_background_auth_success_marks_ready(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser, auth_ready=False)

    await service._background_auth()

    assert service.auth_ready is True
    assert (browser.initialized, browser.logged_in, browser.keepalive) == (True, True, True)


@pytest.mark.parametrize(
    "exc,expected_fragment",
    [
        (AuthenticationError("bad credentials"), "Autenticazione fallita"),
        (BrowserError("no chromium"), "Errore inizializzazione browser"),
        (ValueError("weird"), "ValueError: weird"),
    ],
)
async def test_background_auth_failures_are_reported(main_module, exc, expected_fragment):
    class FailingBrowser(FakeBrowser):
        async def login(self):
            raise exc

    service = _service(main_module, FailingBrowser(), auth_ready=False)

    await service._background_auth()

    assert service.auth_ready is False
    assert expected_fragment in service.auth_status["message"]


async def test_locked_session_failure_is_reported_without_sister_owned_cleanup(main_module):
    class LockedBrowser(FakeBrowser):
        async def login(self):
            raise AuthenticationError("Login failed: User already has an active session on another terminal")

    service = _service(main_module, LockedBrowser(), auth_ready=False)

    await service._background_auth()

    assert service.auth_status["state"] == "unavailable"
    assert "active session" in service.auth_status["message"].lower()


# ---------------------------------------------------------------------------
# Manual browser control
# ---------------------------------------------------------------------------


async def test_start_browser_is_noop_when_ready(main_module):
    service = _service(main_module)

    status = await service.start_browser()

    assert status["state"] == "ready"
    assert getattr(service, "_auth_task", None) is None


async def test_start_browser_clears_previous_failure_and_authenticates(main_module):
    service = _service(main_module, auth_ready=False)
    service._auth_failed_message = "old failure"

    await service.start_browser()
    await service._auth_task

    assert not hasattr(service, "_auth_failed_message")
    assert service.auth_ready is True


async def test_stop_browser_force_closes_and_resets_auth(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser)
    service._enqueue_request_nowait(VisuraRequest(request_id="pending", **_loc()))

    await service.stop_browser(force=True)

    assert browser.closed is True
    assert service.auth_ready is False


async def test_restart_browser_stops_then_starts(main_module):
    browser = FakeBrowser()
    service = _service(main_module, browser)

    await service.restart_browser()
    with suppress(asyncio.CancelledError):
        await service._auth_task

    assert browser.closed is True
    assert service.auth_ready is True
