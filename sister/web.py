"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
Auth: landing page is public; /web/* routes require authentication.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from .database import (
    count_result_rows,
    count_total_result_rows,
    find_result_rows,
    find_workflow_runs,
    get_documents_for_response,
    get_result_record,
    get_workflow_result_record,
)
from .form_config import get_available_form_groups, get_single_step_groups, get_workflow_groups

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
    try:
        return getattr(request.state, "user", None)
    except Exception:
        return None


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


def _build_url(path: str, **params) -> str:
    """Build a URL with only non-empty query params."""
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def _filter_remove_url(key: str, filters: dict) -> str:
    params = {k: v for k, v in filters.items() if k != key and k != "offset"}
    return _build_url("/web/results", **params)


def _format_timestamp(value: Optional[str]) -> Optional[str]:
    """Format ISO timestamps for human-readable display."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value.replace("T", " ")[:16]


def _parse_xml_to_dict(xml_str: str) -> dict:
    """Parse XML string into a nested dict for template rendering."""
    if not xml_str or not xml_str.strip():
        return {}
    try:
        from lxml import etree

        parser = etree.XMLParser(recover=True)
        root = etree.fromstring(xml_str.encode("utf-8", errors="replace"), parser)
    except Exception:
        return {}

    def _elem_to_dict(elem):
        d = {}
        if elem.attrib:
            d.update({k: v for k, v in elem.attrib.items() if not k.startswith("{")})
        for child in elem:
            tag = child.tag
            child_val = _elem_to_dict(child)
            if tag in d:
                if not isinstance(d[tag], list):
                    d[tag] = [d[tag]]
                d[tag].append(child_val)
            else:
                d[tag] = child_val
        text = (elem.text or "").strip()
        if not d and text:
            return text
        if text:
            d["_text"] = text
        return d if d else ""

    return _elem_to_dict(root)


def _titleize_key(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _dom_id(value: str) -> str:
    """Return a conservative DOM id fragment for arbitrary response keys."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return safe or "section"


def _build_result_sections(data: Optional[dict]) -> list[dict]:
    """Normalize arbitrary response payloads into render-friendly sections."""
    if not isinstance(data, dict):
        return []

    sections: list[dict] = []
    used_dom_ids: set[str] = set()

    def next_dom_id(key: str) -> str:
        base = _dom_id(key)
        candidate = base
        suffix = 2
        while candidate in used_dom_ids:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used_dom_ids.add(candidate)
        return candidate

    def _is_scalar(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

    def _is_flat_row(row: dict) -> bool:
        return all(_is_scalar(v) for k, v in row.items() if k and k != "page_visits")

    def _clean_row(row: dict) -> dict:
        """Remove empty-string keys and normalize nulls for flat display."""
        cleaned = {}
        for k, v in row.items():
            if not k:  # skip empty-string keys
                continue
            cleaned[k] = "" if v is None else v
        return cleaned

    def _normalize_downloaded_pdfs(pdfs: list[dict]) -> list[dict]:
        """Normalize downloaded_pdfs into structured per-document dicts."""
        docs = []
        for pdf in pdfs:
            parsed = pdf.get("parsed_data") or {}
            doc: dict[str, Any] = {
                "filename": pdf.get("filename", ""),
                "file_format": pdf.get("file_format", ""),
                "file_size": pdf.get("file_size"),
                "oggetto": pdf.get("oggetto", ""),
                "richiesta_del": pdf.get("richiesta_del", ""),
                "meta": [
                    ("Filename", pdf.get("filename") or "-"),
                    ("Format", pdf.get("file_format") or "-"),
                    ("Size", f"{(pdf.get('file_size') or 0) / 1024:.1f} KB" if pdf.get("file_size") else "-"),
                    ("Oggetto", pdf.get("oggetto") or "-"),
                    ("Richiesta del", pdf.get("richiesta_del") or "-"),
                ],
                "intestati_rows": [
                    {
                        "Nominativo": row.get("Nominativo") or row.get("nominativo") or "-",
                        "Codice Fiscale": row.get("CF") or row.get("CodiceFiscale") or "-",
                        "Quota": (row.get("DirittiReali") or {}).get("Quota", "") if isinstance(row.get("DirittiReali"), dict) else "",
                        "Diritto": (row.get("DirittiReali") or {}).get("Descrizione") or (row.get("DirittiReali") or {}).get("CodiceDir", "") if isinstance(row.get("DirittiReali"), dict) else "",
                    }
                    for row in (parsed.get("intestati") or [])
                ],
                "xml_parsed": _parse_xml_to_dict(parsed.get("xml_content", "")),
            }
            docs.append(doc)
        return docs

    for key, value in data.items():
        if key == "page_visits":
            continue

        title = _titleize_key(key)
        dom_id = next_dom_id(key)
        if isinstance(value, list):
            if not value:
                continue
            if all(isinstance(item, dict) for item in value):
                if key in {"steps", "persisted_steps"}:
                    sections.append({
                        "name": key,
                        "dom_id": dom_id,
                        "title": title,
                        "kind": "workflow_steps",
                        "value": value,
                        "count": len(value),
                    })
                elif key == "downloaded_pdfs":
                    docs = _normalize_downloaded_pdfs(value)
                    sections.append({
                        "name": key,
                        "dom_id": dom_id,
                        "title": "Downloaded Documents",
                        "kind": "downloaded_docs",
                        "docs": docs,
                        "count": len(docs),
                    })
                elif all(_is_flat_row(item) for item in value):
                    cleaned_rows = [_clean_row(item) for item in value]
                    columns: list[str] = []
                    for row in cleaned_rows:
                        for col in row:
                            if col not in columns:
                                columns.append(col)
                    # Skip tables where every row is empty
                    rows = [{col: row.get(col, "") for col in columns} for row in cleaned_rows]
                    if not columns or all(all(not v for v in row.values()) for row in rows):
                        continue
                    sections.append({
                        "name": key,
                        "dom_id": dom_id,
                        "title": title,
                        "kind": "flat_table",
                        "columns": columns,
                        "rows": rows,
                        "count": len(rows),
                    })
                else:
                    sections.append({
                        "name": key,
                        "dom_id": dom_id,
                        "title": title,
                        "kind": "nested_table",
                        "value": value,
                        "count": len(value),
                    })
            else:
                sections.append({
                    "name": key,
                    "dom_id": dom_id,
                    "title": title,
                    "kind": "list",
                    "items": [str(item) for item in value],
                    "value": value,
                    "count": len(value),
                })
        elif isinstance(value, dict):
            items = [(k, v) for k, v in value.items() if k]
            sections.append({
                "name": key,
                "dom_id": dom_id,
                "title": title,
                "kind": "object",
                "items": items,
                "value": value,
                "count": len(items),
            })
        else:
            sections.append({
                "name": key,
                "dom_id": dom_id,
                "title": title,
                "kind": "value",
                "value": value,
            })
    return sections


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


def _get_auth_status() -> dict:
    """Get browser/auth status from the running service, if available."""
    from .main import visura_service
    if visura_service is not None:
        return visura_service.auth_status
    return {"state": "unavailable", "message": "Browser service not initialized"}


@router.get("/web/", response_class=HTMLResponse)
async def web_index(request: Request, user=Depends(_require_auth)):
    """Dashboard — service health and recent activity."""
    theme = _get_theme(request)
    stats = await count_result_rows()
    recent = await find_result_rows(limit=5)
    return theme.render(
        "sister/index.html", request, user=user,
        stats=stats, recent=recent,
        auth_status=_get_auth_status(),
    )


@router.get("/web/forms", response_class=HTMLResponse)
async def web_forms(request: Request, user=Depends(_require_auth)):
    """Query submission forms."""
    theme = _get_theme(request)
    return theme.render(
        "sister/forms.html", request, user=user,
        form_groups=get_available_form_groups(),
        single_step_groups=get_single_step_groups(),
        workflow_groups=get_workflow_groups(),
    )


@router.post("/web/results/refresh", response_class=HTMLResponse)
async def web_results_refresh(request: Request, user=Depends(_require_auth)):
    """Re-populate the database from exported JSON files in outputs/."""
    import asyncio
    import importlib.util
    from pathlib import Path

    from .database import DB_PATH

    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "scripts" / "populate_query_data.py"
    spec = importlib.util.spec_from_file_location("populate_query_data", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    db_path = Path(DB_PATH)
    source = project_root / "outputs"
    stats = await asyncio.to_thread(mod.populate, db_path, source, False)
    logger.info("Database refreshed: %s", stats)

    # Force the async engine to pick up data written by the sync sqlite3 connection
    from . import database as _db
    if _db._engine is not None:
        await _db._engine.dispose()
        _db._engine = None

    return RedirectResponse("/web/results", status_code=303)


@router.get("/web/results", response_class=HTMLResponse)
async def web_results(
    request: Request,
    user=Depends(_require_auth),
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Results browser — paginated list from database."""
    theme = _get_theme(request)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    results = await find_result_rows(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        source=source,
        status=status,
        limit=limit, offset=offset,
    )
    total_count = await count_total_result_rows(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        source=source,
        status=status,
    )
    for item in results:
        item["requested_at_display"] = _format_timestamp(item.get("requested_at"))
        item["responded_at_display"] = _format_timestamp(item.get("responded_at"))
        if item.get("source") != "workflow":
            item["status"] = (
                "completed" if item.get("success") is True
                else "failed" if item.get("success") is False
                else "pending"
            )
    stats = await count_result_rows(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        source=source,
    )
    current_filters = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tipo_catasto,
        "source": source,
        "status": status,
        "limit": limit,
    }
    filter_labels = {
        "provincia": "Provincia",
        "comune": "Comune",
        "foglio": "Foglio",
        "particella": "Particella",
        "tipo_catasto": "Catasto",
        "source": "Source",
        "status": "Status",
    }
    active_filters = [
        {
            "key": key,
            "label": filter_labels[key],
            "value": str(value).replace("_", " ").title() if key in {"source", "status"} else value,
            "remove_url": _filter_remove_url(key, current_filters),
        }
        for key, value in current_filters.items()
        if key in filter_labels and value not in (None, "")
    ]
    stats_filters = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tipo_catasto,
        "source": source,
    }
    stats_urls = {
        "total": _build_url("/web/results", **stats_filters),
        "completed": _build_url("/web/results", status="completed", **stats_filters),
        "partial": _build_url("/web/results", status="partial", **stats_filters),
        "failed": _build_url("/web/results", status="failed", **stats_filters),
        "pending": _build_url("/web/results", status="pending", **stats_filters),
    }
    prev_url = None
    if offset > 0:
        prev_url = _build_url("/web/results", offset=max(offset - limit, 0), **current_filters)
    next_url = None
    if offset + len(results) < total_count:
        next_url = _build_url("/web/results", offset=offset + limit, **current_filters)

    return theme.render(
        "sister/results.html", request, user=user,
        results=results, stats=stats,
        provincia=provincia, comune=comune, foglio=foglio, particella=particella,
        tipo_catasto=tipo_catasto, source=source, status=status,
        limit=limit, offset=offset,
        prev_url=prev_url, next_url=next_url,
        stats_urls=stats_urls,
        active_filters=active_filters,
        current_count=len(results),
        total_count=total_count,
    )


@router.get("/web/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str, user=Depends(_require_auth)):
    """Single result detail page."""
    theme = _get_theme(request)
    result = await get_result_record(request_id)
    if result is None:
        result = await get_workflow_result_record(request_id)
    if result is None:
        response = theme.render(
            "sister/result_detail.html", request, user=user,
            result=None, request_id=request_id, not_found=True,
        )
        if hasattr(response, "status_code"):
            response.status_code = 404
        return response

    result["requested_at_display"] = _format_timestamp(result.get("requested_at"))
    result["responded_at_display"] = _format_timestamp(result.get("responded_at"))
    result["sections"] = _build_result_sections(result.get("data"))
    result["documents"] = await get_documents_for_response(
        request_id, foglio=result.get("foglio"), particella=result.get("particella"),
    )
    for doc in result["documents"]:
        doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
        doc.pop("xml_content", None)
        # Normalize intestati server-side for flat Tabulator display
        doc["intestati_rows"] = [
            {
                "Nominativo": row.get("Nominativo") or row.get("nominativo") or "-",
                "Codice Fiscale": row.get("CF") or row.get("CodiceFiscale") or "-",
                "Quota": (row.get("DirittiReali") or {}).get("Quota", "") if isinstance(row.get("DirittiReali"), dict) else "",
                "Diritto": (row.get("DirittiReali") or {}).get("Descrizione") or (row.get("DirittiReali") or {}).get("CodiceDir", "") if isinstance(row.get("DirittiReali"), dict) else "",
                "Periodo": (row.get("DirittiReali") or {}).get("FineDiritto", "") if isinstance(row.get("DirittiReali"), dict) else "",
            }
            for row in (doc.get("intestati") or [])
        ]
        # Normalize classamento server-side
        doc["classamento_rows"] = [
            {
                "Zona Censuaria": row.get("ZonaCensuaria") or row.get("zona_censuaria") or "-",
                "Categoria": row.get("Categoria") or row.get("categoria") or "-",
                "Classe": row.get("Classe") or row.get("classe") or "-",
                "Rendita (EUR)": row.get("RenditaEuro") or row.get("rendita") or "-",
            }
            for row in (doc.get("classamento") or [])
        ]
        # Build doc metadata as a list of (label, value) tuples
        doc["meta"] = [
            ("Filename", doc.get("filename") or "-"),
            ("Oggetto", doc.get("oggetto") or "-"),
            ("Richiesta del", doc.get("richiesta_del") or "-"),
            ("Tipo", doc.get("document_type") or "-"),
            ("Provincia", doc.get("provincia") or "-"),
            ("Comune", doc.get("comune") or "-"),
            ("Foglio / Particella", f"{doc.get('foglio') or '-'} / {doc.get('particella') or '-'}"),
            ("Subalterno / Sez.Urb", f"{doc.get('subalterno') or '-'} / {doc.get('sezione_urbana') or '-'}"),
        ]
    # Normalize page_visits for flat Tabulator display
    result["page_visit_rows"] = [
        {
            "Step": v.get("step") or "-",
            "URL": v.get("url") or "-",
            "Timestamp": v.get("timestamp") or "-",
            "Errors": ", ".join(v.get("errors", [])) if v.get("errors") else "-",
        }
        for v in (result.get("page_visits") or [])
    ]
    return theme.render(
        "sister/result_detail.html", request, user=user,
        result=result, request_id=request_id, not_found=False,
    )


@router.get("/web/workflows", response_class=HTMLResponse)
async def web_workflows(
    request: Request,
    user=Depends(_require_auth),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Workflow runs list — dedicated view for workflow operations."""
    theme = _get_theme(request)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    runs = await find_workflow_runs(status=status, limit=limit, offset=offset)
    for run in runs:
        run["created_at_display"] = _format_timestamp(run.get("created_at"))
        run["updated_at_display"] = _format_timestamp(run.get("updated_at"))
    return theme.render(
        "sister/workflows.html", request, user=user,
        runs=runs, status=status, limit=limit, offset=offset,
        auth_status=_get_auth_status(),
    )


@router.get("/web/workflows/{workflow_id}", response_class=HTMLResponse)
async def web_workflow_detail(request: Request, workflow_id: str, user=Depends(_require_auth)):
    """Dedicated workflow detail page with step timing and structure."""
    theme = _get_theme(request)
    result = await get_workflow_result_record(workflow_id)
    if result is None:
        response = theme.render(
            "sister/workflow_detail.html", request, user=user,
            result=None, workflow_id=workflow_id,
        )
        if hasattr(response, "status_code"):
            response.status_code = 404
        return response

    result["requested_at_display"] = _format_timestamp(result.get("requested_at"))
    result["responded_at_display"] = _format_timestamp(result.get("responded_at"))
    result["sections"] = _build_result_sections(result.get("data"))
    result["documents"] = await get_documents_for_response(
        workflow_id, foglio=result.get("foglio"), particella=result.get("particella"),
    )
    for doc in result["documents"]:
        doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
        doc.pop("xml_content", None)
    result["page_visit_rows"] = []
    return theme.render(
        "sister/workflow_detail.html", request, user=user,
        result=result, workflow_id=workflow_id,
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

    # Map common CSV column aliases to API field names
    _COLUMN_ALIASES = {
        "p.iva": "identificativo", "piva": "identificativo", "partita_iva": "identificativo",
        "vat": "identificativo", "organization": "identificativo", "company": "identificativo",
        "denominazione": "identificativo", "ragione_sociale": "identificativo",
        "cf": "codice_fiscale", "tax_code": "codice_fiscale",
        "province": "provincia", "municipality": "comune", "city": "comune",
        "sheet": "foglio", "parcel": "particella", "sub": "subalterno",
        "type": "tipo_catasto", "catasto": "tipo_catasto",
        "address": "indirizzo", "via": "indirizzo",
    }
    for row in rows:
        for alias, canonical in _COLUMN_ALIASES.items():
            if alias in row and canonical not in row:
                row[canonical] = row.pop(alias)

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


@router.post("/web/api/workflow/stream")
async def web_api_workflow_stream(request: Request, user=Depends(_require_auth)):
    """SSE proxy for workflow streaming — passes through events incrementally."""
    import httpx

    body = await request.json()
    base = f"http://localhost:{request.url.port or 8025}"

    async def stream_events():
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream(
                "POST", f"{base}/visura/workflow/stream",
                json=body,
            ) as resp:
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event, buffer = buffer.split("\n\n", 1)
                        event = event.strip()
                        if event.startswith("data: "):
                            yield f"{event}\n\n"
                # Flush remaining buffer
                if buffer.strip().startswith("data: "):
                    yield f"{buffer.strip()}\n\n"

    return StreamingResponse(stream_events(), media_type="text/event-stream")


@router.post("/web/api/{endpoint:path}", response_class=JSONResponse)
async def web_api_proxy(endpoint: str, request: Request, user=Depends(_require_auth)):
    """Proxy form submissions to the sister API."""
    import httpx

    body = await request.json()
    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        resp = await client.post(
            f"{base}/visura/{endpoint}",
            json=body,
        )
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text or "Empty response", "status_code": resp.status_code}
    return JSONResponse(content=content, status_code=resp.status_code)


@router.get("/web/api/visura/{request_id}", response_class=JSONResponse)
async def web_api_poll(request_id: str, request: Request, user=Depends(_require_auth)):
    """Poll for result status (proxy)."""
    import httpx

    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(f"{base}/visura/{request_id}")
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text or "Empty response", "status_code": resp.status_code}
    return JSONResponse(content=content, status_code=resp.status_code)
