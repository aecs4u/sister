"""Stateless CSRF protection for the cookie-authenticated web UI.

SISTER signs users in with a JWT stored in a cookie (``aecs4u_session``), so there is no server-side session to hold a
token. The CSRF token is derived from that cookie: ``HMAC-SHA256(secret, cookie value)``. A cross-site page cannot read
it, and it changes with every login. Requests that carry no auth cookie (API-key / bearer clients, the login itself) are
not subject to the check.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Callable, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

HEADER_NAME = "X-CSRF-Token"
FORM_FIELD = "csrf_token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_EXEMPT = ("/visura", "/auth/", "/api/", "/webhooks/", "/sezioni", "/shutdown", "/static/")


def make_token(secret: str, cookie_value: str) -> str:
    return hmac.new(secret.encode("utf-8"), cookie_value.encode("utf-8"), hashlib.sha256).hexdigest()


def token_for_request(request: Request, *, secret: str, cookie_name: str) -> str:
    """Token to embed in pages ("" when the visitor is not signed in)."""
    cookie = request.cookies.get(cookie_name)
    return make_token(secret, cookie) if cookie and secret else ""


class CookieCSRFMiddleware:
    """ASGI middleware rejecting unsafe requests that rely on the auth cookie but lack a valid token."""

    def __init__(
        self,
        app,
        *,
        cookie_name: str,
        secret_getter: Callable[[], str],
        exempt_paths: Iterable[str] = DEFAULT_EXEMPT,
    ) -> None:
        self.app = app
        self.cookie_name = cookie_name
        self.secret_getter = secret_getter
        self.exempt_paths = tuple(exempt_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            return await self.app(scope, receive, send)

        request = Request(scope, receive)
        replay = receive
        path = request.url.path
        if (
            any(path.startswith(prefix) for prefix in self.exempt_paths)
            or request.headers.get("authorization")
            or request.headers.get("x-api-key")
            or not request.cookies.get(self.cookie_name)
        ):
            return await self.app(scope, receive, send)

        expected = token_for_request(request, secret=self.secret_getter(), cookie_name=self.cookie_name)
        submitted = request.headers.get(HEADER_NAME, "")
        if not submitted and request.headers.get("content-type", "").lower().startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            body = await request.body()  # cached on the request; replayed below for the route
            submitted = str((await request.form()).get(FORM_FIELD, ""))

            async def replay():  # noqa: F811 - hand the already-read body to the downstream app
                return {"type": "http.request", "body": body, "more_body": False}

        if expected and submitted and secrets.compare_digest(submitted, expected):
            return await self.app(scope, replay, send)
        return await self._reject(request)(scope, receive, send)

    @staticmethod
    def _reject(request: Request) -> Response:
        if "text/html" in request.headers.get("accept", ""):
            return Response("CSRF token missing or invalid", status_code=403)
        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
