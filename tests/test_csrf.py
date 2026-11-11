"""Cookie-derived CSRF protection (sister/csrf.py)."""

import pytest
from fastapi import FastAPI, Form, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from sister.csrf import CookieCSRFMiddleware, make_token

SECRET = "unit-test-secret"
COOKIE = "aecs4u_session"


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CookieCSRFMiddleware, cookie_name=COOKIE, secret_getter=lambda: SECRET)

    @app.post("/act")
    async def act():
        return {"ok": True}

    @app.post("/form")
    async def form(name: str = Form(...)):
        return {"name": name}

    @app.post("/api/v1/visura/x")
    async def api():
        return {"api": True}

    @app.get("/page")
    async def page(request: Request):
        return {"ok": True}

    return TestClient(app)


def test_safe_methods_are_not_checked(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.get("/page").status_code == 200


def test_cookie_authenticated_post_without_token_is_rejected(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/act").status_code == 403


def test_wrong_token_is_rejected(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/act", headers={"X-CSRF-Token": "nope"}).status_code == 403


def test_token_of_another_login_is_rejected(client):
    client.cookies.set(COOKIE, "jwt-b")
    assert client.post("/act", headers={"X-CSRF-Token": make_token(SECRET, "jwt-a")}).status_code == 403


def test_valid_header_token_is_accepted(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/act", headers={"X-CSRF-Token": make_token(SECRET, "jwt")}).status_code == 200


def test_valid_form_field_is_accepted_and_body_reaches_the_route(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/form", data={"name": "x", "csrf_token": make_token(SECRET, "jwt")})
    assert response.status_code == 200
    assert response.json() == {"name": "x"}


def test_requests_without_the_auth_cookie_are_not_checked(client):
    assert client.post("/act").status_code == 200


def test_api_key_clients_are_not_checked(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/act", headers={"X-API-Key": "k"}).status_code == 200
    assert client.post("/act", headers={"Authorization": "Bearer t"}).status_code == 200


def test_exempt_paths(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/api/v1/visura/x").status_code == 200


def test_html_clients_get_a_plain_403(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/act", headers={"accept": "text/html"})
    assert response.status_code == 403
    assert response.text == "CSRF token missing or invalid"


def test_api_clients_get_a_json_403(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/act", headers={"accept": "application/json"})
    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF token missing or invalid"}


@pytest.mark.parametrize("content_type", ["application/x-www-form-urlencoded", "multipart/form-data; boundary=test"])
@pytest.mark.parametrize("partial_body", [False, True])
@pytest.mark.parametrize("outer_middleware", [False, True])
async def test_disconnected_form_is_rejected_without_running_the_route(content_type, partial_body, outer_middleware):
    async def route(scope, receive, send):
        pytest.fail("An incomplete request must not reach the route")

    middleware = CookieCSRFMiddleware(route, cookie_name=COOKIE, secret_getter=lambda: SECRET)
    if outer_middleware:
        async def dispatch(request, call_next):
            return await call_next(request)

        middleware = BaseHTTPMiddleware(middleware, dispatch=dispatch)

    scope = {
        "type": "http", "method": "POST", "path": "/admin/credits/grant", "root_path": "",
        "scheme": "http", "query_string": b"", "http_version": "1.1",
        "headers": [(b"cookie", f"{COOKIE}=jwt".encode()), (b"content-type", content_type.encode())],
    }
    messages = []
    if partial_body:
        token = make_token(SECRET, "jwt")
        body = (
            f"--test\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n{token}\r\n--test\r\n"
            if content_type.startswith("multipart/") else f"csrf_token={token}&amount="
        ).encode()
        messages.append({"type": "http.request", "body": body, "more_body": True})

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    responses = []

    async def send(message):
        responses.append(message)

    await middleware(scope, receive, send)

    assert responses[0]["type"] == "http.response.start"
    assert responses[0]["status"] == 400
