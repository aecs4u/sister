"""Tests for the VisuraClient async HTTP client (client.py)."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sister.client import VisuraAPIError, VisuraClient

# Save the real class before any patching
_RealAsyncClient = httpx.AsyncClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    return VisuraClient(
        base_url="http://testserver:9999",
        api_key="test-key",
        timeout=5,
        poll_interval=0.05,
        poll_timeout=0.3,
    )


def _mock_transport(handler):
    """Create an httpx mock transport from a sync/async handler."""
    return httpx.MockTransport(handler)


def _patched_client_factory(handler):
    """Return a factory that replaces httpx.AsyncClient with one using a mock transport."""

    def factory(**kw):
        kw.pop("transport", None)
        return _RealAsyncClient(transport=_mock_transport(handler), **kw)

    return factory


# ---------------------------------------------------------------------------
# Constructor / config
# ---------------------------------------------------------------------------


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("VISURA_API_URL", "http://custom:1234/")
    monkeypatch.setenv("VISURA_API_KEY", "env-key")
    monkeypatch.setenv("VISURA_API_TIMEOUT", "42")
    monkeypatch.setenv("VISURA_POLL_INTERVAL", "7")
    monkeypatch.setenv("VISURA_POLL_TIMEOUT", "120")

    c = VisuraClient()
    assert c.base_url == "http://custom:1234"  # trailing slash stripped
    assert c.api_key == "env-key"
    assert c.timeout == 42.0
    assert c.poll_interval == 7.0
    assert c.poll_timeout == 120.0


def test_explicit_params_override_env(monkeypatch):
    monkeypatch.setenv("VISURA_API_URL", "http://from-env:1234")

    c = VisuraClient(base_url="http://explicit:5678")
    assert c.base_url == "http://explicit:5678"


def test_headers_include_api_key():
    c = VisuraClient(base_url="http://x", api_key="my-key")
    headers = c._headers()
    assert headers["X-API-Key"] == "my-key"
    assert headers["Content-Type"] == "application/json"


def test_headers_omit_api_key_when_empty():
    c = VisuraClient(base_url="http://x", api_key="")
    headers = c._headers()
    assert "X-API-Key" not in headers


# ---------------------------------------------------------------------------
# _request (low-level, uses mock transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_success(client, monkeypatch):
    async def handler(request: httpx.Request):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    result = await client._request("GET", "/test")
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_request_raises_on_4xx(client, monkeypatch):
    async def handler(request: httpx.Request):
        return httpx.Response(404, json={"detail": "Not found"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("GET", "/missing")

    assert exc_info.value.status_code == 404
    assert "Not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_request_raises_on_5xx(client, monkeypatch):
    async def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal error")

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("GET", "/fail")

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_request_extracts_detail_from_plain_text(client, monkeypatch):
    async def handler(request: httpx.Request):
        return httpx.Response(400, text="Bad request body")

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("POST", "/bad")

    assert exc_info.value.detail == "Bad request body"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_sends_correct_payload(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_ids": ["req_T_abc"], "status": "queued"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    result = await client.search(
        provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
        tipo_catasto="t", sezione="A", subalterno="3",
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/visura")
    assert captured["body"]["tipo_catasto"] == "T"
    assert captured["body"]["sezione"] == "A"
    assert captured["body"]["subalterno"] == "3"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_search_omits_optional_fields(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_ids": [], "status": "queued"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    await client.search(provincia="Trieste", comune="TRIESTE", foglio="9", particella="166")

    assert "tipo_catasto" not in captured["body"]
    assert "sezione" not in captured["body"]
    assert "subalterno" not in captured["body"]


# ---------------------------------------------------------------------------
# intestati
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intestati_sends_correct_payload(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_id": "intestati_F_xyz", "status": "queued"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    result = await client.intestati(
        provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
        tipo_catasto="f", subalterno="3",
    )

    assert captured["body"]["tipo_catasto"] == "F"
    assert captured["body"]["subalterno"] == "3"
    assert result["request_id"] == "intestati_F_xyz"


# ---------------------------------------------------------------------------
# get_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_result_calls_correct_url(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"request_id": "abc", "status": "completed"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    result = await client.get_result("abc")
    assert captured["url"].endswith("/visura/abc")
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# wait_for_result (mock at _request level for simplicity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_result_returns_on_completed(client):
    call_count = 0

    async def fake_get_result(request_id):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return {"status": "processing"}
        return {"status": "completed", "data": {"ok": True}}

    client.get_result = fake_get_result

    result = await client.wait_for_result("req_1", poll_interval=0.01, poll_timeout=5)
    assert result["status"] == "completed"
    assert call_count == 3


@pytest.mark.asyncio
async def test_wait_for_result_returns_on_error(client):
    async def fake_get_result(request_id):
        return {"status": "error", "error": "failed"}

    client.get_result = fake_get_result

    result = await client.wait_for_result("req_err", poll_interval=0.01, poll_timeout=5)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_wait_for_result_returns_on_expired(client):
    async def fake_get_result(request_id):
        return {"status": "expired"}

    client.get_result = fake_get_result

    result = await client.wait_for_result("req_exp", poll_interval=0.01, poll_timeout=5)
    assert result["status"] == "expired"


@pytest.mark.asyncio
async def test_wait_for_result_raises_timeout(client):
    async def fake_get_result(request_id):
        return {"status": "processing"}

    client.get_result = fake_get_result

    with pytest.raises(TimeoutError, match="Timed out"):
        await client.wait_for_result("req_slow", poll_interval=0.05, poll_timeout=0.1)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_sends_query_params(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [], "count": 0})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    await client.history(provincia="Roma", tipo_catasto="t", limit=10, offset=5)

    assert captured["params"]["provincia"] == "Roma"
    assert captured["params"]["tipo_catasto"] == "T"
    assert captured["params"]["limit"] == "10"
    assert captured["params"]["offset"] == "5"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_calls_correct_endpoint(client, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "healthy"})

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client_factory(handler))

    result = await client.health()
    assert captured["url"].endswith("/health")
    assert result["status"] == "healthy"


# ---------------------------------------------------------------------------
# VisuraAPIError
# ---------------------------------------------------------------------------


def test_sister_error_attributes():
    e = VisuraAPIError(422, "Validation error")
    assert e.status_code == 422
    assert e.detail == "Validation error"
    assert "422" in str(e)
    assert "Validation error" in str(e)
