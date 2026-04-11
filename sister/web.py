"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .database import count_responses, find_responses, get_response
from .form_config import get_available_form_groups

logger = logging.getLogger("sister")

router = APIRouter(tags=["Web UI"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_theme(request: Request):
    """Get the ThemeSetup from app state."""
    return request.app.state.theme_setup


# ---------------------------------------------------------------------------
# Public routes (no auth)
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page."""
    theme = _get_theme(request)
    return theme.render("sister/landing.html", request)


# ---------------------------------------------------------------------------
# Authenticated web routes
# ---------------------------------------------------------------------------


@router.get("/web/", response_class=HTMLResponse)
async def web_index(request: Request):
    """Dashboard — service health and recent activity."""
    theme = _get_theme(request)
    stats = await count_responses()
    recent = await find_responses(limit=5)
    return theme.render(
        "sister/index.html", request,
        stats=stats, recent=recent,
    )


@router.get("/web/forms", response_class=HTMLResponse)
async def web_forms(request: Request):
    """Query submission forms."""
    theme = _get_theme(request)
    form_groups = get_available_form_groups()
    return theme.render(
        "sister/forms.html", request,
        form_groups=form_groups,
    )


@router.get("/web/results", response_class=HTMLResponse)
async def web_results(
    request: Request,
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Results browser — paginated list from database."""
    theme = _get_theme(request)
    results = await find_responses(
        provincia=provincia, comune=comune, tipo_catasto=tipo_catasto,
        limit=limit, offset=offset,
    )
    stats = await count_responses()
    return theme.render(
        "sister/results.html", request,
        results=results, stats=stats,
        provincia=provincia, comune=comune, tipo_catasto=tipo_catasto,
        limit=limit, offset=offset,
    )


@router.get("/web/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str):
    """Single result detail page."""
    theme = _get_theme(request)
    response_data = await get_response(request_id)
    if not response_data:
        return theme.render("sister/result_detail.html", request, result=None, request_id=request_id)
    return theme.render(
        "sister/result_detail.html", request,
        result=response_data, request_id=request_id,
    )


@router.get("/web/about", response_class=HTMLResponse)
async def web_about(request: Request):
    """About page."""
    theme = _get_theme(request)
    return theme.render("sister/about.html", request)


@router.get("/web/privacy", response_class=HTMLResponse)
async def web_privacy(request: Request):
    """Privacy policy."""
    theme = _get_theme(request)
    return theme.render("sister/privacy_policy.html", request)


# ---------------------------------------------------------------------------
# API proxy (for web form submissions)
# ---------------------------------------------------------------------------


@router.post("/web/api/{endpoint:path}", response_class=JSONResponse)
async def web_api_proxy(endpoint: str, request: Request):
    """Proxy form submissions to the sister API."""
    import httpx

    body = await request.json()
    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base}/visura/{endpoint}",
            json=body,
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/web/api/visura/{request_id}", response_class=JSONResponse)
async def web_api_poll(request_id: str, request: Request):
    """Poll for result status (proxy)."""
    import httpx

    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(f"{base}/visura/{request_id}")
    return JSONResponse(content=resp.json(), status_code=resp.status_code)
