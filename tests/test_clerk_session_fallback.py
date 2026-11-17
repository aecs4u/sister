"""A signed-in Clerk user keeps their access after Clerk's 60 s ``__session`` JWT lapses (no forced re-login)."""

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sister import web


def _request(session: dict) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"accept", b"text/html")],
            "path": "/persons",
            "query_string": b"",
            "session": session,
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def _expired(monkeypatch, status_code: int = 401):
    async def deny(request):
        raise HTTPException(status_code=status_code)

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setattr("aecs4u_auth.dependencies.get_current_user", deny)

    async def passthrough(request, user):
        return user

    monkeypatch.setattr(web, "_with_clerk_profile", passthrough)


def test_session_keeps_user_signed_in(monkeypatch):
    _expired(monkeypatch)
    user = asyncio.run(web._require_auth(_request({"auth_method": "clerk", "clerk_user_id": "user_1"})))
    assert user.id == "user_1"


def test_no_session_still_redirects_to_login(monkeypatch):
    from aecs4u_auth.dependencies import RedirectToLogin

    _expired(monkeypatch)
    with pytest.raises(RedirectToLogin):
        asyncio.run(web._require_auth(_request({})))


def test_blocked_account_is_not_bypassed(monkeypatch):
    _expired(monkeypatch, 403)
    from aecs4u_auth.dependencies import RedirectToLogin

    with pytest.raises((HTTPException, RedirectToLogin)):
        asyncio.run(web._require_auth(_request({"auth_method": "clerk", "clerk_user_id": "user_1"})))
