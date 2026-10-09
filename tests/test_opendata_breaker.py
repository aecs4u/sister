"""OpenData is a separate service: it must never stall a page (docs/ui_audit_2026-10-09.md, H5)."""

import asyncio
import time

import pytest

from sister import web


class _Resp:
    status_code = 200

    def json(self):
        return {"runs": [{"id": "wf_1"}]}


def _factory(behaviour, calls):
    class _Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return await behaviour()

    return _Client


@pytest.fixture(autouse=True)
def _closed_breaker(monkeypatch):
    monkeypatch.setattr(web, "_opendata_down_until", 0.0)


@pytest.mark.asyncio
async def test_success_returns_the_response():
    async def ok():
        return _Resp()

    calls = []
    resp = await web._opendata_get("/x", client_factory=_factory(ok, calls))
    assert resp.status_code == 200 and len(calls) == 1
    assert web.opendata_available()


@pytest.mark.asyncio
async def test_failure_opens_the_breaker_and_later_calls_do_not_touch_the_network():
    async def refused():
        raise ConnectionError("refused")

    calls = []
    assert await web._opendata_get("/x", client_factory=_factory(refused, calls)) is None
    assert not web.opendata_available()
    started = time.monotonic()
    assert await web._opendata_get("/x", client_factory=_factory(refused, calls)) is None
    assert len(calls) == 1  # second call short-circuited
    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_breaker_closes_after_the_backoff(monkeypatch):
    async def ok():
        return _Resp()

    monkeypatch.setattr(web, "_opendata_down_until", time.monotonic() - 1)
    assert web.opendata_available()
    assert (await web._opendata_get("/x", client_factory=_factory(ok, []))).status_code == 200


@pytest.mark.asyncio
async def test_a_hung_service_is_cut_off_by_the_time_budget(monkeypatch):
    monkeypatch.setattr(web, "_OPENDATA_TOTAL_TIMEOUT", 0.2)

    async def hang():
        await asyncio.sleep(30)

    started = time.monotonic()
    assert await web._opendata_get("/x", client_factory=_factory(hang, [])) is None
    assert time.monotonic() - started < 1.5
    assert not web.opendata_available()


@pytest.mark.asyncio
async def test_find_workflow_runs_returns_empty_when_down(monkeypatch):
    async def refused(*a, **k):
        return None

    monkeypatch.setattr(web, "_opendata_get", refused)
    assert await web.find_workflow_runs() == []
    assert await web.get_workflow_result_record("wf_1") is None


@pytest.mark.asyncio
async def test_an_unusable_http_library_degrades_instead_of_raising(monkeypatch):
    import sys
    import types

    broken = types.ModuleType("httpx")  # like httpx 1.0.dev: no AsyncClient / Timeout
    monkeypatch.setitem(sys.modules, "httpx", broken)
    assert await web._opendata_get("/x") is None
    assert not web.opendata_available()
