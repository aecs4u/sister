"""Cookie-derived CSRF protection (sister/csrf.py)."""

import pytest
from fastapi import FastAPI, Form, Request
from starlette.testclient import TestClient

from sister.csrf import CookieCSRFMiddleware, make_token

SECRET = "unit-test-secret"
COOKIE = "aecs4u_session"


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CookieCSRFMiddleware, cookie_name=COOKIE, secret_getter=lambda: SECRET)

    @app.post("/web/act")
    async def act():
        return {"ok": True}

    @app.post("/web/form")
    async def form(name: str = Form(...)):
        return {"name": name}

    @app.post("/visura/x")
    async def api():
        return {"api": True}

    @app.get("/web/page")
    async def page(request: Request):
        return {"ok": True}

    return TestClient(app)


def test_safe_methods_are_not_checked(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.get("/web/page").status_code == 200


def test_cookie_authenticated_post_without_token_is_rejected(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/web/act").status_code == 403


def test_wrong_token_is_rejected(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/web/act", headers={"X-CSRF-Token": "nope"}).status_code == 403


def test_token_of_another_login_is_rejected(client):
    client.cookies.set(COOKIE, "jwt-b")
    assert client.post("/web/act", headers={"X-CSRF-Token": make_token(SECRET, "jwt-a")}).status_code == 403


def test_valid_header_token_is_accepted(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/web/act", headers={"X-CSRF-Token": make_token(SECRET, "jwt")}).status_code == 200


def test_valid_form_field_is_accepted_and_body_reaches_the_route(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/web/form", data={"name": "x", "csrf_token": make_token(SECRET, "jwt")})
    assert response.status_code == 200
    assert response.json() == {"name": "x"}


def test_requests_without_the_auth_cookie_are_not_checked(client):
    assert client.post("/web/act").status_code == 200


def test_api_key_clients_are_not_checked(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/web/act", headers={"X-API-Key": "k"}).status_code == 200
    assert client.post("/web/act", headers={"Authorization": "Bearer t"}).status_code == 200


def test_exempt_paths(client):
    client.cookies.set(COOKIE, "jwt")
    assert client.post("/visura/x").status_code == 200


def test_html_clients_get_a_plain_403(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/web/act", headers={"accept": "text/html"})
    assert response.status_code == 403
    assert response.text == "CSRF token missing or invalid"


def test_api_clients_get_a_json_403(client):
    client.cookies.set(COOKIE, "jwt")
    response = client.post("/web/act", headers={"accept": "application/json"})
    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF token missing or invalid"}
