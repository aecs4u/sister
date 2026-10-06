"""Focused tests for the active generic browser dispatcher mapping."""

import pytest

import sister.browser as browser_module
from sister.browser import BrowserManager, _dispatcher_kwargs
from sister.models import GenericSisterRequest


def _request(**params):
    return GenericSisterRequest(
        request_id="generic_1",
        search_type="indirizzo",
        province="Roma",
        municipality="ROMA",
        cadastre_type="F",
        params=params,
    )


def test_dispatcher_kwargs_translates_aliases_and_drops_unsupported_keys():
    async def dispatcher(_page, *, foglio, particella, provincia):
        return foglio, particella, provincia

    kwargs = _dispatcher_kwargs(
        dispatcher,
        _request(sheet="9", parcel="166", unsupported="discard"),
    )

    assert kwargs == {"provincia": "Roma", "foglio": "9", "particella": "166"}


@pytest.mark.asyncio
async def test_generic_browser_dispatch_uses_registered_handler_and_wraps_data(monkeypatch):
    page = object()
    calls = []

    async def get_page():
        return page

    async def dispatcher(actual_page, *, foglio, provincia):
        calls.append((actual_page, foglio, provincia))
        return {"found": True}

    manager = BrowserManager()
    monkeypatch.setattr(manager, "_get_authenticated_page", get_page)
    monkeypatch.setitem(browser_module._GENERIC_DISPATCHERS, "indirizzo", dispatcher)

    response = await manager.esegui_generic(_request(sheet="9", unsupported="discard"))

    assert response.success is True
    assert response.data == {"found": True}
    assert calls == [(page, "9", "Roma")]
