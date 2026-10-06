"""Tests for the BrowserManager dispatch layer (browser.py).

Only the thin ``esegui_*`` wrappers are exercised: the portal functions
(``run_*``) are replaced with fakes and no page is ever driven, so these are
not browser-automation tests. They pin down argument mapping, success/error
wrapping, and the generic search-type routing.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

import sister.browser as browser_mod
import sister.utils as utils
from sister.browser import BrowserManager
from sister.models import (
    AuthenticationError,
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaRequest,
    VisuraIntestatiRequest,
    VisuraPersonaGiuridicaRequest,
    VisuraRequest,
    VisuraSoggettoRequest,
)

PAGE = object()


@pytest.fixture()
def manager(monkeypatch):
    bm = BrowserManager()

    async def _page():
        return PAGE

    monkeypatch.setattr(bm, "_get_authenticated_page", _page)
    return bm


def _fake(monkeypatch, name: str, result=None, exc: Exception | None = None):
    """Replace ``sister.browser.<name>`` with a signature-checked async fake."""
    fake = create_autospec(getattr(utils, name))
    if exc is not None:
        fake.side_effect = exc
    else:
        fake.return_value = result if result is not None else {"ok": name}
    monkeypatch.setattr(browser_mod, name, fake)
    return fake


def _visura_request(**overrides) -> VisuraRequest:
    fields = dict(
        request_id="req_F_1",
        cadastre_type="F",
        province="Roma",
        municipality="ROMA",
        sheet="100",
        parcel="50",
        subunit="3",
        section=None,
    )
    fields.update(overrides)
    return VisuraRequest(**fields)


# ---------------------------------------------------------------------------
# esegui_visura
# ---------------------------------------------------------------------------


async def test_visura_success_wraps_result_and_maps_arguments(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_visura", {"immobili": [1]})

    response = await manager.esegui_visura(_visura_request())

    assert response.success is True
    assert response.data == {"immobili": [1]}
    assert response.request_id == "req_F_1"
    kwargs = fake.call_args.kwargs
    assert kwargs["extract_intestati"] is True
    assert kwargs["request_documents"] is False
    assert kwargs["subalterno"] == "3"
    assert fake.call_args.args[:6] == (PAGE, "Roma", "ROMA", None, "100", "50")


async def test_visura_portal_error_becomes_failed_response(manager, monkeypatch):
    _fake(monkeypatch, "run_visura", exc=RuntimeError("NESSUNA PROVINCIA"))

    response = await manager.esegui_visura(_visura_request())

    assert response.success is False
    assert response.data is None
    assert "NESSUNA PROVINCIA" in response.error


async def test_visura_auth_error_becomes_failed_response(manager, monkeypatch):
    async def _no_session():
        raise AuthenticationError("session gone")

    monkeypatch.setattr(manager, "_get_authenticated_page", _no_session)

    response = await manager.esegui_visura(_visura_request())

    assert response.success is False
    assert response.error == "session gone"


# ---------------------------------------------------------------------------
# esegui_visura_intestati
# ---------------------------------------------------------------------------


async def test_intestati_call_matches_run_visura_signature(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_visura", {"intestati": []})
    request = VisuraIntestatiRequest(
        request_id="int_F_1", cadastre_type="F", province="Roma", municipality="ROMA", sheet="1", parcel="2"
    )

    response = await manager.esegui_visura_intestati(request)

    assert response.success is True, response.error
    assert "target_index" not in fake.call_args.kwargs


async def test_intestati_error_is_wrapped(manager, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("tabella intestati assente")

    monkeypatch.setattr(browser_mod, "run_visura", boom)
    request = VisuraIntestatiRequest(
        request_id="int_T_1", cadastre_type="T", province="Roma", municipality="ROMA", sheet="1", parcel="2"
    )

    response = await manager.esegui_visura_intestati(request)

    assert response.success is False
    assert response.cadastre_type == "T"


# ---------------------------------------------------------------------------
# soggetto / persona giuridica / elenco / ipotecaria
# ---------------------------------------------------------------------------


async def test_soggetto_passes_fiscal_code(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_visura_soggetto", {"soggetto": "RSSMRA85M01H501Z"})
    request = VisuraSoggettoRequest(request_id="sogg_1", fiscal_code="RSSMRA85M01H501Z", cadastre_type="E")

    response = await manager.esegui_visura_soggetto(request)

    assert response.success is True
    fake.assert_awaited_once_with(
        PAGE, "RSSMRA85M01H501Z", tipo_catasto="E", provincia=None, cognome=None, nome=None
    )


async def test_persona_giuridica_maps_identifier_and_province(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_visura_persona_giuridica")
    request = VisuraPersonaGiuridicaRequest(
        request_id="pnf_1", identifier="01234567890", cadastre_type="E", province="Genova"
    )

    response = await manager.esegui_visura_persona_giuridica(request)

    assert response.success is True
    fake.assert_awaited_once_with(PAGE, "01234567890", tipo_catasto="E", provincia="Genova")


async def test_persona_giuridica_error_is_wrapped(manager, monkeypatch):
    _fake(monkeypatch, "run_visura_persona_giuridica", exc=RuntimeError("Provincia 'X' non trovata"))
    request = VisuraPersonaGiuridicaRequest(request_id="pnf_2", identifier="01234567890", cadastre_type="E")

    response = await manager.esegui_visura_persona_giuridica(request)

    assert response.success is False
    assert "non trovata" in response.error


async def test_elenco_immobili_maps_location(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_elenco_immobili")
    request = ElencoImmobiliRequest(
        request_id="el_1", cadastre_type="T", province="Trieste", municipality="TRIESTE"
    )

    response = await manager.esegui_elenco_immobili(request)

    assert response.success is True
    kwargs = fake.call_args.kwargs
    assert (kwargs["tipo_catasto"], kwargs["provincia"], kwargs["comune"]) == ("T", "Trieste", "TRIESTE")


async def test_ispezione_ipotecaria_maps_search_type(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_ispezione_ipotecaria")
    request = IspezioneIpotecariaRequest(
        request_id="ip_1",
        cadastre_type="F",
        province="Roma",
        municipality="ROMA",
        sheet="1",
        parcel="2",
        search_type="immobile",
    )

    response = await manager.esegui_ispezione_ipotecaria(request)

    assert response.success is True, response.error
    kwargs = fake.call_args.kwargs
    assert kwargs["tipo_ricerca"] == "immobile"
    assert (kwargs["foglio"], kwargs["particella"]) == ("1", "2")
    assert kwargs["auto_confirm"] is False


async def test_ispezione_ipotecaria_forwards_subject_and_note_identifiers(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_ispezione_ipotecaria")
    request = IspezioneIpotecariaRequest(
        request_id="ip_2",
        province="Roma",
        search_type="nota",
        fiscal_code="RSSMRA85M01H501Z",
        identifier="01234567890",
        note_number="1234",
        note_year="2020",
        auto_confirm=True,
    )

    response = await manager.esegui_ispezione_ipotecaria(request)

    assert response.success is True, response.error
    kwargs = fake.call_args.kwargs
    assert kwargs["codice_fiscale"] == "RSSMRA85M01H501Z"
    assert kwargs["identificativo"] == "01234567890"
    assert (kwargs["numero_nota"], kwargs["anno_nota"]) == ("1234", "2020")
    assert kwargs["auto_confirm"] is True


# ---------------------------------------------------------------------------
# esegui_generic
# ---------------------------------------------------------------------------


def _generic(search_type: str, params: dict | None = None) -> GenericSisterRequest:
    return GenericSisterRequest(
        request_id=f"{search_type}_1",
        search_type=search_type,
        cadastre_type="F",
        province="Roma",
        municipality="ROMA",
        params=params or {},
    )


async def test_generic_unknown_search_type_is_rejected(manager):
    response = await manager.esegui_generic(_generic("nonexistent"))

    assert response.success is False
    assert "sconosciuto" in response.error


@pytest.mark.parametrize("search_type", sorted(browser_mod._NOARGS_DISPATCHERS))
async def test_generic_noargs_dispatchers_receive_only_the_page(manager, monkeypatch, search_type):
    calls = []

    async def fake(page):
        calls.append(page)
        return {"type": search_type}

    monkeypatch.setitem(browser_mod._NOARGS_DISPATCHERS, search_type, fake)

    response = await manager.esegui_generic(_generic(search_type))

    assert response.success is True
    assert calls == [PAGE]


async def test_generic_indirizzo_forwards_extra_params(manager, monkeypatch):
    captured = {}

    async def fake(page, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setitem(browser_mod._GENERIC_DISPATCHERS, "indirizzo", fake)

    response = await manager.esegui_generic(_generic("indirizzo", {"indirizzo": "VIA ROMA"}))

    assert response.success is True
    assert captured["indirizzo"] == "VIA ROMA"
    assert captured["provincia"] == "Roma"
    assert captured["foglio"] is None


async def test_generic_search_with_foglio_param_reaches_dispatcher(manager, monkeypatch):
    async def fake(page, **kwargs):
        return {"foglio": kwargs["foglio"]}

    monkeypatch.setitem(browser_mod._GENERIC_DISPATCHERS, "mappa", fake)

    response = await manager.esegui_generic(_generic("mappa", {"foglio": "9", "particella": "166"}))

    assert response.success is True, response.error
    assert response.data == {"foglio": "9"}


async def test_generic_visura_immobile_reads_english_or_italian_param_names(manager, monkeypatch):
    fake = _fake(monkeypatch, "run_visura_immobile")

    response = await manager.esegui_generic(
        _generic("visura_immobile", {"sheet": "9", "particella": "166", "sub": "x"})
    )

    assert response.success is True, response.error
    kwargs = fake.call_args.kwargs
    assert kwargs["foglio"] == "9"
    assert kwargs["particella"] == "166"
    assert kwargs["subalterno"] is None
    assert "tipo_catasto" not in kwargs


async def test_generic_visura_immobile_never_falls_back_to_sample_defaults(manager, monkeypatch):
    """run_visura_immobile defaults to Trieste F.9 P.166; missing params must reach it as None."""
    fake = _fake(monkeypatch, "run_visura_immobile")

    await manager.esegui_generic(_generic("visura_immobile"))

    kwargs = fake.call_args.kwargs
    assert (kwargs["provincia"], kwargs["comune"]) == ("Roma", "ROMA")
    assert (kwargs["foglio"], kwargs["particella"]) == (None, None)


def test_italian_param_name_wins_over_english_alias():
    async def dispatcher(page, provincia, foglio=None):
        return None

    kwargs = browser_mod._dispatch_kwargs(dispatcher, _generic("mappa", {"sheet": "1", "foglio": "2"}))

    assert kwargs == {"provincia": "Roma", "foglio": "2"}


_ROUTE_PARAMS = {
    "foglio": "9",
    "particella": "166",
    "indirizzo": "VIA ROMA",
    "partita": "123",
    "numero_nota": "1",
    "anno_nota": "2020",
}


@pytest.mark.parametrize("search_type", sorted(browser_mod._GENERIC_DISPATCHERS))
async def test_every_generic_dispatcher_can_be_called_with_route_params(manager, monkeypatch, search_type):
    """Signature-checked fakes: esegui_generic must only pass keywords each real dispatcher accepts."""
    real = browser_mod._GENERIC_DISPATCHERS[search_type]
    fake = create_autospec(real, return_value={"ok": search_type})
    monkeypatch.setitem(browser_mod._GENERIC_DISPATCHERS, search_type, fake)

    response = await manager.esegui_generic(_generic(search_type, _ROUTE_PARAMS))

    assert response.success is True, response.error
    kwargs = fake.call_args.kwargs
    assert kwargs["provincia"] == "Roma"
    if "foglio" in inspect.signature(real).parameters:
        assert kwargs["foglio"] == "9"


async def test_cdp_close_delegates_disconnect_policy_to_auth_library():
    bm = BrowserManager()
    auth_close = AsyncMock()
    bm._auth = SimpleNamespace(close=auth_close)

    await bm.close()

    auth_close.assert_awaited_once()


async def test_auth_session_api_reports_reuse_origin(manager):
    from datetime import UTC, datetime

    session = SimpleNamespace(authenticated_at=datetime.now(UTC), origin="reused")
    ensure = AsyncMock(return_value=session)
    keepalive = AsyncMock()
    manager._auth = SimpleNamespace(
        ensure_service_session=ensure,
        start_keepalive=keepalive,
        session_origin="reused",
    )

    await manager.login()

    ensure.assert_awaited_once_with("sister")
    keepalive.assert_awaited_once()
    assert manager.session_origin == "reused"


def test_auth_browser_configuration_is_public():
    manager = BrowserManager()

    assert manager._auth.config.no_viewport is True
    assert "--start-maximized" in manager._auth.config.browser_args
