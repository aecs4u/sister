"""The /web/* dependency must fail closed (docs/ui_audit_2026-10-09.md, A1/A2)."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sister import web


def _request(method: str = "GET", accept: str = "text/html", path: str = "/web/results") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(b"accept", accept.encode()), (b"host", b"localhost:8025")],
            "scheme": "http",
            "server": ("localhost", 8025),
        }
    )


@pytest.fixture
def anonymous(monkeypatch):
    """aecs4u-auth reports no authenticated user."""
    import aecs4u_auth.dependencies as deps

    async def _no_user(request):
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    monkeypatch.setattr(deps, "get_current_user", _no_user)
    monkeypatch.setattr(web, "_auth_disabled_warned", False)


@pytest.mark.asyncio
async def test_anonymous_json_client_gets_401(anonymous, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    with pytest.raises(HTTPException) as exc:
        await web._require_auth(_request(accept="application/json"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_post_gets_401_even_from_a_browser(anonymous, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    with pytest.raises(HTTPException) as exc:
        await web._require_auth(_request(method="POST", accept="text/html", path="/web/browser/stop"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_page_navigation_redirects_to_login(anonymous, monkeypatch):
    from aecs4u_auth.dependencies import RedirectToLogin

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    with pytest.raises(RedirectToLogin) as exc:
        await web._require_auth(_request())
    assert exc.value.return_url.endswith("/web/results")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "0", "no"])
async def test_explicit_dev_switch_lets_anonymous_through(anonymous, monkeypatch, value):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", value)
    assert await web._require_auth(_request()) is None


@pytest.mark.asyncio
async def test_authenticated_user_is_returned(monkeypatch):
    import aecs4u_auth.dependencies as deps

    user = object()

    async def _user(request):
        return user

    monkeypatch.setattr(deps, "get_current_user", _user)
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    assert await web._require_auth(_request()) is user


def test_auth_is_required_by_default(monkeypatch):
    monkeypatch.delenv("REQUIRE_AUTHENTICATION", raising=False)
    assert web._auth_required() is True


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_write_route_depends_on_the_auth_check():
    """No POST/PUT/DELETE web route may be registered without ``_require_auth``."""
    unprotected = []
    for route in web.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            if not _depends_on(route.dependant, web._require_auth):
                unprotected.append(route.path)
    assert unprotected == []


class _User:
    def __init__(self, role="user", is_superuser=False):
        self.role = role
        self.is_superuser = is_superuser


def test_admin_gating_is_opt_in(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    assert web._is_admin(_User()) is True  # nobody configured: every authenticated user (documented in main.py)


def test_admin_gating_when_configured(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("SISTER_ADMIN_USERS", "ops@example.com")
    assert web._is_admin(_User(role="admin")) is True
    assert web._is_admin(_User(is_superuser=True)) is True
    assert web._is_admin(_User()) is False
    assert web._is_admin(None) is False


@pytest.mark.asyncio
async def test_require_admin_returns_403_for_regular_users(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("SISTER_ADMIN_USERS", "ops@example.com")
    with pytest.raises(HTTPException) as exc:
        await web._require_admin(_request(), user=_User())
    assert exc.value.status_code == 403
    assert await web._require_admin(_request(), user=_User(role="admin")) is not None


def test_operational_routes_require_admin():
    admin_only = {
        "/web/browser",
        "/web/browser/start",
        "/web/browser/stop",
        "/web/browser/restart",
        "/web/browser/launch-chrome",
        "/web/results/refresh",
        "/web/documents/rescan",
        "/web/documents/import",
        "/web/documents/retrieve-missing-pairs",
    }
    found = set()
    for route in web.router.routes:
        if route.path in admin_only and route.methods & {"GET", "POST"}:
            calls = [d.call for d in route.dependant.dependencies]
            if web._require_admin in calls:
                found.add(route.path)
    assert found == admin_only


def test_api_docs_are_behind_the_same_authentication():
    """/docs, /redoc and /openapi.json must not be public (docs/ui_audit_2026-10-09.md, A5)."""
    import sister.main as main_module

    paths = {"/docs", "/redoc", "/openapi.json"}
    protected = set()
    for route in main_module.app.routes:
        if getattr(route, "path", None) in paths:
            if any(d.call is main_module._docs_auth for d in route.dependant.dependencies):
                protected.add(route.path)
    assert protected == paths


def test_admin_check_tolerates_a_template_without_a_user(monkeypatch):
    """Public pages (guide, cheat sheet, glossary) render the shared sidebar without a ``user``; that crashed with
    ``'user' is undefined`` once SISTER_ADMIN_USERS was set."""
    from jinja2 import Undefined

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("SISTER_ADMIN_USERS", "ops@example.com")
    assert web._is_admin(Undefined()) is False
    assert web._is_admin(None) is False


def test_public_pages_resolve_the_signed_in_user():
    """The personalised public pages must look the user up instead of relying on request.state.user (never set)."""
    import inspect

    for name in ("web_about", "web_privacy", "web_guide", "web_cheatsheet", "web_glossary"):
        assert "_optional_user" in inspect.getsource(getattr(web, name)), name


@pytest.mark.asyncio
async def test_documents_import_reports_indexed_files_and_typed_rows(monkeypatch):
    """The Documenti page "Importa nuovi" button: new files are indexed, then the typed XML tables are filled."""
    import json

    from sister import xml_ingest

    calls = []

    async def _rescan():
        calls.append("rescan")
        return 3

    async def _backfill(force=False, limit=None, batch=50):
        calls.append(("backfill", force))
        return {"documents": 2, "not_visura": 1, "failed": 0, "units": 5, "parcels": 4, "owners": 7}

    monkeypatch.setattr(web, "_rescan_documents_dir", _rescan)
    monkeypatch.setattr(xml_ingest, "backfill_documents", _backfill)

    response = await web.web_documents_import(_request(method="POST"), force=True, user=None)
    body = json.loads(response.body)

    assert response.status_code == 200
    assert calls == ["rescan", ("backfill", True)]
    assert body["indexed"] == 3 and body["force"] is True
    assert body["xml"]["owners"] == 7


@pytest.mark.asyncio
async def test_documents_import_refuses_a_second_concurrent_run(monkeypatch):
    import json

    await web._documents_import_lock.acquire()
    try:
        response = await web.web_documents_import(_request(method="POST"), force=False, user=None)
    finally:
        web._documents_import_lock.release()
    assert response.status_code == 409
    assert "in corso" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_documents_import_returns_the_error_instead_of_a_stack_trace(monkeypatch):
    import json

    async def _boom():
        raise RuntimeError("database non raggiungibile")

    monkeypatch.setattr(web, "_rescan_documents_dir", _boom)
    response = await web.web_documents_import(_request(method="POST"), force=False, user=None)
    assert response.status_code == 500
    assert json.loads(response.body)["error"] == "database non raggiungibile"
