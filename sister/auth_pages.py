"""SISTER's own sign-in / sign-out / password-help pages.

The packaged pages (aecs4u-auth demo templates) assume e-mail + Clerk, use a GET login form, ship their own look and
call Clerk on sign-out. These routes are mounted *before* the package router so they take precedence; the actual
authentication endpoints (``POST /auth/login``, ``POST /auth/logout``, ``GET /auth/me``) are still the package's.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("sister")

router = APIRouter(include_in_schema=False)

DEFAULT_AFTER_LOGIN = "/web/"


def _safe_next(value: str | None) -> str:
    from aecs4u_auth.redirects import sanitize_next_url

    return sanitize_next_url(value, DEFAULT_AFTER_LOGIN, escape_html=False)


def _render(request: Request, template: str, **context) -> HTMLResponse:
    # Italian unless the visitor chose a language; no app sidebar for anonymous visitors (it would list the navigation).
    if "lang" not in request.query_params and "language" not in request.cookies:
        request.state.locale = "it"
    return request.app.state.theme_setup.render(template, request, user=None, sidebar_enabled=False, **context)


async def _signed_in(request: Request) -> bool:
    try:
        from aecs4u_auth.dependencies import get_current_user

        await get_current_user(request)
        return True
    except Exception:
        return False


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, next_url: str | None = None, next: str | None = None):
    target = _safe_next(next_url or next)
    if await _signed_in(request):
        return RedirectResponse(target, status_code=303)
    return _render(request, "auth_login.html", next_url=target)


@router.get("/auth/logout", response_class=HTMLResponse)
async def logout_page(request: Request):
    return _render(request, "auth_logout.html")


@router.get("/auth/forgot-password", response_class=HTMLResponse)
@router.get("/auth/reset-password", response_class=HTMLResponse)
async def password_help_page(request: Request):
    return _render(request, "auth_password_help.html")
