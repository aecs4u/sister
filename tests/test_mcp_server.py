"""MCP dispatch coverage for the guarded mortgage-search API tools."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from sister import mcp_server


class _FakeServer:
    def __init__(self, name: str):
        self.name = name
        self.tools: dict[str, Any] = {}

    def tool(self, *, title: str, annotations: Any):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


def _install_mcp_sdk(monkeypatch: pytest.MonkeyPatch) -> list[_FakeServer]:
    servers: list[_FakeServer] = []

    class ToolAnnotations:
        def __init__(self, **values: Any):
            self.values = values

    server_module = ModuleType("mcp.server")

    def make_server(name: str) -> _FakeServer:
        server = _FakeServer(name)
        servers.append(server)
        return server

    server_module.MCPServer = make_server
    types_module = ModuleType("mcp.types")
    types_module.ToolAnnotations = ToolAnnotations
    package = ModuleType("mcp")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "mcp", package)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)
    return servers


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def mortgage_search_start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"flow_id": 17}

    async def mortgage_search_status(self, flow_id):
        self.calls.append(("status", {"flow_id": flow_id}))
        return {"flow_id": flow_id, "state": "preview"}

    async def mortgage_search_quote(self, flow_id, **kwargs):
        self.calls.append(("quote", {"flow_id": flow_id, **kwargs}))
        return {"flow_id": flow_id, "quote": {"portal_request_id": kwargs["portal_request_id"]}}


@pytest.mark.asyncio
async def test_mcp_mortgage_tools_dispatch_preview_status_and_exact_save_quote(monkeypatch):
    servers = _install_mcp_sdk(monkeypatch)
    client = _FakeClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: client)
    server = mcp_server.create_server()

    assert servers == [server]
    assert {"start_mortgage_search", "mortgage_search_status", "mortgage_search_quote"}.issubset(server.tools)
    assert not any("confirm" in name for name in server.tools)

    denied = await server.tools["start_mortgage_search"](
        provincia="Trieste", codice_fiscale="RSSMRA80A01H501U", confirm_start=False
    )
    assert denied["started"] is False
    assert client.calls == []

    started = await server.tools["start_mortgage_search"](
        provincia="Trieste", codice_fiscale="RSSMRA80A01H501U", confirm_start=True
    )
    assert started == {"started": True, "flow_id": 17, "paid_confirmation_available_here": False}
    assert await server.tools["mortgage_search_status"](17) == {"flow_id": 17, "state": "preview"}
    quoted = await server.tools["mortgage_search_quote"](
        flow_id=17, step="richieste_save", portal_request_id="REQ-12"
    )
    assert quoted["quote"]["portal_request_id"] == "REQ-12"
    assert client.calls == [
        ("start", {"provincia": "Trieste", "codice_fiscale": "RSSMRA80A01H501U"}),
        ("status", {"flow_id": 17}),
        (
            "quote",
            {
                "flow_id": 17,
                "step": "richieste_save",
                "ordinals": None,
                "ordinal": None,
                "portal_request_id": "REQ-12",
                "format": "PDF",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_mcp_marks_paid_queries_and_refuses_submission(monkeypatch):
    _install_mcp_sdk(monkeypatch)

    class QueryClient:
        calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def submit(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"request_id": "should-not-send"}

    client = QueryClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_prepared_queries", {})
    server = mcp_server.create_server()

    queries = server.tools["list_queries"]()
    paid_query = next(item for item in queries if item["command"] == "ipotecaria-immobile")
    assert paid_query["paid"] is True
    preview = server.tools["preview_query"]("ipotecaria-immobile", {"provincia": "Trieste"})
    assert preview["valid"] and preview["paid"] is True

    submitted = await server.tools["submit_query"](
        preview_id=preview["preview_id"], confirm_submission=True
    )

    assert submitted["submitted"] is False and submitted["paid"] is True
    assert "admin review flow" in submitted["error"]
    assert client.calls == []
