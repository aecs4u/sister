"""Tests for the VisuraClient async HTTP client (client.py)."""

import json

import httpx
import pytest

from sister.client import VisuraAPIError, VisuraClient

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


def _inject_transport(client: VisuraClient, handler):
    """Inject a mock transport into the client's persistent httpx.AsyncClient."""
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        headers=client._headers(),
        timeout=client.timeout,
        transport=transport,
    )


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


@pytest.mark.asyncio
async def test_client_reuses_injected_session_and_keeps_it_open():
    # it used to be closed after the first request (``async with`` around the shared client)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "healthy"})

    c = VisuraClient(base_url="http://x")
    _inject_transport(c, handler)
    shared = c._client
    await c.health()
    await c.health()
    assert len(calls) == 2 and c._client is shared and not shared.is_closed


@pytest.mark.asyncio
async def test_async_with_uses_one_client_for_every_request(monkeypatch):
    made = []

    def factory():
        made.append(httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "healthy"})
        )))
        return made[-1]

    c = VisuraClient(base_url="http://x")
    monkeypatch.setattr(c, "_get_client", factory)
    async with c:
        await c.health()
        await c.health()
    assert len(made) == 1 and made[0].is_closed


@pytest.mark.asyncio
async def test_wait_for_result_asks_the_service_to_hold_the_poll(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.params.get("wait"))
        status = "completed" if len(seen) > 1 else "processing"
        return httpx.Response(200, json={"request_id": "r1", "status": status})

    monkeypatch.setenv("VISURA_LONG_POLL", "20")
    c = VisuraClient(base_url="http://x", timeout=60, poll_interval=0.01, poll_timeout=100)
    _inject_transport(c, handler)
    result = await c.wait_for_result("r1")
    assert result["status"] == "completed"
    assert seen == ["20", "20"]


@pytest.mark.asyncio
async def test_wait_for_result_never_holds_longer_than_the_http_timeout(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.params.get("wait"))
        return httpx.Response(200, json={"request_id": "r1", "status": "completed"})

    monkeypatch.setenv("VISURA_LONG_POLL", "25")
    c = VisuraClient(base_url="http://x", timeout=5, poll_timeout=100)
    _inject_transport(c, handler)
    await c.wait_for_result("r1")
    assert seen == [None]  # 5 s timeout: no room for a held poll


def test_client_creates_fresh_when_no_injection():
    c = VisuraClient(base_url="http://x")
    c1 = c._get_client()
    c2 = c._get_client()
    # Without injection, each call creates a fresh client
    assert c1 is not c2


# ---------------------------------------------------------------------------
# _request (low-level, uses mock transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_success(client):
    async def handler(request: httpx.Request):
        return httpx.Response(200, json={"status": "ok"})

    _inject_transport(client, handler)

    result = await client._request("GET", "/test")
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_request_raises_on_4xx(client):
    async def handler(request: httpx.Request):
        return httpx.Response(404, json={"detail": "Not found"})

    _inject_transport(client, handler)

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("GET", "/missing")

    assert exc_info.value.status_code == 404
    assert "Not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_request_raises_on_5xx(client):
    async def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal error")

    _inject_transport(client, handler)

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("GET", "/fail")

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_request_extracts_detail_from_plain_text(client):
    async def handler(request: httpx.Request):
        return httpx.Response(400, text="Bad request body")

    _inject_transport(client, handler)

    with pytest.raises(VisuraAPIError) as exc_info:
        await client._request("POST", "/bad")

    assert exc_info.value.detail == "Bad request body"


@pytest.mark.parametrize(
    ("path", "expected_path"),
    [
        ("/visura", "/api/v1/visura"),
        ("/visura/history", "/api/v1/visura/history"),
        ("/catasto/workflow/run", "/api/v1/catasto/workflow/run"),
        ("/territorio", "/api/v1/territorio"),
        ("/courts/1", "/api/v1/courts/1"),
        ("/sezioni/extract", "/api/v1/sezioni/extract"),
        ("/shutdown", "/api/v1/shutdown"),
        ("/api/v1/visura", "/api/v1/visura"),
        ("/health", "/health"),
        ("/openapi.json", "/openapi.json"),
    ],
)
@pytest.mark.asyncio
async def test_request_uses_versioned_namespace_only_for_json_api_paths(client, path, expected_path):
    captured = {}

    async def handler(request: httpx.Request):
        captured["path"] = request.url.path
        return httpx.Response(200, json={"status": "ok"})

    _inject_transport(client, handler)

    await client._request("GET", path)

    assert captured["path"] == expected_path


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_sends_correct_payload(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_ids": ["req_T_abc"], "status": "queued"})

    _inject_transport(client, handler)

    result = await client.search(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="t",
        sezione="A",
        subalterno="3",
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/v1/visura")
    assert captured["body"]["tipo_catasto"] == "T"
    assert captured["body"]["sezione"] == "A"
    assert captured["body"]["subalterno"] == "3"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_search_omits_optional_fields(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_ids": [], "status": "queued"})

    _inject_transport(client, handler)

    await client.search(provincia="Trieste", comune="TRIESTE", foglio="9", particella="166")

    assert "tipo_catasto" not in captured["body"]
    assert "sezione" not in captured["body"]
    assert "subalterno" not in captured["body"]


# ---------------------------------------------------------------------------
# intestati
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intestati_sends_correct_payload(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_id": "intestati_F_xyz", "status": "queued"})

    _inject_transport(client, handler)

    result = await client.intestati(
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="f",
        subalterno="3",
    )

    assert captured["body"]["tipo_catasto"] == "F"
    assert captured["body"]["subalterno"] == "3"
    assert result["request_id"] == "intestati_F_xyz"


# ---------------------------------------------------------------------------
# get_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_result_calls_correct_url(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"request_id": "abc", "status": "completed"})

    _inject_transport(client, handler)

    result = await client.get_result("abc")
    assert captured["url"].endswith("/api/v1/visura/abc")
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# wait_for_result (mock at method level)
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
async def test_history_sends_query_params(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [], "count": 0})

    _inject_transport(client, handler)

    await client.history(provincia="Roma", tipo_catasto="t", limit=10, offset=5)

    assert captured["params"]["provincia"] == "Roma"
    assert captured["params"]["tipo_catasto"] == "T"
    assert captured["params"]["limit"] == "10"
    assert captured["params"]["offset"] == "5"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_calls_correct_endpoint(client):
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "healthy"})

    _inject_transport(client, handler)

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
