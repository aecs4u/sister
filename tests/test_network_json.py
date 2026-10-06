"""Tests for local SISTER JSON capture and property-row extraction."""

import asyncio
import json

import pytest

from sister.browser import _run_with_network_json
from sister.network_json import SISTERJsonCapture, extract_property_rows


class _Page:
    url = "https://sister3.agenziaentrate.gov.it/Visure/Ricerca.do"

    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit_response(self, response):
        for callback in self.listeners.get("response", []):
            callback(response)


class _Response:
    def __init__(self, url, data, content_type="application/json", status=200):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._data = data.encode() if isinstance(data, str) else data

    async def body(self):
        return self._data


@pytest.mark.asyncio
async def test_capture_json_and_jsonp_only_from_sister_domain():
    page = _Page()
    capture = await SISTERJsonCapture(page).start()
    page.emit_response(_Response(
        "https://sister3.agenziaentrate.gov.it/api/immobili",
        '{"items":[{"foglio":"4","particella":"3993","token":"secret"}]}',
    ))
    page.emit_response(_Response(
        "https://sister3.agenziaentrate.gov.it/Visure/data.js",
        'callback({"ok":true,"rows":[]});',
        "text/plain",
    ))
    page.emit_response(_Response(
        "https://example.invalid/data.json", '{"ignored":true}'
    ))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    responses = await capture.stop()
    assert len(responses) == 2
    assert responses[0]["url"] == "/api/immobili"
    assert responses[0]["data"]["items"][0] == {"foglio": "4", "particella": "3993"}
    assert responses[1]["data"] == {"ok": True, "rows": []}
    assert not page.listeners["response"]


def test_extracts_property_rows_and_skips_soppressa():
    payload = {
        "result": {
            "immobili": [
                {"foglio": "4", "particella": "3993", "subalterno": "2", "categoria": "A/2"},
                {"foglio": "4", "particella": "3994", "stato": "Soppressa"},
            ]
        }
    }
    rows = extract_property_rows(payload)
    assert rows == [
        {"Foglio": "4", "Particella": "3993", "Sub": "2", "Categoria": "A/2"}
    ]


@pytest.mark.asyncio
async def test_capture_supplements_existing_dom_extraction_locally():
    page = _Page()

    async def extract():
        page.emit_response(_Response(
            "https://sister3.agenziaentrate.gov.it/api/immobili.json",
            json.dumps({"items": [{"foglio": "4", "particella": "3993", "categoria": "A/2"}]}),
        ))
        await asyncio.sleep(0)
        return {"immobili": [], "total_results": 0}

    result = await _run_with_network_json(page, extract)
    assert result["immobili"] == [
        {"Foglio": "4", "Particella": "3993", "Categoria": "A/2"}
    ]
    assert result["total_results"] == 1
    assert result["network_json_responses"][0]["data"]["items"][0]["foglio"] == "4"


@pytest.mark.asyncio
async def test_run_attaches_ambiguous_option_decisions(monkeypatch):
    from sister.utils import _record_ambiguous_option

    monkeypatch.delenv("SISTER_JEV_ENABLED", raising=False)
    page = _Page()

    async def extract():
        _record_ambiguous_option(
            "select[name='listacom']", "PA", [(1.0, "082", "PA NORD"), (1.0, "083", "PA EAST")], "082",
            "local_tie_break",
        )
        return {"immobili": [], "total_results": 0}

    result = await _run_with_network_json(page, extract)
    assert result["ambiguous_options"] == [{
        "control": "listacom",
        "requested": "PA",
        "candidates": ["PA NORD", "PA EAST"],
        "selected": "PA NORD",
        "resolution": "local_tie_break",
    }]
    assert "network_json_responses" not in result


@pytest.mark.asyncio
async def test_run_omits_ambiguous_options_when_there_were_none():
    async def extract():
        return {"immobili": [], "total_results": 0}

    result = await _run_with_network_json(_Page(), extract)
    assert "ambiguous_options" not in result

