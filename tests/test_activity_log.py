"""Database record of operational actions (activity_log)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from sister import database, web


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/web/documents/import",
            "query_string": b"",
            "headers": [(b"host", b"localhost:8025")],
            "scheme": "http",
            "server": ("localhost", 8025),
            "client": ("10.1.2.3", 5555),
        }
    )


class _User:
    email = "ops@example.com"


@pytest.mark.usefixtures("fresh_db")
async def test_record_activity_stores_who_what_and_outcome():
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    row_id = await database.record_activity(
        "documents.import",
        actor="ops@example.com",
        params={"force": False},
        result={"indexed": 3, "xml": {"documents": 2}},
        started_at=started,
        client_ip="10.1.2.3",
    )
    assert row_id is not None

    (row,) = await database.get_recent_activity()
    assert row["action"] == "documents.import"
    assert row["actor"] == "ops@example.com"
    assert row["status"] == "success"
    assert row["source"] == "web"
    assert row["params"] == {"force": False}
    assert row["result"]["xml"]["documents"] == 2
    assert row["client_ip"] == "10.1.2.3"
    assert row["duration_ms"] >= 2000


@pytest.mark.usefixtures("fresh_db")
async def test_record_activity_accepts_values_json_cannot_hold_natively():
    await database.record_activity(
        "db.backfill", source="cli", result={"when": datetime(2026, 10, 10, tzinfo=timezone.utc), "n": 1}
    )
    (row,) = await database.get_recent_activity(action="db.backfill")
    assert row["source"] == "cli"
    assert row["result"]["n"] == 1
    assert "2026-10-10" in row["result"]["when"]


@pytest.mark.usefixtures("fresh_db")
async def test_recent_activity_is_newest_first_and_filterable():
    await database.record_activity("documents.rescan", result={"indexed": 0})
    await database.record_activity("documents.import", status="error", error="boom")
    rows = await database.get_recent_activity()
    assert [r["action"] for r in rows] == ["documents.import", "documents.rescan"]
    assert rows[0]["status"] == "error" and rows[0]["error"] == "boom"
    assert [r["action"] for r in await database.get_recent_activity(action="documents.rescan")] == [
        "documents.rescan"
    ]


async def test_record_activity_never_raises_when_the_database_is_unusable(monkeypatch):
    """The audit trail must not break the action it describes."""

    def _broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(database, "is_db_writable", lambda: True)
    monkeypatch.setattr(database, "_get_session_factory", _broken)
    assert await database.record_activity("documents.import") is None


async def test_record_activity_skips_when_persistence_is_off(monkeypatch):
    monkeypatch.setattr(database, "is_db_writable", lambda: False)
    assert await database.record_activity("documents.import") is None


async def _capture(monkeypatch):
    calls = []

    async def _record(action, **kwargs):
        calls.append({"action": action, **kwargs})

    monkeypatch.setattr(web, "record_activity", _record)
    return calls


async def test_audited_route_records_actor_params_and_result(monkeypatch):
    calls = await _capture(monkeypatch)

    @web._audited("browser.stop")
    async def handler(request: Request, force: bool = False, user=None):
        return JSONResponse({"stopped": True, "pages": ["a", "b"]})

    response = await handler(request=_request(), force=True, user=_User())

    assert response.status_code == 200
    (call,) = calls
    assert call["action"] == "browser.stop"
    assert call["actor"] == "ops@example.com"
    assert call["status"] == "success"
    assert call["params"] == {"force": True}
    assert call["result"] == {"http_status": 200, "stopped": True, "pages": 2}
    assert call["client_ip"] == "10.1.2.3"


async def test_audited_route_marks_a_503_as_error_and_a_403_as_rejected(monkeypatch):
    calls = await _capture(monkeypatch)

    @web._audited("browser.start")
    async def unavailable(request: Request, user=None):
        return JSONResponse({"error": "Service not initialized"}, status_code=503)

    @web._audited("browser.start")
    async def forbidden(request: Request, user=None):
        raise HTTPException(status_code=403, detail="Admin role required")

    await unavailable(request=_request(), user=_User())
    with pytest.raises(HTTPException):
        await forbidden(request=_request(), user=_User())

    assert [(c["status"], c.get("error")) for c in calls] == [
        ("error", None),
        ("rejected", "Admin role required"),
    ]
    assert calls[0]["result"]["error"] == "Service not initialized"


def test_audited_routes_keep_their_admin_dependency():
    """Wrapping a handler must not hide its FastAPI dependencies (the admin gate)."""
    audited = {
        "/web/browser/start",
        "/web/browser/stop",
        "/web/browser/restart",
        "/web/browser/launch-chrome",
        "/web/documents/retrieve-missing-pairs",
    }
    found = set()
    for route in web.router.routes:
        if route.path in audited and "POST" in route.methods:
            if web._require_admin in [d.call for d in route.dependant.dependencies]:
                found.add(route.path)
    assert found == audited


@pytest.mark.parametrize("payload", [{"email": "a@b.c"}, {"username": "operatore"}, {}])
def test_actor_falls_back_to_anonymous(payload):
    user = type("U", (), payload)()
    expected = payload.get("email") or payload.get("username") or "anonymous"
    assert web._actor(user) == expected
    assert web._actor(None) == "anonymous"
    assert json.dumps(web._actor(user))
