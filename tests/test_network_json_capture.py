"""Network JSON capture tests that do not need a Playwright browser."""

import json

import pytest

import sister.network_json as network_json


class _Page:
    url = "https://sister3.agenziaentrate.gov.it/Visure/Ricerca.do"

    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, response):
        for callback in self.listeners.get("response", []):
            callback(response)


class _Response:
    def __init__(self, url, body, *, status=200, headers=None):
        self.url = url
        self.status = status
        self.headers = headers or {"content-type": "application/json"}
        self._body = body.encode() if isinstance(body, str) else body
        self.body_reads = 0

    async def body(self):
        self.body_reads += 1
        return self._body


def test_json_like_accepts_xssi_and_jsonp_without_evaluating_code():
    assert network_json._json_like(")]}'\n{\"ok\":true}") == {"ok": True}
    assert network_json._json_like("callback({\"rows\":[]});") == {"rows": []}
    assert network_json._json_like("callback(alert(1))") is None
    assert network_json._json_like("true") is None


def test_safe_json_removes_secrets_recursively_and_bounds_depth():
    payload = {
        "items": [{"foglio": "9", "access_token": "secret", "value": "ok"}],
        "cookieJar": "secret",
    }
    assert network_json._safe_json(payload) == {"items": [{"foglio": "9", "value": "ok"}]}

    nested = value = {}
    for _ in range(22):
        value["child"] = {}
        value = value["child"]
    safe_nested = network_json._safe_json(nested)
    for _ in range(20):
        safe_nested = safe_nested["child"]
    assert safe_nested["child"] is None


@pytest.mark.asyncio
async def test_capture_filters_origin_status_and_unparseable_payloads():
    page = _Page()
    capture = await network_json.SISTERJsonCapture(page).start()
    external = _Response("https://example.invalid/a.json", "{}")
    failed = _Response("https://sister3.agenziaentrate.gov.it/a.json", "{}", status=503)
    invalid = _Response("https://sister3.agenziaentrate.gov.it/a.json", "not json")
    accepted = _Response(
        "https://sister3.agenziaentrate.gov.it/api/result",
        json.dumps({"rows": [{"foglio": "9", "sessionToken": "private"}]}),
    )
    for response in (external, failed, invalid, accepted):
        page.emit(response)

    records = await capture.stop()

    assert len(records) == 1
    assert records[0]["url"] == "/api/result"
    assert records[0]["data"] == {"rows": [{"foglio": "9"}]}
    assert external.body_reads == failed.body_reads == 0
    assert invalid.body_reads == accepted.body_reads == 1
    assert page.listeners["response"] == []


@pytest.mark.asyncio
async def test_capture_enforces_declared_payload_and_total_byte_limits(monkeypatch):
    monkeypatch.setattr(network_json, "MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(network_json, "MAX_TOTAL_BYTES", 3)
    page = _Page()
    capture = await network_json.SISTERJsonCapture(page).start()
    too_large = _Response(
        "https://sister3.agenziaentrate.gov.it/a.json",
        b"{}",
        headers={"content-type": "application/json", "content-length": "5"},
    )
    accepted = _Response("https://sister3.agenziaentrate.gov.it/a.json", b"{}")
    over_total = _Response("https://sister3.agenziaentrate.gov.it/b.json", b"[]")
    for response in (too_large, accepted, over_total):
        page.emit(response)

    records = await capture.stop()

    assert len(records) == 1
    assert records[0]["data"] == {}
    assert too_large.body_reads == 0
    assert accepted.body_reads == over_total.body_reads == 1


@pytest.mark.asyncio
async def test_capture_stops_accepting_after_response_count_limit(monkeypatch):
    monkeypatch.setattr(network_json, "MAX_RESPONSES", 1)
    page = _Page()
    capture = await network_json.SISTERJsonCapture(page).start()
    accepted = _Response("https://sister3.agenziaentrate.gov.it/a.json", "{}")
    ignored = _Response("https://sister3.agenziaentrate.gov.it/b.json", "[]")
    page.emit(accepted)
    await capture.stop()
    page.emit(ignored)
    records = await capture.stop()

    assert len(records) == 1
    assert accepted.body_reads == 1
    assert ignored.body_reads == 0
