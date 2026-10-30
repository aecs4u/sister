"""The authenticated UI dependency must fail closed (docs/ui_audit_2026-10-09.md, A1/A2)."""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sister import web


def _request(method: str = "GET", accept: str = "text/html", path: str = "/results") -> Request:
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
        await web._require_auth(_request(method="POST", accept="text/html", path="/browser/stop"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_page_navigation_redirects_to_login(anonymous, monkeypatch):
    from aecs4u_auth.dependencies import RedirectToLogin

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    with pytest.raises(RedirectToLogin) as exc:
        await web._require_auth(_request())
    assert exc.value.return_url.endswith("/results")


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
    assert web._is_admin(_User()) is False  # nobody configured: nobody is an admin (fail closed)
    assert web._is_admin(_User(role="admin")) is True


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
        "/browser",
        "/browser/start",
        "/browser/stop",
        "/browser/restart",
        "/browser/launch-chrome",
        "/results/refresh",
        "/documents/rescan",
        "/documents/import",
        "/documents/retrieve-missing-pairs",
    }
    found = set()
    for route in web.router.routes:
        if route.path in admin_only and route.methods & {"GET", "POST"}:
            calls = [d.call for d in route.dependant.dependencies]
            if web._require_admin in calls:
                found.add(route.path)
    assert found == admin_only


def test_paid_query_routes_require_login_but_not_admin():
    paid_query_routes = {
        "/forms",
        "/api/ui/batch",
        "/api/ui/workflow/stream",
        "/api/ui/{endpoint:path}",
        "/api/ui/visura/{request_id}",
        "/api/ui/query-billing",
        "/api/ui/query-quote",
    }
    found = {}
    for route in web.router.routes:
        if getattr(route, "path", None) in paid_query_routes:
            found[route.path] = route
    assert set(found) == paid_query_routes
    for path, route in found.items():
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert web._require_auth in calls, path
        assert web._require_admin not in calls, path


@pytest.mark.asyncio
async def test_query_access_requires_configured_price_wallet_and_result_secret(monkeypatch):
    from types import SimpleNamespace

    from sister import purchases_billing

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr(purchases_billing, "wallet_backend", lambda: "memory")
    async def _prices():
        return {"/visura/soggetto": Decimal("25")}

    monkeypatch.setattr(web, "_query_product_prices", _prices)
    user = SimpleNamespace(id="buyer", role="user")
    assert await web._query_access_error(user) is None

    monkeypatch.delenv("SECRET_KEY")
    assert (await web._query_access_error(user)).status_code == 503

    async def _no_prices():
        return None

    monkeypatch.setattr(web, "_query_product_prices", _no_prices)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    assert (await web._query_access_error(user)).status_code == 503
    assert await web._query_access_error(_User(role="admin")) is None


@pytest.mark.asyncio
async def test_query_price_lookup_uses_command_price_then_shared_fallback(monkeypatch):
    monkeypatch.setattr(web, "_query_price_cache", None)
    monkeypatch.setattr(web, "_query_price_cache_until", 0)
    monkeypatch.setattr(web, "_query_price_failure_until", 0)

    class _Response:
        status_code = 200

        def json(self):
            return {
                "pricing": [
                    {"endpoint": "POST /visura/soggetto", "customer_credits": 27},
                    {"endpoint": "POST /visura/query", "customer_credits": 8},
                ]
            }

    async def _opendata_get(path):
        assert path == "/api/pricing"
        return _Response()

    monkeypatch.setattr(web, "_opendata_get", _opendata_get)
    assert await web._query_product_cost("/visura/soggetto") == Decimal("27")
    assert await web._query_product_cost("/visura/nota", allow_shared_fallback=True) == Decimal("8")
    assert await web._query_product_price("/visura/nota", allow_shared_fallback=True) == (
        Decimal("8"), "/visura/query"
    )
    assert await web._query_product_cost("/visura/nota") is None


@pytest.mark.asyncio
async def test_query_price_lookup_caches_upstream_failure_briefly(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(web, "_query_price_cache", None)
    monkeypatch.setattr(web, "_query_price_cache_until", 0)
    monkeypatch.setattr(web, "_query_price_failure_until", 0)
    clock = [100.0]
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = 0

    async def _opendata_get(path):
        nonlocal calls
        assert path == "/api/pricing"
        calls += 1
        return SimpleNamespace(status_code=500)

    monkeypatch.setattr(web, "_opendata_get", _opendata_get)
    assert await web._query_product_prices() is None
    assert await web._query_product_prices() is None
    assert calls == 1

    clock[0] += web._QUERY_PRICE_FAILURE_CACHE_SECONDS + 1
    assert await web._query_product_prices() is None
    assert calls == 2


@pytest.mark.asyncio
async def test_non_admin_can_quote_and_run_explicitly_paid_workflow_steps(monkeypatch):
    import json
    from types import SimpleNamespace

    from sister import purchases_billing, workflows

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr(purchases_billing, "wallet_backend", lambda: "memory")

    async def _prices():
        return {"/visura/query": Decimal("8")}

    async def _price(_path, **_kwargs):
        return Decimal("8"), "/visura/query"

    async def _balance(_user_key):
        return Decimal("1000")

    monkeypatch.setattr(web, "_query_product_prices", _prices)
    monkeypatch.setattr(web, "_query_product_price", _price)
    monkeypatch.setattr(purchases_billing, "credit_balance", _balance)

    payload = {
        "preset": "due-diligence",
        "depth": "full",
        "provincia": "Roma",
        "comune": "ROMA",
        "foglio": "100",
        "particella": "50",
        "include_paid_steps": True,
        "auto_confirm": True,
    }
    plan = workflows.prepare_workflow(payload)
    paid_steps = [step for step in plan.steps if workflows.STEP_METADATA.get(step, {}).get("paid")]
    assert paid_steps
    user = SimpleNamespace(id="buyer", role="user")

    class _JSONRequest:
        def __init__(self, body):
            self.body = body
            self.state = SimpleNamespace()
            self.url = SimpleNamespace(port=8025)

        async def json(self):
            return self.body

    quote = await web.web_api_query_quote(
        _JSONRequest({"kind": "workflow", "payload": payload}), user
    )
    quote_body = json.loads(quote.body)
    assert quote.status_code == 200
    assert quote_body["units"] == len(plan.steps)
    assert quote_body["price_path"] == "/visura/query"

    charges = []
    refunds = []

    async def _charge(user_key, units, reference, unit_cost=None):
        charges.append((user_key, units, unit_cost))
        return "created", object(), Decimal("1000")

    async def _refund(_charge, units, reference):
        refunds.append(units)

    async def _run_workflow_stream(workflow_plan, **_kwargs):
        for step in workflow_plan.steps:
            yield json.dumps({"event": "step", "step": step, "status": "completed"})

    monkeypatch.setattr(purchases_billing, "charge_query_credits", _charge)
    monkeypatch.setattr(web, "_refund_portal_query", _refund)
    monkeypatch.setattr(workflows, "run_workflow_stream", _run_workflow_stream)
    stream = await web.web_api_workflow_stream(
        _JSONRequest(
            {
                **payload,
                "_query_confirmed_price_path": quote_body["price_path"],
                "_query_confirmed_unit_cost": quote_body["unit_cost"],
                "_query_confirmed_units": quote_body["units"],
            }
        ),
        user,
    )
    events = [event async for event in stream.body_iterator]
    assert stream.status_code == 200
    assert len(events) == len(plan.steps)
    assert charges == [("id:buyer", len(plan.steps), Decimal("8"))]
    assert refunds == [0]


def test_query_submission_requires_the_exact_confirmed_price(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    user = SimpleNamespace(id="buyer", role="user")
    body = {
        "_query_confirmed_price_path": "/visura/query",
        "_query_confirmed_unit_cost": "8",
        "_query_confirmed_units": 3,
    }
    assert web._query_price_confirmation_error(user, body, 3, Decimal("8"), "/visura/query") is None
    assert body == {}

    changed_price = {
        "_query_confirmed_price_path": "/visura/query",
        "_query_confirmed_unit_cost": "8",
        "_query_confirmed_units": 3,
    }
    response = web._query_price_confirmation_error(user, changed_price, 3, Decimal("9"), "/visura/query")
    assert response.status_code == 409

    changed_batch = {
        "_query_confirmed_price_path": "/visura/query",
        "_query_confirmed_unit_cost": "8",
        "_query_confirmed_units": 2,
    }
    response = web._query_price_confirmation_error(user, changed_batch, 3, Decimal("8"), "/visura/query")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_non_admin_cannot_poll_an_unscoped_request_id(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    response = await web.web_api_poll(
        "someone-elses-request",
        _request(method="GET", accept="application/json", path="/api/ui/visura/someone-elses-request"),
        user=SimpleNamespace(id="buyer", role="user"),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_visible_document_loader_queries_only_entitled_rows_for_customers(monkeypatch):
    from types import SimpleNamespace

    calls = []

    async def visible_ids(user):
        return None if user.role == "admin" else {1886, 1887}

    async def by_ids(document_ids):
        calls.append(("ids", document_ids))
        return [{"id": document_id} for document_id in sorted(document_ids)]

    async def all_documents(*, limit):
        calls.append(("all", limit))
        return [{"id": 1}]

    monkeypatch.setattr(web, "_visible_document_ids", visible_ids)
    monkeypatch.setattr(web, "get_documents_by_ids", by_ids)
    monkeypatch.setattr(web, "get_all_documents", all_documents)

    customer_rows = await web._load_visible_documents(SimpleNamespace(role="user"))
    assert customer_rows == [{"id": 1886}, {"id": 1887}]
    assert calls == [("ids", {1886, 1887})]

    admin_rows = await web._load_visible_documents(SimpleNamespace(role="admin"))
    assert admin_rows == [{"id": 1}]
    assert calls[-1] == ("all", None)

    admin_page_rows = await web._load_visible_documents(SimpleNamespace(role="admin"), admin_limit=500)
    assert admin_page_rows == [{"id": 1}]
    assert calls[-1] == ("all", 500)


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

    for name in ("web_about", "web_privacy", "web_guide", "web_cheatsheet", "web_glossary", "web_valuation"):
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
