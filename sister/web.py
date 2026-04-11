"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
Auth: landing page is public; /web/* routes require authentication.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

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


def _get_user(request: Request):
    """Get current user from request state (set by auth middleware)."""
    return getattr(request.state, "user", None) or getattr(request, "user", None)


async def _require_auth(request: Request):
    """Dependency: require authenticated user or redirect to login."""
    try:
        from aecs4u_auth.dependencies import get_current_user
        return await get_current_user(request)
    except Exception:
        # Auth not configured or user not authenticated — allow in dev mode
        user = _get_user(request)
        if user:
            return user
        return None


# ---------------------------------------------------------------------------
# Public routes (no auth)
# ---------------------------------------------------------------------------


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon."""
    import os
    icon = os.path.join(os.path.dirname(__file__), "static", "icons", "favicon.ico")
    if os.path.exists(icon):
        return FileResponse(icon)
    return HTMLResponse("", status_code=204)


@router.get("/dashboard", include_in_schema=False)
async def dashboard_redirect():
    """Redirect /dashboard to /web/."""
    return RedirectResponse(url="/web/")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("sister/landing.html", request, user=user)


# ---------------------------------------------------------------------------
# Authenticated web routes
# ---------------------------------------------------------------------------


@router.get("/web/", response_class=HTMLResponse)
async def web_index(request: Request, user=Depends(_require_auth)):
    """Dashboard — service health and recent activity."""
    theme = _get_theme(request)
    stats = await count_responses()
    recent = await find_responses(limit=5)
    return theme.render(
        "sister/index.html", request, user=user,
        stats=stats, recent=recent,
    )


@router.get("/web/forms", response_class=HTMLResponse)
async def web_forms(request: Request, user=Depends(_require_auth)):
    """Query submission forms."""
    theme = _get_theme(request)
    form_groups = get_available_form_groups()
    return theme.render(
        "sister/forms.html", request, user=user,
        form_groups=form_groups,
    )


@router.get("/web/results", response_class=HTMLResponse)
async def web_results(
    request: Request,
    user=Depends(_require_auth),
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
        "sister/results.html", request, user=user,
        results=results, stats=stats,
        provincia=provincia, comune=comune, tipo_catasto=tipo_catasto,
        limit=limit, offset=offset,
    )


@router.get("/web/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str, user=Depends(_require_auth)):
    """Single result detail page."""
    theme = _get_theme(request)
    response_data = await get_response(request_id)
    if not response_data:
        return theme.render("sister/result_detail.html", request, user=user, result=None, request_id=request_id)
    return theme.render(
        "sister/result_detail.html", request, user=user,
        result=response_data, request_id=request_id,
    )


@router.get("/web/about", response_class=HTMLResponse)
async def web_about(request: Request):
    """About page (public)."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("sister/about.html", request, user=user)


@router.get("/web/privacy", response_class=HTMLResponse)
async def web_privacy(request: Request):
    """Privacy policy (public)."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("sister/privacy_policy.html", request, user=user)


# ---------------------------------------------------------------------------
# API proxy (for web form submissions)
# ---------------------------------------------------------------------------


@router.post("/web/api/batch", response_class=JSONResponse)
async def web_api_batch(request: Request, user=Depends(_require_auth)):
    """Parse CSV text and submit each row as a separate API request."""
    import csv
    import io
    import httpx

    body = await request.json()
    csv_data = body.get("csv_data", "")
    command = body.get("command", "search")

    # Parse CSV
    lines = [line for line in csv_data.strip().split("\n") if line.strip() and not line.strip().startswith("#")]
    if len(lines) < 2:
        return JSONResponse({"error": "CSV must have a header row and at least one data row"}, status_code=400)

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = [{k.strip().lower(): v.strip() for k, v in row.items() if v and v.strip()} for row in reader]

    if not rows:
        return JSONResponse({"error": "No valid data rows found"}, status_code=400)

    # Map command to API endpoint
    endpoint_map = {
        "search": "/visura",
        "intestati": "/visura/intestati",
        "soggetto": "/visura/soggetto",
        "persona-giuridica": "/visura/persona-giuridica",
        "elenco-immobili": "/visura/elenco-immobili",
        "indirizzo": "/visura/indirizzo",
        "partita": "/visura/partita",
    }
    api_path = endpoint_map.get(command, f"/visura/{command}")
    base = f"http://localhost:{request.url.port or 8025}"

    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i, row in enumerate(rows):
            try:
                resp = await client.post(f"{base}{api_path}", json=row)
                results.append({"row": i + 1, "status": "submitted", "data": resp.json()})
            except Exception as e:
                results.append({"row": i + 1, "status": "error", "error": str(e)})

    return JSONResponse({
        "command": command,
        "total_rows": len(rows),
        "results": results,
    })


@router.post("/web/api/{endpoint:path}", response_class=JSONResponse)
async def web_api_proxy(endpoint: str, request: Request, user=Depends(_require_auth)):
    """Proxy form submissions to the sister API."""
    import httpx

    body = await request.json()
    base = f"http://localhost:{request.url.port or 8025}"

    # For workflow, pass params as query string (not JSON body)
    if endpoint == "workflow":
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{base}/visura", json=body, params={"force": "false"})
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base}/visura/{endpoint}",
            json=body,
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.get("/web/api/visura/{request_id}", response_class=JSONResponse)
async def web_api_poll(request_id: str, request: Request, user=Depends(_require_auth)):
    """Poll for result status (proxy)."""
    import httpx

    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(f"{base}/visura/{request_id}")
    return JSONResponse(content=resp.json(), status_code=resp.status_code)
