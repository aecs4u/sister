"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
Auth: landing page is public; authenticated UI routes require authentication.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterable, Optional
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import TemplateNotFound
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .database import (
    count_result_rows,
    count_total_result_rows,
    find_documents_with_extraction,
    find_parcel_document_ids,
    find_parcel_request_records,
    find_parcels_with_geometry,
    find_result_rows,
    find_subject_document_associations,
    find_subject_inspection_document_ids,
    find_subject_query_requests,
    get_all_documents,
    get_cadastral_sibling_visuras,
    get_comune_centroids,
    get_db_owners_for_response,
    get_db_properties_for_response,
    get_document_by_id,
    get_document_group_siblings,
    get_document_owner_fiscal_codes,
    get_document_structured_extraction,
    get_documents_by_ids,
    get_documents_for_response,
    get_response,
    get_result_record,
    get_subject_document_ids,
    get_subject_document_ids_for_fiscal_codes,
    get_subject_fiscal_code_for_document,
    get_subject_name,
    iter_all_documents,
    parcel_key,
    record_activity,
    search_person_profiles,
)
from .document_scope import subject_search_scope as _subject_search_scope
from .form_config import get_available_form_groups, get_single_step_groups, get_workflow_groups
from .omi_zones import omi_boundary_geometries, omi_zone_code, omi_zone_for_parcel
from .request_types import known_request_tags, known_request_types, request_type_info
from .result_parsers import format_amount_it, parse_consistency
from .visura_view import build_visura_view

# Opendata API URL — workflow runs/steps are owned by opendata, not sister.
# Sister's web UI proxies workflow list/detail requests to opendata.
_OPENDATA_API_URL = os.getenv("OPENDATA_API_URL", "http://localhost:8024")
_QUERY_SHARED_PRICE_PATH = os.getenv("SISTER_QUERY_SHARED_PRICE_PATH", "/visura/query")
_QUERY_PRICE_CACHE_SECONDS = 60.0
_QUERY_PRICE_FAILURE_CACHE_SECONDS = 30.0
_query_price_cache_until = 0.0
_query_price_cache: dict[str, Decimal] | None = None
_query_price_failure_until = 0.0
_DOCUMENTS_PAGE_SIZE = 100
_QUERY_FORM_TAG = re.compile(r'<form id="(form-[^"]+)" class="sister-form"')


# Base directory for the document browser (/documents).
# Defaults to the parent of the DB data folder (the project data root).
def _files_base() -> "Path":
    from pathlib import Path

    from .database import DATA_ROOT

    return Path(os.getenv("SISTER_FILES_BASE", str(DATA_ROOT / "documents"))).resolve()


def _ispezione_group_key(filename: str) -> str:
    """Return the shared inspection key for a report and its individual notes."""
    stem = Path(filename).stem
    match = re.match(r"^(isp_.+?_T\d+_\d{4})(?:_(?:RP\d+|DOC_.+))?(?:_\d{4}-\d{2}-\d{2})?$", stem, re.IGNORECASE)
    return (match.group(1) if match else stem).casefold()


logger = logging.getLogger("sister")

_OPTIONAL_DATA_DEGRADATIONS_LOCK = Lock()
_OPTIONAL_DATA_DEGRADATIONS: dict[str, int] = {}
_OPTIONAL_DATA_ACTIVE_FAILURES: set[str] = set()


def _record_optional_data_degradation(component: str) -> None:
    """Count optional-data failures without retaining identifiers or error text."""
    with _OPTIONAL_DATA_DEGRADATIONS_LOCK:
        _OPTIONAL_DATA_DEGRADATIONS[component] = _OPTIONAL_DATA_DEGRADATIONS.get(component, 0) + 1
        _OPTIONAL_DATA_ACTIVE_FAILURES.add(component)


def _clear_optional_data_degradation(component: str) -> None:
    """Clear the active failure after a later successful lookup; keep the process-lifetime count."""
    with _OPTIONAL_DATA_DEGRADATIONS_LOCK:
        _OPTIONAL_DATA_ACTIVE_FAILURES.discard(component)


def optional_data_degradation_summary() -> dict[str, Any]:
    """Return process-lifetime counts for optional data used in privileged health details."""
    with _OPTIONAL_DATA_DEGRADATIONS_LOCK:
        counts = dict(_OPTIONAL_DATA_DEGRADATIONS)
        active = sorted(_OPTIONAL_DATA_ACTIVE_FAILURES)
    return {
        "since_process_start": True,
        "total": sum(counts.values()),
        "by_component": counts,
        "active_components": active,
    }

# The UI is authored in Italian: pin <html lang="it"> and the theme's own strings to Italian unless the visitor chose
# a language explicitly (?lang= or the language cookie). Without this the theme follows Accept-Language.


async def _pin_content_locale(request: Request) -> None:
    if "lang" not in request.query_params and "language" not in request.cookies:
        request.state.locale = "it"


router = APIRouter(tags=["Web UI"], dependencies=[Depends(_pin_content_locale)])

_missing_pair_job: dict[str, Any] | None = None
_missing_pair_task: asyncio.Task | None = None
_missing_pair_job_lock = asyncio.Lock()


# Document types that render in a dedicated visura template (so "Apri" is meaningful);
# others only offer the exhaustive view + download.
_VIEWABLE_DOC_TYPES = {
    "visura_fabbricati",
    "visura_storica",
    "visura_terreni",
    "visura_soggetto",
    "planimetria",
    "ispezione_ipotecaria",
}

# document_types that are "visure" (excludes visura_soggetto, which lives under Soggetto).
_VISURA_TYPES = {"visura", "visura_fabbricati", "visura_terreni", "visura_storica"}


def _doc_tipo_visura(d: dict) -> Optional[str]:
    """Classify a visura along the *Tipo* facet: storica / analitica / sintetica.

    Driven by document_type with an oggetto-text fallback. Returns None when the
    document does not fit any tipo (kept out of the Tipo facet).
    """
    dt = d.get("document_type") or ""
    title = (d.get("oggetto") or "").lower()
    if "storic" in title:
        return "storica"
    if "sintetic" in title:
        return "sintetica"
    if dt in ("visura_fabbricati", "visura_terreni") or "analitic" in title:
        return "analitica"
    if dt == "visura":
        return "sintetica"
    return None


def _doc_catasto_ft(d: dict) -> Optional[str]:
    """Classify a property visura by cadastre, including canonical filename cues."""
    dt = d.get("document_type") or ""
    tc = str(d.get("tipo_catasto") or "").strip().upper()
    if dt == "visura_terreni" or tc in {"T", "CT", "TERRENI"}:
        return "terreni"
    if dt == "visura_fabbricati" or tc in {"F", "CF", "FABBRICATI"}:
        return "fabbricati"

    filename = str(d.get("filename") or d.get("file_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = re.match(r"^vi_(?:att|sto|sin)(?:_(ter))?_", filename, re.IGNORECASE)
    if match:
        return "terreni" if match.group(1) else "fabbricati"

    title = str(d.get("oggetto") or "").casefold()
    title_and_filename = f"{title} {filename.casefold()}"
    if "terren" in title_and_filename or "fondo rustico" in title_and_filename:
        return "terreni"
    if "fabbricat" in title_and_filename or "urbano" in title_and_filename:
        return "fabbricati"
    return None


def _doc_visura_subtype(d: dict) -> str:
    """Use parsed metadata, display text, or canonical ``vi_*`` filename to label a visura."""
    raw_subtype = str(d.get("visura_subtype") or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "att": "attuale",
        "sto": "storica",
        "sin": "sintetica",
    }
    raw_subtype = aliases.get(raw_subtype, raw_subtype)
    known_subtypes = {
        "attuale",
        "analitica",
        "sintetica",
        "storica",
        "storica_analitica",
        "storica_sintetica",
        "storica_completa",
    }
    if raw_subtype in known_subtypes:
        return raw_subtype

    title = str(d.get("oggetto") or "").casefold()
    filename = str(d.get("filename") or d.get("file_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
    title_and_filename = f"{title} {filename.casefold()}"
    if "storic" in title_and_filename and "analitic" in title_and_filename:
        return "storica_analitica"
    if "storic" in title_and_filename and "sintetic" in title_and_filename:
        return "storica_sintetica"
    if "storic" in title_and_filename and "complet" in title_and_filename:
        return "storica_completa"
    if "storic" in title_and_filename:
        return "storica"
    if "attual" in title_and_filename and "sintetic" in title_and_filename:
        return "sintetica"
    if "attual" in title_and_filename:
        return "attuale"
    if "sintetic" in title_and_filename:
        return "sintetica"
    if "analitic" in title_and_filename:
        return "analitica"

    match = re.match(r"^vi_(att|sto|sin)(?:_ter)?_", filename, re.IGNORECASE)
    if match:
        return {"att": "attuale", "sto": "storica", "sin": "sintetica"}[match.group(1).casefold()]

    # Older typed records lack the detailed subtype column; the query type still
    # distinguishes the common analytical property visura from a generic visura.
    if d.get("document_type") in {"visura_fabbricati", "visura_terreni"}:
        return "analitica"
    if d.get("document_type") == "visura_storica":
        return "storica"
    if d.get("document_type") == "visura":
        return "sintetica"
    return ""


_CF_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]|\d{11})(?![A-Z0-9])", re.IGNORECASE)


def _extract_cf(d: dict) -> str | None:
    """Return the fiscal code / VAT for the queried subject of a soggetto visura.

    For soggetto documents the intestati_json holds property co-owners, not the
    queried subject — so oggetto/filename (which always embeds the CF by naming
    convention) takes priority; intestati_json is a last-resort fallback for
    docs whose name contains no CF (e.g. "VISURA LORENZIN BARBARA").
    """
    queried_code = str(d.get("queried_fiscal_code") or "").strip().upper()
    if _CF_RE.fullmatch(queried_code):
        return queried_code
    for text in (d.get("oggetto") or "", d.get("filename") or ""):
        m = _CF_RE.search(text.upper())
        if m:
            return m.group(1)
    import json as _json

    raw = d.get("intestati_json")
    if raw:
        try:
            ints = _json.loads(raw)
            if ints and isinstance(ints, list) and ints[0].get("CF"):
                return ints[0]["CF"]
        except Exception:
            pass
    return None


def _subject_visura_fiscal_code(doc: dict) -> str | None:
    """Identify the queried subject without mistaking XML owner rows for the subject."""
    queried_code = str(doc.get("queried_fiscal_code") or "").strip().upper()
    if _CF_RE.fullmatch(queried_code):
        return queried_code
    for source in (doc.get("oggetto") or "", doc.get("filename") or ""):
        match = _CF_RE.search(source.upper())
        if match:
            return match.group(1).upper()
    return None


def _legacy_document_owner_fiscal_codes(doc: dict) -> set[str]:
    """Read owner codes from legacy metadata when a caller still supplies it."""
    raw = doc.get("intestati_json")
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return set()
    if not isinstance(rows, list):
        return set()
    return {
        str(row["CF"]).strip().upper()
        for row in rows
        if isinstance(row, dict) and row.get("CF") and str(row["CF"]).strip()
    }


def _document_subject_fiscal_code(doc: dict) -> str | None:
    """Return the queried subject's code from structured XML, with filename fallback."""
    content = doc.get("xml_content")
    if content:
        try:
            from .xml_ingest import parse_visura_xml

            parsed = parse_visura_xml(content)
            subject = parsed.get("subject") if parsed else None
            fiscal_code = str((subject or {}).get("CodiceFiscale") or "").strip().upper()
            if fiscal_code:
                return fiscal_code
        except Exception as exc:  # The filename still provides a useful fallback for older imports.
            logger.debug("Could not read queried fiscal code from document XML: %s", type(exc).__name__)
    if (doc.get("document_type") or "") == "visura_soggetto":
        return _extract_cf(doc)
    return None


def _subject_cadastre_type(doc: dict) -> str | None:
    """Return E/F/T for a subject visura, falling back to its canonical name."""
    stored = str(doc.get("tipo_catasto") or "").strip().upper()
    if stored in {"E", "F", "T"}:
        return stored

    category_by_token = {"CE": "E", "CF": "F", "CT": "T"}
    tokens = Path(str(doc.get("filename") or "")).stem.upper().split("_")
    return next((category_by_token[token] for token in reversed(tokens[3:]) if token in category_by_token), None)


def _document_display_name(doc: dict) -> tuple[str, bool]:
    """Return a human-readable list label and whether it is cadastral-derived."""
    document_type = doc.get("document_type") or ""
    sheet = str(doc.get("foglio") or "").strip()
    parcel = str(doc.get("particella") or "").strip()
    filename = Path(str(doc.get("filename") or "")).name
    file_coordinates = re.search(
        r"(?:^|_)FG(?P<sheet>[^_]+)_PT(?P<parcel>[^_]+)(?:_SUB(?P<subunit>[^_]+))?(?:_|$)",
        Path(filename).stem,
        re.IGNORECASE,
    )
    is_property_visura = document_type in _VISURA_TYPES or bool(
        re.match(r"^vi_(?:att|sto|sin)(?:_ter)?_", filename, re.IGNORECASE)
    )
    if file_coordinates:
        sheet = sheet or file_coordinates.group("sheet")
        parcel = parcel or file_coordinates.group("parcel")
    if is_property_visura and sheet and parcel:
        parts = ["VISURA", "FG.", sheet, "PART.", parcel]
        subunit = str(doc.get("subalterno") or "").strip() or (
            file_coordinates.group("subunit") if file_coordinates else ""
        )
        municipality = str(doc.get("comune") or "").strip()
        if subunit:
            parts.extend(("SUB.", subunit))
        if municipality:
            parts.extend(("DI", municipality.upper()))
        return " ".join(parts), True

    return str(doc.get("oggetto") or doc.get("filename") or f"Doc {doc.get('id', '')}").strip(), False


def _collapse_to_logical_docs(docs: list[dict]) -> list[dict]:
    """Collapse raw file rows into logical documents, with PDFs as attachments.

    A SISTER request can yield several files for the same record: the structured
    P7M/XML (the data) plus PDF rendering(s). Here, files sharing the same logical
    identity are merged into one document whose ``files`` list carries every
    associated file; the structured file (P7M/XML) is the primary, the PDFs are
    its attachments.

    Logical identity (in priority order):
      1. response_id + document_type + cadastral coords  (when available)
      2. document_type + cadastral coords                (foglio/particella present)
      3. visura_soggetto: document_type + CF + search scope + subtype/date
      4. document_type + oggetto/filename                (fallback)

    Files with the same basename and different extensions are also joined, even
    when request IDs or metadata differ between their rows.
    """
    from collections import defaultdict

    def _logical_key(d: dict):
        rid = d.get("response_id")
        dt = d.get("document_type") or ""
        if dt == "ispezione_ipotecaria":
            # Keep each report/RP note independent; only pair alternate formats
            # that share the same filename stem.
            return (rid, dt, Path(d.get("filename") or str(d.get("id"))).stem.casefold())
        # Soggetto visure: group by CF + search scope + subtype/date regardless
        # of coordinates, which are only the first owned property in the XML.
        if dt == "visura_soggetto":
            cf = _extract_cf(d)
            if cf:
                scope = _subject_search_scope(
                    d.get("filename"),
                    stored_scope=d.get("subject_search_scope"),
                    stored_province=d.get("subject_search_province"),
                )
                return (
                    rid,
                    dt,
                    cf,
                    _subject_cadastre_type(d) or "",
                    d.get("visura_subtype") or "",
                    d.get("situazione_al") or "",
                    scope["search_scope_label"],
                )
        if d.get("foglio") or d.get("particella"):
            return (
                rid,
                dt,
                d.get("provincia"),
                d.get("comune"),
                d.get("foglio"),
                d.get("particella"),
                d.get("subalterno"),
                d.get("sezione_urbana"),
            )
        return (rid, dt, (d.get("oggetto") or d.get("filename") or str(d.get("id"))))

    def _fmt_rank(d: dict) -> int:  # structured files first
        return {"P7M": 0, "XML": 1}.get((d.get("file_format") or "").upper(), 2)

    groups_by_identity: dict[object, list[dict]] = defaultdict(list)
    for d in docs:
        groups_by_identity[_logical_key(d)].append(d)

    # Some imports do not link the PDF and structured file to the same request
    # row, so the identity keys above can split a P7M/XML and PDF pair. Join
    # logical groups sharing a basename only when that basename has multiple
    # extensions; same-extension files retain their normal identity grouping.
    initial_groups = list(groups_by_identity.values())
    groups_by_basename: dict[str, set[int]] = defaultdict(set)
    extensions_by_basename: dict[str, set[str]] = defaultdict(set)
    for group_index, items in enumerate(initial_groups):
        for item in items:
            file_name = str(item.get("filename") or "")
            file_path = Path(file_name)
            basename = file_path.stem.casefold()
            extension = file_path.suffix.casefold()
            if basename and extension:
                groups_by_basename[basename].add(group_index)
                extensions_by_basename[basename].add(extension)

    parents = list(range(len(initial_groups)))

    def _find_group(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for basename, group_indexes in groups_by_basename.items():
        if len(extensions_by_basename[basename]) < 2:
            continue
        first = next(iter(group_indexes))
        for group_index in group_indexes:
            first_root = _find_group(first)
            group_root = _find_group(group_index)
            if group_root != first_root:
                parents[group_root] = first_root

    merged_groups: dict[int, list[dict]] = defaultdict(list)
    for group_index, items in enumerate(initial_groups):
        merged_groups[_find_group(group_index)].extend(items)

    logical: list[dict] = []
    for items in merged_groups.values():
        ordered = sorted(items, key=lambda x: (_fmt_rank(x), x.get("id") or 0))
        primary = dict(ordered[0])  # copy — primary carries the document metadata
        primary["files"] = [
            {
                "id": it.get("id"),
                "file_format": (it.get("file_format") or "").upper(),
                "filename": it.get("filename") or "",
                "size_bytes": it.get("file_size") or 0,
                "size_human": _human_size(it["file_size"]) if it.get("file_size") else "",
            }
            for it in ordered
        ]
        primary["n_files"] = len(ordered)
        primary["n_pdf"] = sum(1 for f in primary["files"] if f["file_format"] == "PDF")
        primary["total_file_size"] = sum(it.get("file_size") or 0 for it in ordered)
        primary["total_size_human"] = _human_size(primary["total_file_size"]) if primary["total_file_size"] else ""
        primary["display_name"], primary["cadastral_display_name"] = _document_display_name(primary)
        primary_structured = (primary.get("file_format") or "").upper() in ("P7M", "XML")
        is_ispezione = (primary.get("document_type") or "") == "ispezione_ipotecaria"
        primary["viewable"] = is_ispezione or (
            primary_structured and (primary.get("document_type") or "") in _VIEWABLE_DOC_TYPES
        )
        if primary.get("document_type") == "visura_soggetto":
            primary["tipo_catasto"] = _subject_cadastre_type(primary)
            primary.update(
                _subject_search_scope(
                    primary.get("filename"),
                    stored_scope=primary.get("subject_search_scope"),
                    stored_province=primary.get("subject_search_province"),
                )
            )
            cf = _extract_cf(primary)
            if cf and len(cf) == 16:
                primary["cf"] = cf
            elif cf and len(cf) == 11 and cf.isdigit():
                primary["vat"] = cf
        logical.append(primary)
    return logical


class _LogicalDocumentAccumulator:
    """Collapse document rows without retaining every raw row in memory.

    The existing collapse rules use logical identity first, then join groups
    sharing a basename when at least two file extensions occur. Keep only one
    primary metadata row and compact attachment summaries while the DB cursor
    streams the catalogue.
    """

    def __init__(self) -> None:
        self._groups_by_identity: dict[object, int] = {}
        self._groups: list[dict[str, Any]] = []
        self._groups_by_basename: dict[str, set[int]] = {}
        self._extensions_by_basename: dict[str, set[str]] = {}

    @staticmethod
    def _format_rank(document: dict) -> int:
        return {"P7M": 0, "XML": 1}.get((document.get("file_format") or "").upper(), 2)

    @staticmethod
    def _logical_key(document: dict) -> object:
        request_id = document.get("response_id")
        document_type = document.get("document_type") or ""
        filename = document.get("filename")
        if document_type == "ispezione_ipotecaria":
            return (request_id, document_type, Path(filename or str(document.get("id"))).stem.casefold())
        if document_type == "visura_soggetto":
            fiscal_code = _extract_cf(document)
            if fiscal_code:
                scope = _subject_search_scope(
                    filename,
                    stored_scope=document.get("subject_search_scope"),
                    stored_province=document.get("subject_search_province"),
                )
                return (
                    request_id,
                    document_type,
                    fiscal_code,
                    _subject_cadastre_type(document) or "",
                    document.get("visura_subtype") or "",
                    document.get("situazione_al") or "",
                    scope["search_scope_label"],
                )
        if document.get("foglio") or document.get("particella"):
            return (
                request_id,
                document_type,
                document.get("provincia"),
                document.get("comune"),
                document.get("foglio"),
                document.get("particella"),
                document.get("subalterno"),
                document.get("sezione_urbana"),
            )
        return (request_id, document_type, document.get("oggetto") or filename or str(document.get("id")))

    def add(self, document: dict) -> None:
        identity = self._logical_key(document)
        group_index = self._groups_by_identity.get(identity)
        rank = self._format_rank(document)
        sort_key = (rank, document.get("id") or 0)
        if group_index is None:
            group_index = len(self._groups)
            self._groups_by_identity[identity] = group_index
            self._groups.append(
                {
                    "primary": dict(document),
                    "primary_sort_key": sort_key,
                    "file_items": [],
                }
            )
        group = self._groups[group_index]
        if sort_key < group["primary_sort_key"]:
            group["primary"] = dict(document)
            group["primary_sort_key"] = sort_key

        file_size = document.get("file_size") or 0
        file_items = group["file_items"]
        file_items.append(
            (
                sort_key,
                {
                    "id": document.get("id"),
                    "file_format": (document.get("file_format") or "").upper(),
                    "filename": document.get("filename") or "",
                    "size_bytes": file_size,
                    "size_human": _human_size(file_size) if file_size else "",
                },
                file_size,
            )
        )

        filename = str(document.get("filename") or "")
        file_path = Path(filename)
        basename = file_path.stem.casefold()
        extension = file_path.suffix.casefold()
        if basename and extension:
            self._groups_by_basename.setdefault(basename, set()).add(group_index)
            self._extensions_by_basename.setdefault(basename, set()).add(extension)

    def finish(self) -> list[dict]:
        parents = list(range(len(self._groups)))

        def _find_group(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        for basename, group_indexes in self._groups_by_basename.items():
            if len(self._extensions_by_basename[basename]) < 2:
                continue
            first = next(iter(group_indexes))
            for group_index in group_indexes:
                first_root = _find_group(first)
                group_root = _find_group(group_index)
                if group_root != first_root:
                    parents[group_root] = first_root

        merged_groups: dict[int, dict[str, Any]] = {}
        for group_index, source in enumerate(self._groups):
            root = _find_group(group_index)
            target = merged_groups.get(root)
            if target is None:
                merged_groups[root] = source
                continue
            if source["primary_sort_key"] < target["primary_sort_key"]:
                target["primary"] = source["primary"]
                target["primary_sort_key"] = source["primary_sort_key"]
            target["file_items"].extend(source["file_items"])
            source["file_items"] = []

        logical: list[dict] = []
        for group in merged_groups.values():
            ordered = sorted(group["file_items"], key=lambda item: item[0])
            primary = group["primary"]
            logical.append(_finalize_logical_document(primary, [item[1] for item in ordered]))

        self._groups.clear()
        self._groups_by_identity.clear()
        self._groups_by_basename.clear()
        self._extensions_by_basename.clear()
        return logical


def _finalize_logical_document(primary: dict, ordered_files: list[dict]) -> dict:
    """Add the same derived fields used by both in-memory and disk-backed grouping."""
    primary["files"] = ordered_files
    primary["n_files"] = len(ordered_files)
    primary["n_pdf"] = sum(1 for item in ordered_files if item["file_format"] == "PDF")
    primary["total_file_size"] = sum(item.get("size_bytes") or 0 for item in ordered_files)
    primary["total_size_human"] = _human_size(primary["total_file_size"]) if primary["total_file_size"] else ""
    primary["display_name"], primary["cadastral_display_name"] = _document_display_name(primary)
    primary_structured = (primary.get("file_format") or "").upper() in ("P7M", "XML")
    is_ispezione = (primary.get("document_type") or "") == "ispezione_ipotecaria"
    primary["viewable"] = is_ispezione or (
        primary_structured and (primary.get("document_type") or "") in _VIEWABLE_DOC_TYPES
    )
    if primary.get("document_type") == "visura_soggetto":
        primary["tipo_catasto"] = _subject_cadastre_type(primary)
        primary.update(
            _subject_search_scope(
                primary.get("filename"),
                stored_scope=primary.get("subject_search_scope"),
                stored_province=primary.get("subject_search_province"),
            )
        )
        fiscal_code = _extract_cf(primary)
        if fiscal_code and len(fiscal_code) == 16:
            primary["cf"] = fiscal_code
        elif fiscal_code and len(fiscal_code) == 11 and fiscal_code.isdigit():
            primary["vat"] = fiscal_code
    return primary


async def _collapse_streamed_documents(documents: AsyncIterable[dict]) -> tuple[list[dict], int]:
    accumulator = _LogicalDocumentAccumulator()
    raw_count = 0
    async for document in documents:
        raw_count += 1
        accumulator.add(document)
    return accumulator.finish(), raw_count


async def _page_streamed_documents(
    documents: AsyncIterable[dict], page: int, page_size: int = _DOCUMENTS_PAGE_SIZE
) -> tuple[list[dict], int, int, int, int, int]:
    """Group a streamed catalogue on disk and materialize only one logical page."""
    import sqlite3
    import tempfile

    page_size = max(1, int(page_size))
    raw_count = 0
    with tempfile.TemporaryDirectory(prefix="sister-documents-") as temp_dir:
        connection = sqlite3.connect(Path(temp_dir) / "catalog.sqlite")
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-8192")
            connection.executescript(
                """
                CREATE TABLE logical_groups (
                    id INTEGER PRIMARY KEY,
                    identity TEXT NOT NULL UNIQUE,
                    primary_json TEXT NOT NULL,
                    primary_rank INTEGER NOT NULL,
                    primary_document_id INTEGER NOT NULL,
                    primary_created_at TEXT NOT NULL,
                    first_sequence INTEGER NOT NULL
                );
                CREATE TABLE catalog_files (
                    group_id INTEGER NOT NULL,
                    file_rank INTEGER NOT NULL,
                    file_document_id INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    file_format TEXT NOT NULL,
                    basename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    file_json TEXT NOT NULL
                );
                CREATE INDEX ix_catalog_files_group ON catalog_files(group_id);
                CREATE TABLE basename_groups (
                    basename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY (basename, extension, group_id)
                );
                CREATE INDEX ix_basename_groups_name ON basename_groups(basename);
                CREATE TABLE group_parents (
                    group_id INTEGER PRIMARY KEY,
                    parent_id INTEGER NOT NULL
                );
                """
            )

            async for document in documents:
                raw_count += 1
                identity = json.dumps(
                    _LogicalDocumentAccumulator._logical_key(document),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                rank = _LogicalDocumentAccumulator._format_rank(document)
                document_id = int(document.get("id") or 0)
                existing = connection.execute(
                    "SELECT id, primary_rank, primary_document_id FROM logical_groups WHERE identity = ?",
                    (identity,),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO logical_groups (
                            identity, primary_json, primary_rank, primary_document_id,
                            primary_created_at, first_sequence
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity,
                            json.dumps(document, ensure_ascii=False, default=str),
                            rank,
                            document_id,
                            str(document.get("created_at") or ""),
                            raw_count,
                        ),
                    )
                    group_id = int(cursor.lastrowid)
                    connection.execute(
                        "INSERT INTO group_parents (group_id, parent_id) VALUES (?, ?)",
                        (group_id, group_id),
                    )
                else:
                    group_id = int(existing[0])
                    if (rank, document_id) < (int(existing[1]), int(existing[2])):
                        connection.execute(
                            """
                            UPDATE logical_groups
                            SET primary_json = ?, primary_rank = ?, primary_document_id = ?, primary_created_at = ?
                            WHERE id = ?
                            """,
                            (
                                json.dumps(document, ensure_ascii=False, default=str),
                                rank,
                                document_id,
                                str(document.get("created_at") or ""),
                                group_id,
                            ),
                        )

                file_size = document.get("file_size") or 0
                filename = str(document.get("filename") or "")
                file_path = Path(filename)
                basename = file_path.stem.casefold()
                extension = file_path.suffix.casefold()
                file_format = (document.get("file_format") or "").upper()
                file_item = {
                    "id": document.get("id"),
                    "file_format": file_format,
                    "filename": filename,
                    "size_bytes": file_size,
                    "size_human": _human_size(file_size) if file_size else "",
                }
                connection.execute(
                    """
                    INSERT INTO catalog_files (
                        group_id, file_rank, file_document_id, sequence, file_format,
                        basename, extension, file_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        rank,
                        document_id,
                        raw_count,
                        file_format,
                        basename,
                        extension,
                        json.dumps(file_item, ensure_ascii=False, default=str),
                    ),
                )
                if basename and extension:
                    connection.execute(
                        "INSERT OR IGNORE INTO basename_groups (basename, extension, group_id) VALUES (?, ?, ?)",
                        (basename, extension, group_id),
                    )

            connection.commit()

            def _find_root(group_id: int) -> int:
                current = group_id
                path = []
                while True:
                    row = connection.execute(
                        "SELECT parent_id FROM group_parents WHERE group_id = ?", (current,)
                    ).fetchone()
                    if row is None or int(row[0]) == current:
                        root = current
                        break
                    path.append(current)
                    current = int(row[0])
                if path:
                    connection.executemany(
                        "UPDATE group_parents SET parent_id = ? WHERE group_id = ?",
                        [(root, child) for child in path],
                    )
                return root

            for (basename,) in connection.execute(
                """
                SELECT basename
                FROM basename_groups
                GROUP BY basename
                HAVING COUNT(DISTINCT extension) >= 2
                ORDER BY basename
                """
            ):
                first_root = None
                for (group_id,) in connection.execute(
                    "SELECT DISTINCT group_id FROM basename_groups WHERE basename = ? ORDER BY group_id",
                    (basename,),
                ):
                    root = _find_root(int(group_id))
                    if first_root is None:
                        first_root = root
                    elif root != first_root:
                        connection.execute(
                            "UPDATE group_parents SET parent_id = ? WHERE group_id = ?",
                            (first_root, root),
                        )

            connection.execute(
                "CREATE TABLE group_roots (group_id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL)"
            )
            root_batch = []
            for (group_id,) in connection.execute("SELECT id FROM logical_groups ORDER BY id"):
                group_id = int(group_id)
                root_batch.append((group_id, _find_root(group_id)))
                if len(root_batch) >= 500:
                    connection.executemany(
                        "INSERT INTO group_roots (group_id, root_id) VALUES (?, ?)", root_batch
                    )
                    root_batch.clear()
            if root_batch:
                connection.executemany(
                    "INSERT INTO group_roots (group_id, root_id) VALUES (?, ?)", root_batch
                )
            connection.execute("CREATE INDEX ix_group_roots_root ON group_roots(root_id)")

            connection.executescript(
                """
                CREATE TABLE logical_documents (
                    root_id INTEGER PRIMARY KEY,
                    primary_json TEXT NOT NULL,
                    primary_created_at TEXT NOT NULL,
                    primary_document_id INTEGER NOT NULL,
                    first_sequence INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        roots.root_id,
                        groups.primary_json,
                        groups.primary_created_at,
                        groups.primary_document_id,
                        groups.first_sequence,
                        ROW_NUMBER() OVER (
                            PARTITION BY roots.root_id
                            ORDER BY groups.primary_rank, groups.primary_document_id, groups.first_sequence
                        ) AS choice
                    FROM logical_groups groups
                    JOIN group_roots roots ON roots.group_id = groups.id
                )
                INSERT INTO logical_documents (
                    root_id, primary_json, primary_created_at, primary_document_id, first_sequence
                )
                SELECT
                    root_id,
                    MAX(CASE WHEN choice = 1 THEN primary_json END),
                    MAX(CASE WHEN choice = 1 THEN primary_created_at END),
                    MAX(CASE WHEN choice = 1 THEN primary_document_id END),
                    MIN(first_sequence)
                FROM ranked
                GROUP BY root_id
                """
            )
            connection.execute(
                "CREATE INDEX ix_logical_documents_order ON logical_documents(primary_created_at, primary_document_id)"
            )

            total = int(connection.execute("SELECT COUNT(*) FROM logical_documents").fetchone()[0])
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, int(page or 1)), page_count)
            page_offset = (page - 1) * page_size
            selected = connection.execute(
                """
                SELECT root_id, primary_json
                FROM logical_documents
                ORDER BY primary_created_at DESC, primary_document_id DESC, first_sequence
                LIMIT ? OFFSET ?
                """,
                (page_size, page_offset),
            ).fetchall()
            roots = [int(row[0]) for row in selected]
            files_by_root: dict[int, list[tuple[int, dict]]] = {root: [] for root in roots}
            if roots:
                placeholders = ", ".join("?" for _ in roots)
                file_rows = connection.execute(
                    f"""
                    SELECT roots.root_id, files.file_rank, files.file_document_id, files.sequence, files.file_json
                    FROM catalog_files files
                    JOIN group_roots roots ON roots.group_id = files.group_id
                    WHERE roots.root_id IN ({placeholders})
                    ORDER BY roots.root_id, files.file_rank, files.file_document_id, files.sequence
                    """,
                    roots,
                )
                for root_id, file_rank, file_id, sequence, file_json in file_rows:
                    files_by_root[int(root_id)].append(
                        ((int(file_rank), int(file_id), int(sequence)), json.loads(file_json))
                    )

            page_documents = []
            for root_id, primary_json in selected:
                ordered_files = [item for _, item in sorted(files_by_root[int(root_id)], key=lambda row: row[0])]
                page_documents.append(
                    _finalize_logical_document(json.loads(primary_json), ordered_files)
                )
            return page_documents, page, page_count, page_offset, total, raw_count
        finally:
            connection.close()


async def _load_visible_logical_document_groups(user, document_ids: set[int]) -> list[dict]:
    """Load only visible logical groups containing one of the requested physical files."""
    target_ids = {int(document_id) for document_id in document_ids if document_id is not None}
    if not target_ids:
        return []
    sibling_rows = await get_document_group_siblings(target_ids)
    visible_rows = await _filter_purchased_documents(user, sibling_rows)
    groups = _collapse_to_logical_docs(visible_rows)
    return [
        document
        for document in groups
        if int(document.get("id") or 0) in target_ids
        or any(int(file.get("id") or 0) in target_ids for file in document.get("files") or [])
    ]


def _link_related_visura_docs(
    docs: list[dict],
    *,
    visible_subject_docs: list[dict] | None = None,
    owner_fiscal_codes_by_document_id: dict[int, list[str]] | None = None,
    property_docs_by_owner_cf: dict[str, list[dict]] | None = None,
) -> None:
    """Attach reciprocal links between subject and property visure by CF/VAT.

    The current page supplies property-owner codes from normalized ownership
    rows and may link a page subject to property files elsewhere in the visible
    catalogue. Legacy ``intestati_json`` remains a compatibility source.
    """
    subjects_by_cf: dict[str, list[dict]] = {}
    properties_by_id: dict[int, dict] = {}

    for doc in [*docs, *(visible_subject_docs or [])]:
        doc_type = doc.get("document_type") or ""
        if doc_type == "visura_soggetto":
            cf = _subject_visura_fiscal_code(doc)
            if cf:
                bucket = subjects_by_cf.setdefault(cf, [])
                if all(subject.get("id") != doc.get("id") for subject in bucket):
                    bucket.append(doc)

    for doc in docs:
        if (doc.get("document_type") or "") in _VISURA_TYPES and doc.get("id") is not None:
            properties_by_id[doc["id"]] = doc

    subject_links: dict[int, dict[int, dict[str, Any]]] = {}
    property_links: dict[int, dict[int, dict[str, Any]]] = {}
    for property_doc in properties_by_id.values():
        property_id = property_doc["id"]
        owner_codes = _legacy_document_owner_fiscal_codes(property_doc)
        owner_codes.update(
            str(code).strip().upper()
            for code in (owner_fiscal_codes_by_document_id or {}).get(property_id, [])
            if code and str(code).strip()
        )
        for cf in owner_codes:
            for subject_doc in subjects_by_cf.get(cf, []):
                subject_id = subject_doc.get("id")
                if subject_id is None:
                    continue
                subject_links.setdefault(subject_id, {})[property_id] = {
                    "id": property_id,
                    "label": (
                        property_doc.get("display_name")
                        or property_doc.get("oggetto")
                        or property_doc.get("filename")
                        or f"Doc {property_id}"
                    ),
                }
                property_links.setdefault(property_id, {})[subject_id] = {
                    "id": subject_id,
                    "label": (
                        subject_doc.get("display_name")
                        or subject_doc.get("oggetto")
                        or subject_doc.get("filename")
                        or f"Doc {subject_id}"
                    ),
                    "fiscal_code": cf,
                }

    for doc in docs:
        doc_id = doc.get("id")
        if doc.get("document_type") == "visura_soggetto":
            related_properties = dict(subject_links.get(doc_id, {}))
            subject_cf = _subject_visura_fiscal_code(doc)
            for property_doc in (property_docs_by_owner_cf or {}).get(subject_cf or "", []):
                property_id = property_doc.get("id")
                if property_id is not None:
                    related_properties.setdefault(
                        property_id,
                        {
                            "id": property_id,
                            "label": property_doc.get("label") or f"Doc {property_id}",
                        },
                    )
            doc["related_immobile_docs"] = list(related_properties.values())
        elif (doc.get("document_type") or "") in _VISURA_TYPES:
            doc["related_soggetto_docs"] = list(property_links.get(doc_id, {}).values())


def _build_property_map(
    docs: list[dict],
    *,
    owner_subject_docs: list[dict] | None = None,
    owner_fiscal_codes_by_document_id: dict[int, list[str]] | None = None,
) -> dict:
    """Group documents by cadastral coordinates into a nested property map.

    Visura-soggetto documents are nested as owner children under property docs
    (visura_storica / visura_fabbricati / visura_terreni) using normalized owner
    fiscal codes, with ``intestati_json`` retained for legacy callers. Unlinked
    subject documents are grouped by person; other ungrouped documents go under
    ``altri``.

    Returns:
      ``immobili``: provincia → foglio → particella → subalterno → docs
      ``soggetti``: list of {cf, label, docs} for unlinked soggetto documents
      ``altri``: indexed documents that have no cadastral or subject grouping
    """
    import json
    import re

    _SOGGETTO_TYPE = "visura_soggetto"

    def _decorate_one(d: dict) -> dict:
        d.setdefault("size_human", _human_size(d["file_size"]) if d.get("file_size") else "")
        d.setdefault("created_display", (d.get("created_at") or "")[:16].replace("T", " "))
        d.setdefault("viewable", (d.get("document_type") or "") in _VIEWABLE_DOC_TYPES)
        return d

    def _decorate(items: list[dict]) -> list[dict]:
        return [_decorate_one(d) for d in items]

    def _cf_from_subject_doc(doc: dict) -> Optional[str]:
        for source in (doc.get("oggetto") or "", doc.get("filename") or ""):
            match = _CF_RE.search(source.upper())
            if match:
                return match.group(1)
        return None

    def _cfs_from_intestati(istr: str) -> list[str]:
        try:
            seen, out = set(), []
            for row in json.loads(istr):
                cf = row.get("CF")
                if cf and cf not in seen:
                    normalized_cf = str(cf).strip().upper()
                    seen.add(normalized_cf)
                    out.append(normalized_cf)
            return out
        except Exception:
            return []

    # Global CF → [soggetto docs] index. Paginated location views pass the
    # complete subject list separately so a property's owner links survive
    # pagination without adding every subject row to the rendered page.
    cf_soggetti: dict[str, list[dict]] = {}
    subject_index: dict[str, dict[int | str, dict]] = {}
    for d in [*docs, *(owner_subject_docs or [])]:
        if (d.get("document_type") or "") == _SOGGETTO_TYPE:
            cf = _cf_from_subject_doc(d)
            if cf:
                document_id = d.get("id")
                identity = document_id if document_id is not None else f"{d.get('filename') or ''}:{id(d)}"
                subject_index.setdefault(cf, {}).setdefault(identity, d)
    cf_soggetti = {cf: list(subjects.values()) for cf, subjects in subject_index.items()}

    # Separate docs with cadastral coords from the rest
    immobili_docs = [d for d in docs if d.get("foglio") and d.get("particella") and d.get("provincia")]

    # Track doc IDs claimed as owner children under a property doc
    claimed_ids: set[int] = set()

    def _owner_docs_for(property_doc: dict) -> list[dict]:
        """Return visura_soggetto docs for all owners in this property doc."""
        owner_codes = _cfs_from_intestati(property_doc.get("intestati_json"))
        owner_codes.extend(
            code.strip().upper()
            for code in (owner_fiscal_codes_by_document_id or {}).get(property_doc.get("id"), [])
            if code and code.strip()
        )
        owner_codes = list(dict.fromkeys(owner_codes))
        if not owner_codes:
            return []
        result = []
        for cf in owner_codes:
            for od in cf_soggetti.get(cf, []):
                did = od.get("id")
                if did not in claimed_ids:
                    claimed_ids.add(did)
                    result.append(_decorate_one(dict(od)))
        return sorted(result, key=lambda d: (d.get("oggetto") or d.get("filename") or ""))

    # Build raw nested dict: provincia → foglio → particella → subalterno
    prov_raw: dict[str, dict] = {}
    for d in immobili_docs:
        prov = (d.get("provincia") or "?").strip().upper()
        comune = (d.get("comune") or "").strip() or None
        foglio = (d.get("foglio") or "").strip().lstrip("0") or (d.get("foglio") or "")
        particella = (d.get("particella") or "").strip().lstrip("0") or (d.get("particella") or "")
        sub = (d.get("subalterno") or "").strip() or None
        sezu = (d.get("sezione_urbana") or "").strip() or None

        p = prov_raw.setdefault(prov, {})
        f = p.setdefault(foglio, {})
        pt = f.setdefault(particella, {"comune": None, "sezione_urbana": None, "subs": {}})
        if comune and not pt["comune"]:
            pt["comune"] = comune
        if sezu and not pt["sezione_urbana"]:
            pt["sezione_urbana"] = sezu
        pt["subs"].setdefault(sub or "__nessuno__", []).append(d)

    def _sort_num(val: str) -> tuple:
        return (0, int(val)) if val.isdigit() else (1, val)

    def _sort_sub(sub_key: str) -> tuple:
        if sub_key == "__nessuno__":
            return (1, [])
        parts = sub_key.split("/")
        try:
            return (0, [int(p) for p in parts])
        except ValueError:
            return (0, [sub_key])

    def _build_tree():
        result = []
        for prov_key in sorted(prov_raw.keys()):
            fogli_list = []
            for foglio_key in sorted(prov_raw[prov_key].keys(), key=_sort_num):
                particelle_list = []
                for pt_key in sorted(prov_raw[prov_key][foglio_key].keys(), key=_sort_num):
                    pt_data = prov_raw[prov_key][foglio_key][pt_key]
                    subs_list = []
                    for sub_key in sorted(pt_data["subs"].keys(), key=_sort_sub):
                        raw = _decorate(pt_data["subs"][sub_key])

                        # Separate property docs from soggetto docs at this sub-level
                        prop_docs = [d for d in raw if (d.get("document_type") or "") != _SOGGETTO_TYPE]
                        sogg_here = [d for d in raw if (d.get("document_type") or "") == _SOGGETTO_TYPE]

                        # Soggetti at parcel level are always owned by this parcel —
                        # mark them claimed so they don't appear in the Soggetti section
                        for d in sogg_here:
                            claimed_ids.add(d.get("id"))

                        # Attach owner docs to each property doc
                        for d in prop_docs:
                            d["owner_docs"] = _owner_docs_for(d)

                        # Soggetti at this sub not claimed by any property doc stay as siblings
                        orphan_sogg = [
                            d
                            for d in sogg_here
                            if d.get("id") not in {od.get("id") for pd in prop_docs for od in pd.get("owner_docs", [])}
                        ]

                        final = sorted(prop_docs + orphan_sogg, key=lambda d: d.get("filename") or "")
                        subs_list.append(
                            {
                                "subalterno": None if sub_key == "__nessuno__" else sub_key,
                                "docs": final,
                            }
                        )
                    particelle_list.append(
                        {
                            "particella": pt_key,
                            "comune": pt_data["comune"],
                            "sezione_urbana": pt_data["sezione_urbana"],
                            "subalternos": subs_list,
                            "total_docs": sum(
                                1 + len(d.get("owner_docs", []))
                                for s in subs_list
                                for d in s["docs"]
                            ),
                        }
                    )
                fogli_list.append(
                    {
                        "foglio": foglio_key,
                        "particelle": particelle_list,
                        "total_docs": sum(p["total_docs"] for p in particelle_list),
                    }
                )
            result.append(
                {
                    "provincia": prov_key,
                    "fogli": fogli_list,
                    "total_docs": sum(f["total_docs"] for f in fogli_list),
                }
            )
        return result

    def _build_soggetti(immobili_tree):
        """Soggetti not linked to property docs, grouped by CF or subject name."""
        by_person: dict[tuple[str, str], dict] = {}
        for d in docs:
            if (d.get("document_type") or "") != _SOGGETTO_TYPE:
                continue
            if d.get("id") in claimed_ids:
                continue
            oggetto = d.get("oggetto") or ""
            filename = d.get("filename") or ""

            # The queried subject's CF can be present in either display field,
            # and is not guaranteed to be parenthesized. Avoid intestati_json
            # here: for a soggetto visura it describes property co-owners and
            # can incorrectly merge different queried subjects.
            cf = None
            for source in (oggetto, filename):
                match = _CF_RE.search(source.upper())
                if match:
                    cf = match.group(1).upper()
                    break

            label_source = oggetto or filename
            label = _CF_RE.sub("", label_source)
            label = label.strip(" ()-_\t\r\n") or filename or "Soggetto"
            compact_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            if compact_label in {"soggetto", "visura soggetto", "visura per soggetto"} and filename:
                label = _CF_RE.sub("", filename).strip(" ()-_") or filename
            if not cf:
                # Without a CF, group by the best available subject name. Use
                # the document id only when neither field provides a name.
                normalized_label = re.sub(r"\s+", " ", label).strip().casefold()
                identity = ("name", normalized_label or str(d.get("id") or filename))
            else:
                identity = ("cf", cf)

            if identity not in by_person:
                by_person[identity] = {"cf": cf, "label": label, "docs": []}
            by_person[identity]["docs"].append(_decorate_one(d))
        for entry in by_person.values():
            entry["docs"].sort(key=lambda d: d.get("filename") or "")
        return sorted(by_person.values(), key=lambda x: x["label"].casefold())

    immobili_tree = _build_tree()
    soggetti_tree = _build_soggetti(immobili_tree)

    # The location view is a different arrangement of the same logical
    # documents used by the category and file views. Keep records without
    # cadastral coordinates visible in an explicit catch-all group.
    represented_ids: set[int] = set()
    for province in immobili_tree:
        for sheet in province["fogli"]:
            for parcel in sheet["particelle"]:
                for sub in parcel["subalternos"]:
                    for document in sub["docs"]:
                        if document.get("id") is not None:
                            represented_ids.add(document["id"])
                        represented_ids.update(
                            owner["id"] for owner in document.get("owner_docs", []) if owner.get("id") is not None
                        )
    for subject in soggetti_tree:
        represented_ids.update(document["id"] for document in subject["docs"] if document.get("id") is not None)

    other_docs = [
        _decorate_one(d)
        for d in docs
        if d.get("id") not in represented_ids
    ]
    other_docs.sort(key=lambda d: (d.get("created_at") or "", d.get("id") or 0), reverse=True)
    return {"immobili": immobili_tree, "soggetti": soggetti_tree, "altri": other_docs}


def _build_document_tree(docs: list[dict]) -> list[dict]:
    """Build the hierarchical document index using the unified query-type taxonomy.

    Single-step query groups (top-level):
      Visura per Immobile → one table with catasto and subtype columns
      Visura per Soggetto → one table with fiscal-code and VAT columns
      Elenco degli Immobili, Planimetria Catastale, Elaborato Planimetrico,
      Estratto di Mappa, Elenco Subalterni, Ispezione / Visura Ipotecaria,
      Note di Trascrizione / Iscrizione, Titoli Telematici, Richieste.

    Input should already be collapsed via _collapse_to_logical_docs().
    Returns node dicts: {key, label, icon, color, count, docs, children}.
    """
    from collections import defaultdict

    def _decorate(items: list[dict]) -> list[dict]:
        rows = sorted(items, key=lambda d: (d.get("created_at") or "", d.get("id") or 0), reverse=True)
        for d in rows:
            d.setdefault("size_human", _human_size(d["file_size"]) if d.get("file_size") else "")
            d["created_display"] = (d.get("created_at") or "")[:16].replace("T", " ")
            d.setdefault("viewable", (d.get("document_type") or "") in _VIEWABLE_DOC_TYPES)
        return rows

    def leaf(key, label, icon, color, items, is_soggetto=False):
        return {
            "key": key,
            "label": label,
            "icon": icon,
            "color": color,
            "count": len(items),
            "docs": _decorate(items),
            "children": [],
            "is_soggetto": is_soggetto,
        }

    def node(key, label, icon, color, children, count, docs=None, is_soggetto=False):
        return {
            "key": key,
            "label": label,
            "icon": icon,
            "color": color,
            "count": count,
            "docs": _decorate(docs or []),
            "children": children,
            "is_soggetto": is_soggetto,
        }

    by_type: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        by_type[d.get("document_type") or "(altro)"].append(d)

    tree: list[dict] = []
    classified_types = set(_VISURA_TYPES) | {"visura_soggetto"}

    def add_document_category(key, label, icon, color, *type_codes, always=False):
        items = [d for d in docs if (d.get("document_type") or "") in type_codes]
        classified_types.update(type_codes)
        if items or always:
            category = leaf(key, label, icon, color, items)
            category["empty_message"] = "Nessun documento in questa categoria."
            tree.append(category)

    # ── Visura per Immobile ────────────────────────────────────────────────
    visure = [d for d in docs if (d.get("document_type") or "") in _VISURA_TYPES]
    if visure:
        for doc in visure:
            if not doc.get("tipo_catasto"):
                cadastre = _doc_catasto_ft(doc)
                if cadastre:
                    doc["tipo_catasto"] = "F" if cadastre == "fabbricati" else "T"
            if not doc.get("visura_subtype"):
                doc["visura_subtype"] = _doc_visura_subtype(doc) or None
        tree.append(
            node(
                "visura_immobile",
                "Visura per Immobile",
                "fa-house",
                "success",
                [],
                count=len(visure),
                docs=visure,
            )
        )

    # ── Visura per Soggetto ────────────────────────────────────────────────
    soggetti = by_type.get("visura_soggetto", [])
    if soggetti:
        tree.append(
            leaf(
                "visura_soggetto",
                "Visura per Soggetto",
                "fa-user-group",
                "info",
                soggetti,
                is_soggetto=True,
            )
        )

    # ── Intestatari ────────────────────────────────────────────────────────
    add_document_category(
        "elenco_immobili",
        "Elenco degli Immobili",
        "fa-list",
        "primary",
        "elenco_immobili",
        "intestati",
    )

    # ── Planimetria Catastale ──────────────────────────────────────────────
    add_document_category(
        "planimetria",
        "Planimetria Catastale",
        "fa-ruler-combined",
        "secondary",
        "planimetria",
        "planimetria_catastale",
    )

    # ── Elaborato Planimetrico (EPA) ──────────────────────────────────────
    add_document_category(
        "elaborato_planimetrico",
        "Elaborato Planimetrico (EPA)",
        "fa-drafting-compass",
        "secondary",
        "elaborato_planimetrico",
        "epa",
    )

    # ── Richieste ──────────────────────────────────────────────────────────
    add_document_category("richieste", "Richieste", "fa-clock-rotate-left", "secondary", "richieste")

    # ── Additional cadastral document families ────────────────────────────
    add_document_category(
        "estratto_mappa",
        "Estratto di Mappa o Visura di Mappa",
        "fa-map",
        "secondary",
        "estratto_mappa",
        "estratto_di_mappa",
        "visura_mappa",
        "mappa",
        always=True,
    )
    add_document_category(
        "elenco_subalterni",
        "Elenco Subalterni",
        "fa-layer-group",
        "secondary",
        "elenco_subalterni",
        "elenco_subalterno",
        always=True,
    )
    add_document_category(
        "ispezione_ipotecaria",
        "Ispezione / Visura Ipotecaria",
        "fa-file-circle-check",
        "primary",
        "ispezione_ipotecaria",
        "visura_ipotecaria",
    )
    add_document_category(
        "nota_trascrizione_iscrizione",
        "Nota di Trascrizione o Iscrizione",
        "fa-file-signature",
        "primary",
        "nota_trascrizione",
        "nota_iscrizione",
        "nota_trascrizione_iscrizione",
        "nota_annotazione",
        always=True,
    )
    add_document_category(
        "titolo_telematico",
        "Titolo Telematico (Atto Notarile)",
        "fa-file-contract",
        "primary",
        "titolo_telematico",
        "titolo_notarile",
        "atto_notarile",
        always=True,
    )

    # Keep unexpected and newly imported document codes visible as well.
    unclassified = [
        d for d in docs if (d.get("document_type") or "(altro)") not in classified_types
    ]
    if unclassified:
        tree.append(leaf("altro", "Altri documenti", "fa-file", "secondary", unclassified))

    return tree


def _paginate_documents(docs: list[dict], page: int, page_size: int = _DOCUMENTS_PAGE_SIZE) -> tuple[list[dict], int, int, int]:
    """Return a stable newest-first document page and its display bounds."""
    ordered = sorted(
        docs,
        key=lambda document: (str(document.get("created_at") or ""), int(document.get("id") or 0)),
        reverse=True,
    )
    page_size = max(1, int(page_size))
    page_count = max(1, (len(ordered) + page_size - 1) // page_size)
    page = min(max(1, int(page or 1)), page_count)
    start = (page - 1) * page_size
    selected = ordered[start : start + page_size]
    return selected, page, page_count, start


def _dossiers_base() -> "Path":
    """Filesystem root holding dossier JSON files (multi/single-step query responses)."""
    from pathlib import Path

    from .database import DATA_ROOT

    return Path(os.getenv("SISTER_DOSSIERS_BASE", str(DATA_ROOT / "dossiers"))).resolve()


# Cache only the lightweight index metadata. The file's nanosecond mtime and
# size are checked on every scan, so changed files are parsed again immediately.
_DOSSIER_META_CACHE: dict[str, tuple[int, int, dict]] = {}
_DOSSIER_META_CACHE_LOCK = Lock()


def _cached_dossier_meta(path: Path, stat_result: os.stat_result) -> dict:
    cache_key = str(path.resolve())
    signature = (stat_result.st_mtime_ns, stat_result.st_size)
    with _DOSSIER_META_CACHE_LOCK:
        cached = _DOSSIER_META_CACHE.get(cache_key)
    if cached and cached[:2] == signature:
        return dict(cached[2])

    import json

    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    meta = _dossier_meta(path.name, data, stat_result.st_size, stat_result.st_mtime)
    meta["size_bytes"] = stat_result.st_size
    meta["mtime_ts"] = stat_result.st_mtime
    search_terms = [path.name, meta["title"], meta["ident"]]
    search_terms.extend(param["v"] for param in meta["request_params"])
    meta["search_text"] = " ".join(str(term) for term in search_terms if term).casefold()
    meta["filter_kind"] = "response" if meta["kind"] == "multi_response" else meta["kind"]

    with _DOSSIER_META_CACHE_LOCK:
        _DOSSIER_META_CACHE[cache_key] = (*signature, meta)
    return dict(meta)


def _load_dossier_index(base: Path) -> list[dict]:
    """Read index metadata for dossier files, reusing unchanged file metadata."""
    dossiers: list[dict] = []
    seen_cache_keys: set[str] = set()
    try:
        children = list(base.iterdir()) if base.exists() else []
    except OSError as exc:
        logger.warning("Cannot scan dossier directory %s: %s", base, exc)
        children = []

    def modified_time(path: Path) -> float:
        try:
            return path.stat().st_mtime if path.is_file() else 0
        except OSError:
            return 0

    for child in sorted(children, key=modified_time, reverse=True):
        if not child.is_file() or child.suffix.lower() != ".json":
            continue
        cache_key = None
        try:
            stat = child.stat()
            cache_key = str(child.resolve())
            seen_cache_keys.add(cache_key)
            dossiers.append(_cached_dossier_meta(child, stat))
        except Exception as exc:
            if cache_key:
                with _DOSSIER_META_CACHE_LOCK:
                    _DOSSIER_META_CACHE.pop(cache_key, None)
            logger.warning("Skipping unreadable dossier JSON %s: %s", child, exc)

    with _DOSSIER_META_CACHE_LOCK:
        stale_cache_keys = _DOSSIER_META_CACHE.keys() - seen_cache_keys
        for cache_key in stale_cache_keys:
            _DOSSIER_META_CACHE.pop(cache_key, None)
    return dossiers


def _dossier_group_key(name: str, kind: str, data: Any) -> str:
    """Return a grouping key; dossiers that share the same key are collapsed into one paired card.

    Two cases produce a non-unique key:
      • wf_<step>_<base>_<8hexchars>_<timestamp> files sharing the same <base>
        (e.g. wf_search_Agrigento_32_12_* and wf_intestati_Agrigento_32_12_*)
      • dict-of-responses files (immobili.json, results.json) — single file, unique on their own
    All other dossiers get a unique key equal to their stem.
    """
    import re

    stem = name.rsplit(".", 1)[0]

    if kind == "response" and stem.startswith("wf_"):
        # strip step-type prefix (wf_search_, wf_intestati_, wf_visura_, …)
        after_step = re.sub(r"^wf_[a-z]+_", "", stem)
        # strip trailing 8-char hex id + timestamp (_aa27e89f_20260412_075235)
        base = re.sub(r"_[0-9a-f]{8}_\d{8}_\d{6}$", "", after_step)
        if base and base != after_step:
            return f"wf_pair:{base}"

    # every other dossier is unique
    return f"solo:{stem}"


def _dossier_subtype(name: str, kind: str, data: Any) -> str:
    """Determine the query subtype for a dossier (used for second-level grouping)."""
    if kind == "workflow":
        if isinstance(data, dict):
            return data.get("preset") or "altro"
        return "altro"

    if kind == "response":
        stem = name.lower().split(".")[0]
        if stem.startswith("pnf_"):
            return "persona_giuridica"
        if stem.startswith("soggetto_"):
            return "soggetto_pf"
        if stem.startswith("eimm_"):
            return "elenco_immobili"
        if stem.startswith("intestati_") or stem.startswith("wf_intestati_"):
            return "intestati"
        if stem.startswith("richieste_"):
            return "richieste"
        if stem.startswith("req_") or stem.startswith("wf_search_") or stem.startswith("wf_"):
            tc = (data.get("tipo_catasto") or "") if isinstance(data, dict) else ""
            if tc == "F":
                return "visura_fabbricati"
            if tc == "T":
                return "visura_terreni"
            return "visura"
        # fallback: infer from data payload keys
        d = (data.get("data") or {}) if isinstance(data, dict) else {}
        if isinstance(d, dict):
            if d.get("soggetto"):
                return "soggetto_pf"
            if d.get("intestati"):
                return "intestati"
            if d.get("immobili"):
                return "visura"
        return "altro"

    if kind == "batch":
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                d = first.get("data") or {}
                if isinstance(d, dict) and d.get("soggetto"):
                    return "soggetto"
        return "altro"

    if kind == "multi_response":
        return "multi_response"

    return "altro"


_TC_LABEL = {"F": "Fabbricati", "T": "Terreni", "E": "Entrambi"}


def _dossier_meta(name: str, data: Any, size_bytes: int, mtime: float) -> dict:
    """Summarize one dossier JSON for the index listing.

    Recognizes three shapes:
      • multi-step workflow : {workflow_id, preset, steps[], aggregate, summary}
      • single-step response: {request_id, success, tipo_catasto, data, ...}
      • batch / list        : a JSON array of operations
    """
    kind, title, ident = "altro", name, ""
    badges: list[str] = []
    n_steps = n_results = 0
    ok = None
    # request_params: ordered list of {k, v} pairs shown in the "Richiesta" half of the card
    request_params: list[dict] = []
    # response_meta: {n_results, exported_at} shown in the "Risposta" half
    response_meta: dict = {}

    if isinstance(data, dict) and "steps" in data and ("summary" in data or "aggregate" in data):
        kind = "workflow"
        ident = data.get("workflow_id") or ""
        title = data.get("preset") or data.get("description") or name
        summ = data.get("summary") or {}
        n_steps = summ.get("total_steps") or (len(data.get("steps")) if isinstance(data.get("steps"), list) else 0)
        completed = summ.get("completed")
        if completed is not None:
            badges.append(f"{completed}/{n_steps} step")
        for k in ("properties", "owners", "addresses", "risk_flags"):
            if summ.get(k):
                badges.append(f"{summ[k]} {k}")
        if data.get("description"):
            request_params.append({"k": "Descrizione", "v": data["description"]})
        response_meta = {
            "n_results": summ.get("properties") or summ.get("completed") or 0,
            "exported_at": "",
        }

    elif isinstance(data, dict) and ("request_id" in data or "data" in data):
        kind = "response"
        ident = data.get("request_id") or ""
        success = data.get("success")
        if success is not None:
            ok = str(success).lower() not in ("false", "0", "no", "error")
        tc = data.get("tipo_catasto") or ""
        d = data.get("data") or {}

        # ── Request params (what was queried) ──────────────────────────
        if tc:
            request_params.append({"k": "Catasto", "v": _TC_LABEL.get(tc, tc)})
        if isinstance(d, dict):
            sogg = d.get("soggetto")
            if sogg:
                request_params.append({"k": "CF / P.IVA", "v": str(sogg)})
            for field, label in (
                ("provincia", "Provincia"),
                ("comune", "Comune"),
                ("foglio", "Foglio"),
                ("particella", "Particella"),
            ):
                if d.get(field):
                    request_params.append({"k": label, "v": str(d[field])})

        # ── Response meta (what came back) ─────────────────────────────
        if isinstance(d, dict):
            n_results = d.get("total_results") or 0
        response_meta = {
            "n_results": n_results,
            "exported_at": data.get("exported_at", "").replace("T", " ")[:16],
        }
        if n_results:
            badges.append(f"{n_results} risultati")

    elif isinstance(data, list):
        kind = "batch"
        n_results = len(data)
        request_params.append({"k": "Operazioni", "v": str(len(data))})

    elif isinstance(data, dict) and data and all(isinstance(v, dict) and "request_id" in v for v in data.values()):
        # dict-of-responses files (e.g. immobili.json, results.json)
        # Each value is a response keyed by request_id.
        kind = "multi_response"
        entries_list = list(data.values())
        ok = all(str(e.get("success", "True")).lower() not in ("false", "0") for e in entries_list)
        for e in entries_list:
            tc = e.get("tipo_catasto") or ""
            d = e.get("data") or {}
            nr = (d.get("total_results") or 0) if isinstance(d, dict) else 0
            label = _TC_LABEL.get(tc, tc) if tc else "?"
            request_params.append({"k": label, "v": f"{nr} risultati" if nr else "–"})
        n_results = sum(
            (e.get("data") or {}).get("total_results") or 0 for e in entries_list if isinstance(e.get("data"), dict)
        )
        response_meta = {"n_results": n_results, "exported_at": ""}

    subtype = _dossier_subtype(name, kind, data)
    group_key = _dossier_group_key(name, kind, data)

    return {
        "name": name,
        "kind": kind,
        "subtype": subtype,
        "group_key": group_key,
        "title": title or name,
        "ident": ident,
        "ok": ok,
        "badges": badges,
        "n_steps": n_steps,
        "n_results": n_results,
        "request_params": request_params,
        "response_meta": response_meta,
        "size_human": _human_size(size_bytes),
        "size_bytes": size_bytes,
        "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
        "mtime_ts": mtime,
    }


# ---------------------------------------------------------------------------
# Unified query-type taxonomy — shared by documents and dossiers pages.
# Top-level groups (key → label, icon, color).
# ---------------------------------------------------------------------------
_QUERY_GROUP_META: dict[str, tuple[str, str, str]] = {
    "visura_immobile": ("Visura per Immobile", "fa-house", "success"),
    "visura_soggetto": ("Visura per Soggetto", "fa-user-group", "info"),
    "intestati": ("Intestatari", "fa-users", "primary"),
    "planimetria": ("Planimetrie", "fa-ruler-combined", "secondary"),
    "epa": ("Elaborati Planimetrici", "fa-drafting-compass", "secondary"),
    "richieste": ("Richieste", "fa-clock-rotate-left", "secondary"),
    "workflow": ("Workflow Multi-step", "fa-diagram-project", "primary"),
    "batch": ("Batch", "fa-layer-group", "warning"),
    "altro": ("Altro", "fa-file", "secondary"),
}

# Subgroups within each top-level group (key → label, icon, color).
_QUERY_SUBGROUP_META: dict[str, tuple[str, str, str]] = {
    # Under visura_immobile
    "visura_fabbricati": ("Fabbricati", "fa-building", "success"),
    "visura_terreni": ("Terreni", "fa-seedling", "warning"),
    "visura": ("Visura Catastale", "fa-file-contract", "success"),
    "multi_response": ("Coppia F+T", "fa-copy", "success"),
    # Under intestati
    "intestati": ("Intestatari", "fa-users", "primary"),
    "elenco_immobili": ("Elenco Immobili", "fa-list", "primary"),
    # Under visura_soggetto
    "soggetto_pf": ("Persona Fisica", "fa-user", "info"),
    "persona_giuridica": ("Persona Giuridica", "fa-building-user", "info"),
    # Under workflow (keyed by preset name)
    "due-diligence": ("Due Diligence", "fa-file-contract", "primary"),
    "patrimonio": ("Asset Investigation", "fa-magnifying-glass", "info"),
    "fondiario": ("Land Survey", "fa-mountain", "success"),
    "aziendale": ("Corporate Audit", "fa-briefcase", "warning"),
    # Under batch
    "soggetto": ("Soggetto", "fa-user", "warning"),
    # Generic fallback
    "altro": ("Altro", "fa-file", "secondary"),
}


def _dossier_query_group(kind: str, subtype: str) -> str:
    """Map a dossier's (kind, subtype) to the unified query-type group key."""
    if kind == "workflow":
        return "workflow"
    if kind == "batch":
        return "batch"
    if kind == "multi_response":
        return "visura_immobile"
    if kind == "response":
        if subtype in ("visura", "visura_fabbricati", "visura_terreni"):
            return "visura_immobile"
        if subtype in ("intestati", "elenco_immobili"):
            return "intestati"
        if subtype in ("soggetto_pf", "persona_giuridica"):
            return "visura_soggetto"
        if subtype == "richieste":
            return "richieste"
    return "altro"


def _build_dossier_groups(dossiers: list[dict]) -> list[dict]:
    """Pair related responses and group dossiers by query type and subtype."""
    from collections import defaultdict

    def collapse_pairs(entries: list[dict]) -> list[dict]:
        pair_groups: dict[str, list[dict]] = defaultdict(list)
        for dossier in entries:
            pair_groups[dossier.get("group_key", f"solo:{dossier['name']}")].append(dossier)

        collapsed: list[dict] = []
        for group_key, peers in pair_groups.items():
            if not group_key.startswith("wf_pair:") or len(peers) <= 1:
                collapsed.extend(peers)
                continue

            base_name = group_key[len("wf_pair:") :]
            primary = max(peers, key=lambda peer: peer["mtime_ts"])
            total_results = sum(peer.get("n_results", 0) for peer in peers)
            combined_params: list[dict] = []
            for peer in peers:
                for param in peer.get("request_params", []):
                    if param not in combined_params:
                        combined_params.append(param)
            combined_size = sum(peer.get("size_bytes", 0) for peer in peers)

            merged = dict(primary)
            merged.update(
                {
                    "title": base_name.replace("_", " "),
                    "paired": True,
                    "peers": sorted(peers, key=lambda peer: (peer["subtype"], peer["name"])),
                    "ok": all(peer.get("ok") is not False for peer in peers),
                    "n_results": total_results,
                    "badges": [f"{total_results} risultati"] if total_results else [],
                    "request_params": combined_params,
                    "size_bytes": combined_size,
                    "size_human": _human_size(combined_size),
                    "search_text": " ".join(peer.get("search_text", "") for peer in peers).casefold(),
                    "response_meta": {
                        "n_results": total_results,
                        "exported_at": primary.get("response_meta", {}).get("exported_at", ""),
                    },
                }
            )
            collapsed.append(merged)
        return collapsed

    query_buckets: dict[str, list[dict]] = defaultdict(list)
    for dossier in dossiers:
        query_buckets[_dossier_query_group(dossier["kind"], dossier["subtype"])].append(dossier)

    groups: list[dict] = []
    group_order = (
        "visura_immobile",
        "visura_soggetto",
        "intestati",
        "planimetria",
        "epa",
        "richieste",
        "workflow",
        "batch",
        "altro",
    )
    for group_key in group_order:
        entries = query_buckets.get(group_key)
        if not entries:
            continue
        label, icon, color = _QUERY_GROUP_META[group_key]
        collapsed = collapse_pairs(entries)

        subtype_buckets: dict[str, list[dict]] = defaultdict(list)
        for dossier in collapsed:
            subtype_key = dossier["subtype"] if dossier["kind"] != "multi_response" else "multi_response"
            subtype_buckets[subtype_key].append(dossier)

        subgroups = []
        for subtype_key, subtype_entries in subtype_buckets.items():
            subtype_meta = _QUERY_SUBGROUP_META.get(subtype_key)
            if subtype_meta:
                subtype_label, subtype_icon, subtype_color = subtype_meta
            else:
                subtype_label = subtype_key.replace("-", " ").replace("_", " ").title()
                subtype_icon = "fa-file"
                subtype_color = color
            subgroups.append(
                {
                    "key": f"{group_key}_{subtype_key}",
                    "label": subtype_label,
                    "icon": subtype_icon,
                    "color": subtype_color,
                    "count": len(subtype_entries),
                    "entries": subtype_entries,
                }
            )
        subgroups.sort(key=lambda subgroup: (subgroup["label"] == "Altro", subgroup["label"]))
        groups.append(
            {
                "key": group_key,
                "label": label,
                "icon": icon,
                "color": color,
                "count": len(collapsed),
                "subgroups": subgroups,
            }
        )

    return groups


# Kept for backwards compat with any internal callers (batch-viewer etc.).
_DOSSIER_KIND_META = _QUERY_GROUP_META


def _is_batch_dossier(data: Any) -> bool:
    """True when data is a list of dicts each containing both org-context fields and a 'data' key."""
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    return (
        isinstance(first, dict)
        and "data" in first
        and ("organization_name" in first or "vat_number" in first or "request_id" in first)
    )


def _parse_batch_dossier(data: list) -> dict:
    """Split a batch list into three aligned tables for the batch viewer template."""
    import ast

    summary_rows: list[dict] = []
    immobili_rows: list[dict] = []
    immobili_col_set: set[str] = set()

    for idx, item in enumerate(data):
        raw = item.get("data")
        if isinstance(raw, str):
            try:
                raw = ast.literal_eval(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}

        org = item.get("organization_name") or item.get("vat_number") or str(idx)
        status = item.get("status") or ""
        ts = (item.get("timestamp") or "").replace("T", " ")[:16]
        imm_list = raw.get("immobili") or []
        n_imm = len(imm_list) if isinstance(imm_list, list) else 0

        err_raw = raw.get("error") or item.get("error")
        err_str = str(err_raw).strip() if err_raw and str(err_raw).strip() not in ("None", "") else ""
        soggetto = str(raw.get("soggetto") or "").strip()

        summary_rows.append(
            {
                "idx": idx,
                "organization_name": org,
                "vat_number": item.get("vat_number") or "",
                "soggetto": soggetto,
                "tipo_catasto": item.get("tipo_catasto") or "",
                "status": status,
                "n_immobili": n_imm,
                "total_results": raw.get("total_results") or 0,
                "timestamp": ts,
                "error": err_str,
            }
        )

        for imm in (imm_list if isinstance(imm_list, list) else []):
            if not isinstance(imm, dict):
                continue
            row: dict = {"idx": idx, "organization_name": org}
            for k, v in imm.items():
                if k:  # skip empty-string keys
                    row[k] = v
                    immobili_col_set.add(k)
            immobili_rows.append(row)

    # Stable column order for immobili table
    priority = [
        "Denominazione",
        "Sede",
        "Codice Fiscale",
        "Comune",
        "Provincia",
        "Foglio",
        "Particella",
        "Sub",
        "Categoria",
        "Classe",
        "Rendita",
    ]
    immobili_cols = [c for c in priority if c in immobili_col_set]
    immobili_cols += sorted(c for c in immobili_col_set if c not in priority)

    total_immobili = len(immobili_rows)
    n_ok = sum(1 for r in summary_rows if r["status"] == "completed")
    n_err = sum(1 for r in summary_rows if r["status"] == "error")

    n_with = sum(1 for r in summary_rows if r["n_immobili"])
    # Detect whether immobili rows are cadastral (have Foglio/Comune) or entity matches
    is_entity_match = bool(immobili_col_set) and not (immobili_col_set & {"Foglio", "Comune", "Categoria"})

    return {
        "summary_rows": summary_rows,
        "immobili_rows": immobili_rows,
        # data_rows removed — soggetto/total_results/error merged into summary_rows
        "immobili_cols": immobili_cols,
        "is_entity_match": is_entity_match,
        "stats": {
            "total": len(data),
            "ok": n_ok,
            "error": n_err,
            "with_immobili": n_with,
            "without_immobili": len(data) - n_with - n_err,
            "total_immobili": total_immobili,
            "pct_ok": round(n_ok / len(data) * 100) if data else 0,
            "pct_with": round(n_with / len(data) * 100) if data else 0,
        },
    }


def _dossier_to_result(name: str, data: Any) -> dict:
    """Transform a dossier JSON into the ``result`` shape consumed by result_detail.html.

    Workflow dossiers expose steps/aggregate/summary as sections; single-step
    responses expose their ``data`` payload; anything else is shown via the
    exhaustive nested dump. The raw JSON panel always reflects the full file.
    """
    base = {
        "status": "completed",
        "request_type": "dossier",
        "tipo_catasto": "-",
        "requested_at_display": "-",
        "responded_at_display": "-",
        "provincia": "-",
        "comune": "-",
        "foglio": "-",
        "particella": "-",
        "sezione": "-",
        "subalterno": "-",
        "error": None,
        "documents": [],
        "page_visit_rows": [],
    }

    if isinstance(data, dict) and "steps" in data:
        base.update(
            {
                "request_id": data.get("workflow_id") or name,
                "request_type": data.get("preset") or "workflow",
                "data": data,
                "sections": _build_result_sections(data),
            }
        )
    elif isinstance(data, dict) and ("data" in data or "request_id" in data):
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        base.update(
            {
                "request_id": data.get("request_id") or name,
                "tipo_catasto": data.get("tipo_catasto") or "-",
                "responded_at_display": data.get("exported_at") or "-",
                "error": data.get("error"),
                "status": "completed" if data.get("success", True) else "failed",
                "data": payload or {"_": True},
                "sections": _build_result_sections(payload),
            }
        )
    else:
        wrapped = {"contenuto": data}
        base.update(
            {
                "request_id": name,
                "data": wrapped,
                "sections": _build_result_sections(wrapped),
            }
        )
    return base


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


async def _optional_user(request: Request):
    """The signed-in user on pages that are public but personalised (sidebar, admin links); None when anonymous."""
    try:
        from aecs4u_auth.dependencies import get_current_user

        return await get_current_user(request)
    except Exception:
        return _get_user(request)


def _auth_required() -> bool:
    """True unless auth is explicitly disabled with REQUIRE_AUTHENTICATION=false (dev only)."""
    return os.getenv("REQUIRE_AUTHENTICATION", "true").lower() not in ("false", "0", "no")


_auth_disabled_warned = False


async def _require_auth(request: Request):
    """Dependency: require an authenticated user; fail closed.

    Page navigations (Accept: text/html) are redirected to the login page, everything else gets a
    401 JSON response. Only an explicit ``REQUIRE_AUTHENTICATION=false`` lets anonymous requests
    through (logged once); an auth backend that cannot be loaded never does.
    """
    global _auth_disabled_warned
    from fastapi import HTTPException

    try:
        from aecs4u_auth.dependencies import RedirectToLogin, get_current_user
    except ImportError as exc:
        if _auth_required():
            logger.error("aecs4u-auth non disponibile con REQUIRE_AUTHENTICATION attivo: %s", exc)
            raise HTTPException(status_code=503, detail="Authentication backend unavailable") from exc
        return _get_user(request)

    try:
        return await get_current_user(request)
    except HTTPException:
        if not _auth_required():
            if not _auth_disabled_warned:
                _auth_disabled_warned = True
                logger.warning("REQUIRE_AUTHENTICATION=false: le rotte UI accettano richieste anonime")
            return _get_user(request)
        if "text/html" in request.headers.get("accept", "") and request.method == "GET":
            raise RedirectToLogin(return_url=str(request.url))
        raise


def _is_admin(user) -> bool:
    """Return whether the authenticated principal has an explicit admin role or allow-list entry."""
    if not _auth_required():
        return True
    from jinja2 import Undefined

    if user is None or isinstance(user, Undefined):  # templates rendered without a user (public pages)
        return False
    if getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).lower() == "admin":
        return True
    has_role = getattr(user, "has_role", None)
    if callable(has_role) and has_role("admin"):
        return True
    configured_admins = {
        identity.strip().casefold() for identity in os.getenv("SISTER_ADMIN_USERS", "").split(",") if identity.strip()
    }
    if not configured_admins:
        return False
    user_identities = {
        str(getattr(user, attribute, "") or "").strip().casefold()
        for attribute in ("username", "email", "id")
    }
    return bool(configured_admins & user_identities)


async def _require_admin(request: Request, user=Depends(_require_auth)):
    """Dependency for operational actions (Browser Control, imports, rescans): authenticated *and* admin."""
    from fastapi import HTTPException

    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def _actor(user) -> str:
    """Who is acting, for the activity log (the signed-in user's email/username; 'anonymous' when auth is off)."""
    for attr in ("email", "username", "name"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value:
            return value
    return "anonymous"


def _favorite_user_key(user) -> Optional[str]:
    """Return the stable authenticated identity used to scope document favourites."""
    user_id = getattr(user, "id", None)
    if user_id is not None:
        return f"id:{user_id}"
    for attr in ("username", "email"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value.strip():
            return f"{attr}:{value.strip().casefold()}"
    return None


def _principal_keys(user) -> list[str]:
    """Every account key one signed-in principal can be known by (``id:``, ``username:``, ``email:``).

    Purchases are matched against all of them, case-insensitively, so a grant typed as ``email:a@b.it`` and one
    typed as ``id:a@b.it`` reach the same person.
    """
    keys: list[str] = []
    user_id = getattr(user, "id", None)
    if user_id is not None and str(user_id).strip():
        keys.append(f"id:{str(user_id).strip()}")
    for attr in ("username", "email"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value.strip():
            keys.append(f"{attr}:{value.strip().casefold()}")
    return keys


async def _visible_document_ids(user) -> set[int] | None:
    """Return the documents visible to this principal, or None for an explicit admin."""
    if _is_admin(user):
        return None
    keys = _principal_keys(user)
    if not keys:
        return set()
    from .database import get_document_purchase_ids

    return await get_document_purchase_ids(keys)


async def _filter_purchased_documents(user, documents: list[dict]) -> list[dict]:
    allowed_ids = await _visible_document_ids(user)
    if allowed_ids is None:
        return documents
    return [item for item in documents if item.get("id") is not None and int(item["id"]) in allowed_ids]


async def _load_visible_documents(user, *, admin_limit: int | None = None) -> list[dict]:
    """Load only documents this principal can see; avoid scanning the full catalog for purchasers."""
    visible_ids = await _visible_document_ids(user)
    if visible_ids is None:
        return await get_all_documents(limit=admin_limit)
    return await get_documents_by_ids(visible_ids)


async def _require_document_access(user, document_id: int) -> None:
    """Fail closed for document content unless this principal purchased it or is an admin."""
    from fastapi import HTTPException

    if _is_admin(user):
        return
    keys = _principal_keys(user)
    if not keys:
        raise HTTPException(status_code=404, detail="Document not found")
    from .database import has_document_purchase

    if not await has_document_purchase(keys, document_id):
        # Avoid confirming the existence of a document that belongs to another customer.
        raise HTTPException(status_code=404, detail="Document not found")


async def _require_document_path_access(user, target: Path) -> None:
    """Allow a purchased, indexed file path; unindexed filesystem files stay admin-only."""
    from fastapi import HTTPException

    if _is_admin(user):
        return
    from .database import get_indexed_file_paths

    indexed = await get_indexed_file_paths()
    resolved_target = target.resolve()
    matching_ids = {
        int(document_id)
        for indexed_path, document_id in indexed.items()
        if Path(indexed_path).resolve() == resolved_target
    }
    if len(matching_ids) != 1:
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, next(iter(matching_ids)))


_PG_BIGINT_MAX = 2**63 - 1

PURCHASE_MESSAGES = {
    "bad_key": "Chiave utente non valida: usa id:…, email:… oppure username:… (scegli un utente dall’elenco).",
    "key_too_long": "La chiave utente supera i 320 caratteri.",
    "bad_document": "ID documento non valido: indica un numero intero positivo.",
    "document_not_found": "Documento non trovato nel catalogo.",
    "bad_date": "Data di acquisto non valida.",
    "future_date": "La data di acquisto non può essere nel futuro.",
    "bad_expiry": "Data di scadenza non valida o precedente all’acquisto.",
    "note_too_long": "La nota supera i 2000 caratteri.",
    "reason_too_long": "Il motivo supera i 500 caratteri.",
    "duplicate": "Questo utente ha già accesso a questo documento.",
    "duplicate_payment": "Questo pagamento è già stato registrato.",
    "not_found": "Acquisto non trovato.",
    "bad_amount": "Importo non valido: indica un numero maggiore di zero (massimo 100000).",
    "credits_off": "I crediti prepagati non sono attivi (imposta SISTER_DOCUMENT_CREDITS e un wallet durevole).",
    "failed": "Operazione non riuscita. Riprova o controlla il registro accessi.",
}


def _purchase_identity(raw: str) -> str:
    """Validate and normalize the stable principal key used by document favorites and purchases."""
    from .database import PurchaseError

    value = str(raw or "").strip()
    if len(value) > 320:
        raise PurchaseError("key_too_long")
    prefix, separator, identifier = value.partition(":")
    if not separator or prefix.casefold() not in {"id", "email", "username"} or not identifier.strip():
        raise PurchaseError("bad_key")
    prefix = prefix.casefold()
    identifier = identifier.strip()
    if any(ch.isspace() for ch in identifier):
        raise PurchaseError("bad_key")
    if prefix in {"email", "username"}:
        identifier = identifier.casefold()
    return f"{prefix}:{identifier}"


def _purchase_document_id(raw: object) -> int:
    from .database import PurchaseError

    text_value = str(raw or "").strip()
    if not text_value.isascii() or not text_value.isdigit():
        raise PurchaseError("bad_document")
    value = int(text_value)
    if not 1 <= value <= _PG_BIGINT_MAX:
        raise PurchaseError("bad_document")
    return value


def _purchase_datetime(raw: str, *, allow_future: bool = False, error: str = "bad_date") -> datetime:
    """Interpret admin-entered local times in Rome and persist timestamps in UTC."""
    from zoneinfo import ZoneInfo

    from .database import PurchaseError

    value = str(raw or "").strip()
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Rome"))
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PurchaseError(error) from None
    if not allow_future and parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise PurchaseError("future_date")
    return parsed


async def _apply_user_manual_valuations(kpis: dict[str, Any] | None, user) -> dict[str, Any] | None:
    """Decorate subject KPIs with the signed-in user's provider entries and selected sources."""
    if not kpis:
        return kpis
    asset_keys = {
        identity.get("asset_key")
        for group in kpis.get("table_estimates", [])
        for kind in ("buildings", "lands")
        for estimate in group.get(kind, [])
        for identity in [estimate.get("asset_identity") or {}]
        if identity.get("asset_key")
    }
    saved_values = {}
    manual_valuation_unavailable = False
    user_key = _favorite_user_key(user)
    if user_key and asset_keys:
        try:
            from .database import ensure_user_property_omi_selections, get_user_manual_property_valuations

            saved_values = await get_user_manual_property_valuations(user_key, asset_keys)
            guessed_selections = []
            for group in kpis.get("table_estimates", []):
                for estimate in group.get("buildings", []):
                    identity = estimate.get("asset_identity") or {}
                    if not identity.get("asset_key"):
                        continue
                    try:
                        typology_code = int(estimate["omi_tipologia"]) if estimate.get("omi_tipologia") else None
                    except (TypeError, ValueError):
                        typology_code = None
                    legacy_zone_code = estimate.pop("_omi_legacy_zone_code", None)
                    guessed_selections.append({
                        "asset_key": identity["asset_key"],
                        "zone_code": estimate.get("omi_zona"),
                        "typology_code": typology_code,
                        "legacy_zone_code": legacy_zone_code,
                    })
            await ensure_user_property_omi_selections(user_key, guessed_selections)
            _clear_optional_data_degradation("manual_property_valuations")
        except Exception as exc:  # The official valuation page remains available if this optional data is unavailable.
            logger.warning("Manual property valuations unavailable: %s", type(exc).__name__)
            _record_optional_data_degradation("manual_property_valuations")
            manual_valuation_unavailable = True
    from .land_registry_kpis import apply_user_manual_valuations

    decorated = apply_user_manual_valuations(kpis, saved_values)
    if manual_valuation_unavailable:
        return {**decorated, "manual_valuation_unavailable": True}
    return decorated


_LAND_REGISTRY_CALCULATION_TYPE = "subject_land_registry_kpis"
_LAND_REGISTRY_CALCULATION_VERSION = "11"
_LAND_REGISTRY_CALCULATION_MAX_AGE_SECONDS = 12 * 60 * 60


async def _subject_land_registry_calculation(
    xml_parsed: dict[str, Any], map_lookup: dict[str, Any] | None, user, *, scope_type: str, scope_id: Any
) -> dict[str, Any]:
    """Load or persist the complete subject valuation/KPI result for these source inputs."""
    normalized_scope_id = str(scope_id or "").strip()
    viewer_key = _favorite_user_key(user) or "anonymous"
    input_payload = json.dumps(
        {"xml": xml_parsed, "map": map_lookup},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    input_hash = hashlib.sha256(input_payload.encode("utf-8")).hexdigest()
    if normalized_scope_id:
        try:
            from .database import get_calculation_snapshot

            cached = await get_calculation_snapshot(
                scope_type=scope_type,
                scope_id=normalized_scope_id,
                viewer_key=viewer_key,
                calculation_type=_LAND_REGISTRY_CALCULATION_TYPE,
                input_hash=input_hash,
                algorithm_version=_LAND_REGISTRY_CALCULATION_VERSION,
                max_age_seconds=_LAND_REGISTRY_CALCULATION_MAX_AGE_SECONDS,
            )
            if cached is not None:
                return cached
        except Exception as exc:  # Calculations still work when the optional snapshot store is unavailable.
            logger.warning("Land registry calculation snapshot unavailable: %s", type(exc).__name__)

    from .land_registry_kpis import subject_land_registry_kpis

    kpis = await subject_land_registry_kpis(
        xml_parsed,
        map_lookup,
        include_assessment_context=True,
        selection_user_id=_favorite_user_key(user),
    )
    kpis = await _apply_user_manual_valuations(kpis, user)
    if normalized_scope_id:
        try:
            from .database import set_calculation_snapshot

            await set_calculation_snapshot(
                scope_type=scope_type,
                scope_id=normalized_scope_id,
                viewer_key=viewer_key,
                calculation_type=_LAND_REGISTRY_CALCULATION_TYPE,
                input_hash=input_hash,
                algorithm_version=_LAND_REGISTRY_CALCULATION_VERSION,
                result=kpis,
            )
        except Exception as exc:  # Persistence is additive; a database write must not hide the page calculation.
            logger.warning("Land registry calculation snapshot could not be saved: %s", type(exc).__name__)
    return kpis


def _client_ip(request: Optional[Request]) -> Optional[str]:
    return request.client.host if request is not None and request.client else None


def _audited(action: str):
    """Record the call of a route handler in ``activity_log`` (who, parameters, HTTP outcome, duration).

    For handlers whose result is a small JSON/dict the summary is stored too (lists are reduced to their length).
    Handlers that know better numbers (imports, rescans) call ``record_activity`` themselves instead.
    """

    def _summary(response) -> dict:
        body: Any = response
        if isinstance(response, JSONResponse):
            try:
                import json

                body = json.loads(response.body)
            except (ValueError, TypeError):
                body = {}
        summary: dict[str, Any] = {"http_status": getattr(response, "status_code", 200)}
        if isinstance(body, dict):
            for key, value in body.items():
                if isinstance(value, (list, tuple, dict)):
                    summary[key] = len(value)
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    summary[key] = value if not isinstance(value, str) else value[:200]
        return summary

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request") or next((a for a in args if isinstance(a, Request)), None)
            user = kwargs.get("user")
            params = {k: v for k, v in kwargs.items() if k not in {"request", "user"} and isinstance(v, (str, int, float, bool))}
            started = datetime.now(timezone.utc)

            def _with_details() -> dict:
                # Handlers that read a form put the facts worth auditing (who/what/why) in request.state.audit_params.
                extra = getattr(getattr(request, "state", None), "audit_params", None)
                return {**params, **extra} if isinstance(extra, dict) else params

            try:
                response = await handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                from fastapi import HTTPException

                rejected = isinstance(exc, HTTPException) and exc.status_code < 500
                await record_activity(
                    action,
                    actor=_actor(user),
                    status="rejected" if rejected else "error",
                    params=_with_details(),
                    error=str(getattr(exc, "detail", None) or exc),
                    started_at=started,
                    client_ip=_client_ip(request),
                )
                raise
            code = getattr(response, "status_code", 200)
            response_headers = getattr(response, "headers", {})
            redirect_query = urlsplit(response_headers.get("location", "")).query
            rejected_redirect = code < 400 and (
                "error=" in redirect_query or "status=not_found" in redirect_query
            )
            await record_activity(
                action,
                actor=_actor(user),
                status=(
                    "rejected"
                    if rejected_redirect or 400 <= code < 500
                    else "error"
                    if code >= 500
                    else "success"
                ),
                params=_with_details(),
                result=_summary(response),
                started_at=started,
                client_ip=_client_ip(request),
            )
            return response

        return wrapper

    return decorator


def _build_url(path: str, **params) -> str:
    """Build a URL with only non-empty query params."""
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def _filter_remove_url(key: str, filters: dict) -> str:
    params = {k: v for k, v in filters.items() if k != key and k != "offset"}
    return _build_url("/results", **params)


def _format_timestamp(value: Optional[str | datetime]) -> Optional[str]:
    """Format ISO timestamps for human-readable display."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if not isinstance(value, str):
        return str(value)
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
        if root is None:
            return {}
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


def _merge_subject_visura_asset_groups(parsed_documents: list[dict]) -> dict:
    """Combine the asset groups from same-period subject visure for one portfolio view."""
    if not parsed_documents:
        return {}

    root_names = (
        "VisuraSoggettoAttuale",
        "VisuraSoggettoStorica",
        "VisuraSoggettoSintetica",
        "VisuraSoggetto",
    )
    primary = dict(parsed_documents[0])
    primary_root_name = next((name for name in root_names if isinstance(primary.get(name), dict)), None)
    if not primary_root_name:
        return primary

    import json

    from .provinces import province_code

    all_groups = []
    seen_groups = set()

    def group_items(value):
        if isinstance(value, list):
            for child in value:
                yield from group_items(child)
        elif isinstance(value, dict):
            yield value

    for parsed in parsed_documents:
        root_name = next((name for name in root_names if isinstance(parsed.get(name), dict)), None)
        if not root_name:
            continue
        visura = parsed[root_name]
        request = visura.get("DatiRichiesta") or {}
        source_province = ""
        if isinstance(request, dict):
            raw_province = request.get("Provincia") or ""
            source_province = province_code(raw_province) or str(raw_province).strip().upper()
        raw_groups = visura.get("GruppoUnitaImmobiliari")
        for group in group_items(raw_groups):
            if source_province and not group.get("Provincia"):
                group = {**group, "Provincia": source_province}
            fingerprint = json.dumps(group, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint not in seen_groups:
                seen_groups.add(fingerprint)
                all_groups.append(group)

    if all_groups:
        primary_visura = dict(primary[primary_root_name])
        primary_visura["GruppoUnitaImmobiliari"] = all_groups
        primary[primary_root_name] = primary_visura
    return primary


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
                        "Quota": (
                            (row.get("DirittiReali") or {}).get("Quota", "")
                            if isinstance(row.get("DirittiReali"), dict)
                            else ""
                        ),
                        "Diritto": (
                            (row.get("DirittiReali") or {}).get("Descrizione")
                            or (row.get("DirittiReali") or {}).get("CodiceDir", "")
                            if isinstance(row.get("DirittiReali"), dict)
                            else ""
                        ),
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
                    sections.append(
                        {
                            "name": key,
                            "dom_id": dom_id,
                            "title": title,
                            "kind": "workflow_steps",
                            "value": value,
                            "count": len(value),
                        }
                    )
                elif key == "downloaded_pdfs":
                    docs = _normalize_downloaded_pdfs(value)

                    # Aggregate intestati_rows from all documents into a top-level
                    # flat_table so the owner data is immediately visible without
                    # expanding every accordion item.
                    _agg: list[dict] = []
                    for _doc in docs:
                        _doc_label = _doc.get("oggetto") or _doc.get("filename") or "-"
                        for _row in (_doc.get("intestati_rows") or []):
                            _agg.append({"Subalterno": _doc_label, **_row})
                    if _agg:
                        _pri = ["Subalterno", "Nominativo", "Codice Fiscale", "Quota", "Diritto"]
                        _seen_c: set[str] = set()
                        _agg_cols: list[str] = []
                        for _c in _pri:
                            if _c not in _seen_c:
                                _seen_c.add(_c)
                                _agg_cols.append(_c)
                        for _r in _agg:
                            for _c in _r:
                                if _c not in _seen_c:
                                    _seen_c.add(_c)
                                    _agg_cols.append(_c)
                        sections.append(
                            {
                                "name": "intestati_da_documenti",
                                "dom_id": next_dom_id("intestati_da_documenti"),
                                "title": f"Intestati ({len(_agg)})",
                                "kind": "flat_table",
                                "columns": _agg_cols,
                                "rows": [{_c: _r.get(_c, "") for _c in _agg_cols} for _r in _agg],
                                "count": len(_agg),
                            }
                        )

                    sections.append(
                        {
                            "name": key,
                            "dom_id": dom_id,
                            "title": "Downloaded Documents",
                            "kind": "downloaded_docs",
                            "docs": docs,
                            "count": len(docs),
                        }
                    )
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
                    sections.append(
                        {
                            "name": key,
                            "dom_id": dom_id,
                            "title": title,
                            "kind": "flat_table",
                            "columns": columns,
                            "rows": rows,
                            "count": len(rows),
                        }
                    )
                else:
                    # Mixed rows: inline dict subfields, count lists → flat_table.
                    # Additionally, each list-of-dict field becomes its own child flat_table.
                    _col_order: list[str] = []
                    _col_set: set[str] = set()
                    # Identify a scalar "join key" (id / index field) for child tables.
                    _join_key: str | None = None
                    _list_dict_fields: list[str] = []  # list-of-dict fields to expand

                    def _add_col(name: str) -> None:
                        if name not in _col_set:
                            _col_set.add(name)
                            _col_order.append(name)

                    for _item in value:
                        for _k, _v in _item.items():
                            if not _k:
                                continue
                            if _is_scalar(_v):
                                _add_col(_k)
                                if _join_key is None and _k.lower() in ("id", "index", "result_index", "idx", "numero"):
                                    _join_key = _k
                            elif isinstance(_v, dict):
                                for _sk in _v:
                                    if _sk:
                                        _add_col(_sk)
                            elif isinstance(_v, list):
                                _add_col(f"{_k}_n")
                                if _v and isinstance(_v[0], dict) and _k not in _list_dict_fields:
                                    _list_dict_fields.append(_k)

                    if _col_order:
                        _flat_rows = []
                        for _i, _item in enumerate(value):
                            _flat: dict = {}
                            for _k, _v in _item.items():
                                if not _k:
                                    continue
                                if _is_scalar(_v):
                                    _flat[_k] = "" if _v is None else _v
                                elif isinstance(_v, dict):
                                    for _sk, _sv in _v.items():
                                        if _sk:
                                            _flat[_sk] = (
                                                "" if _sv is None
                                                else _sv if _is_scalar(_sv)
                                                else str(_sv)
                                            )
                                elif isinstance(_v, list):
                                    _flat[f"{_k}_n"] = len(_v)
                            _flat_rows.append({_c: _flat.get(_c, "") for _c in _col_order})
                        sections.append(
                            {
                                "name": key,
                                "dom_id": dom_id,
                                "title": title,
                                "kind": "flat_table",
                                "columns": _col_order,
                                "rows": _flat_rows,
                                "count": len(_flat_rows),
                            }
                        )

                        # Child sections: one flat_table per list-of-dict field.
                        for _lf in _list_dict_fields:
                            _jk = _join_key or "_idx"
                            _cc_order: list[str] = [_jk]
                            _cc_set: set[str] = {_jk}
                            for _i, _item in enumerate(value):
                                for _child in (_item.get(_lf) or []):
                                    if isinstance(_child, dict):
                                        for _ck in _child:
                                            if _ck and _ck not in _cc_set:
                                                _cc_set.add(_ck)
                                                _cc_order.append(_ck)
                            _child_rows: list[dict] = []
                            for _i, _item in enumerate(value):
                                _jv = _item.get(_join_key, _i) if _join_key else _i
                                for _child in (_item.get(_lf) or []):
                                    if isinstance(_child, dict):
                                        _crow: dict = {_jk: _jv}
                                        for _ck, _cv in _child.items():
                                            if _ck:
                                                _crow[_ck] = (
                                                    "" if _cv is None
                                                    else _cv if _is_scalar(_cv)
                                                    else str(_cv)
                                                )
                                        _child_rows.append({_c: _crow.get(_c, "") for _c in _cc_order})
                            if _child_rows:
                                sections.append(
                                    {
                                        "name": f"{key}_{_lf}",
                                        "dom_id": next_dom_id(f"{key}_{_lf}"),
                                        "title": f"{title} · {_titleize_key(_lf)}",
                                        "kind": "flat_table",
                                        "columns": _cc_order,
                                        "rows": _child_rows,
                                        "count": len(_child_rows),
                                        "parent_section": key,
                                        "join_key": _jk,
                                    }
                                )
                    else:
                        sections.append(
                            {
                                "name": key,
                                "dom_id": dom_id,
                                "title": title,
                                "kind": "nested_table",
                                "value": value,
                                "count": len(value),
                            }
                        )
            else:
                sections.append(
                    {
                        "name": key,
                        "dom_id": dom_id,
                        "title": title,
                        "kind": "list",
                        "items": [str(item) for item in value],
                        "value": value,
                        "count": len(value),
                    }
                )
        elif isinstance(value, dict):
            items = [(k, v) for k, v in value.items() if k]
            sections.append(
                {
                    "name": key,
                    "dom_id": dom_id,
                    "title": title,
                    "kind": "object",
                    "items": items,
                    "value": value,
                    "count": len(items),
                }
            )
        else:
            sections.append(
                {
                    "name": key,
                    "dom_id": dom_id,
                    "title": title,
                    "kind": "value",
                    "value": value,
                }
            )
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
    """Redirect the explicit dashboard URL to the dashboard at /."""
    return RedirectResponse(url="/")


@router.get("/landing", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("_landing.html", request, user=user)


@router.get("/omi/valuation", response_class=HTMLResponse, name="omi_valuation")
async def web_omi_valuation(
    request: Request,
    comune: Optional[str] = None,
    zona: Optional[str] = None,
    refresh: bool = False,
    user=Depends(_optional_user),
):
    """Overview of one OMI zone (quotes over time, variations, context), or the zone list of a municipality."""
    from .omi_zone_overview import load_zone_overview, overview_chart_data

    theme = _get_theme(request)
    locale = getattr(request.state, "locale", "it")
    normalized_comune = (comune or "").strip().upper()
    normalized_zona = (zona or "").strip().upper()
    search_submitted = bool(normalized_comune or normalized_zona)
    error_message = None
    overview: dict[str, Any] = {}
    response_status = 200

    if search_submitted and not normalized_comune:
        error_message = "Inserisci il codice del comune."
        response_status = 422
    elif len(normalized_comune) > 24 or len(normalized_zona) > 24:
        error_message = "Il codice del comune e la zona OMI devono contenere al massimo 24 caratteri."
        response_status = 422
    elif search_submitted:
        try:
            loaded = await load_zone_overview(normalized_comune, normalized_zona, refresh=refresh)
        except Exception:
            logger.exception(
                "OMI valuation page lookup failed for comune=%s zona=%s", normalized_comune, normalized_zona
            )
            loaded = None
        if loaded is None:
            error_message = "Le quotazioni OMI non sono temporaneamente disponibili."
            response_status = 503
        else:
            overview = loaded

    response = theme.render(
        "catasto/omi_valuation.html",
        request,
        user=user,
        locale=locale,
        page_title="Valutazione OMI per zona",
        comune=normalized_comune,
        zona=normalized_zona,
        search_submitted=search_submitted,
        error_message=error_message,
        overview=overview,
        chart_data=overview_chart_data(overview) if overview.get("zone_found") else None,
        omi_url="/omi/valuation",
        omi_method_url="/valuation",
        chart_js_url="/static/vendor/chart.js-4.4.4/chart.umd.min.js",
    )
    response.status_code = response_status
    return response


# ---------------------------------------------------------------------------
# Authenticated web routes
# ---------------------------------------------------------------------------


def _get_auth_status() -> dict:
    """Get browser/auth status from the running service, if available."""
    from .main import visura_service

    if visura_service is not None:
        return visura_service.auth_status
    return {"state": "unavailable", "message": "Browser service not initialized"}


@router.get("/", response_class=HTMLResponse)
async def web_index(request: Request, user=Depends(_require_auth)):
    """Dashboard — service health and recent activity (operators); regular users land on their documents."""
    if not _is_admin(user):
        return RedirectResponse(url="/documents", status_code=303)
    theme = _get_theme(request)
    stats = await count_result_rows()
    recent = await find_result_rows(limit=5)
    for item in recent:
        item["requested_at_display"] = _format_timestamp(item.get("requested_at"))
    return theme.render(
        "index.html",
        request,
        user=user,
        stats=stats,
        recent=recent,
        auth_status=_get_auth_status(),
    )


async def _wait_for_missing_pair_response(service, request_id: str, timeout_seconds: int = 3600):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await service.get_response(request_id)
        if response is not None:
            return response
        if service.get_request_state(request_id) == "expired":
            return None
        await asyncio.sleep(2)
    return None


async def _run_missing_pair_batch(job: dict[str, Any], batch: dict[str, Any], service) -> None:
    """Submit each deduplicated free visura request, then fetch and index available documents."""
    from .missing_pairs import build_missing_pair_batch
    from .models import GenericSisterRequest

    job["status"] = "running"
    requests = batch["requests"]
    try:
        for index, item in enumerate(requests, start=1):
            if not service.auth_ready:
                job["error"] = "SISTER browser is no longer authenticated. Sign in and start a new batch."
                job["status"] = "partial" if job["processed_count"] else "error"
                break

            job["current"] = item["label"]
            job["current_index"] = index
            request_id = f"missing_pair_{uuid4().hex}"
            sister_request = GenericSisterRequest(
                request_id=request_id,
                search_type=item["search_type"],
                province=item["province"],
                municipality=item.get("municipality"),
                cadastre_type=item["cadastre_type"],
                params=item["params"],
            )
            try:
                submission = await service.add_generic_request(sister_request, force=True)
                request_id = getattr(submission, "request_id", request_id)
                job["submitted_count"] += 1
                response = await _wait_for_missing_pair_response(service, request_id)
                if response is None:
                    result_status = "timeout"
                    error = "No response before the one-hour wait expired"
                    job["failed_count"] += 1
                elif response.success:
                    result_status = "completed"
                    error = None
                    job["success_count"] += 1
                else:
                    result_status = "error"
                    error = response.error or "SISTER request failed"
                    job["failed_count"] += 1
                job["processed_count"] += 1
                job["results"].append(
                    {
                        "label": item["label"],
                        "source_files": item["source_files"],
                        "status": result_status,
                        "error": error,
                    }
                )
                if result_status == "timeout":
                    job["error"] = "Stopped after a request timed out; remaining requests were not submitted."
                    job["status"] = "partial"
                    break
            except Exception as exc:
                job["processed_count"] += 1
                job["failed_count"] += 1
                job["results"].append(
                    {
                        "label": item["label"],
                        "source_files": item["source_files"],
                        "status": "error",
                        "error": str(exc),
                    }
                )

        if job["submitted_count"]:
            job["current"] = "Downloading available PDF/P7M documents"
            downloaded = await service.download_richieste_documents()
            job["downloaded_count"] = len(downloaded)

        remaining = await run_in_threadpool(build_missing_pair_batch, _files_base())
        job["remaining_pair_count"] = len(remaining["missing_pairs"])
        job["remaining_pairs"] = remaining["missing_pairs"][:100]
        if job["status"] == "running":
            if job["failed_count"]:
                job["status"] = "completed_with_errors"
            elif job["remaining_pair_count"]:
                job["status"] = "completed_with_gaps"
            else:
                job["status"] = "completed"
    except Exception as exc:
        logger.exception("Missing-pair batch %s failed", job["job_id"])
        job["error"] = str(exc)
        job["status"] = "partial" if job["submitted_count"] else "error"
    finally:
        job["current"] = None
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _missing_pair_job_view(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {"status": "idle"}
    return {
        key: value
        for key, value in job.items()
        if key not in {"_task"}
    }


@router.post("/documents/retrieve-missing-pairs", response_class=JSONResponse)
@_audited("documents.retrieve_missing_pairs")
async def web_retrieve_missing_pairs(request: Request, user=Depends(_require_admin)):
    """Find missing PDF/P7M pairs and start a free-visura recovery batch."""
    global _missing_pair_job, _missing_pair_task

    from .main import visura_service
    from .missing_pairs import build_missing_pair_batch

    batch = await run_in_threadpool(build_missing_pair_batch, _files_base())
    if batch.get("error"):
        return JSONResponse({"error": batch["error"]}, status_code=503)
    async with _missing_pair_job_lock:
        if _missing_pair_job and _missing_pair_job["status"] in {"queued", "running"}:
            return JSONResponse(
                {"error": "A missing-pair batch is already running", "job": _missing_pair_job_view(_missing_pair_job)},
                status_code=409,
            )
        if batch["requests"] and (visura_service is None or not visura_service.auth_ready):
            return JSONResponse(
                {
                    "error": "SISTER browser is not authenticated. Sign in before retrieving missing pairs.",
                    "missing_pair_count": len(batch["missing_pairs"]),
                    "request_count": len(batch["requests"]),
                },
                status_code=503,
            )

        job = {
            "job_id": uuid4().hex,
            "status": "queued" if batch["requests"] else "completed",
            "missing_pair_count": len(batch["missing_pairs"]),
            "request_count": len(batch["requests"]),
            "excluded_paid_count": batch["excluded_paid"],
            "skipped_count": len(batch["skipped"]),
            "skipped_files": batch["skipped"][:100],
            "processed_count": 0,
            "submitted_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "downloaded_count": 0,
            "remaining_pair_count": len(batch["missing_pairs"]),
            "remaining_pairs": batch["missing_pairs"][:100],
            "current_index": 0,
            "current": None,
            "results": [],
            "error": None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
        }
        if batch["missing_pairs"] and not batch["requests"]:
            job["message"] = "Missing pairs were found, but none could be mapped to a supported free visura request."
        elif not batch["missing_pairs"]:
            job["message"] = "All supported PDF/P7M files already have their counterpart."
        _missing_pair_job = job
        if batch["requests"]:
            _missing_pair_task = asyncio.create_task(_run_missing_pair_batch(job, batch, visura_service))

    return JSONResponse(_missing_pair_job_view(job), status_code=202 if batch["requests"] else 200)


@router.get("/documents/retrieve-missing-pairs/status", response_class=JSONResponse)
async def web_retrieve_missing_pairs_status(request: Request, user=Depends(_require_admin)):
    async with _missing_pair_job_lock:
        return JSONResponse(_missing_pair_job_view(_missing_pair_job))


def _examples_as_placeholders(groups):
    """Show a parameter's example as a grey placeholder, never as a pre-filled value.

    The shared form template writes ``example`` into the input's ``value`` (the Due Diligence form therefore opened with
    ``Roma / ROMA / 100 / 50`` filled in, one click away from a real portal query for a made-up parcel). Hidden inputs
    and selects keep theirs: those values are part of the request.
    """
    import dataclasses

    fixed = []
    for group in groups:
        params = []
        for param in group.params:
            if param.example and param.input_type not in {"hidden", "select", "checkbox", "radio"} and not param.options:
                param = dataclasses.replace(param, placeholder=param.placeholder or f"es. {param.example}", example=None)
            params.append(param)
        fixed.append(dataclasses.replace(group, params=params))
    return fixed


@router.get("/forms", response_class=HTMLResponse)
async def web_forms(request: Request, user=Depends(_require_auth)):
    """Query submission forms."""
    access_error = await _query_access_error(user)
    if access_error is not None:
        raise access_error
    from . import purchases_billing

    theme = _get_theme(request)
    prices = await _query_product_prices()
    response = theme.render(
        "catasto/forms.html",
        request,
        user=user,
        form_groups=_examples_as_placeholders(get_available_form_groups()),
        single_step_groups=_examples_as_placeholders(get_single_step_groups()),
        workflow_groups=_examples_as_placeholders(get_workflow_groups()),
        query_credits_enabled=purchases_billing.query_credits_enabled(bool(prices)),
    )
    # The shared template omits method/action, so HTML defaults to GET and leaks query values into the URL when
    # JavaScript is disabled. Query forms are JavaScript-driven and must never submit sensitive values as GET.
    if isinstance(response, HTMLResponse) and response.body:
        html = response.body.decode("utf-8")
        html, form_count = _QUERY_FORM_TAG.subn(
            r'<form method="post" action="/forms" id="\1" class="sister-form"', html
        )
        if form_count:
            response.body = html.encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
    return response


@router.post("/results/refresh", response_class=HTMLResponse)
async def web_results_refresh(request: Request, user=Depends(_require_admin)):
    """Import exported response JSON files into the configured database."""
    import importlib.util
    from pathlib import Path

    from .database import OUTPUTS_DIR, save_request, save_response

    started = datetime.now(timezone.utc)
    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "scripts" / "populate_query_data.py"
    spec = importlib.util.spec_from_file_location("populate_query_data", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    source = Path(OUTPUTS_DIR)
    imported = 0
    for _, payload in list(mod._iter_response_payloads(source)):
        fields = mod._infer_request_fields(payload)
        await save_request(
            request_id=fields["request_id"],
            request_type=fields["request_type"],
            tipo_catasto=fields["tipo_catasto"],
            provincia=fields["provincia"],
            comune=fields["comune"],
            foglio=fields["foglio"],
            particella=fields["particella"],
            sezione=fields["sezione"],
            subalterno=fields["subalterno"],
            cache_key=fields["cache_key"],
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        await save_response(
            request_id=fields["request_id"],
            success=mod._success(payload),
            tipo_catasto=fields["tipo_catasto"],
            data=data,
            error=payload.get("error"),
            export=False,
        )
        imported += 1
    logger.info("Imported %s response payloads from %s", imported, source)
    await record_activity(
        "results.import",
        actor=_actor(user),
        target=str(source),
        result={"imported": imported},
        started_at=started,
        client_ip=_client_ip(request),
    )

    return RedirectResponse("/results", status_code=303)


@router.get("/results", response_class=HTMLResponse)
async def web_results(
    request: Request,
    user=Depends(_require_admin),
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    request_type: Optional[str] = None,
    tag: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    fiscal_code: Optional[str] = None,
    person: Optional[str] = None,
    company: Optional[str] = None,
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
        request_type=request_type,
        tag=tag,
        source=source,
        status=status,
        limit=limit,
        offset=offset,
        fiscal_code=fiscal_code,
        person=person,
        company=company,
    )
    total_count = await count_total_result_rows(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        request_type=request_type,
        tag=tag,
        source=source,
        status=status,
        fiscal_code=fiscal_code,
        person=person,
        company=company,
    )
    for item in results:
        item["requested_at_display"] = _format_timestamp(item.get("requested_at"))
        item["responded_at_display"] = _format_timestamp(item.get("responded_at"))
        type_info = request_type_info(item.get("request_type"))
        item["request_type_label"] = item.get("request_type_label") or type_info.label
        item["request_type_description"] = item.get("request_type_description") or type_info.description
        item["request_type_tags"] = item.get("request_type_tags") or list(type_info.tags)
        if item.get("source") != "workflow" and not item.get("status"):
            item["status"] = (
                "completed" if item.get("success") is True else "failed" if item.get("success") is False else "pending"
            )
    stats = await count_result_rows(
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        source=source,
        request_type=request_type,
        tag=tag,
        fiscal_code=fiscal_code,
        person=person,
        company=company,
    )

    request_type_options = [{"code": code, "label": info.label} for code, info in known_request_types()]
    known_codes = {item["code"] for item in request_type_options}
    if request_type and request_type not in known_codes:
        request_type_options.append({"code": request_type, "label": request_type_info(request_type).label})
        known_codes.add(request_type)
    for item in results:
        code = item.get("request_type")
        if code and code not in known_codes:
            request_type_options.append({"code": code, "label": item["request_type_label"]})
            known_codes.add(code)
    request_type_options.sort(key=lambda item: item["label"].casefold())
    tag_options = set(known_request_tags())
    if tag:
        tag_options.add(tag)
    for item in results:
        tag_options.update(item.get("request_type_tags") or [])
    tag_options = sorted(tag_options, key=str.casefold)
    current_filters = {
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tipo_catasto,
        "request_type": request_type,
        "tag": tag,
        "source": source,
        "status": status,
        "fiscal_code": fiscal_code,
        "person": person,
        "company": company,
        "limit": limit,
    }
    filter_labels = {
        "provincia": "Provincia",
        "comune": "Comune",
        "foglio": "Foglio",
        "particella": "Particella",
        "tipo_catasto": "Catasto",
        "request_type": "Tipo richiesta",
        "tag": "Categoria",
        "source": "Origine",
        "status": "Stato",
        "fiscal_code": "Codice fiscale / P.IVA",
        "person": "Persona",
        "company": "Azienda",
    }
    request_type_labels = {item["code"]: item["label"] for item in request_type_options}
    active_filters = [
        {
            "key": key,
            "label": filter_labels[key],
            "value": (
                request_type_labels.get(value, request_type_info(value).label)
                if key == "request_type"
                else str(value).replace("_", " ").title()
                if key in {"source", "status"}
                else value
            ),
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
        "request_type": request_type,
        "tag": tag,
        "source": source,
        "fiscal_code": fiscal_code,
        "person": person,
        "company": company,
    }
    stats_urls = {
        "total": _build_url("/results", **stats_filters),
        "completed": _build_url("/results", status="completed", **stats_filters),
        "partial": _build_url("/results", status="partial", **stats_filters),
        "failed": _build_url("/results", status="failed", **stats_filters),
        "no_match": _build_url("/results", status="no_match", **stats_filters),
        "pending": _build_url("/results", status="pending", **stats_filters),
    }
    prev_url = None
    if offset > 0:
        prev_url = _build_url("/results", offset=max(offset - limit, 0), **current_filters)
    next_url = None
    if offset + len(results) < total_count:
        next_url = _build_url("/results", offset=offset + limit, **current_filters)

    return theme.render(
        "catasto/results.html",
        request,
        user=user,
        results=results,
        stats=stats,
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        request_type=request_type,
        tag=tag,
        source=source,
        status=status,
        fiscal_code=fiscal_code,
        person=person,
        company=company,
        limit=limit,
        offset=offset,
        prev_url=prev_url,
        next_url=next_url,
        stats_urls=stats_urls,
        active_filters=active_filters,
        request_type_options=request_type_options,
        tag_options=tag_options,
        current_count=len(results),
        total_count=total_count,
    )


# OpenData owns workflow storage and is a separate service. It must never be able to stall a page: calls get a short
# connect timeout and a total budget, and after a failure the service is not probed again for a while (circuit breaker),
# so a down or hung OpenData costs one fast failure instead of seconds on every page view.
_OPENDATA_CONNECT_TIMEOUT = 1.0
_OPENDATA_TOTAL_TIMEOUT = 4.0
_OPENDATA_BACKOFF_SECONDS = 30.0
_opendata_down_until = 0.0


def opendata_available() -> bool:
    """False while the circuit breaker is open (OpenData failed recently)."""
    return time.monotonic() >= _opendata_down_until


async def _opendata_get(path: str, params: Optional[dict] = None, *, client_factory=None):
    """GET *path* on OpenData. Returns the response, or None when the service is down/unreachable/backing off."""
    global _opendata_down_until
    if not opendata_available():
        return None
    try:
        # Everything that can fail (importing/constructing the HTTP client included) stays inside the try: OpenData
        # being unusable must degrade the page, not break it.
        if client_factory is None:
            import httpx

            client_factory = httpx.AsyncClient
            timeout = httpx.Timeout(_OPENDATA_TOTAL_TIMEOUT, connect=_OPENDATA_CONNECT_TIMEOUT)
        else:
            timeout = _OPENDATA_TOTAL_TIMEOUT
        async with client_factory(timeout=timeout) as client:
            return await asyncio.wait_for(
                client.get(f"{_OPENDATA_API_URL}{path}", params=params), timeout=_OPENDATA_TOTAL_TIMEOUT
            )
    except Exception as exc:  # connection refused, timeout, DNS, ...
        _opendata_down_until = time.monotonic() + _OPENDATA_BACKOFF_SECONDS
        logger.warning(
            "OpenData non raggiungibile (%s): nuovo tentativo tra %ds", type(exc).__name__, _OPENDATA_BACKOFF_SECONDS
        )
        return None


async def _query_product_prices() -> dict[str, Decimal] | None:
    """Load the active SISTER query product prices from OpenData, with a brief in-process cache.

    OpenData remains the price source of truth. A missing or unreachable catalogue is represented by ``None`` and
    causes paid queries to fail closed. Failed lookups are briefly cached to avoid retrying a broken endpoint on every
    form refresh; a successful but empty catalogue is returned as an empty mapping.
    """
    global _query_price_cache, _query_price_cache_until, _query_price_failure_until
    now = time.monotonic()
    if _query_price_cache is not None and now < _query_price_cache_until:
        return dict(_query_price_cache)
    if now < _query_price_failure_until:
        return None

    response = await _opendata_get("/api/pricing")
    if response is None or response.status_code != 200:
        _query_price_failure_until = now + _QUERY_PRICE_FAILURE_CACHE_SECONDS
        if response is not None:
            logger.warning("OpenData query price catalogue returned HTTP %s", response.status_code)
        return None
    try:
        from . import purchases_billing

        prices = purchases_billing.parse_query_price_catalog(response.json())
    except Exception as exc:  # noqa: BLE001 - invalid price data must never turn into a free query
        logger.warning("OpenData query price catalogue invalid: %s", type(exc).__name__)
        _query_price_failure_until = now + _QUERY_PRICE_FAILURE_CACHE_SECONDS
        return None
    _query_price_cache = prices
    _query_price_cache_until = now + _QUERY_PRICE_CACHE_SECONDS
    _query_price_failure_until = 0.0
    return dict(prices)


async def _query_product_price(path: str, *, allow_shared_fallback: bool = False) -> tuple[Decimal | None, str | None]:
    from . import purchases_billing

    prices = await _query_product_prices()
    if prices is None:
        return None, None
    requested_path = purchases_billing.normalize_query_price_path(path)
    cost = prices.get(requested_path)
    if cost is not None:
        return cost, requested_path
    shared_path = purchases_billing.normalize_query_price_path(_QUERY_SHARED_PRICE_PATH)
    if allow_shared_fallback and shared_path in prices:
        return prices[shared_path], shared_path
    return None, None


async def _query_product_cost(path: str, *, allow_shared_fallback: bool = False) -> Decimal | None:
    return (await _query_product_price(path, allow_shared_fallback=allow_shared_fallback))[0]


async def get_workflow_result_record(request_id: str) -> Optional[dict]:
    """Fetch a workflow run from opendata (returns None on miss or error)."""
    resp = await _opendata_get(f"/catasto/workflow/runs/{request_id}")
    if resp is not None and resp.status_code == 200:
        return resp.json()
    return None


@router.get("/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str, user=Depends(_require_admin)):
    """Single result detail page."""
    theme = _get_theme(request)
    result = await get_result_record(request_id)
    if result is None and request_id.startswith("wf_"):
        result = await get_workflow_result_record(request_id)
    if result is None:
        response = theme.render(
            "result_detail.html",
            request,
            user=user,
            result=None,
            request_id=request_id,
            not_found=True,
        )
        if hasattr(response, "status_code"):
            response.status_code = 404
        return response

    result["requested_at_display"] = _format_timestamp(result.get("requested_at"))
    result["responded_at_display"] = _format_timestamp(result.get("responded_at"))
    result["sections"] = _build_result_sections(result.get("data"))
    result["documents"] = await _filter_purchased_documents(
        user,
        await get_documents_for_response(
            request_id,
            foglio=result.get("foglio"),
            particella=result.get("particella"),
        ),
    )
    for doc in result["documents"]:
        doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
        doc["view"] = build_visura_view(doc.get("xml_content", ""))
        doc.pop("xml_content", None)
        # Normalize intestati server-side for flat Tabulator display
        doc["intestati_rows"] = [
            {
                "Nominativo": row.get("Nominativo") or row.get("nominativo") or "-",
                "Codice Fiscale": row.get("CF") or row.get("CodiceFiscale") or "-",
                "Quota": (
                    (row.get("DirittiReali") or {}).get("Quota", "")
                    if isinstance(row.get("DirittiReali"), dict)
                    else ""
                ),
                "Diritto": (
                    (row.get("DirittiReali") or {}).get("Descrizione")
                    or (row.get("DirittiReali") or {}).get("CodiceDir", "")
                    if isinstance(row.get("DirittiReali"), dict)
                    else ""
                ),
                "Periodo": (
                    (row.get("DirittiReali") or {}).get("FineDiritto", "")
                    if isinstance(row.get("DirittiReali"), dict)
                    else ""
                ),
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
    result["db_properties"] = await get_db_properties_for_response(request_id)
    result["db_owners"] = await get_db_owners_for_response(request_id)
    return theme.render(
        "result_detail.html",
        request,
        user=user,
        result=result,
        request_id=request_id,
        not_found=False,
    )


async def find_workflow_runs(status: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """Fetch workflow runs from opendata (returns empty list on error)."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    resp = await _opendata_get("/catasto/workflow/runs", params)
    if resp is not None and resp.status_code == 200:
        return resp.json().get("runs", [])
    return []


@router.get("/workflows", response_class=HTMLResponse)
async def web_workflows(
    request: Request,
    user=Depends(_require_admin),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Workflow runs list — proxied from opendata (which owns workflow storage)."""
    theme = _get_theme(request)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    runs = await find_workflow_runs(status=status, limit=limit, offset=offset)

    for run in runs:
        run["created_at_display"] = _format_timestamp(run.get("created_at"))
        run["updated_at_display"] = _format_timestamp(run.get("updated_at"))

    return theme.render(
        "workflows.html",
        request,
        user=user,
        runs=runs,
        status=status,
        limit=limit,
        offset=offset,
        opendata_down=not opendata_available(),
        auth_status=_get_auth_status(),
    )


@router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
async def web_workflow_detail(request: Request, workflow_id: str, user=Depends(_require_admin)):
    """Workflow detail page — proxied from opendata."""
    theme = _get_theme(request)

    result = await get_workflow_result_record(workflow_id)

    if result is None:
        response = theme.render(
            "workflow_detail.html",
            request,
            user=user,
            result=None,
            workflow_id=workflow_id,
        )
        if hasattr(response, "status_code"):
            response.status_code = 404
        return response

    result["requested_at_display"] = _format_timestamp(result.get("requested_at"))
    result["responded_at_display"] = _format_timestamp(result.get("responded_at"))
    result["sections"] = _build_result_sections(result.get("data"))
    result["documents"] = await _filter_purchased_documents(
        user,
        await get_documents_for_response(
            workflow_id,
            foglio=result.get("foglio"),
            particella=result.get("particella"),
        ),
    )
    for doc in result["documents"]:
        doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
        doc["view"] = build_visura_view(doc.get("xml_content", ""))
        doc.pop("xml_content", None)
    result["page_visit_rows"] = []
    return theme.render(
        "workflow_detail.html",
        request,
        user=user,
        result=result,
        workflow_id=workflow_id,
    )


@router.get("/profile", response_class=HTMLResponse)
async def web_profile(request: Request, user=Depends(_require_auth)):
    """Signed-in user's profile (target of the header user menu)."""
    theme = _get_theme(request)
    user = user or _get_user(request)
    from . import purchases_billing

    balance = None
    if purchases_billing.credit_grants_enabled() and _principal_keys(user):
        try:
            balance = await purchases_billing.credit_balance(_favorite_user_key(user) or _principal_keys(user)[0])
        except Exception as exc:  # noqa: BLE001 - the profile must open even if the wallet is unreachable
            logger.warning("Credit balance unavailable: %s", type(exc).__name__)
    return theme.render("profile.html", request, user=user, account_keys=_principal_keys(user), credit_balance=balance)


@router.get("/settings", response_class=HTMLResponse)
async def web_settings(request: Request, user=Depends(_require_auth)):
    """Signed-in user's preferences, rendered from the shared theme template."""
    user = user or _get_user(request)
    is_admin = _is_admin(user)
    return _get_theme(request).render(
        "settings.html",
        request,
        user=user,
        page_title="Impostazioni",
        sidebar_enabled=is_admin,
        nav_items=(
            [
                {
                    "id": "admin",
                    "label": "Amministrazione",
                    "icon": "fa-shield-halved",
                    "children": [
                        {"label": "Dashboard SISTER", "url": "/", "icon": "fa-house"},
                        {"label": "Gestione", "url": "/admin", "icon": "fa-shield-halved"},
                        {"label": "Catalogo documenti", "url": "/admin/documents", "icon": "fa-folder-open"},
                        {"label": "Registro accessi", "url": "/admin/activity", "icon": "fa-clock-rotate-left"},
                        {
                            "label": "Acquisti documenti",
                            "url": "/admin/document-purchases",
                            "icon": "fa-file-invoice",
                        },
                        {"label": "Browser", "url": "/browser", "icon": "fa-window-maximize"},
                    ],
                }
            ]
            if is_admin
            else []
        ),
    )


@router.get("/admin", response_class=HTMLResponse)
async def web_admin(request: Request, user=Depends(_require_admin)):
    """Admin landing page for managing document ownership."""
    return _get_theme(request).render("admin/index.html", request, user=user)


@router.get("/admin/documents", response_class=HTMLResponse)
async def web_admin_documents(
    request: Request, q: str = "", page: int = 1, user=Depends(_require_admin)
):
    """Browse the indexed document catalog and open ownership management for a document."""
    from .database import list_admin_documents

    page_size = 50
    page = max(1, page)
    documents, total = await list_admin_documents(q, limit=page_size, offset=(page - 1) * page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
        documents, total = await list_admin_documents(q, limit=page_size, offset=(page - 1) * page_size)
    return _get_theme(request).render(
        "admin/documents.html",
        request,
        user=user,
        documents=documents,
        query=q,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@router.get("/admin/activity", response_class=HTMLResponse)
async def web_admin_activity(request: Request, user=Depends(_require_admin)):
    """Show the latest audited changes to document ownership grants."""
    from zoneinfo import ZoneInfo

    from .database import get_recent_activity

    events = await get_recent_activity(limit=100, action_prefix="admin.document_purchase.")
    for event in events:
        started_at = event.get("started_at")
        if started_at:
            try:
                parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                event["started_at_local"] = parsed.astimezone(ZoneInfo("Europe/Rome")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                event["started_at_local"] = str(started_at)
    return _get_theme(request).render("admin/activity.html", request, user=user, events=events)


def _numeric_text(value: str | None) -> str:
    """A number from a query string, or '' (the manager never prints free text that came from a link)."""
    from decimal import Decimal, InvalidOperation

    try:
        return format(Decimal(str(value)).normalize(), "f") if value not in (None, "") else ""
    except InvalidOperation:
        return ""


def _purchase_redirect(*, status: str = "", error: str = "") -> RedirectResponse:
    """Back to the purchase manager with a status or an error *code* (never free text from the request)."""
    query = {"error": error} if error else {"status": status}
    return RedirectResponse(url=f"/admin/document-purchases?{urlencode(query)}", status_code=303)


def _purchase_audit(request: Request, **details: Any) -> None:
    """Facts for the activity log (picked up by ``@_audited``): who got access to what, and why."""
    current = getattr(request.state, "audit_params", None) or {}
    request.state.audit_params = {**current, **{k: v for k, v in details.items() if v not in (None, "")}}


@router.get("/admin/document-purchases", response_class=HTMLResponse)
async def web_admin_document_purchases(
    request: Request,
    q: str = "",
    status: str = "",
    error: str = "",
    state: str = "active",
    document_id: int | None = None,
    user=Depends(_require_admin),
):
    """List and manage the purchase records that grant document access."""
    from zoneinfo import ZoneInfo

    from .database import list_document_purchases

    state = state if state in {"active", "revoked", "all"} else "active"
    purchases = await list_document_purchases(q, status=state)
    rome = ZoneInfo("Europe/Rome")
    for purchase in purchases:
        for source_key, target_key in (("purchased_at", "purchased_at_local"), ("expires_at", "expires_at_local")):
            moment = purchase.get(source_key)
            purchase[target_key] = moment.astimezone(rome).strftime("%Y-%m-%dT%H:%M") if moment else ""
        revoked_at = purchase.get("revoked_at")
        purchase["revoked_at_local"] = revoked_at.astimezone(rome).strftime("%Y-%m-%d %H:%M") if revoked_at else ""
    known_users = sorted(
        {p["user_id"] for p in purchases} | set(getattr(request.app.state, "local_user_keys", []) or [])
    )
    from . import purchases_billing
    all_query_prices = await _query_product_prices() or {}
    query_prices = {
        path: cost for path, cost in all_query_prices.items() if path == "/visura" or path.startswith("/visura/")
    }

    purchase_link = ""
    if document_id:
        try:
            purchase_link = str(request.base_url).rstrip("/") + "/buy/" + purchases_billing.purchase_link_token(document_id)
        except RuntimeError:  # no SECRET_KEY: links cannot be signed
            purchase_link = ""
    return _get_theme(request).render(
        "admin/document_purchases.html",
        request,
        user=user,
        purchases=purchases,
        query=q,
        state=state,
        selected_document_id=document_id or "",
        status=status,
        error_message=PURCHASE_MESSAGES.get(error, PURCHASE_MESSAGES["failed"]) if error else "",
        known_users=known_users,
        purchase_link=purchase_link,
        link_days=purchases_billing.LINK_TTL_SECONDS // 86400,
        card_enabled=purchases_billing.billing_configured(),
        card_price=purchases_billing.document_price_label(),
        credits_enabled=purchases_billing.credits_enabled(),
        credit_cost=purchases_billing.credit_cost(),
        query_credits_enabled=purchases_billing.query_credits_enabled(bool(query_prices)),
        query_price_list=[
            {"endpoint": path, "credits": str(cost)}
            for path, cost in sorted(query_prices.items())
        ],
        wallet_enabled=purchases_billing.credit_grants_enabled(),
        credit_balance=_numeric_text(request.query_params.get("balance")),
    )


def _purchase_form_values(form) -> tuple[str, int, str]:
    note = str(form.get("note", "")).strip()
    if len(note) > 2000:
        from .database import PurchaseError

        raise PurchaseError("note_too_long")
    return _purchase_identity(str(form.get("user_id", ""))), _purchase_document_id(form.get("document_id")), note


def _purchase_expiry(form, purchased_at: datetime | None) -> datetime | None:
    from .database import PurchaseError

    raw = str(form.get("expires_at", "")).strip()
    if not raw:
        return None
    expires_at = _purchase_datetime(raw, allow_future=True, error="bad_expiry")
    if purchased_at is not None and expires_at <= purchased_at:
        raise PurchaseError("bad_expiry")
    return expires_at


@router.post("/admin/document-purchases/create")
@_audited("admin.document_purchase.create")
async def web_admin_document_purchase_create(request: Request, user=Depends(_require_admin)):
    from .database import PurchaseError, create_document_purchase

    form = await request.form()
    try:
        user_key, document_id, note = _purchase_form_values(form)
        _purchase_audit(request, user_id=user_key, document_id=document_id, note=note[:200])
        purchased_at = _purchase_datetime(str(form.get("purchased_at", "")))
        expires_at = _purchase_expiry(form, purchased_at)
        await create_document_purchase(
            user_key,
            document_id,
            purchased_at,
            _favorite_user_key(user) or _actor(user),
            note,
            source="admin_grant",
            expires_at=expires_at,
        )
    except PurchaseError as exc:
        _purchase_audit(request, error_code=exc.code)
        return _purchase_redirect(error=exc.code)
    return _purchase_redirect(status="created")


@router.post("/admin/document-purchases/update")
@_audited("admin.document_purchase.update")
async def web_admin_document_purchase_update(request: Request, user=Depends(_require_admin)):
    from .database import PurchaseError, update_document_purchase

    form = await request.form()
    try:
        old_user_id = _purchase_identity(str(form.get("old_user_id", "")))
        old_document_id = _purchase_document_id(form.get("old_document_id"))
        user_key, document_id, note = _purchase_form_values(form)
        _purchase_audit(
            request,
            user_id=user_key,
            document_id=document_id,
            old_user_id=old_user_id if old_user_id != user_key else None,
            old_document_id=old_document_id if old_document_id != document_id else None,
            note=note[:200],
        )
        posted = str(form.get("purchased_at", "")).strip()
        # The input has minute precision: when it is unchanged keep the stored instant (and its seconds).
        purchased_at = None if posted == str(form.get("purchased_at_orig", "")).strip() else _purchase_datetime(posted)
        expires_at = _purchase_expiry(form, purchased_at)
        await update_document_purchase(
            old_user_id, old_document_id, user_key, document_id, purchased_at, note, expires_at
        )
    except PurchaseError as exc:
        _purchase_audit(request, error_code=exc.code)
        return _purchase_redirect(error=exc.code)
    return _purchase_redirect(status="updated")


async def _revoke_purchase(request: Request) -> RedirectResponse:
    from .database import PurchaseError, revoke_document_purchase

    form = await request.form()
    try:
        user_key = _purchase_identity(str(form.get("user_id", "")))
        document_id = _purchase_document_id(form.get("document_id"))
        reason = str(form.get("reason", "")).strip()
        if len(reason) > 500:
            raise PurchaseError("reason_too_long")
        _purchase_audit(request, user_id=user_key, document_id=document_id, reason=reason[:200])
        actor = getattr(request.state, "audit_user", None)
        revoked = await revoke_document_purchase(user_key, document_id, _favorite_user_key(actor) or _actor(actor), reason)
    except PurchaseError as exc:
        _purchase_audit(request, error_code=exc.code)
        return _purchase_redirect(error=exc.code)
    return _purchase_redirect(status="deleted" if revoked else "not_found")


@router.post("/admin/document-purchases/revoke")
@_audited("admin.document_purchase.revoke")
async def web_admin_document_purchase_revoke(request: Request, user=Depends(_require_admin)):
    request.state.audit_user = user
    return await _revoke_purchase(request)


@router.post("/admin/document-purchases/delete")
@_audited("admin.document_purchase.revoke")
async def web_admin_document_purchase_delete(request: Request, user=Depends(_require_admin)):
    """Backward-compatible alias: access is revoked (soft), never physically deleted from the UI."""
    request.state.audit_user = user
    return await _revoke_purchase(request)


@router.get("/about", response_class=HTMLResponse)
async def web_about(request: Request):
    """About page (public)."""
    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("about.html", request, user=user)


@router.get("/privacy", response_class=HTMLResponse)
async def web_privacy(request: Request):
    """Privacy policy (public)."""
    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("legal_privacy.html", request, user=user)


_INFO_PAGES = {
    "terms": ("info_terms.html", "Termini di servizio", "fa-file-contract", True),
    "help": ("info_help.html", "Centro assistenza", "fa-circle-question", True),
    "contact": ("info_contact.html", "Contatti", "fa-envelope", True),
    "notifications": ("info_notifications.html", "Notifiche", "fa-bell", False),
}


async def _render_info_page(request: Request, key: str, user):
    template, heading, icon, _public = _INFO_PAGES[key]
    return _get_theme(request).render(template, request, user=user, page_heading=heading, page_icon=icon)


@router.get("/terms", response_class=HTMLResponse)
async def web_terms(request: Request):
    """Terms of service (public: the sign-in page links to it)."""
    return await _render_info_page(request, "terms", await _optional_user(request))


@router.get("/help", response_class=HTMLResponse)
async def web_help(request: Request):
    """Help centre (public)."""
    return await _render_info_page(request, "help", await _optional_user(request))


@router.get("/contact", response_class=HTMLResponse)
async def web_contact(request: Request):
    """Contact page (public)."""
    return await _render_info_page(request, "contact", await _optional_user(request))


@router.get("/notifications", response_class=HTMLResponse)
async def web_notifications(request: Request, user=Depends(_require_auth)):
    """Notifications inbox (target of the header user menu; empty until notifications exist)."""
    return await _render_info_page(request, "notifications", user)


@router.get("/guide", response_class=HTMLResponse)
async def web_guide(request: Request):
    """User guide for the SISTER portal."""
    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("guide.html", request, user=user)


@router.get("/cheatsheet", response_class=HTMLResponse)
async def web_cheatsheet(request: Request):
    """Quick-reference cheat sheet for SISTER."""
    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("cheatsheet.html", request, user=user)


@router.get("/glossary", response_class=HTMLResponse)
async def web_glossary(request: Request):
    """Glossary of document types and cadastral terms."""
    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("glossary.html", request, user=user)


@router.get("/valuation", response_class=HTMLResponse)
async def web_valuation(request: Request):
    """How properties are valued: statutory cadastral (IMU) value and the indicative OMI estimate."""
    from .valuation_docs import valuation_page_context

    theme = _get_theme(request)
    user = await _optional_user(request)
    return theme.render("valuation.html", request, user=user, **valuation_page_context())


# ---------------------------------------------------------------------------
# 3D building view (Google Photorealistic 3D Maps, the data behind Google Earth)
# ---------------------------------------------------------------------------

_GEOCODE_CACHE: dict[str, dict[str, Any] | None] = {}
_NOMINATIM_URL = os.getenv("SISTER_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search").strip()
_GEOCODE_LOCK = asyncio.Lock()
_GEOCODE_LAST_REQUEST_AT = 0.0


# Addresses are sent to a third party (Nominatim/OpenStreetMap) only on a cache miss, and the answer is kept on disk so a
# restart (uvicorn --reload restarts often) does not repeat it. ``SISTER_GEOCODING=off`` keeps the cache but never calls out.
_GEOCODING_ENABLED = os.getenv("SISTER_GEOCODING", "on").strip().lower() not in {"0", "false", "no", "off"}
_GEOCODE_CACHE_FILE = Path(os.getenv("SISTER_GEOCODE_CACHE", "data/geocode_cache.json"))
_geocode_cache_loaded = False


def _load_geocode_cache() -> None:
    global _geocode_cache_loaded
    if _geocode_cache_loaded:
        return
    _geocode_cache_loaded = True
    try:
        stored = json.loads(_GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(stored, dict):
        for key, value in stored.items():
            if isinstance(key, str) and (value is None or isinstance(value, dict)):
                _GEOCODE_CACHE.setdefault(key, value)


def _save_geocode_cache() -> None:
    try:
        _GEOCODE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GEOCODE_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_GEOCODE_CACHE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_GEOCODE_CACHE_FILE)
    except OSError as exc:  # a read-only data directory must not break the page
        logger.debug("Geocode cache not saved: %s", type(exc).__name__)


async def _geocode_address(query: str) -> dict[str, Any] | None:
    """Resolve an address with Nominatim, caching results and spacing requests at one per second."""
    global _GEOCODE_LAST_REQUEST_AT
    _load_geocode_cache()
    key = re.sub(r"\s+", " ", query).strip().casefold()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    if not _GEOCODING_ENABLED:
        return None
    import httpx

    async with _GEOCODE_LOCK:
        if key in _GEOCODE_CACHE:
            return _GEOCODE_CACHE[key]
        delay = 1.0 - (time.monotonic() - _GEOCODE_LAST_REQUEST_AT)
        if delay > 0:
            await asyncio.sleep(delay)
        _GEOCODE_LAST_REQUEST_AT = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "sister/1 (info@aecs4u.com)"}) as client:
                response = await client.get(
                    _NOMINATIM_URL, params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "it"}
                )
                response.raise_for_status()
                rows = response.json()
        except Exception as exc:
            logger.warning("Geocoding unavailable: %s", type(exc).__name__)
            return None  # not cached: a network error should not stick
        hit = None
        if rows:
            try:
                latitude = float(rows[0]["lat"])
                longitude = float(rows[0]["lon"])
            except (KeyError, TypeError, ValueError):
                rows = []
            else:
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    hit = {
                        "lat": latitude,
                        "lng": longitude,
                        "label": rows[0].get("display_name", query),
                        "kind": rows[0].get("type", ""),
                    }
        _GEOCODE_CACHE[key] = hit
        _save_geocode_cache()
        return hit


@router.get("/3d", response_class=HTMLResponse)
async def web_building_3d(
    request: Request,
    q: str = "",
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    user=Depends(_require_auth),
):
    """3D view of a building: Google Photorealistic 3D Maps centred on an address (or on explicit ``lat``/``lng``)."""
    theme = _get_theme(request)
    query = q.strip()[:200]
    place: dict[str, Any] | None = None
    if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
        place = {"lat": lat, "lng": lng, "label": query or f"{lat:.6f}, {lng:.6f}", "kind": "coordinate"}
    elif query:
        place = await _geocode_address(query)
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    earth_url = ""
    if place:
        # Opens the same view in Google Earth Web (cannot be embedded: it sends X-Frame-Options).
        earth_url = f"https://earth.google.com/web/@{place['lat']:.7f},{place['lng']:.7f},0a,300d,35y,0h,60t,0r"
    return theme.render(
        "building.html",
        request,
        user=user,
        query=query,
        place=place,
        maps_api_key=api_key,
        earth_url=earth_url,
    )


@router.get("/3d/kmz")
async def web_building_3d_kmz(
    lat: float,
    lng: float,
    radius: int = 80,
    name: str = "Edificio",
    user=Depends(_require_auth),
):
    """KMZ of the OpenStreetMap building volumes around a point (viewer on ``/3d`` and download)."""
    from fastapi import HTTPException
    from fastapi.responses import Response

    from .building_kmz import OverpassUnavailable, build_kmz, fetch_buildings

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="Coordinate non valide")
    radius = max(20, min(radius, 300))
    label = name.strip()[:120] or "Edificio"
    try:
        data = await fetch_buildings(lat, lng, radius)
    except OverpassUnavailable:
        raise HTTPException(status_code=503, detail="OpenStreetMap (Overpass) non disponibile, riprova tra poco")
    payload, count = build_kmz(data, lat, lng, label, radius=radius)
    if not count:
        raise HTTPException(status_code=404, detail="Nessun edificio trovato in OpenStreetMap vicino a questo punto")
    filename = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or "edificio"
    return Response(
        payload,
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f'attachment; filename="{filename}_3d.kmz"', "Cache-Control": "private, max-age=600"},
    )


# ---------------------------------------------------------------------------
# Document browser + structured viewers  (/documents/*)
# ---------------------------------------------------------------------------


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def _load_document_favorite_state(doc: dict, user, *, as_pdf: bool = False) -> bool:
    """Attach the signed-in user's saved state for a single document."""
    favorite_user = _favorite_user_key(user)
    enabled = bool(favorite_user) and not as_pdf
    doc["favorites_enabled"] = enabled
    doc["is_favorite"] = False
    if enabled and doc.get("id") is not None:
        from .database import get_document_favorite_ids

        doc["is_favorite"] = int(doc["id"]) in await get_document_favorite_ids(favorite_user)
    return enabled


async def _render_doc_from_db(
    doc: dict,
    request,
    theme,
    user,
    force_template: str | None = None,
    subject_fiscal_code: str | None = None,
    subject_route_prefix: str | None = None,
    subject_documents: list[dict] | None = None,
    subject_asset_documents: list[dict] | None = None,
    subject_portfolio: dict | None = None,
    subject_hub: dict | None = None,
    subject_archive: dict | None = None,
    subject_favorite_enabled: bool = False,
    subject_is_favorite: bool = False,
    as_pdf: bool = False,
):
    """Finalize a doc dict fetched from the DB and render the matching visura template.

    By default the template is chosen from the XML root element. Pass
    ``force_template`` (e.g. "result_detail" or "result_detail.html") to override
    the auto-selection — used by ``?template=`` on the document route to render the
    generic, exhaustive view of any document.
    """
    document_favorites_enabled = await _load_document_favorite_state(doc, user, as_pdf=as_pdf)

    doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
    if subject_asset_documents:
        doc["xml_parsed"] = _merge_subject_visura_asset_groups(
            [
                doc["xml_parsed"],
                *(_parse_xml_to_dict(source.get("xml_content", "")) for source in subject_asset_documents),
            ]
        )
    doc["view"] = build_visura_view(doc.get("xml_content", ""))
    doc.pop("xml_content", None)
    doc["intestati_rows"] = [
        {
            "Nominativo": r.get("Nominativo") or r.get("nominativo") or "-",
            "Codice Fiscale": r.get("CF") or r.get("CodiceFiscale") or "-",
            "Quota": (r.get("DirittiReali") or {}).get("Quota", "") if isinstance(r.get("DirittiReali"), dict) else "",
            "Diritto": (
                (r.get("DirittiReali") or {}).get("Descrizione", "") if isinstance(r.get("DirittiReali"), dict) else ""
            ),
            "Periodo": (
                (r.get("DirittiReali") or {}).get("FineDiritto", "") if isinstance(r.get("DirittiReali"), dict) else ""
            ),
        }
        for r in (doc.get("intestati") or [])
    ]
    doc["classamento_rows"] = [
        {
            "Zona Censuaria": r.get("ZonaCensuaria") or "-",
            "Categoria": r.get("Categoria") or "-",
            "Classe": r.get("Classe") or "-",
            "Rendita (EUR)": r.get("RenditaEuro") or "-",
        }
        for r in (doc.get("classamento") or [])
    ]
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
    xml_p = doc.get("xml_parsed") or {}

    map_lookup = await _google_maps_context(doc)
    if map_lookup is not None and subject_asset_documents:
        map_lookup["related_document_ids"] = [
            int(source["id"]) for source in subject_asset_documents if source.get("id") is not None
        ]
    doc["land_registry_map"] = await _land_registry_map_context(map_lookup)

    # Explicit override → generic exhaustive view via result_detail.html
    if force_template and force_template.replace(".html", "") == "result_detail":
        result = _doc_as_result(doc)
        return theme.render(
            "result_detail.html",
            request,
            user=user,
            result=result,
            request_id=str(doc.get("id") or doc.get("filename") or ""),
            document_favorites_enabled=document_favorites_enabled,
        )

    if any(
        key in xml_p
        for key in ("VisuraFabbricatiStorica", "VisuraFabbricatiAttuale", "VisuraFabbricatiSintetica", "VisuraFabbricati")
    ):
        land_map_kind = "fabbricati"
    elif any(
        key in xml_p
        for key in ("VisuraSoggettoAttuale", "VisuraSoggettoStorica", "VisuraSoggettoSintetica", "VisuraSoggetto")
    ):
        land_map_kind = "soggetto"
    elif any(
        key in xml_p
        for key in (
            "VisuraTerreniAttuale",
            "VisuraTerreniStorica",
            "VisuraTerreniSintetica",
            "VisuraTerrenoAttuale",
            "VisuraTerrenoStorica",
            "VisuraTerrenoSintetica",
            "VisuraTerreno",
        )
    ):
        land_map_kind = "terreni"
    else:
        land_map_kind = None
    if land_map_kind is None:
        result = _doc_as_result(doc)
        return theme.render(
            "result_detail.html",
            request,
            user=user,
            result=result,
            request_id=str(doc.get("id") or doc.get("filename") or ""),
            document_favorites_enabled=document_favorites_enabled,
        )
    land_registry_kpis = None
    land_registry_history = None
    if land_map_kind == "soggetto":
        from .land_registry_history import subject_land_registry_history

        # the groups only the elenco lists carry its ownership, so the valuation covers them too
        xml_shown = _subject_tree_with_elenco(xml_p, subject_hub)

        async def _no_hub_map():
            return None

        # The valuation, the ownership history and the hub map do not depend on each other: they used to run one after
        # the other (3-4 s each on a large subject), now they overlap.
        land_registry_kpis, land_registry_history, hub_map = await asyncio.gather(
            _subject_land_registry_calculation(
                xml_shown, map_lookup, user, scope_type="document", scope_id=doc.get("id") or doc.get("filename")
            ),
            subject_land_registry_history(xml_p, max_events=None) if as_pdf else subject_land_registry_history(xml_p),
            _hub_parcel_map(subject_hub) if (subject_hub and not as_pdf) else _no_hub_map(),
        )
    from .display import merge_identifiers, readable_owners, subject_for_theme

    # the lookups above need the visura's own tree; the page then lists the elenco immobili's other units too
    if land_map_kind != "soggetto":
        xml_shown = xml_p
    doc = {**doc, "xml_parsed": merge_identifiers(subject_for_theme(readable_owners(xml_shown)))}
    if as_pdf:
        # Subject reports also carry the archive and parcel evidence shown alongside the visura.
        subject_report = None
        pdf_lookup = map_lookup if land_map_kind == "soggetto" else None
        if land_map_kind == "soggetto":
            if map_lookup:
                try:
                    map_payload = await _parcel_map_payload(map_lookup)
                    pdf_lookup = {**map_lookup, "omi_zone_features": map_payload["omi_zone_features"]}
                except Exception as exc:  # noqa: BLE001 - the report remains useful without the optional OMI overlay.
                    logger.warning("OMI map overlay unavailable in PDF: %s", type(exc).__name__)
            if subject_fiscal_code:
                subject_report = {
                    "archive": subject_archive,
                    "subject_documents": subject_documents or [],
                    "subject_hub": subject_hub,
                    "subject_portfolio": subject_portfolio,
                    "land_registry_history": land_registry_history,
                }
                if subject_hub:
                    pdf_lookup = await _hub_parcel_map_for_pdf(subject_hub) or pdf_lookup
        return await _subject_pdf_response(
            {**doc, "xml_parsed": xml_shown}, land_registry_kpis, pdf_lookup,
            subject_fiscal_code or "",
            subject_report=subject_report,
        )
    if land_map_kind == "soggetto" and subject_hub:
        # every parcel of the elenco is on the map, not only the ones the visura lists
        doc["land_registry_map"] = hub_map or doc.get("land_registry_map")
    return theme.render(
        "catasto/entity.html",
        request,
        user=user,
        doc=doc,
        google_maps=map_lookup if land_map_kind == "fabbricati" else None,
        land_registry_map=doc.get("land_registry_map"),
        land_registry_kpis=land_registry_kpis,
        land_registry_history=land_registry_history,
        subject_fiscal_code=subject_fiscal_code,
        subject_route_prefix=subject_route_prefix,
        subject_documents=subject_documents,
        subject_portfolio=subject_portfolio,
        subject_hub=subject_hub,
        subject_archive=subject_archive,
        subject_favorite_enabled=subject_favorite_enabled,
        subject_is_favorite=subject_is_favorite,
        document_favorites_enabled=document_favorites_enabled,
        subject_document_id=doc.get("id") if subject_fiscal_code else None,
        manual_valuation_enabled=bool(_favorite_user_key(user)),
        **_land_map_layout(land_map_kind, theme),
    )


def _cadastral_map_lookup_key(item: dict, *, feature_type: str) -> str:
    """Build the same normalized location key the polygon query derives from the comune and unit columns."""
    from .provinces import province_code
    from .reference import belfiore_code, place_name

    fields = [
        place_name(item.get("municipality")),
        province_code(item.get("province")),
        belfiore_code(item.get("municipality_code")),
        item.get("section"),
        item.get("sheet"),
    ]
    if feature_type == "parcel":
        fields.append(item.get("parcel"))
    elif feature_type == "sheet":
        # Ignore cached outlines created by the old query, which unioned every numeric alias.
        fields.append("sheet-match-v2")
    return "\x1f".join(str(value or "").strip().upper() for value in fields)


async def _read_visura_cadastral_map_cache(document_id: int) -> list[tuple]:
    """Read cached polygons for one document from Sister's PostGIS view."""
    dsn = os.getenv("DATABASE_DSN", "").strip()
    if not dsn:
        return []
    try:
        import psycopg

        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        async with await psycopg.AsyncConnection.connect(
            dsn,
            connect_timeout=1,
            options="-c search_path=sister -c default_transaction_read_only=on -c statement_timeout=1500",
        ) as connection:
            cursor = await connection.execute(
                """SELECT feature_type,
                          concat_ws(chr(31), upper(btrim(coalesce(comune, ''))), upper(btrim(coalesce(province_code, ''))),
                                    upper(btrim(coalesce(comune_code, ''))), upper(btrim(coalesce(section, ''))),
                                    upper(btrim(coalesce(sheet, ''))),
                                    CASE WHEN feature_type = 'parcel' THEN upper(btrim(coalesce(parcel, ''))) END,
                                    CASE WHEN feature_type = 'sheet' THEN 'SHEET-MATCH-V2' END) AS lookup_key,
                          comune AS municipality, province_code AS province,
                          comune_code AS municipality_code, section, sheet, parcel, source_reference,
                          checked_at >= now() - interval '30 days' AS is_fresh,
                          checked_at IS NOT NULL AS is_checked,
                          ST_AsGeoJSON(geom, 6) AS geometry,
                          ST_X(ST_Centroid(geom)) AS longitude,
                          ST_Y(ST_Centroid(geom)) AS latitude
                   FROM sister.v_visura_cadastral_polygon
                   WHERE document_id = %s""",
                (document_id,),
            )
            return await cursor.fetchall()
    except Exception as exc:  # Cache is an optimization; the cadastral source remains the fallback.
        logger.debug("Sister cadastral polygon cache unavailable: %s", type(exc).__name__)
        return []


async def _write_visura_cadastral_map_cache(records: list[tuple]) -> None:
    """Persist matched and missing map lookups so later visits use Sister's indexed view."""
    if not records:
        return
    try:
        from .database import is_db_writable

        if not is_db_writable():
            return
        import psycopg
        from psycopg import sql

        dsn = os.getenv("DATABASE_DSN", "").strip()
        if not dsn:
            return
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        async with await psycopg.AsyncConnection.connect(
            dsn,
            connect_timeout=1,
            options="-c search_path=sister -c statement_timeout=2500",
        ) as connection:
            async with connection.cursor() as cursor:
                from .provinces import province_code
                from .reference import belfiore_code, place_name

                rows = [
                    {
                        "feature_type": feature_type,
                        "section": (section or "").strip().upper(),
                        "sheet": (sheet or "").strip(),
                        "parcel": (parcel or "").strip(),
                        "source_reference": source_reference,
                        "geom": geojson,
                        "name": place_name(municipality),
                        "prov": province_code(province),
                        "code": belfiore_code(municipality_code),
                    }
                    for (feature_type, _key, municipality, province, municipality_code, section, sheet, parcel,
                         source_reference, geojson) in records
                ]
                istat_schema = os.getenv("DATABASE_ISTAT_SCHEMA", "istat")
                municipalities = sql.SQL("{}.municipalities").format(sql.Identifier(istat_schema))
                await cursor.executemany(
                    sql.SQL("""WITH target AS (
                           SELECT numeric_code FROM {}
                           WHERE (%(code)s::text IS NOT NULL AND upper(btrim(cadastral_code)) = upper(btrim(%(code)s::text)))
                              OR (%(name)s::text IS NOT NULL
                                  AND upper(btrim(coalesce(italian_name, name))) = upper(btrim(%(name)s::text))
                                  AND (%(prov)s::text IS NULL OR upper(btrim(province_abbreviation)) = upper(btrim(%(prov)s::text))))
                           ORDER BY (upper(btrim(cadastral_code)) = upper(btrim(%(code)s::text))) DESC NULLS LAST, numeric_code
                           LIMIT 1
                       )
                       INSERT INTO sister.parcel_geometry_cache (
                           feature_type, comune_id, section, sheet, parcel, source_reference, geom, checked_at
                       )
                       SELECT %(feature_type)s, target.numeric_code, %(section)s, %(sheet)s, %(parcel)s, %(source_reference)s,
                              ST_SetSRID(ST_GeomFromGeoJSON((%(geom)s)::text), 4326), now()
                       FROM target
                       ON CONFLICT (feature_type, comune_id, section, sheet, parcel) DO UPDATE SET
                           source_reference = EXCLUDED.source_reference,
                           geom = COALESCE(EXCLUDED.geom, sister.parcel_geometry_cache.geom),
                           checked_at = now()""").format(municipalities),
                    rows,
                )
            await connection.commit()
    except Exception as exc:  # A cache write must never make the document page fail.
        logger.debug("Sister cadastral polygon cache write failed: %s: %s", type(exc).__name__, exc)


async def _read_subject_map_features_from_sister_views(
    connection, parcel_identifiers: list[dict], sheet_identifiers: list[dict]
) -> tuple[list[tuple], list[tuple]]:
    """Read the hub's requested keys from SISTER's live cadastral source views."""
    from .provinces import province_code, province_name

    def source_province_values(item: dict) -> list[str]:
        raw_province = str(item.get("province") or "").strip()
        code = province_code(raw_province) or raw_province
        name = province_name(code) or raw_province
        return sorted({value.strip().upper() for value in (raw_province, code, name) if value.strip()})

    max_keys_per_query = 10
    if len(parcel_identifiers) > max_keys_per_query or len(sheet_identifiers) > max_keys_per_query:
        parcel_rows = []
        sheet_rows = []
        for start in range(0, len(parcel_identifiers), max_keys_per_query):
            rows, _ = await _read_subject_map_features_from_sister_views(
                connection, parcel_identifiers[start : start + max_keys_per_query], []
            )
            parcel_rows.extend((int(row[0]) + start, *row[1:]) for row in rows)
        for start in range(0, len(sheet_identifiers), max_keys_per_query):
            _, rows = await _read_subject_map_features_from_sister_views(
                connection, [], sheet_identifiers[start : start + max_keys_per_query]
            )
            sheet_rows.extend((int(row[0]) + start, *row[1:]) for row in rows)
        return parcel_rows, sheet_rows

    parcel_branches = []
    parcel_params = []
    for index, item in enumerate(parcel_identifiers):
        sheet_values = {item["sheet"].upper()}
        parcel_values = {item["parcel"].upper()}
        if item["sheet"].isdigit():
            numeric_sheet = str(int(item["sheet"]))
            sheet_values.update(numeric_sheet.zfill(width) for width in (2, 3, 4))
            if int(numeric_sheet) >= 100 and int(numeric_sheet) % 100 == 0:
                alternate_sheet = str(int(numeric_sheet) // 100)
                sheet_values.add(alternate_sheet)
                sheet_values.update(alternate_sheet.zfill(width) for width in (2, 3, 4))
            sheet_values.add(numeric_sheet.zfill(4) + "00")
        if item["parcel"].isdigit():
            numeric_parcel = str(int(item["parcel"]))
            parcel_values.update(numeric_parcel.zfill(width) for width in (2, 3, 4, 5))

        section = item["section"].upper()
        full_prefix = f"{item['municipality_code']}{section}"
        first_prefix = f"{item['municipality_code']}{section[:1]}" if section else ""
        parcel_branches.append(
            """SELECT %s::integer AS parcel_index,
                      coalesce(p.national_cadastral_reference, p.canonical_reference) AS source_reference,
                      p.geom,
                      CASE
                          WHEN %s::text = '' THEN 0
                          WHEN left(upper(replace(coalesce(p.national_cadastral_reference,
                                                       p.canonical_reference), '_', '')),
                                     char_length(%s::text)) = upper(%s::text) THEN 2
                          WHEN %s::text <> ''
                               AND left(upper(replace(coalesce(p.national_cadastral_reference,
                                                             p.canonical_reference), '_', '')),
                                        char_length(%s::text)) = upper(%s::text) THEN 1
                          ELSE 0
                      END AS section_rank
               FROM sister.v_cadastral_source_parcel_geometry p
               WHERE ((%s::text <> ''
                       AND upper(btrim(p.municipality_code)) = upper(btrim(%s::text)))
                   OR (lower(btrim(p.municipality_name)) = lower(btrim(%s::text))
                       AND upper(btrim(p.province)) = ANY(%s::text[])
                       AND (%s::text = '' OR p.municipality_code IS NULL
                            OR btrim(p.municipality_code) = '')))
                 AND upper(btrim(p.sheet)) = ANY(%s::text[])
                 AND upper(btrim(p.parcel)) = ANY(%s::text[])"""
        )
        parcel_params.extend(
            [
                index,
                section,
                full_prefix,
                full_prefix,
                first_prefix,
                first_prefix,
                first_prefix,
                item["municipality_code"],
                item["municipality_code"],
                item["municipality"],
                source_province_values(item),
                item["municipality_code"],
                sorted(sheet_values),
                sorted(parcel_values),
            ]
        )

    parcel_rows = []
    if parcel_branches:
        query = """WITH matched AS (
                        """ + " UNION ALL ".join(parcel_branches) + """
                    ), ranked AS (
                        SELECT *, max(section_rank) OVER (PARTITION BY parcel_index) AS best_section_rank
                        FROM matched
                    )
                    SELECT parcel_index,
                           count(DISTINCT source_reference) AS reference_count,
                           min(ST_SRID(geom)) AS srid,
                           max(ST_SRID(geom)) AS max_srid,
                           ST_X(ST_Centroid(ST_Collect(geom))) AS longitude,
                           ST_Y(ST_Centroid(ST_Collect(geom))) AS latitude,
                           ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)), 6) AS geometry
                    FROM ranked
                    WHERE section_rank = best_section_rank
                    GROUP BY parcel_index"""
        cursor = await connection.execute(query, tuple(parcel_params))
        parcel_rows = await cursor.fetchall()

    sheet_candidates: dict[int, dict[str, int]] = {}
    for index, item in enumerate(sheet_identifiers):
        raw_sheet = item["sheet"].strip().upper()
        sheet_values = {raw_sheet: 3}
        if raw_sheet.isdigit():
            normalized_sheet = str(int(raw_sheet))
            sheet_values[normalized_sheet] = max(sheet_values.get(normalized_sheet, 0), 2)
            for width in (2, 3, 4):
                padded = normalized_sheet.zfill(width)
                sheet_values[padded] = max(sheet_values.get(padded, 0), 1)
            if int(normalized_sheet) >= 100 and int(normalized_sheet) % 100 == 0:
                alternate = str(int(normalized_sheet) // 100)
                sheet_values[alternate] = max(sheet_values.get(alternate, 0), 0)
                for width in (2, 3, 4):
                    padded = alternate.zfill(width)
                    sheet_values[padded] = max(sheet_values.get(padded, 0), 0)

        section = item["section"].upper()
        section_prefixes = []
        if section:
            section_prefixes.append((section, 2))
            if len(section) > 1:
                section_prefixes.append((section[:1], 1))
        section_prefixes.append(("", 0))
        reference_ranks: dict[str, int] = {}
        for sheet_value, sheet_rank in sheet_values.items():
            numeric_sheet = str(int(sheet_value)) if sheet_value.isdigit() else sheet_value
            padded_sheet = numeric_sheet.zfill(4) if numeric_sheet.isdigit() else numeric_sheet
            for section_prefix, section_rank in section_prefixes:
                for suffix, suffix_rank in (("00", 2), ("", 1)):
                    references = [(f"{item['municipality_code']}{section_prefix}{padded_sheet}{suffix}", 1)]
                    if section_prefix:
                        references.append((f"{item['municipality_code']}{section_prefix}_{padded_sheet}{suffix}", 0))
                    else:
                        references.append((f"{item['municipality_code']}_{padded_sheet}{suffix}", 0))
                    for reference, separator_rank in references:
                        rank = section_rank * 10000 + sheet_rank * 1000 + suffix_rank * 10 + separator_rank
                        reference_ranks[reference.upper()] = max(reference_ranks.get(reference.upper(), -1), rank)
        sheet_candidates[index] = reference_ranks

    sheet_branches = []
    sheet_params = []
    for index, item in enumerate(sheet_identifiers):
        reference_ranks = sheet_candidates.get(index) or {}
        if not reference_ranks:
            continue
        references = sorted(reference_ranks)
        case_sql = "CASE s.sheet_reference " + " ".join("WHEN %s THEN %s::integer" for _ in references)
        case_sql += " ELSE -1 END"
        sheet_branches.append(
            f"""SELECT %s::integer AS sheet_index,
                       s.sheet_reference,
                       s.geom,
                       {case_sql} AS reference_rank
                FROM sister.v_cadastral_source_sheet_geometry s
                WHERE s.id = ANY(%s::bigint[])
                  AND s.sheet_reference = ANY(%s::text[])
                  AND ((%s::text <> '' AND upper(btrim(s.municipality_code)) = upper(btrim(%s::text)))
                       OR (lower(btrim(s.municipality_name)) = lower(btrim(%s::text))
                           AND upper(btrim(s.province)) = ANY(%s::text[])
                           AND (%s::text = '' OR s.municipality_code IS NULL
                                OR btrim(s.municipality_code) = '')))"""
        )
        sheet_params.extend([index])
        for reference in references:
            sheet_params.extend([reference, reference_ranks[reference]])
        reference_ids = [
            int(hashlib.md5(f"cadastral-sheets:{reference}".encode("utf-8")).hexdigest()[:13], 16) + 1
            for reference in references
        ]
        sheet_params.extend(
            [
                reference_ids,
                references,
                item["municipality_code"],
                item["municipality_code"],
                item["municipality"],
                source_province_values(item),
                item["municipality_code"],
            ]
        )

    sheet_rows = []
    if sheet_branches:
        query = """WITH matched AS (
                        """ + " UNION ALL ".join(sheet_branches) + """
                    ), ranked AS (
                        SELECT *, max(reference_rank) OVER (PARTITION BY sheet_index) AS best_reference_rank
                        FROM matched
                    )
                    SELECT sheet_index,
                           count(DISTINCT sheet_reference) AS reference_count,
                           min(ST_SRID(geom)) AS srid,
                           max(ST_SRID(geom)) AS max_srid,
                           ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)), 6) AS geometry
                    FROM ranked
                    WHERE reference_rank = best_reference_rank
                    GROUP BY sheet_index"""
        cursor = await connection.execute(query, tuple(sheet_params))
        sheet_rows = await cursor.fetchall()

    return parcel_rows, sheet_rows


_MAP_REFRESH_INFLIGHT: set[int] = set()
_MAP_REFRESH_LAST: dict[int, float] = {}
_MAP_REFRESH_MIN_INTERVAL = float(os.getenv("SISTER_MAP_REFRESH_SECONDS", "600"))


def _schedule_map_refresh(doc: dict) -> None:
    """Re-run the cadastral lookup for ``doc`` in the background (one at a time per document, at most every 10 min).

    The page is served from the cache that already exists; the lookup used to run inside the request every time a single
    parcel was missing or unchecked, which made subject pages wait 5-10 s on the geometry source for the same answer.
    """
    document_id = doc.get("id")
    if document_id is None:
        return
    document_id = int(document_id)
    now = time.monotonic()
    if document_id in _MAP_REFRESH_INFLIGHT or now - _MAP_REFRESH_LAST.get(document_id, -1e9) < _MAP_REFRESH_MIN_INTERVAL:
        return
    _MAP_REFRESH_INFLIGHT.add(document_id)
    _MAP_REFRESH_LAST[document_id] = now

    async def _refresh() -> None:
        try:
            await _google_maps_context(doc, refresh_only=True)
        except Exception as exc:  # noqa: BLE001 - a refresh must never surface in a page
            logger.debug("Map refresh for document %s failed: %s", document_id, type(exc).__name__)
        finally:
            _MAP_REFRESH_INFLIGHT.discard(document_id)

    try:
        asyncio.get_running_loop().create_task(_refresh())
    except RuntimeError:  # no running loop (sync caller): skip, the next view schedules it
        _MAP_REFRESH_INFLIGHT.discard(document_id)


_ADJACENT_MEMO: dict[str, tuple[float, list]] = {}
_ADJACENT_INFLIGHT: set[str] = set()
_ADJACENT_UNAVAILABLE: set[str] = set()
_ADJACENT_TTL = float(os.getenv("SISTER_ADJACENT_PARCELS_SECONDS", "86400"))


def _adjacent_parcels_memo(reference: str, dsn: str, options: str, sql: str) -> list:
    """Adjacent-parcel rows already known for ``reference`` ([] when not yet); schedules the lookup when missing or stale."""
    entry = _ADJACENT_MEMO.get(reference)
    if entry and time.monotonic() - entry[0] < _ADJACENT_TTL:
        return entry[1]
    if reference not in _ADJACENT_INFLIGHT:
        _ADJACENT_INFLIGHT.add(reference)

        async def _fetch() -> None:
            import psycopg

            rows: list = []
            try:
                async with await psycopg.AsyncConnection.connect(
                    dsn, connect_timeout=2, options=options.replace("statement_timeout=2500", "statement_timeout=15000")
                ) as connection:
                    cursor = await connection.execute(sql, (reference, reference))
                    rows = await cursor.fetchall()
                    _ADJACENT_UNAVAILABLE.discard(reference)
                    _clear_optional_data_degradation("adjacent_parcels")
            except Exception as exc:  # noqa: BLE001 - optional data: remember the miss so it is not retried on every view
                logger.warning("Adjacent cadastral parcels unavailable: %s", type(exc).__name__)
                _ADJACENT_UNAVAILABLE.add(reference)
                _record_optional_data_degradation("adjacent_parcels")
            finally:
                _ADJACENT_MEMO[reference] = (time.monotonic(), rows)
                _ADJACENT_INFLIGHT.discard(reference)

        try:
            asyncio.get_running_loop().create_task(_fetch())
        except RuntimeError:
            _ADJACENT_INFLIGHT.discard(reference)
    return entry[1] if entry else []


_MAP_CONTEXT_CACHE: dict[int, tuple[float, dict]] = {}
_MAP_CONTEXT_TTL = float(os.getenv("SISTER_MAP_CONTEXT_SECONDS", "600"))


async def _google_maps_context(doc: dict, *, refresh_only: bool = False) -> dict[str, Any] | None:
    """``_google_maps_context_uncached`` with a short in-memory memo per document.

    A property visura has no persistent polygon cache (only subject visure do), so every view of the same document asked the
    geometry source again (~5 s). The answer for one document does not change within minutes: reuse it for ``SISTER_MAP_CONTEXT_SECONDS``.
    """
    import copy

    document_id = doc.get("id")
    if refresh_only or document_id is None or _MAP_CONTEXT_TTL <= 0:
        return await _google_maps_context_uncached(doc, refresh_only=refresh_only)
    cached = _MAP_CONTEXT_CACHE.get(int(document_id))
    if cached and time.monotonic() - cached[0] < _MAP_CONTEXT_TTL:
        return copy.deepcopy(cached[1])
    context = await _google_maps_context_uncached(doc)
    if isinstance(context, dict) and context.get("lookup_status") in {"matched", "not_found"}:
        if len(_MAP_CONTEXT_CACHE) > 500:
            _MAP_CONTEXT_CACHE.clear()
        _MAP_CONTEXT_CACHE[int(document_id)] = (time.monotonic(), copy.deepcopy(context))
    return context


async def _google_maps_context_uncached(doc: dict, *, refresh_only: bool = False) -> dict[str, Any] | None:
    """Build verified parcel coordinates plus Google Maps and Street View links.

    ``refresh_only`` is the background re-check (see ``_schedule_map_refresh``): it skips the cache shortcuts, queries the
    cadastral source and rewrites the cache; its context is discarded.
    """
    view = doc.get("view") or {}
    units = view.get("units") or []
    unit = units[0] if units and isinstance(units[0], dict) else {}
    identifier = unit.get("identifier") or {}
    xml_parsed = doc.get("xml_parsed") or {}
    xml_body = next(
        (
            value for key, value in xml_parsed.items()
            if str(key).startswith("Visura") and isinstance(value, dict)
        ),
        {},
    ) if isinstance(xml_parsed, dict) else {}
    request = view.get("request") or xml_body.get("DatiRichiesta") or {}
    request_identifier = request.get("identifier") or {}

    subject_keys = (
        "VisuraSoggettoAttuale", "VisuraSoggettoStorica", "VisuraSoggettoSintetica", "VisuraSoggetto",
    )
    subject_body = next(
        (xml_parsed.get(key) for key in subject_keys if isinstance(xml_parsed.get(key), dict)),
        {},
    ) if isinstance(xml_parsed, dict) else {}
    is_subject_visura = bool(subject_body)

    def as_items(value):
        if isinstance(value, list):
            return [item for child in value for item in as_items(child)]
        return [value] if isinstance(value, dict) else []

    # Keep this source in sync with the two tables rendered by the subject
    # visura template: only current Fabbricati/Terreni rows are shown there.
    subject_table_identifiers = []
    for group in as_items(subject_body.get("GruppoUnitaImmobiliari")):
        if not isinstance(group, dict):
            continue
        for row_key in ("ImmobileFabbricatiS", "ImmobileTerreniS"):
            for property_row in as_items(group.get(row_key)):
                if not isinstance(property_row, dict):
                    continue
                dati = property_row.get("DatiIdentificativi") or {}
                identifiers = as_items(dati.get("IdentificativoDefinitivo")) if isinstance(dati, dict) else []
                for table_identifier in identifiers:
                    item = dict(table_identifier)
                    classamento = property_row.get("DatiClassamentoF") or {}
                    if isinstance(classamento, dict):
                        item["Categoria"] = classamento.get("Categoria") or ""
                    if not item.get("CodiceComune") and group.get("CodiceComune"):
                        item["CodiceComune"] = group["CodiceComune"]
                    if not item.get("Comune") and group.get("Descrizione"):
                        item["Comune"] = re.sub(r"\s*\([^)]*\)\s*$", "", str(group["Descrizione"])).strip()
                    if not item.get("Provincia") and group.get("Provincia"):
                        item["Provincia"] = group["Provincia"]
                    if not item.get("Provincia") and request.get("Provincia"):
                        item["Provincia"] = request["Provincia"]
                    subject_table_identifiers.append(item)
    if is_subject_visura and not subject_table_identifiers:
        return None

    def all_xml_parcels(value):
        found = []
        if isinstance(value, list):
            for item in value:
                found.extend(all_xml_parcels(item))
        elif isinstance(value, dict):
            for key in ("IdentificativoDefinitivo", "IdentificativoDefinitivoRiferimento"):
                candidate = value.get(key)
                if isinstance(candidate, dict) and candidate.get("Foglio") and (
                    candidate.get("ParticellaNum") or candidate.get("Particella")
                ):
                    found.append(candidate)
            for item in value.values():
                found.extend(all_xml_parcels(item))
        return found

    xml_identifiers = all_xml_parcels(xml_body)
    xml_identifier = xml_identifiers[0] if xml_identifiers else {}
    primary_xml_identifier = (
        subject_table_identifiers[0] if is_subject_visura and subject_table_identifiers
        else xml_identifier
    )
    address = str(unit.get("address") or doc.get("indirizzo") or "").strip()
    address = re.sub(r"\s+n\.?\s*SNC\b", "", address, flags=re.IGNORECASE)
    address = re.sub(r"\s+Piano\s+[A-Z0-9]+\s*$", "", address, flags=re.IGNORECASE)
    address = re.sub(r"\s+", " ", address).strip(" ,")
    municipality = str(
        identifier.get("municipality") or primary_xml_identifier.get("Comune")
        or doc.get("comune") or request.get("Comune") or ""
    ).strip()
    province = str(
        identifier.get("province") or primary_xml_identifier.get("Provincia")
        or doc.get("provincia") or request.get("Provincia") or ""
    ).strip()
    sheet = str(
        identifier.get("sheet")
        or (primary_xml_identifier.get("Foglio") if is_subject_visura else "")
        or doc.get("foglio") or request_identifier.get("sheet") or request.get("Foglio")
        or primary_xml_identifier.get("Foglio") or ""
    ).strip()
    parcel = str(
        identifier.get("parcel")
        or (
            (primary_xml_identifier.get("ParticellaNum") or primary_xml_identifier.get("Particella"))
            if is_subject_visura else ""
        )
        or doc.get("particella")
        or request_identifier.get("parcel")
        or request.get("ParticellaNum")
        or request.get("Particella")
        or primary_xml_identifier.get("ParticellaNum")
        or primary_xml_identifier.get("Particella")
        or ""
    ).strip()
    subunit = str(identifier.get("subunit") or doc.get("subalterno") or "").strip()
    section = str(
        identifier.get("urban_section")
        or identifier.get("census_section")
        or primary_xml_identifier.get("SezUrbana")
        or primary_xml_identifier.get("SezCensuaria")
        or doc.get("sezione_urbana")
        or ""
    ).strip()
    municipality_code = str(
        identifier.get("municipality_code")
        or primary_xml_identifier.get("CodiceComune")
        or request_identifier.get("municipality_code")
        or doc.get("codice_comune")
        or request.get("CodiceComune")
        or ""
    ).strip().upper()

    def parcel_field(identifier: dict, *names: str) -> str:
        return str(next((identifier.get(name) for name in names if identifier.get(name)), "")).strip()

    unit_identifiers = []
    for current_unit in units:
        if not isinstance(current_unit, dict):
            continue
        if isinstance(current_unit.get("identifier"), dict):
            classification = current_unit.get("classification") or {}
            unit_identifiers.append({
                **current_unit["identifier"],
                "category": classification.get("category", "") if isinstance(classification, dict) else "",
            })
        classification = current_unit.get("classification") or {}
        category = classification.get("category", "") if isinstance(classification, dict) else ""
        unit_identifiers.extend(
            {**related, "category": category}
            for related in current_unit.get("related", []) if isinstance(related, dict)
        )
    if is_subject_visura:
        candidates = subject_table_identifiers
    else:
        candidates = [*unit_identifiers, request_identifier]
        if not unit_identifiers:
            candidates.extend(xml_identifiers)
    parcel_identifiers = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_municipality = parcel_field(candidate, "municipality", "Comune") or municipality
        candidate_province = parcel_field(candidate, "province", "Provincia") or province
        candidate_code = parcel_field(candidate, "municipality_code", "CodiceComune") or municipality_code
        candidate_section = parcel_field(
            candidate, "urban_section", "SezUrbana", "census_section", "SezCensuaria"
        ) or section
        candidate_sheet = parcel_field(candidate, "sheet", "Foglio")
        candidate_parcel = parcel_field(candidate, "parcel", "ParticellaNum", "Particella")
        candidate_subunit = parcel_field(candidate, "subunit", "Subalterno")
        if candidate_subunit in {"-", "—"}:
            candidate_subunit = ""
        candidate_category = parcel_field(candidate, "category", "Categoria")
        if not (candidate_municipality and candidate_province and candidate_sheet and candidate_parcel):
            continue
        key = (
            candidate_municipality.casefold(), candidate_province.upper(), candidate_code.upper(),
            candidate_section.upper(), candidate_sheet.upper(), candidate_parcel.upper(),
        )
        existing_item = next((item for item in parcel_identifiers if item["key"] == key), None)
        if existing_item:
            if candidate_subunit and all(
                candidate_subunit.casefold() != value.casefold()
                for value in existing_item["subunits"]
            ):
                existing_item["subunits"].append(candidate_subunit)
            continue
        parcel_identifiers.append({
            "key": key,
            "municipality": candidate_municipality,
            "province": candidate_province,
            "municipality_code": candidate_code.upper(),
            "section": candidate_section.upper(),
            "sheet": candidate_sheet,
            "parcel": candidate_parcel,
            "subunits": [candidate_subunit] if candidate_subunit else [],
            "category": candidate_category,
            "reference": parcel_field(candidate, "reference") or " · ".join(
                part for part in (
                    f"{candidate_municipality} ({candidate_code.upper()})" if candidate_code else candidate_municipality,
                    f"FG {candidate_sheet}", f"PT {candidate_parcel}",
                ) if part
            ),
        })

    selected_key = (
        municipality.casefold(), province.upper(), municipality_code.upper(), section.upper(), sheet.upper(), parcel.upper(),
    )
    if not is_subject_visura and not parcel_identifiers and municipality and province and sheet and parcel:
        parcel_identifiers.append({
            "key": selected_key,
            "municipality": municipality,
            "province": province,
            "municipality_code": municipality_code,
            "section": section.upper(),
            "sheet": sheet,
            "parcel": parcel,
            "reference": f"FG {sheet} · PT {parcel}",
        })
    selected_parcel_reference = next(
        (item["reference"] for item in parcel_identifiers if item["key"] == selected_key),
        f"FG {sheet} · PT {parcel}",
    )
    location_parts = [part for part in (address, municipality, province) if part]
    if not location_parts:
        return None
    location = ", ".join((*location_parts, "Italia"))

    def search_url(query: str) -> str:
        return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": query})

    def map_embed_url(query: str) -> str:
        return "https://maps.google.com/maps?" + urlencode(
            {"q": query, "z": 18, "output": "embed"}
        )

    def street_view_embed_url(point: str) -> str:
        return "https://maps.google.com/maps?" + urlencode(
            {
                "layer": "c",
                "cbll": point,
                "cbp": "11,0,0,0,0",
                "output": "svembed",
            }
        )

    def parcel_map_reference(item: dict[str, str]) -> dict[str, str]:
        return {
            "reference": item["reference"],
            "municipality": item["municipality"],
            "municipality_code": item["municipality_code"],
            "province": item["province"],
            "section": item["section"],
            "sheet": item["sheet"],
            "parcel": item["parcel"],
            "category": item.get("category", ""),
        }

    context: dict[str, Any] = {
        "document_id": doc.get("id"),
        "location": location,
        "municipality": municipality,
        "province": province,
        "sheet": sheet,
        "parcel": parcel,
        "map_url": search_url(location),
        "map_embed_url": map_embed_url(location),
        # Without a verified coordinate Google can only search the address; the
        # user can select Street View where coverage exists.
        "street_view_url": search_url(location) if address else "",
        "street_view_embed_url": "",
        "centroid": None,
        "lookup_status": "not_configured",
        "lookup_message": "",
        "related_subunits": [],
        "adjacent_parcels": [],
        "adjacent_lookup_status": "not_requested",
        "parcel_markers": [],
        "parcel_features": [],
        "sheet_features": [],
        "listed_only": is_subject_visura,
        "sheet_count": 0,
        "mapped_sheet_count": 0,
        "unmapped_parcels": [],
        "selected_parcel_reference": selected_parcel_reference,
        "parcel_count": len(parcel_identifiers),
    }
    context["unmapped_parcels"] = [item["reference"] for item in parcel_identifiers]
    context["unmapped_parcel_items"] = [parcel_map_reference(item) for item in parcel_identifiers]

    sheet_identifiers = []
    if is_subject_visura:
        seen_sheet_keys = set()
        for item in parcel_identifiers:
            key = (
                item["municipality"].casefold(), item["province"].upper(),
                item["municipality_code"].upper(), item["section"].upper(), item["sheet"].upper(),
            )
            if key not in seen_sheet_keys:
                seen_sheet_keys.add(key)
                sheet_identifiers.append(item)

    def use_cached_map_features(cached_rows: list[tuple], *, require_fresh: bool) -> bool:
        """Apply complete recent cache data, or any checked data as a DB outage fallback."""
        if not is_subject_visura:
            return False
        cached_by_key = {(row[0], row[1]): row for row in cached_rows}
        expected = {
            ("parcel", _cadastral_map_lookup_key(item, feature_type="parcel"))
            for item in parcel_identifiers
        }
        expected.update(
            ("sheet", _cadastral_map_lookup_key(item, feature_type="sheet"))
            for item in sheet_identifiers
        )
        if require_fresh:
            if not expected or not all(
                pair in cached_by_key and bool(cached_by_key[pair][9]) for pair in expected
            ):
                return False
        elif not any(
            pair in cached_by_key and bool(cached_by_key[pair][10]) for pair in expected
        ):
            return False

        import json

        matched_indices = set()
        for index, item in enumerate(parcel_identifiers):
            key = ("parcel", _cadastral_map_lookup_key(item, feature_type="parcel"))
            row = cached_by_key.get(key)
            if not row or not row[10] or not row[11]:
                continue
            try:
                geometry = json.loads(row[11]) if isinstance(row[11], str) else row[11]
                if not isinstance(geometry, dict):
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            matched_indices.add(index)
            context["parcel_features"].append({
                "type": "Feature",
                "properties": {
                    "reference": item["reference"],
                    "sheet": item["sheet"],
                    "parcel": item["parcel"],
                    "subunits": item.get("subunits", []),
                    "section": item["section"],
                    "municipality": item["municipality"],
                    "municipality_code": item["municipality_code"],
                    "province": item["province"],
                    "category": item.get("category", ""),
                },
                "geometry": geometry,
            })
            try:
                longitude, latitude = float(row[12]), float(row[13])
            except (TypeError, ValueError):
                continue
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                continue
            context["parcel_markers"].append({
                "latitude": latitude,
                "longitude": longitude,
                "reference": item["reference"],
                "municipality": item["municipality"],
                "municipality_code": item["municipality_code"],
                "province": item["province"],
                "section": item["section"],
                "sheet": item["sheet"],
                "parcel": item["parcel"],
                "subunits": item.get("subunits", []),
                "category": item.get("category", ""),
                "selected": item["key"] == selected_key,
            })

        context["sheet_count"] = len(sheet_identifiers)
        for item in sheet_identifiers:
            row = cached_by_key.get(("sheet", _cadastral_map_lookup_key(item, feature_type="sheet")))
            if not row or not row[10] or not row[11]:
                continue
            try:
                geometry = json.loads(row[11]) if isinstance(row[11], str) else row[11]
                if not isinstance(geometry, dict):
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            context["sheet_features"].append({
                "type": "Feature",
                "properties": {
                    "reference": (
                        f"{item['municipality']} ({item['municipality_code']}) · "
                        + (f"Sezione {item['section']} · " if item["section"] else "")
                        + f"Foglio {item['sheet']}"
                    ),
                    "sheet": item["sheet"],
                    "section": item["section"],
                    "municipality": item["municipality"],
                    "municipality_code": item["municipality_code"],
                    "province": item["province"],
                },
                "geometry": geometry,
            })

        context["mapped_sheet_count"] = len(context["sheet_features"])
        context["unmapped_parcels"] = [
            item["reference"] for index, item in enumerate(parcel_identifiers) if index not in matched_indices
        ]
        context["unmapped_parcel_items"] = [
            parcel_map_reference(item)
            for index, item in enumerate(parcel_identifiers) if index not in matched_indices
        ]
        context["mapped_parcel_count"] = len(context["parcel_markers"])
        if context["parcel_markers"] and not any(marker["selected"] for marker in context["parcel_markers"]):
            context["parcel_markers"][0]["selected"] = True
        context["lookup_status"] = "matched" if context["parcel_markers"] else "not_found"
        if context["parcel_markers"]:
            context["lookup_message"] = (
                f"Trovate {context['mapped_parcel_count']} particelle tra quelle elencate nelle tabelle della visura."
            )
        else:
            context["lookup_message"] = (
                "Nessuna geometria particellare trovata per i riferimenti elencati nelle tabelle della visura."
            )
        if not require_fresh:
            context["lookup_message"] = (
                "Cartografia catastale non raggiungibile; sono mostrate le geometrie memorizzate in Sister. "
                + context["lookup_message"]
            )
        return True

    if municipality and province and sheet and parcel:
        try:
            siblings = await get_cadastral_sibling_visuras(
                province=province,
                municipality=municipality,
                sheet=sheet,
                parcel=parcel,
                subunit=subunit,
                section=section,
            )
            context["related_subunits"] = [
                {
                    **sibling,
                    "url": f"/documents/{sibling['document_id']}",
                }
                for sibling in siblings
            ]
        except Exception as exc:
            logger.warning("Sibling subaltern lookup unavailable: %s", type(exc).__name__)

    document_id = doc.get("id")
    cached_map_rows = []
    if is_subject_visura and document_id is not None:
        try:
            cached_map_rows = await _read_visura_cadastral_map_cache(int(document_id))
        except (TypeError, ValueError):
            cached_map_rows = []
        if not refresh_only:
            if use_cached_map_features(cached_map_rows, require_fresh=True):
                return context
            # Some parcels were never confirmed (or are older than 30 days) but earlier answers exist: show those now and
            # complete them in the background instead of making the visitor wait for the geometry source.
            if cached_map_rows and use_cached_map_features(cached_map_rows, require_fresh=False):
                _schedule_map_refresh(doc)
                return context

    # Resolve hub keys in the cadastral database directly: its indexed regional
    # views serve parcel lookups quickly. The FDW views in SISTER remain a
    # fallback for installations without a direct cadastral connection.
    direct_cadastral_dsn = os.getenv("CADASTRAL_POSTGRES_DSN", "").strip()
    use_sister_cadastral_views = (
        is_subject_visura and document_id is None and not direct_cadastral_dsn
    )
    dsn = direct_cadastral_dsn or os.getenv("DATABASE_DSN", "").strip()
    if not dsn or not (municipality and province and sheet and parcel):
        if use_cached_map_features(cached_map_rows, require_fresh=False):
            return context
        return context

    sheet_values = {sheet.upper()}
    if sheet.isdigit():
        normalized_sheet = str(int(sheet))
        sheet_values.update(normalized_sheet.zfill(width) for width in (2, 3, 4))
        if int(normalized_sheet) >= 100 and int(normalized_sheet) % 100 == 0:
            normalized_sheet = str(int(normalized_sheet) // 100)
            sheet_values.add(normalized_sheet)
            sheet_values.update(normalized_sheet.zfill(width) for width in (2, 3, 4))
        # AdE regional map extracts commonly store a four-character sheet
        # followed by the two-zero precision suffix.
        sheet_values.add(normalized_sheet.zfill(4) + "00")
    parcel_values = {parcel.upper()}
    if parcel.isdigit():
        normalized_parcel = str(int(parcel))
        parcel_values.update(normalized_parcel.zfill(width) for width in (2, 3, 4, 5))

    # The regional union view has expression indexes on municipality, province,
    # sheet and parcel. Keeping those expressions intact avoids scanning the
    # full cadastral extracts.
    sql = """
        SELECT national_cadastral_reference,
               min(municipality_code) AS municipality_code,
               min(municipality_name) AS municipality_name,
               min(province) AS province,
               min(ST_SRID(geom)) AS srid,
               max(ST_SRID(geom)) AS max_srid,
               ST_X(ST_Centroid(ST_Collect(geom))) AS longitude,
               ST_Y(ST_Centroid(ST_Collect(geom))) AS latitude
        FROM spatial.cadastral_parcel
        WHERE lower(btrim(municipality_name)) = lower(btrim(%s))
          AND upper(btrim(province)) = upper(btrim(%s))
          AND upper(btrim(sheet)) = ANY(%s)
          AND upper(btrim(parcel)) = ANY(%s)
          AND (%s = '' OR upper(btrim(municipality_code)) = %s)
        GROUP BY national_cadastral_reference
        LIMIT 2
    """
    adjacent_sql = """
        WITH target AS (
            SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
            FROM spatial.cadastral_parcel
            WHERE national_cadastral_reference = %s
            GROUP BY national_cadastral_reference
        )
        SELECT COALESCE(n.national_cadastral_reference, n.canonical_reference) AS cadastral_reference,
               min(n.municipality_code) AS municipality_code,
               min(n.municipality_name) AS municipality_name,
               min(n.province) AS province,
               min(n.sheet) AS sheet,
               min(n.parcel) AS parcel,
               min(ST_SRID(n.geom)) AS srid,
               max(ST_SRID(n.geom)) AS max_srid,
               ST_X(ST_Centroid(ST_Collect(n.geom))) AS longitude,
               ST_Y(ST_Centroid(ST_Collect(n.geom))) AS latitude
        FROM spatial.cadastral_parcel AS n
        CROSS JOIN target AS t
        WHERE n.geom && t.geom
          AND ST_Touches(n.geom, t.geom)
          AND COALESCE(n.national_cadastral_reference, n.canonical_reference) IS DISTINCT FROM %s
        GROUP BY COALESCE(n.national_cadastral_reference, n.canonical_reference)
        ORDER BY min(n.municipality_name), min(n.sheet), min(n.parcel)
        LIMIT 25
    """
    marker_sql = """
        WITH wanted AS (
            SELECT * FROM unnest(%s::int[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[])
                AS w(parcel_index, municipality, province, municipality_code, section, sheet, parcel)
        ), matched AS (
            SELECT w.parcel_index,
                   w.section,
                   p.national_cadastral_reference,
                   p.canonical_reference,
                   p.geom,
                   CASE
                       WHEN w.section = '' THEN 0
                       WHEN left(
                           upper(replace(COALESCE(p.national_cadastral_reference, p.canonical_reference), '_', '')),
                           char_length(w.municipality_code || w.section)
                       ) = upper(w.municipality_code || w.section) THEN 2
                       WHEN char_length(w.section) > 1
                           AND left(
                               upper(replace(COALESCE(p.national_cadastral_reference, p.canonical_reference), '_', '')),
                               char_length(w.municipality_code || left(w.section, 1))
                           ) = upper(w.municipality_code || left(w.section, 1)) THEN 1
                       ELSE 0
                   END AS section_rank
            FROM wanted AS w
            JOIN spatial.cadastral_parcel AS p
              ON lower(btrim(p.municipality_name)) = lower(btrim(w.municipality))
             AND upper(btrim(p.province)) = upper(btrim(w.province))
             AND upper(btrim(p.sheet)) = w.sheet
             AND upper(btrim(p.parcel)) = w.parcel
        ), ranked AS (
            SELECT *, max(section_rank) OVER (PARTITION BY parcel_index) AS best_section_rank
            FROM matched
        )
        SELECT r.parcel_index,
               count(DISTINCT COALESCE(r.national_cadastral_reference, r.canonical_reference)) AS reference_count,
               min(ST_SRID(r.geom)) AS srid,
               max(ST_SRID(r.geom)) AS max_srid,
               ST_X(ST_Centroid(ST_Collect(r.geom))) AS longitude,
               ST_Y(ST_Centroid(ST_Collect(r.geom))) AS latitude,
               ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(r.geom)), 6) AS geometry
        FROM ranked AS r
        WHERE r.section_rank = r.best_section_rank
        GROUP BY r.parcel_index
    """
    exact_marker_sql = """
        WITH wanted AS (
            SELECT * FROM unnest(%s::int[], %s::text[], %s::int[])
                AS w(parcel_index, national_reference, section_rank)
        ), matched AS (
            SELECT w.parcel_index, w.section_rank, p.national_cadastral_reference,
                   p.canonical_reference, p.geom
            FROM wanted AS w
            JOIN spatial.cadastral_parcel AS p
              ON p.id = (('x' || substr(md5('cadastral-parcels:' || w.national_reference), 1, 13))::bit(52)::bigint + 1)
             AND upper(btrim(COALESCE(p.national_cadastral_reference, p.canonical_reference))) = w.national_reference
        ), ranked AS (
            SELECT *, max(section_rank) OVER (PARTITION BY parcel_index) AS best_section_rank
            FROM matched
        )
        SELECT parcel_index,
               count(DISTINCT COALESCE(national_cadastral_reference, canonical_reference)) AS reference_count,
               min(ST_SRID(geom)) AS srid,
               max(ST_SRID(geom)) AS max_srid,
               ST_X(ST_Centroid(ST_Collect(geom))) AS longitude,
               ST_Y(ST_Centroid(ST_Collect(geom))) AS latitude,
               ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)), 6) AS geometry
        FROM ranked
        WHERE section_rank = best_section_rank
        GROUP BY parcel_index
    """
    sheet_sql = """
        WITH wanted AS (
            SELECT * FROM unnest(%s::int[], %s::text[], %s::text[], %s::text[], %s::text[])
                AS w(sheet_index, municipality, province, municipality_code, section)
        ), wanted_refs AS (
            SELECT * FROM unnest(%s::int[], %s::text[], %s::int[])
                AS r(sheet_index, sheet_reference, reference_rank)
        ), matched AS (
            SELECT w.sheet_index, r.reference_rank, s.sheet_reference, s.geom
            FROM wanted AS w
            JOIN wanted_refs AS r ON r.sheet_index = w.sheet_index
            JOIN spatial.cadastral_sheet AS s
              ON s.id = (('x' || substr(md5('cadastral-sheets:' || r.sheet_reference), 1, 13))::bit(52)::bigint + 1)
             AND s.sheet_reference = r.sheet_reference
             AND (
                    (w.municipality_code <> '' AND upper(btrim(s.municipality_code)) = w.municipality_code)
                    OR (
                        lower(btrim(s.municipality_name)) = lower(btrim(w.municipality))
                        AND upper(btrim(s.province)) = upper(btrim(w.province))
                        AND (w.municipality_code = '' OR s.municipality_code IS NULL OR btrim(s.municipality_code) = '')
                    )
                 )
        ), ranked AS (
            SELECT *, max(reference_rank) OVER (PARTITION BY sheet_index) AS best_reference_rank
            FROM matched
        )
        SELECT sheet_index,
               count(DISTINCT sheet_reference) AS reference_count,
               min(ST_SRID(geom)) AS srid,
               max(ST_SRID(geom)) AS max_srid,
               ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)), 6) AS geometry
        FROM ranked
        WHERE reference_rank = best_reference_rank
        GROUP BY sheet_index
    """
    rows = []
    adjacent_rows = []
    marker_rows = []
    sheet_rows = []
    # A query that raised (statement timeout under load) says nothing about the parcel or sheet: such results must
    # not be stored as "checked, no geometry", which the cache treats as a fresh answer for 30 days.
    parcel_lookup_failed = False
    sheet_lookup_failed = False
    try:
        import psycopg

        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        connection_options = "-c default_transaction_read_only=on -c statement_timeout=2500"
        if use_sister_cadastral_views:
            connection_options += " -c search_path=sister,public"
        async with await psycopg.AsyncConnection.connect(
            dsn,
            connect_timeout=2,
            options=connection_options,
        ) as connection:
            if use_sister_cadastral_views:
                try:
                    marker_rows, sheet_rows = await _read_subject_map_features_from_sister_views(
                        connection, parcel_identifiers, sheet_identifiers
                    )
                except Exception as exc:
                    parcel_lookup_failed = True
                    sheet_lookup_failed = True
                    logger.warning("SISTER cadastral geometry views unavailable: %s: %s", type(exc).__name__, exc)
            if not is_subject_visura:
                cursor = await connection.execute(
                    sql,
                    (
                        municipality,
                        province,
                        sorted(sheet_values),
                        sorted(parcel_values),
                        municipality_code,
                        municipality_code,
                    ),
                )
                rows = await cursor.fetchall()
            if parcel_identifiers and not use_sister_cadastral_views:
                wanted_rows = []
                exact_reference_wanted = []
                for index, item in enumerate(parcel_identifiers):
                    sheet_values = {item["sheet"].upper()}
                    parcel_values = {item["parcel"].upper()}
                    if item["sheet"].isdigit():
                        numeric_sheet = str(int(item["sheet"]))
                        sheet_values.update(numeric_sheet.zfill(width) for width in (2, 3, 4))
                        if int(numeric_sheet) >= 100 and int(numeric_sheet) % 100 == 0:
                            numeric_sheet = str(int(numeric_sheet) // 100)
                            sheet_values.add(numeric_sheet)
                            sheet_values.update(numeric_sheet.zfill(width) for width in (2, 3, 4))
                    reference_sheet_values = set(sheet_values)
                    if item["sheet"].isdigit():
                        sheet_values.add(numeric_sheet.zfill(4) + "00")
                    if item["parcel"].isdigit():
                        normalized_parcel = str(int(item["parcel"]))
                        parcel_values.update(normalized_parcel.zfill(width) for width in (2, 3, 4, 5))

                    section_prefixes = []
                    if item["section"]:
                        section_prefixes.append((item["section"].upper(), 2))
                        if len(item["section"]) > 1:
                            section_prefixes.append((item["section"][0].upper(), 1))
                    section_prefixes.append(("", 0))
                    reference_candidates = set()
                    for sheet_value in reference_sheet_values:
                        normalized_value = str(int(sheet_value)) if sheet_value.isdigit() else sheet_value
                        padded_sheet = normalized_value.zfill(4) if normalized_value.isdigit() else normalized_value
                        sheet_suffixes = {padded_sheet, padded_sheet + "00"}
                        if normalized_value.isdigit():
                            sheet_suffixes.update({normalized_value, normalized_value + "00"})
                        parcel_candidates = set(parcel_values)
                        if item["parcel"].isdigit():
                            parcel_candidates.add(str(int(item["parcel"])))
                        for section_prefix, section_rank in section_prefixes:
                            code_prefixes = [f"{item['municipality_code']}{section_prefix}"]
                            if section_prefix:
                                code_prefixes.append(f"{item['municipality_code']}{section_prefix}_")
                            else:
                                code_prefixes.append(f"{item['municipality_code']}_")
                            if not section_prefix:
                                code_prefixes.append(item["municipality_code"])
                            for code_prefix in code_prefixes:
                                for sheet_suffix in sheet_suffixes:
                                    for parcel_value in parcel_candidates:
                                        reference_candidates.add((
                                            f"{code_prefix}{sheet_suffix}.{parcel_value}".upper(), section_rank,
                                        ))
                    exact_reference_wanted.extend(
                        (index, reference, section_rank)
                        for reference, section_rank in sorted(reference_candidates)
                    )
                    for sheet_value in sorted(sheet_values):
                        for parcel_value in sorted(parcel_values):
                            wanted_rows.append((
                                index, item["municipality"], item["province"], item["municipality_code"],
                                item["section"], sheet_value, parcel_value,
                            ))
                exact_indices = set()
                if exact_reference_wanted:
                    try:
                        async with connection.transaction():
                            exact_cursor = await connection.execute(
                                exact_marker_sql,
                                tuple([row[column] for row in exact_reference_wanted] for column in range(3)),
                            )
                            exact_marker_rows = await exact_cursor.fetchall()
                            marker_rows.extend(exact_marker_rows)
                            exact_indices = {int(row[0]) for row in exact_marker_rows}
                    except Exception as exc:
                        logger.warning("Exact cadastral parcel lookup unavailable: %s: %s", type(exc).__name__, exc)

                # Fall back to municipality/sheet/parcel fields only for rows
                # whose canonical reference did not resolve through the local
                # cadastral identity index.
                fallback_rows = [row for row in wanted_rows if row[0] not in exact_indices]
                if fallback_rows:
                    try:
                        async with connection.transaction():
                            marker_cursor = await connection.execute(
                                marker_sql,
                                tuple([row[column] for row in fallback_rows] for column in range(7)),
                            )
                            marker_rows.extend(await marker_cursor.fetchall())
                    except Exception as exc:
                        parcel_lookup_failed = True
                        logger.warning("Cadastral parcel markers unavailable: %s: %s", type(exc).__name__, exc)

                if is_subject_visura and not use_sister_cadastral_views:
                    sheet_wanted = []
                    sheet_reference_ranks: dict[tuple[int, str], int] = {}
                    for index, item in enumerate(sheet_identifiers):
                        raw_sheet = item["sheet"].strip().upper()
                        sheet_values = {raw_sheet: 3}
                        if raw_sheet.isdigit():
                            normalized_sheet = str(int(raw_sheet))
                            sheet_values[normalized_sheet] = max(sheet_values.get(normalized_sheet, 0), 2)
                            for width in (2, 3, 4):
                                padded = normalized_sheet.zfill(width)
                                sheet_values[padded] = max(sheet_values.get(padded, 0), 1)
                            if int(normalized_sheet) >= 100 and int(normalized_sheet) % 100 == 0:
                                # Some source extracts encode these sheets as the number divided by 100.
                                alternate = str(int(normalized_sheet) // 100)
                                sheet_values[alternate] = max(sheet_values.get(alternate, 0), 0)
                                for width in (2, 3, 4):
                                    padded = alternate.zfill(width)
                                    sheet_values[padded] = max(sheet_values.get(padded, 0), 0)

                        section = item["section"].upper()
                        section_prefixes = []
                        if section:
                            section_prefixes.append((section, 2))
                            if len(section) > 1:
                                section_prefixes.append((section[:1], 1))
                        section_prefixes.append(("", 0))

                        for sheet_value, sheet_rank in sheet_values.items():
                            numeric_sheet = str(int(sheet_value)) if sheet_value.isdigit() else sheet_value
                            padded_sheet = numeric_sheet.zfill(4) if numeric_sheet.isdigit() else numeric_sheet
                            for section_prefix, section_rank in section_prefixes:
                                for suffix, suffix_rank in (("00", 2), ("", 1)):
                                    references = [(f"{item['municipality_code']}{section_prefix}{padded_sheet}{suffix}", 1)]
                                    if section_prefix:
                                        references.append((
                                            f"{item['municipality_code']}{section_prefix}_{padded_sheet}{suffix}", 0,
                                        ))
                                    else:
                                        references.append((f"{item['municipality_code']}_{padded_sheet}{suffix}", 0))
                                    for reference, separator_rank in references:
                                        # Prefer the exact section and sheet spelling, then the standard 00 suffix;
                                        # legacy numeric aliases are considered only if no closer reference exists.
                                        rank = section_rank * 10000 + sheet_rank * 1000 + suffix_rank * 10 + separator_rank
                                        key = (index, reference)
                                        sheet_reference_ranks[key] = max(sheet_reference_ranks.get(key, -1), rank)
                        sheet_wanted.append((
                            index, item["municipality"], item["province"], item["municipality_code"], section,
                        ))

                    sheet_reference_wanted = [
                        (index, reference, rank)
                        for (index, reference), rank in sorted(sheet_reference_ranks.items())
                    ]

                    if sheet_wanted and sheet_reference_wanted:
                        try:
                            async with connection.transaction():
                                sheet_cursor = await connection.execute(
                                    sheet_sql,
                                    tuple([row[column] for row in sheet_wanted] for column in range(5))
                                    + tuple([row[column] for row in sheet_reference_wanted] for column in range(3)),
                                )
                                sheet_rows = await sheet_cursor.fetchall()
                        except Exception as exc:
                            sheet_lookup_failed = True
                            logger.warning("Cadastral sheet geometries unavailable: %s: %s", type(exc).__name__, exc)
            if len(rows) == 1 and rows[0][0]:
                # Neighbouring parcels are optional decoration and the query can run for seconds (it unions the target's
                # geometry): serve what an earlier view found and let a background task fill the memo for the next one.
                adjacent_reference = rows[0][0]
                adjacent_rows = _adjacent_parcels_memo(adjacent_reference, dsn, connection_options, adjacent_sql)
                if adjacent_reference in _ADJACENT_UNAVAILABLE:
                    context["adjacent_lookup_status"] = "unavailable"
                elif adjacent_reference in _ADJACENT_INFLIGHT:
                    context["adjacent_lookup_status"] = "loading"
                else:
                    context["adjacent_lookup_status"] = "available"
    except Exception as exc:
        logger.warning("Cadastral map lookup unavailable: %s", type(exc).__name__)
        if use_cached_map_features(cached_map_rows, require_fresh=False):
            return context
        context["lookup_status"] = "unavailable"
        context["lookup_message"] = (
            "La cartografia catastale locale non è disponibile; i collegamenti cercano l’indirizzo della visura."
        )
        return context

    import json

    matched_indices = set()
    parcel_geometries = {}
    sheet_geometries = {}
    for marker_row in marker_rows:
        try:
            index = int(marker_row[0])
            reference_count = int(marker_row[1])
            srid, max_srid = int(marker_row[2]), int(marker_row[3])
            longitude, latitude = float(marker_row[4]), float(marker_row[5])
        except (TypeError, ValueError, IndexError):
            continue
        if (
            reference_count < 1 or srid != 4326 or max_srid != 4326
            or not (-180 <= longitude <= 180) or not (-90 <= latitude <= 90)
            or not (0 <= index < len(parcel_identifiers))
        ):
            continue
        item = parcel_identifiers[index]
        matched_indices.add(index)
        context["parcel_markers"].append({
            "latitude": latitude,
            "longitude": longitude,
            "reference": item["reference"],
            "municipality": item["municipality"],
            "municipality_code": item["municipality_code"],
            "province": item["province"],
            "section": item["section"],
            "sheet": item["sheet"],
            "parcel": item["parcel"],
            "subunits": item.get("subunits", []),
            "selected": item["key"] == selected_key,
        })
        if marker_row[6]:
            try:
                geometry = json.loads(marker_row[6]) if isinstance(marker_row[6], str) else marker_row[6]
                if isinstance(geometry, dict):
                    parcel_geometries[index] = geometry
                    if context["listed_only"]:
                        context["parcel_features"].append({
                            "type": "Feature",
                            "properties": {
                                "reference": item["reference"],
                                "sheet": item["sheet"],
                                "parcel": item["parcel"],
                                "subunits": item.get("subunits", []),
                                "section": item["section"],
                                "municipality": item["municipality"],
                                "municipality_code": item["municipality_code"],
                                "province": item["province"],
                                "category": item.get("category", ""),
                            },
                            "geometry": geometry,
                        })
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Cadastral parcel geometry could not be decoded")

    if context["listed_only"]:
        context["sheet_count"] = len({
            (
                item["municipality"].casefold(), item["province"].upper(), item["municipality_code"],
                item["section"], item["sheet"],
            )
            for item in parcel_identifiers
        })
        for sheet_row in sheet_rows:
            try:
                index = int(sheet_row[0])
                srid, max_srid = int(sheet_row[2]), int(sheet_row[3])
                if not (0 <= index < len(sheet_identifiers)) or srid != 4326 or max_srid != 4326:
                    continue
                geometry = json.loads(sheet_row[4]) if isinstance(sheet_row[4], str) else sheet_row[4]
                if not isinstance(geometry, dict):
                    continue
            except (TypeError, ValueError, IndexError, json.JSONDecodeError):
                continue
            item = sheet_identifiers[index]
            sheet_geometries[index] = geometry
            context["sheet_features"].append({
                "type": "Feature",
                "properties": {
                    "reference": (
                        f"{item['municipality']} ({item['municipality_code']}) · "
                        + (f"Sezione {item['section']} · " if item["section"] else "")
                        + f"Foglio {item['sheet']}"
                    ),
                    "sheet": item["sheet"],
                    "section": item["section"],
                    "municipality": item["municipality"],
                    "municipality_code": item["municipality_code"],
                    "province": item["province"],
                },
                "geometry": geometry,
            })
        context["mapped_sheet_count"] = len(context["sheet_features"])

    if document_id is not None:
        cache_records = []
        for index, item in enumerate(parcel_identifiers):
            geometry = parcel_geometries.get(index)
            if geometry is None and parcel_lookup_failed:
                continue
            cache_records.append((
                "parcel",
                _cadastral_map_lookup_key(item, feature_type="parcel"),
                item["municipality"],
                item["province"],
                item["municipality_code"],
                item["section"],
                item["sheet"],
                item["parcel"],
                None,
                json.dumps(geometry, separators=(",", ":")) if geometry else None,
            ))
        for index, item in enumerate(sheet_identifiers):
            geometry = sheet_geometries.get(index)
            if geometry is None and sheet_lookup_failed:
                continue
            cache_records.append((
                "sheet",
                _cadastral_map_lookup_key(item, feature_type="sheet"),
                item["municipality"],
                item["province"],
                item["municipality_code"],
                item["section"],
                item["sheet"],
                "",
                None,
                json.dumps(geometry, separators=(",", ":")) if geometry else None,
            ))
        await _write_visura_cadastral_map_cache(cache_records)

    context["unmapped_parcels"] = [
        item["reference"] for index, item in enumerate(parcel_identifiers) if index not in matched_indices
    ]
    context["unmapped_parcel_items"] = [
        parcel_map_reference(item)
        for index, item in enumerate(parcel_identifiers) if index not in matched_indices
    ]
    if context["parcel_markers"] and not any(marker["selected"] for marker in context["parcel_markers"]):
        context["parcel_markers"][0]["selected"] = True
    context["mapped_parcel_count"] = len(context["parcel_markers"])

    if is_subject_visura:
        context["lookup_status"] = "matched" if context["parcel_markers"] else "not_found"
        context["lookup_message"] = (
            f"Trovate {context['mapped_parcel_count']} particelle tra quelle elencate nelle tabelle della visura."
            if context["parcel_markers"]
            else "Nessuna geometria particellare trovata per i riferimenti elencati nelle tabelle della visura."
        )
        return context

    context["lookup_status"] = "not_found"
    reference = f"FG {sheet} · PT {parcel}"
    if len(rows) != 1:
        context["lookup_message"] = (
            f"La particella {reference} non è stata trovata in modo univoco nella cartografia catastale locale. "
            "I collegamenti cercano l’indirizzo della visura; Street View dipende dalla copertura."
        )
        return context

    row = rows[0]
    try:
        longitude = float(row[6])
        latitude = float(row[7])
        srid, max_srid = int(row[4]), int(row[5])
    except (TypeError, ValueError):
        return context
    if (
        srid != 4326
        or max_srid != 4326
        or not (-180 <= longitude <= 180)
        or not (-90 <= latitude <= 90)
    ):
        logger.warning("Cadastral parcel has invalid WGS84 coordinates")
        context["lookup_status"] = "invalid_coordinates"
        context["lookup_message"] = (
            f"La geometria catastale della particella {reference} non ha coordinate WGS84 valide; "
            "i collegamenti cercano l’indirizzo della visura."
        )
        return context

    point = f"{latitude:.6f},{longitude:.6f}"
    context.update(
        {
            "map_url": search_url(point),
            "map_embed_url": map_embed_url(point),
            "street_view_url": "https://www.google.com/maps/@?"
            + urlencode({"api": "1", "map_action": "pano", "viewpoint": point}),
            "street_view_embed_url": street_view_embed_url(point),
            "centroid": point,
            "lookup_status": "matched",
            "lookup_message": f"Centroide catastale della particella {reference}: {point} (WGS84).",
        }
    )
    seen_adjacent = set()
    for adjacent in adjacent_rows:
        try:
            adjacent_srid, adjacent_max_srid = int(adjacent[6]), int(adjacent[7])
            adjacent_longitude, adjacent_latitude = float(adjacent[8]), float(adjacent[9])
        except (TypeError, ValueError):
            continue
        if (
            adjacent_srid != 4326
            or adjacent_max_srid != 4326
            or not (-180 <= adjacent_longitude <= 180)
            or not (-90 <= adjacent_latitude <= 90)
        ):
            continue
        adjacent_municipality = str(adjacent[2] or municipality).strip()
        adjacent_province = str(adjacent[3] or province).strip()
        adjacent_sheet = str(adjacent[4] or "").strip()
        adjacent_parcel = str(adjacent[5] or "").strip()
        dedupe_key = (
            str(adjacent[1] or adjacent[0] or "").upper(),
            adjacent_sheet.upper(),
            adjacent_parcel.upper(),
        )
        if dedupe_key in seen_adjacent or not (adjacent_sheet and adjacent_parcel):
            continue
        seen_adjacent.add(dedupe_key)
        adjacent_point = f"{adjacent_latitude:.6f},{adjacent_longitude:.6f}"
        context["adjacent_parcels"].append(
            {
                "sheet": adjacent_sheet,
                "parcel": adjacent_parcel,
                "municipality": adjacent_municipality,
                "province": adjacent_province,
                "map_url": search_url(adjacent_point),
            }
        )
    return context


# Document kind -> (theme template that renders the document, block of that template that holds the map).
# `catasto/entity.html` extends the parent and places the map in the slot.
_LAND_MAP_LAYOUTS: dict[str, tuple[str, str]] = {
    "fabbricati": ("catasto/visura_immobile.html", "after_query"),
    "terreni": ("catasto/visura_immobile.html", "after_query"),
    "soggetto": ("catasto/visura_soggetto_archive.html", "sidecar"),
    "ipotecaria": ("catasto/ispezione_ipotecaria.html", "content"),
}


def _land_map_layout(kind: str, theme=None) -> dict[str, str]:
    """Template variables for `catasto/entity.html` for one document kind.

    Older installed theme releases still provide separate Fabbricati and
    Terreni templates. Keep those releases working while preferring the
    unified template when it is available.
    """
    parent, slot = _LAND_MAP_LAYOUTS[kind]
    if theme is not None and kind in ("fabbricati", "terreni"):
        try:
            theme.templates.env.get_template(parent)
        except TemplateNotFound:
            parent = {
                "fabbricati": "catasto/visura_fabbricati.html",
                "terreni": "catasto/visura_terreni.html",
            }[kind]
            # Let a missing fallback raise with the actual template name.
            theme.templates.env.get_template(parent)
    return {"land_map_parent": parent, "land_map_slot": slot}


async def _land_registry_map_context(lookup: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build an embeddable map, using a municipality center if parcel coordinates are unknown."""
    if not lookup or not (lookup.get("sheet") and lookup.get("parcel")):
        return None

    document_id = lookup.get("document_id")
    data_url = lookup.get("data_url") or (
        f"/documents/{int(document_id)}/parcel-map.json" if document_id is not None else ""
    )
    related_ids = [int(value) for value in lookup.get("related_document_ids") or [] if value is not None]
    if related_ids and data_url:
        data_url += "&" if "?" in data_url else "?"
        data_url += urlencode({"also": related_ids}, doseq=True)

    context: dict[str, Any] = {
        "available": False,
        "data_url": data_url,
        "sheet": lookup["sheet"],
        "parcel": lookup["parcel"],
        "municipality": lookup.get("municipality") or "",
        "province": lookup.get("province") or "",
        "parcel_markers": lookup.get("parcel_markers") or [],
        "parcel_features": lookup.get("parcel_features") or [],
        "sheet_features": lookup.get("sheet_features") or [],
        "omi_zone_features": lookup.get("omi_zone_features") or {"type": "FeatureCollection", "features": []},
        "listed_only": bool(lookup.get("listed_only")),
        "unmapped_parcels": lookup.get("unmapped_parcels") or [],
        "parcel_count": lookup.get("parcel_count") or 0,
        "mapped_parcel_count": lookup.get("mapped_parcel_count") or 0,
        "sheet_count": lookup.get("sheet_count") or 0,
        "mapped_sheet_count": lookup.get("mapped_sheet_count") or 0,
        "message": lookup.get("lookup_message")
        or "La geometria catastale non è disponibile nella cartografia locale.",
    }
    centroid = lookup.get("centroid")
    center_kind = "parcel"
    if not context["parcel_markers"] and centroid:
        try:
            marker_latitude, marker_longitude = map(float, str(centroid).split(",", 1))
            if -90 <= marker_latitude <= 90 and -180 <= marker_longitude <= 180:
                context["parcel_markers"] = [{
                    "latitude": marker_latitude,
                    "longitude": marker_longitude,
                    "reference": lookup.get("selected_parcel_reference") or f"FG {context['sheet']} · PT {context['parcel']}",
                    "selected": True,
                }]
                context["mapped_parcel_count"] = max(context["mapped_parcel_count"], 1)
                context["unmapped_parcels"] = [
                    reference for reference in context["unmapped_parcels"]
                    if reference != lookup.get("selected_parcel_reference")
                ]
        except (TypeError, ValueError):
            pass
    if not centroid and context["parcel_markers"]:
        first_marker = context["parcel_markers"][0]
        centroid = f"{first_marker['latitude']},{first_marker['longitude']}"
        center_kind = "parcel" if len(context["parcel_markers"]) == 1 else "parcels"
    if not centroid and context["sheet_features"]:
        sheet_coordinates: list[tuple[float, float]] = []

        def collect_coordinates(value: Any) -> None:
            if isinstance(value, dict):
                if value.get("type") == "GeometryCollection":
                    value = value.get("geometries") or []
                else:
                    value = value.get("coordinates") or []
            if not isinstance(value, (list, tuple)):
                return
            if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                longitude, latitude = float(value[0]), float(value[1])
                if -180 <= longitude <= 180 and -90 <= latitude <= 90:
                    sheet_coordinates.append((longitude, latitude))
                return
            for item in value:
                collect_coordinates(item)

        for feature in context["sheet_features"]:
            if isinstance(feature, dict):
                collect_coordinates(feature.get("geometry"))
        if sheet_coordinates:
            west = min(point[0] for point in sheet_coordinates)
            east = max(point[0] for point in sheet_coordinates)
            south = min(point[1] for point in sheet_coordinates)
            north = max(point[1] for point in sheet_coordinates)
            centroid = f"{(south + north) / 2},{(west + east) / 2}"
            center_kind = "sheet"
    if not centroid and context["municipality"]:
        try:
            from land_registry.map_layers import get_map_search_source

            matches = await asyncio.wait_for(
                get_map_search_source().search_municipalities(context["municipality"], limit=10),
                timeout=3,
            )
            municipality_key = context["municipality"].casefold()
            match = next(
                (
                    item for item in matches
                    if str(item.get("canonical_name") or "").strip().casefold() == municipality_key
                ),
                matches[0] if len(matches) == 1 else None,
            )
            if match and match.get("latitude") is not None and match.get("longitude") is not None:
                centroid = f"{match['latitude']},{match['longitude']}"
                center_kind = "municipality"
        except Exception as exc:
            logger.warning("Municipality map center unavailable: %s", type(exc).__name__)
    if not centroid:
        return context
    try:
        latitude_text, longitude_text = str(centroid).split(",", 1)
        latitude, longitude = float(latitude_text), float(longitude_text)
    except (TypeError, ValueError):
        return context
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return context

    context.update(
        {
            "available": True,
            "latitude": latitude,
            "longitude": longitude,
        "zoom": 17 if center_kind in ("parcel", "parcels") else 14,
            "center_kind": center_kind,
            "message": (
                "La posizione esatta della particella non è disponibile; la mappa è centrata sul comune."
                if center_kind == "municipality"
                else lookup.get("lookup_message") or ""
            ),
        }
    )
    return context


def _doc_as_result(doc: dict) -> dict:
    """Wrap a single DB document into a synthetic ``result`` object so that
    result_detail.html (which is built around a request ``result`` with a list
    of ``documents``) can render the document exhaustively.

    The document is exposed as the sole entry in ``result.documents``; the
    template's per-document block renders ``doc.xml_parsed`` via render_nested,
    giving a complete field-by-field dump.
    """
    return {
        "status": "completed",
        "request_id": str(doc.get("id") or ""),
        "document_id": doc.get("id"),
        "document_is_favorite": bool(doc.get("is_favorite")),
        "document_favorites_enabled": bool(doc.get("favorites_enabled")),
        "request_type": doc.get("document_type") or "document",
        "tipo_catasto": doc.get("tipo_catasto") or "-",
        "requested_at_display": doc.get("richiesta_del") or "-",
        "responded_at_display": "-",
        "provincia": doc.get("provincia") or "-",
        "comune": doc.get("comune") or "-",
        "foglio": doc.get("foglio") or "-",
        "particella": doc.get("particella") or "-",
        "sezione": doc.get("sezione_urbana") or "-",
        "subalterno": doc.get("subalterno") or "-",
        "error": None,
        # Truthy data → template renders the documents block (not the "pending" notice)
        "data": {"document": True},
        "sections": [],
        "documents": [doc],
        "land_registry_map": doc.get("land_registry_map"),
        "page_visit_rows": [],
    }


@router.get("/documents/view/{path:path}", response_class=HTMLResponse)
async def web_document_view(request: Request, path: str, user=Depends(_require_auth)):
    """Parse a p7m/xml file from the data directory and render it in the matching visura template."""
    theme = _get_theme(request)
    base = _files_base()
    target = (base / path).resolve()

    if target != base and base not in target.parents:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists() or not target.is_file():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")
    await _require_document_path_access(user, target)

    # Parse the file (handles .p7m extraction + XML parsing)
    xml_content = ""
    ext = target.suffix.lower()
    if ext == ".p7m":
        from .utils import _p7m_payload

        payload = _p7m_payload(str(target))
        if payload:
            xml_content = payload.decode("utf-8", errors="ignore")
    elif ext in (".xml",):
        xml_content = target.read_text(encoding="utf-8", errors="ignore")
    else:
        # Not a structured document — fall back to download
        return FileResponse(str(target), filename=target.name)

    xml_parsed = _parse_xml_to_dict(xml_content)

    # Build a doc dict that matches what the visura templates expect
    doc: dict[str, Any] = {
        "id": None,
        "filename": target.name,
        "file_path": str(target),
        "oggetto": target.stem,
        "document_type": "",
        "provincia": "",
        "comune": "",
        "foglio": "",
        "particella": "",
        "subalterno": "",
        "sezione_urbana": "",
        "tipo_catasto": "",
        "intestati": [],
        "classamento": [],
        "indirizzo": "",
        "xml_parsed": xml_parsed,
        "view": build_visura_view(xml_content),
    }

    # Populate structured fields from xml_parsed if possible
    if xml_parsed:
        from .utils import _parse_visura_xml

        parsed = _parse_visura_xml(str(target))
        if parsed:
            doc.update({k: v for k, v in parsed.items() if k != "xml_content"})
            doc["document_type"] = parsed.get("tipo", "")

    doc["intestati_rows"] = [
        {
            "Nominativo": r.get("Nominativo") or r.get("nominativo") or "-",
            "Codice Fiscale": r.get("CF") or r.get("CodiceFiscale") or "-",
            "Quota": (r.get("DirittiReali") or {}).get("Quota", "") if isinstance(r.get("DirittiReali"), dict) else "",
            "Diritto": (
                (r.get("DirittiReali") or {}).get("Descrizione", "") if isinstance(r.get("DirittiReali"), dict) else ""
            ),
            "Periodo": (
                (r.get("DirittiReali") or {}).get("FineDiritto", "") if isinstance(r.get("DirittiReali"), dict) else ""
            ),
        }
        for r in (doc.get("intestati") or [])
    ]
    doc["classamento_rows"] = [
        {
            "Zona Censuaria": r.get("ZonaCensuaria") or "-",
            "Categoria": r.get("Categoria") or "-",
            "Classe": r.get("Classe") or "-",
            "Rendita (EUR)": r.get("RenditaEuro") or "-",
        }
        for r in (doc.get("classamento") or [])
    ]
    doc["meta"] = [
        ("Filename", target.name),
        ("Path", str(target.relative_to(base))),
        ("Tipo", doc.get("document_type") or "-"),
        ("Provincia", doc.get("provincia") or "-"),
        ("Foglio / Particella", f"{doc.get('foglio') or '-'} / {doc.get('particella') or '-'}"),
        ("Subalterno", doc.get("subalterno") or "-"),
    ]

    # Select template by XML root element
    is_fabbricati = False
    if any(
        key in xml_parsed
        for key in ("VisuraFabbricatiStorica", "VisuraFabbricatiAttuale", "VisuraFabbricatiSintetica", "VisuraFabbricati")
    ):
        template = "catasto/visura_immobile.html"
        is_fabbricati = True
    elif any(
        key in xml_parsed
        for key in ("VisuraSoggettoAttuale", "VisuraSoggettoStorica", "VisuraSoggettoSintetica", "VisuraSoggetto")
    ):
        template = "catasto/visura_soggetto.html"
    elif any(
        key in xml_parsed
        for key in (
            "VisuraTerreniAttuale",
            "VisuraTerreniStorica",
            "VisuraTerreniSintetica",
            "VisuraTerrenoAttuale",
            "VisuraTerrenoStorica",
            "VisuraTerrenoSintetica",
            "VisuraTerreno",
        )
    ):
        template = "catasto/visura_immobile.html"
    else:
        template = "result_detail.html"

    return theme.render(
        template,
        request,
        user=user,
        doc=doc,
        google_maps=await _google_maps_context(doc) if is_fabbricati else None,
    )


@router.get("/files")
@router.get("/files/{path:path}")
async def web_files_redirect(request: Request, path: str = ""):
    target = "/documents" + (f"/{path}" if path else "")
    return RedirectResponse(url=target, status_code=301)


@router.post("/documents/export-named")
@_audited("documents.export_named")
async def web_documents_export_named(request: Request, user=Depends(_require_auth)):
    """Copy all documents to documents/named/ using the display name stored in oggetto."""
    import shutil

    base = _files_base()
    dest_dir = base / "named"
    dest_dir.mkdir(exist_ok=True)

    docs = await _load_visible_documents(user)
    mapping = {
        d["filename"]: d["oggetto"]
        for d in docs
        if d.get("filename") and d.get("oggetto") and d["filename"] != d["oggetto"]
    }

    copied, skipped, missing = [], [], []
    for old_name, new_name in mapping.items():
        src = base / old_name
        if not src.exists():
            missing.append(old_name)
            continue
        dst = dest_dir / new_name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            skipped.append(new_name)
            continue
        shutil.copy2(src, dst)
        copied.append(new_name)
        logger.info("Exported: %s → named/%s", old_name, new_name)

    return {
        "copied": len(copied),
        "skipped": len(skipped),
        "missing": len(missing),
        "dest": str(dest_dir),
        "files": copied,
        "missing_files": missing,
    }


@router.get("/documents/{doc_id}/view")
async def web_document_view_by_id(request: Request, doc_id: int, user=Depends(_require_auth)):
    """Serve the document file inline (for in-browser PDF viewing)."""
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import Response

    doc = await get_document_by_id(doc_id)
    if doc is None or not doc.get("file_path"):
        raise HTTPException(status_code=404, detail="Document file not found")
    await _require_document_access(user, doc_id)
    p = Path(doc["file_path"])
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Document file not found")
    _MEDIA = {".pdf": "application/pdf", ".p7m": "application/pkcs7-mime", ".xml": "application/xml"}
    media_type = _MEDIA.get(p.suffix.lower(), "application/octet-stream")
    filename = doc.get("filename") or p.name
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return Response(content=p.read_bytes(), media_type=media_type, headers=headers)


@router.get("/documents/{doc_id}/pdf")
async def web_document_pdf(request: Request, doc_id: int, user=Depends(_require_auth)):
    """PDF of a visura (A4 landscape), typeset with Typst from the same data the page shows."""
    from fastapi import HTTPException
    from fastapi.responses import Response

    from .pdf_typst import PdfExportError, build_context, pdf_filename, render_pdf

    doc = await get_document_by_id(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, doc_id)
    xml_content = doc.pop("xml_content", "") or ""
    doc["xml_parsed"] = _parse_xml_to_dict(xml_content)
    doc["view"] = build_visura_view(xml_content)
    lookup = await _google_maps_context(doc)
    kpis = None
    if any(key in doc["xml_parsed"] for key in ("VisuraSoggettoAttuale", "VisuraSoggettoStorica",
                                                "VisuraSoggettoSintetica", "VisuraSoggetto")):
        kpis = await _subject_land_registry_calculation(
            doc["xml_parsed"], lookup, user, scope_type="document", scope_id=doc.get("id")
        )
    try:
        context = build_context(doc, kpis, lookup)
        content = await run_in_threadpool(render_pdf, context)
    except PdfExportError as exc:
        logger.warning("PDF export of document %s failed: %s", doc_id, exc)
        raise HTTPException(status_code=422, detail=f"PDF export not available: {exc}") from exc
    stem = Path(str(doc.get("filename") or "")).stem
    headers = {"Content-Disposition": f'attachment; filename="{pdf_filename(stem, doc_id)}"', "Cache-Control": "no-store"}
    return Response(content=content, media_type="application/pdf", headers=headers)


@router.get("/documents/{doc_id}/download")
async def web_document_download(request: Request, doc_id: int, user=Depends(_require_auth)):
    """Download the original file backing a DB-indexed document."""
    from pathlib import Path

    from fastapi import HTTPException

    doc = await get_document_by_id(doc_id)
    if doc is None or not doc.get("file_path"):
        raise HTTPException(status_code=404, detail="Document file not found")
    await _require_document_access(user, doc_id)
    p = Path(doc["file_path"])
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Document file not found")
    return FileResponse(str(p), filename=doc.get("filename") or p.name)


def _extract_planimetria_zip(file_path: str) -> bytes | None:
    """Extract the inner ZIP payload from a CAdES-signed P7M file (aecs4u-crypto).

    Strips the CMS signature layer via the shared pure-Python extractor (no
    openssl). Returns raw ZIP bytes, or None if extraction fails or the payload
    is not a ZIP.
    """
    from aecs4u_crypto import P7MError, extract_p7m_payload

    try:
        data = extract_p7m_payload(open(file_path, "rb").read())
    except (P7MError, OSError):
        return None
    return data if data.startswith(b"PK\x03\x04") else None


def _read_planimetria_geojson(file_path: str) -> dict | None:
    """Extract metadata from a planimetria P7M ZIP.

    Returns a dict for ALL extractable planimetria P7M files, regardless of
    the inner format.  ``geojson`` is populated only when the ZIP contains a
    .geojson file; otherwise it is None and ``fmt`` describes the actual format.

    Return keys::

        geojson      – FeatureCollection dict, or None
        crs_text     – raw CRS description text
        epsg         – parsed EPSG code (int) or None
        proj4        – proj4 string for the CRS
        files        – list of filenames inside the ZIP
        fmt          – 'geojson' | 'dxf' | 'cxf' | 'cmf' | 'unknown'

    Returns None only if the P7M cannot be extracted (not a ZIP at all).
    """
    import io
    import json
    import re
    import zipfile

    zip_bytes = _extract_planimetria_zip(file_path)
    if zip_bytes is None:
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = z.namelist()
            crs_name = next((n for n in names if "_SistemaDiRappresentazione" in n), None)
            crs_text = z.read(crs_name).decode("utf-8", errors="replace") if crs_name else ""

            geojson_name = next((n for n in names if n.lower().endswith(".geojson")), None)
            gj = json.loads(z.read(geojson_name)) if geojson_name else None
    except Exception:
        return None

    # Detect inner format
    exts = [n.rsplit(".", 1)[-1].lower() for n in names if "." in n and "_Sistema" not in n]
    if "geojson" in exts:
        fmt = "geojson"
    elif "dxf" in exts:
        fmt = "dxf"
    elif any(e in exts for e in ("cxf", "cxg")):
        fmt = "cxf"
    elif any(e in exts for e in ("cmf", "cmb")):
        fmt = "cmf"
    else:
        fmt = "unknown"

    # Parse EPSG from the CRS description file
    epsg: int | None = None
    m = re.search(r"EPSG[:\s]*(\d{4,5})", crs_text)
    if m:
        epsg = int(m.group(1))

    _PROJ4 = {
        7791: "+proj=utm +zone=32 +ellps=GRS80 +units=m +no_defs",
        7792: "+proj=utm +zone=33 +ellps=GRS80 +units=m +no_defs",
        32632: "+proj=utm +zone=32 +datum=WGS84 +units=m +no_defs",
        32633: "+proj=utm +zone=33 +datum=WGS84 +units=m +no_defs",
    }
    proj4 = _PROJ4.get(epsg or 0, "+proj=utm +zone=32 +ellps=GRS80 +units=m +no_defs")

    return {
        "geojson": gj,
        "crs_text": crs_text,
        "epsg": epsg,
        "proj4": proj4,
        "files": [n for n in names if "_SistemaDiRappresentazione" not in n],
        "fmt": fmt,
    }


def _classify_cad_label(label: str) -> str:
    lbl = label.strip().upper()
    if lbl in ("ACQUA", "ACQUE") or lbl.startswith("ACQUA"):
        return "acqua"
    if lbl in ("STRADA", "STRADE") or lbl.startswith("STRADA"):
        return "strada"
    if lbl in ("FABBRICATI", "FABBRIC"):
        return "fabbricato"
    if lbl == "LINEEVARIE":
        return "lineevarie"
    if lbl in ("SIMBOLI", "TESTI", "FIDUCIALI", "CENTROIDI", "RILIEVI"):
        return "simboli"
    if lbl == "CONFINE":
        return "confine"
    if lbl and lbl[0].isdigit():
        return "particella"
    return "confine"


def _parse_cxf_entities(content: str) -> list[dict]:
    lines = [line.strip() for line in content.replace("\r", "").split("\n")]
    entities: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i] != "BORDO":
            i += 1
            continue
        i += 1
        label = lines[i] if i < len(lines) else ""
        i += 7  # skip past label, dim, angle, posX, posY, pintX, pintY to n.isole
        n_holes = int(lines[i]) if i < len(lines) else 0
        i += 1
        n_total = int(lines[i]) if i < len(lines) else 0  # total verts: outer + all holes
        i += 1
        hole_verts = []
        for _ in range(n_holes):
            hole_verts.append(int(lines[i]))
            i += 1
        # read all n_total coordinate pairs then split outer/holes
        all_coords = []
        for _ in range(n_total):
            x = float(lines[i])
            i += 1
            y = float(lines[i])
            i += 1
            all_coords.append([x, y])
        n_outer = n_total - sum(hole_verts)
        coords = all_coords[:n_outer]
        holes = []
        pos = n_outer
        for nv in hole_verts:
            holes.append(all_coords[pos : pos + nv])
            pos += nv
        entities.append({
            "type": _classify_cad_label(label),
            "label": label.strip(),
            "coords": coords,
            "holes": holes,
        })
    return entities


def _parse_cmf_entities(content: bytes) -> list[dict]:
    from lxml import etree

    root = etree.fromstring(content)
    entities: list[dict] = []
    for bordo in root.findall(".//BORDO"):
        label = bordo.get("codbo", "")
        gbordo = bordo.find("GBORDO")
        if gbordo is None:
            continue
        n_total = int(gbordo.get("n.vert", 0))  # total verts: outer + all holes
        hole_verts = [int(v.text) for v in gbordo.findall("VERTISOLA")]
        coord_el = gbordo.find("COORD")
        if coord_el is None:
            continue
        all_coords = []
        for pair in (coord_el.text or "").split():
            xy = pair.split(",")
            if len(xy) == 2:
                all_coords.append([float(xy[0]), float(xy[1])])
        n_outer = n_total - sum(hole_verts)
        coords = all_coords[:n_outer]
        holes = []
        pos = n_outer
        for nv in hole_verts:
            holes.append(all_coords[pos : pos + nv])
            pos += nv
        entities.append({
            "type": _classify_cad_label(label),
            "label": label.strip(),
            "coords": coords,
            "holes": holes,
        })
    return entities


def _parse_dxf_entities(dxf_bytes: bytes) -> list[dict]:
    """Parse DXF R12 (AC1009) POLYLINE entities by scanning raw group code pairs."""
    _LAYER_TYPE = {
        "FABBRICATI": "fabbricato",
        "PARTICELLE": "particella",
        "STRADE": "strada",
        "ACQUE": "acqua",
        "CONFINE": "confine",
        "LINEEVARIE": "lineevarie",
        "SIMBOLI": "simboli",
        "TESTI": "simboli",
        "FIDUCIALI": "simboli",
        "CENTROIDI": "simboli",
        "RILIEVI": "simboli",
    }
    try:
        content = dxf_bytes.decode("latin-1")
    except Exception:
        return []

    # Parse as (group_code, value) pairs — lines alternate code / value
    raw = [line.strip() for line in content.replace("\r", "").split("\n") if line.strip()]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(raw):
        pairs.append((raw[i], raw[i + 1]))
        i += 2

    entities: list[dict] = []
    in_entities = False
    current: dict | None = None
    current_x: float = 0.0
    in_vertex = False

    for code, value in pairs:
        if code == "2" and value == "ENTITIES":
            in_entities = True
            continue
        if code == "0" and value == "ENDSEC":
            if in_entities:
                if current and current["coords"]:
                    entities.append(current)
                in_entities = False
                current = None
            continue

        if not in_entities:
            continue

        if code == "0":
            if value == "POLYLINE":
                if current and current["coords"]:
                    entities.append(current)
                current = {"type": "confine", "label": "0", "coords": [], "holes": []}
                in_vertex = False
            elif value == "VERTEX":
                in_vertex = True
            elif value in ("SEQEND", "LINE", "TEXT", "INSERT", "BLOCK", "ENDBLK"):
                in_vertex = False
                if value in ("SEQEND",) and current and current["coords"]:
                    pass  # coords already accumulated
        elif current is not None:
            if code == "8":  # layer
                if not in_vertex:
                    layer = value.upper()
                    current["label"] = layer
                    current["type"] = _LAYER_TYPE.get(layer, _classify_cad_label(layer))
            elif code == "10":  # X coordinate
                try:
                    current_x = float(value)
                except ValueError:
                    pass
            elif code == "20":  # Y coordinate
                try:
                    if in_vertex:
                        current["coords"].append([current_x, float(value)])
                except ValueError:
                    pass

    if current and current["coords"]:
        entities.append(current)

    return entities


def _build_plan_data(file_path: str) -> dict | None:
    """Parse a planimetria P7M to a JSON-serialisable entity list, with disk cache.

    Cache is stored at ``{file_path}.plan.json`` and is only written once.
    Returns None if the file cannot be extracted or is in an unsupported format.
    """
    import json as _json

    cache_path = file_path + ".plan.json"
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return _json.load(f)
        except Exception:
            pass  # corrupt cache — re-parse

    zip_bytes = _extract_planimetria_zip(file_path)
    if zip_bytes is None:
        return None

    import io as _io
    import zipfile as _zf

    try:
        with _zf.ZipFile(_io.BytesIO(zip_bytes)) as z:
            names = z.namelist()

            geojson_name = next((n for n in names if n.lower().endswith(".geojson")), None)
            cxf_name = next((n for n in names if n.lower().endswith(".cxf")), None)
            cmf_name = next((n for n in names if n.lower().endswith(".cmf")), None)
            dxf_name = next((n for n in names if n.lower().endswith(".dxf")), None)

            if geojson_name:
                return None  # GeoJSON is handled by the existing geojson route

            if cxf_name:
                content = z.read(cxf_name).decode("latin-1")
                entities = _parse_cxf_entities(content)
                fmt = "cxf"
            elif cmf_name:
                content = z.read(cmf_name)
                entities = _parse_cmf_entities(content)
                fmt = "cmf"
            elif dxf_name:
                content = z.read(dxf_name)
                entities = _parse_dxf_entities(content)
                fmt = "dxf"
            else:
                return None
    except Exception:
        return None

    if not entities:
        return None

    # Compute bounding box across all entities
    all_x = [p[0] for e in entities for p in e["coords"]]
    all_y = [p[1] for e in entities for p in e["coords"]]
    bbox = {"minx": min(all_x), "miny": min(all_y), "maxx": max(all_x), "maxy": max(all_y)}

    result = {"format": fmt, "entities": entities, "bbox": bbox}
    try:
        with open(cache_path, "w") as f:
            _json.dump(result, f, separators=(",", ":"))
    except Exception:
        pass
    return result


@router.get("/documents/{doc_id}/plan")
async def web_document_plan(doc_id: int, request: Request, user=Depends(_require_auth)):
    """Return parsed cadastral entity data for CXF/CMF/DXF planimetrie."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    doc = await get_document_by_id(doc_id)
    if doc is None or not doc.get("file_path"):
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, doc_id)

    import asyncio as _asyncio

    result = await _asyncio.get_event_loop().run_in_executor(None, _build_plan_data, doc["file_path"])
    if result is None:
        raise HTTPException(status_code=422, detail="Plan data not available for this document")
    return JSONResponse(result)


@router.put("/documents/{doc_id}/favorite", response_class=JSONResponse)
async def web_document_favorite(request: Request, doc_id: int, user=Depends(_require_auth)):
    """Set a signed-in user's favourite state for one indexed document."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save favourites")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict) or type(payload.get("favorite")) is not bool:
        raise HTTPException(status_code=422, detail="favorite must be a boolean")

    document = await get_document_by_id(doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, doc_id)
    from .database import set_document_favorite

    await set_document_favorite(user_key, doc_id, payload["favorite"])
    return JSONResponse({"document_id": doc_id, "favorite": payload["favorite"]})


@router.put("/companies/{fiscal_code}/favorite", response_class=JSONResponse)
async def web_company_favorite(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    """Set a signed-in user's favourite state for one company profile."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save favourites")
    normalized_code = fiscal_code.strip()
    if not re.fullmatch(r"\d{11}", normalized_code):
        raise HTTPException(status_code=404, detail="Azienda non trovata")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict) or type(payload.get("favorite")) is not bool:
        raise HTTPException(status_code=422, detail="favorite must be a boolean")

    from .database import set_subject_favorite

    await set_subject_favorite(user_key, "company", normalized_code, payload["favorite"])
    return JSONResponse(
        {"subject_type": "company", "subject_code": normalized_code, "favorite": payload["favorite"]}
    )


@router.put("/persons/{fiscal_code}/favorite", response_class=JSONResponse)
async def web_person_favorite(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    """Set a signed-in user's favourite state for one person profile."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save favourites")
    normalized_code = fiscal_code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{16}", normalized_code):
        raise HTTPException(status_code=404, detail="Persona non trovata")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict) or type(payload.get("favorite")) is not bool:
        raise HTTPException(status_code=422, detail="favorite must be a boolean")

    from .database import set_subject_favorite

    await set_subject_favorite(user_key, "person", normalized_code, payload["favorite"])
    return JSONResponse(
        {"subject_type": "person", "subject_code": normalized_code, "favorite": payload["favorite"]}
    )


async def _web_subject_note(request: Request, fiscal_code: str, subject_type: str, user):
    """Read or save one authenticated user's private note on a subject profile."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save notes")

    normalized_code = fiscal_code.strip().upper() if subject_type == "person" else fiscal_code.strip()
    valid_code = (
        bool(re.fullmatch(r"[A-Z0-9]{16}", normalized_code))
        if subject_type == "person"
        else bool(re.fullmatch(r"\d{11}", normalized_code))
    )
    if not valid_code:
        raise HTTPException(
            status_code=404,
            detail="Persona non trovata" if subject_type == "person" else "Azienda non trovata",
        )

    from .database import get_subject_note, set_subject_note

    if request.method == "GET":
        note = await get_subject_note(user_key, subject_type, normalized_code)
        return JSONResponse({"subject_type": subject_type, "subject_code": normalized_code, "note": note or ""})

    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict) or type(payload.get("note")) is not str:
        raise HTTPException(status_code=422, detail="note must be a string")
    note = payload["note"].strip()
    if len(note) > 5000:
        raise HTTPException(status_code=422, detail="The note must be 5000 characters or fewer")

    await set_subject_note(user_key, subject_type, normalized_code, note)
    return JSONResponse({"subject_type": subject_type, "subject_code": normalized_code, "note": note})


@router.get("/companies/{fiscal_code}/note", response_class=JSONResponse)
async def web_company_note_get(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    return await _web_subject_note(request, fiscal_code, "company", user)


@router.put("/companies/{fiscal_code}/note", response_class=JSONResponse)
async def web_company_note_put(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    return await _web_subject_note(request, fiscal_code, "company", user)


@router.get("/persons/{fiscal_code}/note", response_class=JSONResponse)
async def web_person_note_get(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    return await _web_subject_note(request, fiscal_code, "person", user)


@router.put("/persons/{fiscal_code}/note", response_class=JSONResponse)
async def web_person_note_put(request: Request, fiscal_code: str, user=Depends(_require_auth)):
    return await _web_subject_note(request, fiscal_code, "person", user)


@router.get("/favorites", response_class=HTMLResponse)
async def web_favorites(request: Request, user=Depends(_require_auth)):
    """Show and manage the signed-in user's saved documents and subject profiles."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Sign in to manage your favourites")

    from .database import get_document_favorite_ids, get_subject_favorite_codes

    favorite_ids = await get_document_favorite_ids(user_key)
    company_favorites = [
        {"code": code, "subject_type": "company", "is_favorite": True}
        for code in sorted(await get_subject_favorite_codes(user_key, "company"))
    ]
    person_favorites = [
        {"code": code, "subject_type": "person", "is_favorite": True}
        for code in sorted(await get_subject_favorite_codes(user_key, "person"))
    ]
    documents = await _filter_purchased_documents(user, await get_documents_by_ids(favorite_ids))
    for document in documents:
        document["is_favorite"] = document.get("id") in favorite_ids
    documents.sort(
        key=lambda document: (document.get("created_at") or "", document.get("id") or 0),
        reverse=True,
    )

    return _get_theme(request).render(
        "favorites.html",
        request,
        user=user,
        documents=documents,
        company_favorites=company_favorites,
        person_favorites=person_favorites,
        favorites_enabled=True,
    )


_MANUAL_VALUATION_PROVIDERS = {"immobiliare", "idealista", "other"}
_MANUAL_VALUATION_SOURCE_DOMAINS = {
    "immobiliare": ("immobiliare.it",),
    "idealista": ("idealista.it",),
}


def _manual_valuation_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1000000000000"):
        return None
    rounded = amount.quantize(Decimal("0.01"))
    return rounded if Decimal("0") < rounded < Decimal("1000000000000") else None


def _manual_affrancation_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or ("." in text and len(text.rsplit(".", 1)[1]) == 3):
        text = text.replace(".", "")
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000"):
        return None
    try:
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _manual_actual_size_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = (
        str(value)
        .strip()
        .replace("m²", "")
        .replace("m2", "")
        .replace("M²", "")
        .replace("M2", "")
    )
    text = text.replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or ("." in text and len(text.rsplit(".", 1)[1]) == 3):
        text = text.replace(".", "")
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("10000000"):
        return None
    try:
        rounded = amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return rounded if Decimal("0") < rounded < Decimal("10000000") else None


def _manual_valuation_source_url(provider: str, value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    source_url = str(value).strip()
    if len(source_url) > 2000:
        raise ValueError("Il link della fonte supera il limite di 2000 caratteri.")
    try:
        parsed = urlsplit(source_url)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("Inserisci un link http o https valido.") from None
    if parsed.scheme not in {"https", "http"} or not hostname or any(char.isspace() for char in hostname):
        raise ValueError("Inserisci un link http o https valido.")
    if parsed.username or parsed.password:
        raise ValueError("Il link non può contenere credenziali.")
    allowed_domains = _MANUAL_VALUATION_SOURCE_DOMAINS.get(provider)
    if allowed_domains and not any(
        hostname.casefold() == domain or hostname.casefold().endswith("." + domain)
        for domain in allowed_domains
    ):
        raise ValueError("Il link deve appartenere al portale selezionato.")
    return source_url


@router.put("/manual-property-valuations/{asset_key}", response_class=JSONResponse)
async def web_manual_property_valuation(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Save or replace a user's manually transcribed estimate from an external provider."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save valuations")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")

    provider = str(payload.get("provider") or "").strip().lower()
    if provider not in _MANUAL_VALUATION_PROVIDERS:
        raise HTTPException(status_code=422, detail="provider must be immobiliare, idealista, or other")
    value_min = _manual_valuation_decimal(payload.get("value_min"))
    raw_value_max = payload.get("value_max")
    value_max = _manual_valuation_decimal(raw_value_max)
    if value_min is None:
        raise HTTPException(status_code=422, detail="Inserisci un valore minimo maggiore di zero.")
    if raw_value_max is not None and str(raw_value_max).strip() and value_max is None:
        raise HTTPException(status_code=422, detail="Il valore massimo non è un importo valido.")
    if value_max is not None and value_max < value_min:
        raise HTTPException(status_code=422, detail="Il valore massimo deve essere almeno quello minimo.")
    source_date = None
    raw_date = str(payload.get("source_date") or "").strip()
    if raw_date:
        try:
            source_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="source_date must use YYYY-MM-DD") from None
    notes = str(payload.get("notes") or "").strip() or None
    if notes and len(notes) > 1000:
        raise HTTPException(status_code=422, detail="Le note non possono superare 1000 caratteri.")
    try:
        source_url = _manual_valuation_source_url(provider, payload.get("source_url"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    from .database import set_user_manual_property_valuation

    await set_user_manual_property_valuation(
        user_key, asset_key, provider, value_min, value_max, source_date, source_url, notes
    )
    return JSONResponse({"ok": True, "asset_key": asset_key, "provider": provider})


@router.put("/manual-property-valuations/{asset_key}/affrancation", response_class=JSONResponse)
async def web_property_affrancation_amount(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Save a user's affrancazione amount, or clear it to use the automatic estimate again."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save affrancazione amounts")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")
    raw_amount = payload.get("amount")
    amount = _manual_affrancation_decimal(raw_amount)
    if raw_amount is not None and str(raw_amount).strip() and amount is None:
        raise HTTPException(status_code=422, detail="Inserisci un importo di affrancazione valido, pari o superiore a zero.")

    from .database import set_user_property_affrancation_amount

    await set_user_property_affrancation_amount(user_key, asset_key, amount)
    return JSONResponse({"ok": True, "asset_key": asset_key, "amount": str(amount) if amount is not None else None})


@router.put("/manual-property-valuations/{asset_key}/omi-override", response_class=JSONResponse)
async def web_property_omi_override(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Save or clear a user's custom value range for one OMI-valued building."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to save OMI adjustments")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")

    raw_min = payload.get("value_min")
    raw_max = payload.get("value_max")
    if raw_min is None or not str(raw_min).strip():
        if raw_max is not None and str(raw_max).strip():
            raise HTTPException(status_code=422, detail="Inserisci anche il valore minimo.")
        value_min = value_max = None
    else:
        value_min = _manual_valuation_decimal(raw_min)
        value_max = _manual_valuation_decimal(raw_max) if raw_max is not None and str(raw_max).strip() else None
        if value_min is None:
            raise HTTPException(status_code=422, detail="Inserisci un valore minimo maggiore di zero.")
        if raw_max is not None and str(raw_max).strip() and value_max is None:
            raise HTTPException(status_code=422, detail="Il valore massimo non è un importo valido.")
        if value_max is not None and value_max < value_min:
            raise HTTPException(status_code=422, detail="Il valore massimo deve essere almeno quello minimo.")

    from .database import set_user_property_omi_override

    await set_user_property_omi_override(user_key, asset_key, value_min, value_max)
    return JSONResponse({
        "ok": True,
        "asset_key": asset_key,
        "value_min": str(value_min) if value_min is not None else None,
        "value_max": str(value_max) if value_max is not None else None,
    })


@router.put("/manual-property-valuations/{asset_key}/omi-selection", response_class=JSONResponse)
async def web_property_omi_selection(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Save one user's OMI zone and typology choice for a cadastral property."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to select OMI data")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")

    comune_code = str(payload.get("comune_code") or "").strip().upper()
    is_cadastral_code = re.fullmatch(r"[A-Z0-9]{4}", comune_code)
    is_istat_code = re.fullmatch(r"\d{5,6}", comune_code)
    if not (is_cadastral_code or is_istat_code):
        raise HTTPException(status_code=422, detail="Codice catastale o ISTAT del comune non valido.")
    raw_zone = payload.get("zone_code")
    zone_code = str(raw_zone or "").strip().upper() or None
    if zone_code is not None and len(zone_code) > 24:
        raise HTTPException(status_code=422, detail="Codice zona OMI non valido.")
    raw_typology = payload.get("typology_code")
    if raw_typology is None or not str(raw_typology).strip():
        typology_code = None
    else:
        try:
            typology_code = int(raw_typology)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Tipologia OMI non valida.") from None
        if typology_code <= 0:
            raise HTTPException(status_code=422, detail="Tipologia OMI non valida.")

    from .omi_quotes import omi_comune_quotes

    quotes = await omi_comune_quotes(comune_code)
    if quotes is not None:
        zone_quotes = [
            row for row in quotes if str(row.get("zona") or "").strip().upper() == zone_code
        ] if zone_code else quotes
        if zone_code and not zone_quotes:
            raise HTTPException(status_code=422, detail="La zona OMI non è disponibile per questo comune.")
        if typology_code is not None and not any(
            str(row.get("cod_tipologia") or "").strip() == str(typology_code) for row in zone_quotes
        ):
            raise HTTPException(status_code=422, detail="La tipologia OMI non è disponibile nella zona selezionata.")
    # Keep user selections durable when the optional OMI quotes database is temporarily unreachable.

    from .database import set_user_property_omi_selection

    await set_user_property_omi_selection(user_key, asset_key, zone_code, typology_code)
    return JSONResponse({
        "ok": True,
        "asset_key": asset_key,
        "zone_code": zone_code,
        "typology_code": typology_code,
    })


@router.put("/manual-property-valuations/{asset_key}/actual-size", response_class=JSONResponse)
async def web_property_actual_size(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Save or clear this user's effective square-metre size for one cadastral property."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to edit property size")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")

    raw_size = payload.get("actual_size_sqm")
    actual_size_sqm = _manual_actual_size_decimal(raw_size)
    if raw_size is not None and str(raw_size).strip() and actual_size_sqm is None:
        raise HTTPException(status_code=422, detail="Inserisci una superficie valida maggiore di zero.")

    from .database import set_user_property_actual_size

    await set_user_property_actual_size(user_key, asset_key, actual_size_sqm)
    return JSONResponse({
        "ok": True,
        "asset_key": asset_key,
        "actual_size_sqm": str(actual_size_sqm) if actual_size_sqm is not None else None,
    })


@router.put("/manual-property-valuations/{asset_key}/preference", response_class=JSONResponse)
async def web_manual_property_valuation_preference(request: Request, asset_key: str, user=Depends(_require_auth)):
    """Set which available valuation source this user prefers for one cadastral property."""
    from fastapi import HTTPException

    user_key = _favorite_user_key(user)
    if not user_key:
        raise HTTPException(status_code=401, detail="Authentication required to select a valuation source")
    if not re.fullmatch(r"[a-f0-9]{64}", asset_key):
        raise HTTPException(status_code=404, detail="Cadastral property not found")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Expected a JSON body") from None
    source = str(payload.get("source") or "").strip().lower() if isinstance(payload, dict) else ""
    if source != "omi" and source not in _MANUAL_VALUATION_PROVIDERS:
        raise HTTPException(status_code=422, detail="source must be omi, immobiliare, idealista, or other")
    from .database import set_user_property_valuation_preference

    saved = await set_user_property_valuation_preference(user_key, asset_key, source)
    if not saved:
        raise HTTPException(status_code=422, detail="Inserisci prima una stima per la fonte scelta.")
    return JSONResponse({"ok": True, "asset_key": asset_key, "preferred_source": source})


@router.get("/documents/{doc_id}/geojson")
async def web_document_geojson(doc_id: int, request: Request, user=Depends(_require_auth)):
    """Return the GeoJSON payload extracted from a planimetria P7M document."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    doc = await get_document_by_id(doc_id)
    if doc is None or not doc.get("file_path"):
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, doc_id)

    result = _read_planimetria_geojson(doc["file_path"])
    if result is None or result.get("geojson") is None:
        raise HTTPException(status_code=422, detail="Not a planimetria GeoJSON document")

    return JSONResponse({
        "geojson": result["geojson"],
        "epsg": result["epsg"],
        "proj4": result["proj4"],
        "crs_text": result["crs_text"],
    })


@router.get("/documents/{doc_id}/parcel-map.json", response_class=JSONResponse)
async def web_document_parcel_map_data(
    doc_id: int,
    also: list[int] = Query(default=[]),
    user=Depends(_require_auth),
):
    """Return the parcel geometries and markers for an authenticated document map."""
    from fastapi import HTTPException

    doc = await get_document_by_id(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await _require_document_access(user, doc_id)

    xml_content = doc.get("xml_content", "")
    doc["xml_parsed"] = _parse_xml_to_dict(xml_content)
    if also:
        if (doc.get("document_type") or "") != "visura_soggetto":
            raise HTTPException(status_code=422, detail="Related visure are only supported for subject maps")
        fiscal_code = _document_subject_fiscal_code(doc)
        primary_scope = _subject_search_scope(
            doc.get("filename"),
            stored_scope=doc.get("subject_search_scope"),
            stored_province=doc.get("subject_search_province"),
        )
        if not fiscal_code:
            raise HTTPException(status_code=422, detail="Subject identity is missing")
        if _subject_cadastre_type(doc) != "E" or primary_scope["search_scope"] != "provincial":
            raise HTTPException(status_code=422, detail="This map is not a provincial E-cadastre portfolio")

        related_parsed = [doc["xml_parsed"]]
        seen_ids = {doc_id}
        for related_id in also:
            if related_id in seen_ids:
                continue
            seen_ids.add(related_id)
            await _require_document_access(user, related_id)
            related = await get_document_by_id(related_id)
            if not related:
                raise HTTPException(status_code=422, detail="Related visura does not match this subject portfolio")
            related_scope = _subject_search_scope(
                related.get("filename"),
                stored_scope=related.get("subject_search_scope"),
                stored_province=related.get("subject_search_province"),
            )
            if (
                related.get("document_type") != "visura_soggetto"
                or _document_subject_fiscal_code(related) != fiscal_code
                or _subject_cadastre_type(related) != "E"
                or related.get("visura_subtype") != doc.get("visura_subtype")
                or related.get("situazione_al") != doc.get("situazione_al")
                or related_scope["search_scope"] != "provincial"
            ):
                raise HTTPException(status_code=422, detail="Related visura does not match this subject portfolio")
            related_parsed.append(_parse_xml_to_dict(related.get("xml_content", "")))
        doc["xml_parsed"] = _merge_subject_visura_asset_groups(related_parsed)
    doc["view"] = build_visura_view(xml_content)
    lookup = await _google_maps_context(doc)
    if lookup is None:
        raise HTTPException(status_code=422, detail="Parcel map data is not available")

    return JSONResponse(await _parcel_map_payload(lookup), headers={"Cache-Control": "private, max-age=300"})


def _omi_zone_features_for_properties(
    parcel_markers: list[dict[str, Any]],
    parcel_features: list[dict[str, Any]],
    unmapped_parcels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """All OMI zones in owned-property municipalities, marking zones overlapping owned parcels."""
    try:
        from .provinces import province_code, province_name
    except ImportError:
        return []

    municipalities: dict[tuple[str, str], dict[str, Any]] = {}
    matched_zones: dict[tuple[str, str], set[str]] = {}

    def register_municipality(properties: dict[str, Any]) -> tuple[str, str] | None:
        cadastral_code = str(properties.get("municipality_code") or "").strip().upper()
        raw_province = str(properties.get("province") or "").strip()
        if not cadastral_code or not raw_province:
            return None
        sigla = province_code(raw_province)
        province = province_name(sigla) if sigla else raw_province
        key = ((province or raw_province).casefold(), cadastral_code)
        municipalities.setdefault(key, {
            "province": province or raw_province,
            "municipality": str(properties.get("municipality") or "").strip(),
            "municipality_code": cadastral_code,
        })
        return key

    for properties in (*parcel_markers, *unmapped_parcels):
        if isinstance(properties, dict):
            register_municipality(properties)

    for feature in parcel_features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        key = register_municipality(properties)
        if key is None or not isinstance(feature.get("geometry"), dict):
            continue
        municipality = municipalities[key]
        zone = omi_zone_for_parcel(
            municipality["province"],
            municipality["municipality_code"],
            feature,
        )
        if zone:
            matched_zones.setdefault(key, set()).add(zone)

    if not municipalities:
        return []

    municipalities_by_province: dict[str, list[tuple[tuple[str, str], dict[str, Any]]]] = {}
    for key, municipality in municipalities.items():
        municipalities_by_province.setdefault(municipality["province"], []).append((key, municipality))

    selected: dict[str, dict[str, Any]] = {}
    for province, province_municipalities in municipalities_by_province.items():
        zone_features = omi_boundary_geometries(province)
        if not zone_features:
            continue
        zones_by_comune: dict[str, list[tuple[dict[str, Any], Any]]] = {}
        for zone_feature, zone_geometry in zone_features:
            zone_properties = zone_feature.get("properties") or {}
            code = str(zone_properties.get("cod_comune") or zone_properties.get("codcom") or "").strip().upper()
            if code:
                zones_by_comune.setdefault(code, []).append((zone_feature, zone_geometry))

        for municipality_key, municipality in province_municipalities:
            cadastral_code = municipality["municipality_code"]
            for zone_feature, zone_geometry in zones_by_comune.get(cadastral_code, []):
                source_properties = zone_feature.get("properties") or {}
                zone = omi_zone_code(source_properties)
                if not zone:
                    continue
                matches_owned_parcel = zone in matched_zones.get(municipality_key, set())

                geometry_json = json.dumps(zone_feature["geometry"], sort_keys=True, separators=(",", ":"))
                feature_key = hashlib.sha1(
                    f"{province}|{cadastral_code}|{zone}|{geometry_json}".encode("utf-8")
                ).hexdigest()
                vintage = "/".join(
                    str(source_properties.get(key) or "").strip()
                    for key in ("anno", "semestre")
                    if str(source_properties.get(key) or "").strip()
                )
                properties = {
                    "reference": f"Zona OMI {zone} · {municipality['municipality'] or cadastral_code}",
                    "omi_zone": zone,
                    "omi_feature_key": feature_key,
                    "municipality": municipality["municipality"],
                    "municipality_code": cadastral_code,
                    "province": province,
                    "boundary_vintage": vintage,
                    "matches_owned_parcel": matches_owned_parcel,
                }
                selected[feature_key] = {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": zone_feature["geometry"],
                }
    return list(selected.values())


async def _parcel_map_payload(lookup: dict[str, Any]) -> dict[str, Any]:
    """The JSON the parcel map script fetches: markers, parcel and sheet outlines, parcels without a position."""
    unmapped_parcels = [dict(item) for item in lookup.get("unmapped_parcel_items") or []]
    parcel_fit_points = []
    # Owned-parcel maps retain unmapped cadastral references for sheet matching, but must not place them
    # at a municipality centroid: that would look like an actual parcel position.
    centroids = await get_comune_centroids(item.get("municipality_code") for item in unmapped_parcels)
    if lookup.get("listed_only"):
        # Use approximate municipality points only to fit the viewport. They are deliberately
        # kept out of parcel markers and the unmapped parcel records shown to the user.
        fit_codes = {str(item.get("municipality_code") or "").strip().upper() for item in unmapped_parcels}
        for code in sorted(fit_codes):
            point = centroids.get(code)
            if point:
                parcel_fit_points.append({"latitude": point[0], "longitude": point[1]})
    else:
        for item in unmapped_parcels:
            point = centroids.get(str(item.get("municipality_code") or "").strip().upper())
            if point:
                item["latitude"], item["longitude"] = point
    sheet_features = lookup.get("sheet_features") or []
    parcel_features = lookup.get("parcel_features") or []
    parcel_markers = lookup.get("parcel_markers") or []
    omi_zone_features = (
        await run_in_threadpool(
            _omi_zone_features_for_properties,
            parcel_markers,
            parcel_features,
            lookup.get("unmapped_parcel_items") or [],
        )
        if lookup.get("listed_only") else []
    )
    return {
        "parcel_markers": parcel_markers,
        "parcel_features": {
            "type": "FeatureCollection",
            "features": parcel_features,
        },
        "sheet_features": {
            "type": "FeatureCollection",
            "features": sheet_features,
        },
        "omi_zone_features": {
            "type": "FeatureCollection",
            "features": omi_zone_features,
        },
        "unmapped_parcels": unmapped_parcels,
        "parcel_fit_points": parcel_fit_points,
    }


async def _subject_parcel_map_data(code: str, code_pattern: str, key: str | None, after: int):
    """Map data of a subject page without a document behind it, as increments (``after`` = chunks already drawn).

    The page names the data it was built with (``k``). If the service no longer has it (restart, eviction) the map is
    rebuilt from the subject's saved search, so a page left open never ends on "map not loaded". A lookup that failed
    answers an empty, finished map, not an error.
    """
    from fastapi import HTTPException

    normalized_code = code.strip().upper()
    if not re.fullmatch(code_pattern, normalized_code):
        raise HTTPException(status_code=404, detail="Soggetto non trovato")
    headers = {"Cache-Control": "no-store"}
    if not key or (key not in _HUB_MAP_DATA and key not in _HUB_MAP_TASKS):
        profile = await _subject_query_profile(normalized_code, [], [], 1)
        if profile is None:
            raise HTTPException(status_code=404, detail="Nessuna ricerca per soggetto archiviata")
        parcels = _hub_map_parcels(profile)
        if not parcels:
            return JSONResponse({**_empty_map_payload(), "next": 0, "done": True, "progress": 1.0}, headers=headers)
        key = _hub_map_key(parcels)
        if key not in _HUB_MAP_DATA and key not in _HUB_MAP_TASKS:
            _HUB_MAP_TASKS[key] = asyncio.create_task(_build_hub_map(key, parcels, _hub_data_url(profile, key)))
            await asyncio.sleep(0)  # let the task register its progress before the first poll
    return JSONResponse(await _hub_map_increment(key, max(0, after)), headers=headers)


@router.get("/persons/{fiscal_code}/parcel-map.json", response_class=JSONResponse)
async def web_person_parcel_map_data(fiscal_code: str, k: str | None = None, after: int = 0, user=Depends(_require_auth)):
    if not _is_admin(user):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Mappa non trovata")
    return await _subject_parcel_map_data(fiscal_code, r"[A-Z0-9]{16}", k, after)


@router.get("/companies/{fiscal_code}/parcel-map.json", response_class=JSONResponse)
async def web_company_parcel_map_data(fiscal_code: str, k: str | None = None, after: int = 0, user=Depends(_require_auth)):
    if not _is_admin(user):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Mappa non trovata")
    return await _subject_parcel_map_data(fiscal_code, r"\d{11}", k, after)


async def _backfill_document_metadata(base: Path, parsed_by_stem: dict) -> None:
    """Fill the unit, view subtype, reference date and archived XML text of documents that have none yet.

    A document counts as lacking metadata when it has neither a cadastral unit nor archived content; its file is found
    by name under SISTER_FILES_BASE (= base).
    """
    import asyncio

    from sqlalchemy import text

    from .database import DATABASE_ARCHIVE_SCHEMA, _get_session_factory, is_db_writable
    from .db_models import Document, DocumentContent
    from .reference import clean, ensure_lookup, get_or_create_cadastral_unit
    from .result_parsers import parse_it_date
    from .utils import _document_key, _parse_ispezione_ipotecaria_pdf, _parse_visura_xml, _persist_flattened_xml

    if not is_db_writable():
        return

    session_factory = _get_session_factory()

    # Older scans guessed these filenames as generic visure. Correct their type
    # even when their metadata already exists.
    async with session_factory() as session:
        await ensure_lookup(session, "document_type", "ispezione_ipotecaria")
        await ensure_lookup(session, "document_type", "elenco_immobili")
        await ensure_lookup(session, "document_type", "elenco_subalterni")
        await session.execute(
            text(
                "UPDATE document SET document_type = 'ispezione_ipotecaria' "
                "WHERE substr(lower(file_name), 1, 4) = 'isp_' "
                "AND document_type IS DISTINCT FROM 'ispezione_ipotecaria'"
            )
        )
        await session.execute(
            text(
                "UPDATE document SET document_type = 'elenco_subalterni' "
                "WHERE substr(lower(file_name), 1, 7) = 'el_sub_' "
                "AND document_type IS DISTINCT FROM 'elenco_subalterni'"
            )
        )
        await session.execute(
            text(
                "UPDATE document SET document_type = 'elenco_immobili' "
                "WHERE lower(file_name) LIKE '%.dat' "
                "AND document_type IS DISTINCT FROM 'elenco_immobili'"
            )
        )
        await session.commit()

    # Step 1: collect the documents that have no metadata yet.
    archive = f'"{DATABASE_ARCHIVE_SCHEMA}"'
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.file_name FROM document d"  # noqa: S608 (quoted schema name)
                    f" LEFT JOIN {archive}.document_content c ON c.document_id = d.id"
                    " WHERE d.cadastral_unit_id IS NULL AND c.document_id IS NULL"
                )
            )
        ).fetchall()

    if not rows:
        return

    # Step 2: build a filename → absolute path map from disk.
    name_to_path: dict[str, Path] = {}
    for fpath in base.rglob("*"):
        if fpath.is_file():
            name_to_path[fpath.name] = fpath

    backfilled = 0

    for doc_id, filename in rows:
        actual = name_to_path.get(filename)
        if actual is None:
            logger.debug("Backfill: file not found for doc %d (%s)", doc_id, filename)
            continue

        # Parse the file to extract metadata.
        stem = actual.stem
        parsed = parsed_by_stem.get(stem)
        if parsed is None and actual.suffix.lower() == ".pdf":
            parsed = parsed_by_stem.get(_document_key(stem))
        if actual.suffix.lower() == ".pdf" and actual.name.casefold().startswith("isp_"):
            parsed = await asyncio.to_thread(_parse_ispezione_ipotecaria_pdf, str(actual))
        elif parsed is None and actual.suffix.lower() in (".p7m", ".xml"):
            parsed = await asyncio.to_thread(_parse_visura_xml, str(actual))
            if parsed:
                parsed_by_stem[stem] = parsed

        if not parsed:
            parsed = {}

        foglio = parsed.get("foglio", "")
        particella = parsed.get("particella", "")
        xml_content = parsed.get("xml_content")

        if not (xml_content or foglio or particella):
            continue

        async with session_factory() as session:
            unit_id = await get_or_create_cadastral_unit(
                session,
                cadastre_type=parsed.get("tipo_catasto"),
                province=parsed.get("provincia"),
                municipality=parsed.get("comune"),
                sheet=foglio,
                parcel=particella,
                subunit=parsed.get("subalterno"),
                section=parsed.get("sezione_urbana"),
            )
            document = await session.get(Document, doc_id)
            document.cadastral_unit_id = unit_id
            document.view_subtype = clean(parsed.get("visura_subtype"))
            document.reference_date = parse_it_date(parsed.get("situazione_al"))
            session.add(document)
            if xml_content:
                session.add(DocumentContent(document_id=doc_id, content=xml_content.replace("\x00", "")))
            await session.flush()
            await _persist_flattened_xml(session, doc_id, xml_content)
            await session.commit()
        backfilled += 1

    if backfilled:
        logger.info("Rescan backfill: %d document(s) got their metadata", backfilled)


@router.post("/documents/rescan", response_class=HTMLResponse)
async def web_documents_rescan(request: Request, user=Depends(_require_admin)):
    """Scan the documents directory for files not yet indexed in the DB and register them."""
    started = datetime.now(timezone.utc)
    try:
        indexed = await _rescan_documents_dir()
    except Exception as exc:  # noqa: BLE001
        await record_activity(
            "documents.rescan",
            actor=_actor(user),
            status="error",
            error=str(exc) or exc.__class__.__name__,
            started_at=started,
            client_ip=_client_ip(request),
        )
        raise
    await record_activity(
        "documents.rescan",
        actor=_actor(user),
        result={"indexed": indexed},
        started_at=started,
        client_ip=_client_ip(request),
    )
    return RedirectResponse("/documents", status_code=303)


async def _rescan_documents_dir() -> int | None:
    """Index the files of the documents directory that are not in the DB yet.

    Returns how many new files were indexed (``None`` when the directory does not exist).
    """
    import asyncio

    from .database import get_indexed_file_paths, get_indexed_filenames
    from .utils import (
        _document_key,
        _parse_ispezione_ipotecaria_pdf,
        _parse_visura_pdf,
        _parse_visura_xml,
        _save_documents_to_db,
    )

    base = _files_base()
    if not base.exists():
        return None

    indexed_paths = set((await get_indexed_file_paths()).keys())
    indexed_names = await get_indexed_filenames()

    _EXT_FORMAT = {".pdf": "PDF", ".xml": "XML", ".p7m": "P7M", ".dat": "DAT"}
    _NAME_TYPE = [
        ("isp_", "ispezione_ipotecaria"),
        ("el_sub_", "elenco_subalterni"),
        ("vi_att_fab", "visura_fabbricati"),
        ("vi_sto_fab", "visura_fabbricati"),
        ("vi_att_ter", "visura_terreni"),
        ("vi_sto_ter", "visura_terreni"),
        ("vs_att", "visura_soggetto"),
        ("vs_sin", "visura_soggetto"),
        ("vs_sto", "visura_soggetto"),
        ("sogg", "visura_soggetto"),
        ("pnf", "visura_pnf"),
    ]

    def _guess_type_from_name(name: str) -> str:
        n = name.lower()
        for prefix, dtype in _NAME_TYPE:
            if prefix in n:
                return dtype
        return "visura"

    # Subdirectories whose contents should never be indexed (export artefacts, etc.)
    _excluded_prefixes = {str(base / "named") + "/"}

    def _excluded(p: Path) -> bool:
        s = str(p)
        return any(s.startswith(pfx) for pfx in _excluded_prefixes)

    # First pass: parse all P7M/XML files to build a stem → parsed_data map
    # so PDFs can inherit coordinates from their paired signed counterpart.
    parsed_by_stem: dict[str, dict] = {}
    for fpath in sorted(base.rglob("*")):
        if _excluded(fpath) or not fpath.is_file() or fpath.suffix.lower() not in (".p7m", ".xml"):
            continue
        stem = fpath.stem  # e.g. "DOC_123" from "DOC_123.p7m"
        if stem in parsed_by_stem:
            continue
        result = await asyncio.to_thread(_parse_visura_xml, str(fpath))
        if result:
            parsed_by_stem[stem] = result
            # PDFs pair without the request date (another request, possibly another day)
            parsed_by_stem.setdefault(_document_key(stem), result)

    new_docs = []
    for fpath in sorted(base.rglob("*")):
        if _excluded(fpath) or not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext not in _EXT_FORMAT:
            continue
        if str(fpath) in indexed_paths or fpath.name in indexed_names:
            continue

        fmt = _EXT_FORMAT[ext]
        parsed = None
        if ext in (".p7m", ".xml"):
            parsed = parsed_by_stem.get(fpath.stem)
            if parsed is None:
                parsed = await asyncio.to_thread(_parse_visura_xml, str(fpath))
            if fpath.name.casefold().startswith("isp_"):
                parsed = dict(parsed or {})
                parsed["tipo"] = "ispezione_ipotecaria"
        elif ext == ".pdf":
            # PDF: try to inherit metadata from a paired P7M/XML with the same stem (or the same without the date)
            parsed = parsed_by_stem.get(fpath.stem) or parsed_by_stem.get(_document_key(fpath.stem))
            if fpath.name.casefold().startswith("isp_"):
                parsed = await asyncio.to_thread(_parse_ispezione_ipotecaria_pdf, str(fpath))
            elif parsed is None:
                # No paired structured file — parse PDF content directly
                parsed = await asyncio.to_thread(_parse_visura_pdf, str(fpath))
        else:
            parsed = {}

        if ext == ".dat":
            parsed = dict(parsed or {})
            parsed["tipo"] = "elenco_immobili"
        elif fpath.name.casefold().startswith("el_sub_"):
            parsed = dict(parsed or {})
            parsed["tipo"] = "elenco_subalterni"

        if parsed is None:
            parsed = {"tipo": _guess_type_from_name(fpath.name)}

        new_docs.append(
            {
                "filename": fpath.name,
                "path": str(fpath),
                "file_format": fmt,
                "file_size": fpath.stat().st_size,
                "oggetto": None,
                "richiesta_del": None,
                "parsed_data": parsed,
            }
        )

    if new_docs:
        await _save_documents_to_db(new_docs)
        logger.info("Rescan: %d nuovi documenti indicizzati", len(new_docs))

    # Fill the metadata of existing docs that are missing it.
    await _backfill_document_metadata(base, parsed_by_stem)

    return len(new_docs)


_documents_import_lock = asyncio.Lock()


@router.post("/documents/import", response_class=JSONResponse)
async def web_documents_import(request: Request, force: bool = False, user=Depends(_require_admin)):
    """Index new files, then fill the typed tables from their XML (what ``sister db backfill xml`` does).

    Without ``force`` only documents that have no typed rows yet are read; with it every XML document is redone.
    One import at a time: a second request gets 409 instead of racing the first.
    """
    from . import xml_ingest

    audit = {"actor": _actor(user), "params": {"force": force}, "client_ip": _client_ip(request)}
    if _documents_import_lock.locked():
        await record_activity(
            "documents.import", status="rejected", error="importazione gia' in corso", **audit
        )
        return JSONResponse({"error": "Un'importazione e' gia' in corso"}, status_code=409)
    started = datetime.now(timezone.utc)
    async with _documents_import_lock:
        try:
            indexed = await _rescan_documents_dir()
            xml = await xml_ingest.backfill_documents(force=force)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import documenti non riuscito")
            message = str(exc) or exc.__class__.__name__
            await record_activity(
                "documents.import", status="error", error=message, started_at=started, **audit
            )
            return JSONResponse({"error": message}, status_code=500)
    logger.info("Import documenti: %s file indicizzati, XML %s (force=%s)", indexed, xml, force)
    await record_activity(
        "documents.import", result={"indexed": indexed, "xml": xml}, started_at=started, **audit
    )
    return JSONResponse({"indexed": indexed, "xml": xml, "force": force})


@router.get("/documents/purchased", response_class=HTMLResponse)
async def web_document_purchased(request: Request, user=Depends(_require_auth)):
    """Where Stripe Checkout returns after a payment (the webhook grants the access)."""
    template, _heading, icon, _public = "info_purchase_done.html", "", "fa-circle-check", False
    return _get_theme(request).render(
        template, request, user=user, page_heading="Pagamento ricevuto", page_icon=icon
    )


@router.get("/buy/{token}", response_class=HTMLResponse)
async def web_buy_document(request: Request, token: str, user=Depends(_require_auth)):
    """Purchase page opened from a signed link an admin created: one document, its price and the ways to pay."""
    from fastapi import HTTPException

    from . import purchases_billing
    from .database import get_document_by_id, has_document_purchase

    document_id = purchases_billing.document_id_from_token(token)
    doc = await get_document_by_id(document_id) if document_id else None
    if doc is None:
        raise HTTPException(status_code=404, detail="Link di acquisto non valido o scaduto")
    keys = _principal_keys(user)
    owned = _is_admin(user) or bool(keys and await has_document_purchase(keys, document_id))
    credits_on = purchases_billing.credits_enabled()
    balance = None
    if credits_on and keys:
        try:
            balance = await purchases_billing.credit_balance(_favorite_user_key(user) or keys[0])
        except Exception as exc:  # noqa: BLE001 - the page still works with cards if the wallet is unreachable
            logger.warning("Credit balance unavailable: %s", type(exc).__name__)
            credits_on = False
    return _get_theme(request).render(
        "buy.html",
        request,
        user=user,
        doc=doc,
        document_id=document_id,
        owned=owned,
        card_enabled=purchases_billing.billing_configured(),
        card_price=purchases_billing.document_price_label(),
        credits_enabled=credits_on,
        credit_cost=purchases_billing.credit_cost(),
        credit_balance=balance,
        error=request.query_params.get("error", ""),
    )


@router.post("/documents/{doc_id}/purchase")
@_audited("document.purchase.start")
async def web_document_purchase(request: Request, doc_id: int, user=Depends(_require_auth)):
    """Buy one document: ``method=card`` starts Stripe Checkout, ``method=credits`` pays from the prepaid wallet."""
    from fastapi import HTTPException

    from . import purchases_billing
    from .database import get_document_by_id, has_document_purchase

    form = await request.form()
    method = str(form.get("method") or "card")
    keys = _principal_keys(user)
    user_key = _favorite_user_key(user)
    if not keys or not user_key:
        raise HTTPException(status_code=403, detail="Account non identificabile")
    if await get_document_by_id(doc_id) is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if _is_admin(user) or await has_document_purchase(keys, doc_id):
        return RedirectResponse(url=f"/documents/{doc_id}", status_code=303)
    _purchase_audit(request, document_id=doc_id, user_id=user_key, reason=method)
    if method == "credits":
        if not purchases_billing.credits_enabled():
            raise HTTPException(status_code=503, detail="Pagamento con crediti non attivo")
        outcome = await purchases_billing.pay_with_credits(user_key, doc_id)
        if outcome == "insufficient":
            raise HTTPException(status_code=402, detail="Crediti insufficienti per questo documento")
        return RedirectResponse(url=f"/documents/{doc_id}", status_code=303)
    if not purchases_billing.billing_configured():
        raise HTTPException(status_code=503, detail="Pagamenti non configurati")
    try:
        url = await purchases_billing.start_document_checkout(request, user_key, getattr(user, "email", None), doc_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Stripe/network problems are shown as a clean 502, details stay in the log
        logger.error("Document checkout failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Pagamento non disponibile al momento") from None
    return RedirectResponse(url=url, status_code=303)


@router.post("/admin/credits/grant")
@_audited("admin.credits.grant")
async def web_admin_credits_grant(request: Request, user=Depends(_require_admin)):
    """Load prepaid credits on an account (the wallet is shared by the person's id:/email:/username: keys)."""
    from decimal import Decimal, InvalidOperation

    from . import purchases_billing
    from .database import PurchaseError

    if not purchases_billing.credit_grants_enabled():
        return _purchase_redirect(error="credits_off")
    form = await request.form()
    try:
        user_key = _purchase_identity(str(form.get("user_id", "")))
        try:
            amount = Decimal(str(form.get("amount", "")).replace(",", ".").strip())
        except InvalidOperation:
            raise PurchaseError("bad_amount") from None
        if not (Decimal("0") < amount <= Decimal("100000")):
            raise PurchaseError("bad_amount")
        reason = str(form.get("reason", "")).strip()[:200]
        _purchase_audit(request, user_id=user_key, reason=reason, amount=str(amount))
        balance = await purchases_billing.grant_credits(user_key, amount, reason, _actor(user))
    except PurchaseError as exc:
        _purchase_audit(request, error_code=exc.code)
        return _purchase_redirect(error=exc.code)
    return RedirectResponse(url=f"/admin/document-purchases?status=credits&balance={balance}", status_code=303)


@router.get("/documents", response_class=HTMLResponse)
@router.get("/documents/{path:path}", response_class=HTMLResponse)
async def web_documents(
    request: Request,
    path: str = "",
    template: str = "",
    view: str = "",
    page: int = 1,
    favorites: bool = False,
    user=Depends(_require_auth),
):
    """Documents hub — single-step query responses.

    Root (``/documents``) shows category, location, and file-oriented views of
    the same indexed document records. ``?view=map`` and ``?view=files`` select
    the matching layout. Pure-integer paths dispatch to the single-document
    viewer; other paths may still address a specific stored file.

    Query params:
        view: ``map`` to select the location tab or ``files`` to select the file-oriented tab.
        template: force a viewer template, e.g. ``?template=result_detail`` for
            the exhaustive field-by-field view.
        page: one-based page number for the document hub's newest-first list.
    """

    theme = _get_theme(request)

    # Normalize trailing slash (e.g. /documents/21/ → "21")
    path = path.strip("/")

    # Integer path → DB-backed structured single-document viewer
    if path and path.isdigit():
        doc = await get_document_by_id(int(path))
        if doc is None:
            from fastapi import HTTPException

            # Same answer as a document the caller may not see: never a 200 for "not found".
            raise HTTPException(status_code=404, detail="Document not found")
        await _require_document_access(user, int(path))
        if not template and (doc.get("document_type") or "") == "visura_soggetto":
            fiscal_code = _document_subject_fiscal_code(doc)
            if not fiscal_code:
                try:
                    fiscal_code = await get_subject_fiscal_code_for_document(int(path))
                except Exception as exc:  # noqa: BLE001 - older databases may not have populated the subject index.
                    logger.debug("Subject identity unavailable for document %s: %s", path, type(exc).__name__)
            if fiscal_code:
                route = "/companies" if len(fiscal_code) == 11 and fiscal_code.isdigit() else "/persons"
                return RedirectResponse(url=f"{route}/{fiscal_code}", status_code=307)
        # Planimetria P7M: render interactive GeoJSON map viewer when possible.
        if (
            not template
            and (doc.get("document_type") or "") in ("planimetria", "elaborato_planimetrico")
            and (doc.get("file_format") or "").upper() == "P7M"
            and doc.get("file_path")
        ):
            fp = Path(doc["file_path"])
            if fp.is_file():
                gj_result = _read_planimetria_geojson(str(fp))
                if gj_result is not None:
                    map_lookup = await _google_maps_context(doc)
                    await _load_document_favorite_state(doc, user)
                    return theme.render(
                        "planimetria_land_map.html",
                        request,
                        user=user,
                        doc=doc,
                        doc_id=int(path),
                        epsg=gj_result["epsg"],
                        proj4=gj_result["proj4"],
                        crs_text=gj_result["crs_text"],
                        n_features=len((gj_result.get("geojson") or {}).get("features", [])),
                        has_geojson=gj_result.get("geojson") is not None,
                        fmt=gj_result.get("fmt", "unknown"),
                        zip_files=gj_result.get("files", []),
                        land_registry_map=await _land_registry_map_context(map_lookup),
                    )
                # P7M not extractable — fall through to direct download
                return FileResponse(str(fp), filename=fp.name, media_type="application/octet-stream")
        if not template and (doc.get("document_type") or "") == "ispezione_ipotecaria" and doc.get("file_path"):
            from .utils import _parse_ispezione_ipotecaria_pdf

            source = Path(doc["file_path"])
            if not source.is_file() and doc.get("filename"):
                candidate = _files_base() / doc["filename"]
                if candidate.is_file():
                    source = candidate
            group_key = _ispezione_group_key(source.name)
            siblings = []
            if source.parent.is_dir():
                siblings = sorted(
                    (
                        sibling
                        for sibling in source.parent.iterdir()
                        if sibling.is_file()
                        and sibling.suffix.casefold() == ".pdf"
                        and sibling.name.casefold().startswith("isp_")
                        and _ispezione_group_key(sibling.name) == group_key
                    ),
                    key=lambda sibling: (
                        sibling.resolve() != source.resolve(),
                        "_rp" in sibling.stem.casefold(),
                        sibling.name.casefold(),
                    ),
                )
            if source.suffix.casefold() == ".pdf" and source.is_file() and source not in siblings:
                siblings.insert(0, source)

            from .database import get_indexed_file_paths

            indexed_paths = await get_indexed_file_paths()
            id_by_path = {
                Path(indexed_path).resolve(): indexed_id
                for indexed_path, indexed_id in indexed_paths.items()
                if indexed_path
            }
            visible_document_ids = await _visible_document_ids(user)
            if visible_document_ids is not None:
                siblings = [
                    sibling
                    for sibling in siblings
                    if (
                        int(doc["id"])
                        if sibling.resolve() == source.resolve()
                        else id_by_path.get(sibling.resolve())
                    )
                    in visible_document_ids
                ]

            async def _parse_sibling(path: Path) -> dict:
                parsed = await asyncio.to_thread(_parse_ispezione_ipotecaria_pdf, str(path))
                file_id = doc.get("id") if path.resolve() == source.resolve() else id_by_path.get(path.resolve())
                return {
                    "filename": path.name,
                    "id": file_id,
                    "parsed": parsed,
                    "file_url": (
                        f"/documents/{file_id}/view" if file_id else None
                    ),
                    "download_url": f"/documents/{file_id}/download" if file_id else None,
                }

            inspection_documents = await asyncio.gather(*(_parse_sibling(path) for path in siblings))
            ocular_extraction = await get_document_structured_extraction(int(doc["id"]))
            map_lookup = await _google_maps_context(doc)
            await _load_document_favorite_state(doc, user)
            return theme.render(
                "catasto/entity.html",
                request,
                user=user,
                doc=doc,
                inspection_documents=inspection_documents,
                ocular_extraction=ocular_extraction,
                land_registry_map=await _land_registry_map_context(map_lookup),
                request_id=str(doc.get("id") or doc.get("filename") or ""),
                **_land_map_layout("ipotecaria"),
            )
        return await _render_doc_from_db(doc, request, theme, user, force_template=template or None)

    # The cadastral map remains a separate experience. All document browser
    # views share the tabbed documents hub below.
    if not path and view == "cadastral-map":
        files = await _load_visible_documents(user)
        logical = _collapse_to_logical_docs(files)
        return theme.render(
            "documents_map.html",
            request,
            user=user,
            total=len(logical),
            n_files=len(files),
            land_registry_api_base="/land-registry/api",
            land_registry_static_base="/land-registry/static",
        )

    if path:
        base = _files_base()
        target = (base / path).resolve()
        # Keep direct file URLs available, while deriving all three browser
        # views solely from indexed database records.
        if target != base and base not in target.parents:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Access denied")
        if target.is_file():
            await _require_document_path_access(user, target)
            return FileResponse(str(target), filename=target.name)
        if not target.is_dir():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Not found")

    active_view = (
        "files"
        if path or view == "files"
        else "location"
        if view in ("map", "tree")
        else "categories"
    )
    favorite_user = _favorite_user_key(user)
    favorites_only = bool(favorites and favorite_user)
    favorite_document_ids: set[int] = set()
    # Favorite views need only favorite rows and their alternate-format
    # siblings; cross-page links are loaded from normalized relations below.
    if favorites_only:
        from .database import get_document_favorite_ids

        favorite_document_ids = await get_document_favorite_ids(favorite_user)
        document_files = await get_document_group_siblings(favorite_document_ids)
        document_files = await _filter_purchased_documents(user, document_files)
        raw_document_file_count = len(document_files)
        logical = _collapse_to_logical_docs(document_files)
        del document_files
    else:
        visible_document_ids = await _visible_document_ids(user)
        if visible_document_ids is None:
            # Keep only one logical page in memory. The full-catalog grouping
            # index is disk-backed so exact totals and file joins remain intact.
            (
                page_documents,
                page,
                page_count,
                page_offset,
                total,
                raw_document_file_count,
            ) = await _page_streamed_documents(iter_all_documents(), page)
            logical = None
        else:
            document_files = await get_documents_by_ids(visible_document_ids)
            raw_document_file_count = len(document_files)
            logical = _collapse_to_logical_docs(document_files)
            del document_files
    if favorite_user and not favorites_only:
        from .database import get_document_favorite_ids

        favorite_document_ids = await get_document_favorite_ids(favorite_user)
    if logical is None:
        for document in page_documents:
            logical_file_ids = {file.get("id") for file in document.get("files") or []}
            if document.get("id") is not None:
                logical_file_ids.add(document["id"])
            document["is_favorite"] = bool(logical_file_ids & favorite_document_ids)
        document_file_count = raw_document_file_count
    else:
        for document in logical:
            logical_file_ids = {file.get("id") for file in document.get("files") or []}
            if document.get("id") is not None:
                logical_file_ids.add(document["id"])
            document["is_favorite"] = bool(logical_file_ids & favorite_document_ids)
        if favorites_only:
            logical = [document for document in logical if document.get("is_favorite")]
        document_file_count = (
            sum(document.get("n_files") or 1 for document in logical)
            if favorites_only
            else raw_document_file_count
        )
        page_documents, page, page_count, page_offset = _paginate_documents(logical, page)
    tree = _build_document_tree(page_documents) if active_view == "categories" else []
    page_file_id_to_logical_id: dict[int, int] = {}
    for document in page_documents:
        logical_id = document.get("id")
        if logical_id is None:
            continue
        page_file_id_to_logical_id[int(logical_id)] = int(logical_id)
        for file in document.get("files") or []:
            file_id = file.get("id")
            if file_id is not None:
                page_file_id_to_logical_id[int(file_id)] = int(logical_id)

    page_subject_codes = {
        code
        for document in page_documents
        if (document.get("document_type") or "") == "visura_soggetto"
        if (code := _subject_visura_fiscal_code(document))
    }
    owner_codes_by_file_id = await get_document_owner_fiscal_codes(
        page_file_id_to_logical_id,
        fiscal_codes=page_subject_codes,
    )
    owner_fiscal_codes_by_document_id: dict[int, list[str]] = {}
    property_docs_by_owner_cf: dict[str, dict[int, dict[str, Any]]] = {}
    page_logical_by_id = {
        int(document["id"]): document for document in page_documents if document.get("id") is not None
    }
    property_file_ids_to_resolve = (
        {int(file_id) for file_id in owner_codes_by_file_id} if page_subject_codes else set()
    )
    subject_codes_to_resolve: set[str] = set()

    for file_id, fiscal_codes in owner_codes_by_file_id.items():
        file_id = int(file_id)
        page_logical_id = page_file_id_to_logical_id.get(file_id)
        page_logical_doc = page_logical_by_id.get(page_logical_id) if page_logical_id is not None else None
        if page_logical_doc and (page_logical_doc.get("document_type") or "") in _VISURA_TYPES:
            combined = owner_fiscal_codes_by_document_id.setdefault(page_logical_id, [])
            for fiscal_code in fiscal_codes:
                if fiscal_code not in combined:
                    combined.append(fiscal_code)

    for document in page_documents:
        if (document.get("document_type") or "") not in _VISURA_TYPES:
            continue
        subject_codes_to_resolve.update(_legacy_document_owner_fiscal_codes(document))
        document_id = document.get("id")
        if document_id is not None:
            subject_codes_to_resolve.update(
                str(code).strip().upper()
                for code in owner_fiscal_codes_by_document_id.get(int(document_id), [])
                if code and str(code).strip()
            )

    # Load only visible logical groups related to this page, rather than
    # indexing every logical file in the catalogue a second time.
    owner_subject_docs = []
    if subject_codes_to_resolve:
        owner_subject_ids = await get_subject_document_ids_for_fiscal_codes(subject_codes_to_resolve)
        owner_subject_groups = await _load_visible_logical_document_groups(user, owner_subject_ids)
        owner_subject_docs = [
            document
            for document in owner_subject_groups
            if (document.get("document_type") or "") == "visura_soggetto"
            and _subject_visura_fiscal_code(document) in subject_codes_to_resolve
        ]

    property_doc_by_file_id: dict[int, dict[str, Any]] = {}
    if property_file_ids_to_resolve:
        related_property_groups = await _load_visible_logical_document_groups(
            user, property_file_ids_to_resolve
        )
        for document in related_property_groups:
            if (document.get("document_type") or "") not in _VISURA_TYPES:
                continue
            document_id = document.get("id")
            if document_id is not None:
                property_doc_by_file_id[int(document_id)] = document
            for file in document.get("files") or []:
                file_id = file.get("id")
                if file_id is not None:
                    property_doc_by_file_id[int(file_id)] = document

    if page_subject_codes:
        for file_id, fiscal_codes in owner_codes_by_file_id.items():
            property_doc = property_doc_by_file_id.get(int(file_id))
            if not property_doc:
                continue
            property_id = property_doc.get("id")
            if property_id is None:
                continue
            for fiscal_code in fiscal_codes:
                if fiscal_code in page_subject_codes:
                    property_docs_by_owner_cf.setdefault(fiscal_code, {})[int(property_id)] = {
                        "id": int(property_id),
                        "label": (
                            property_doc.get("display_name")
                            or property_doc.get("oggetto")
                            or property_doc.get("filename")
                            or f"Doc {property_id}"
                        ),
                    }

    _link_related_visura_docs(
        page_documents,
        visible_subject_docs=owner_subject_docs,
        owner_fiscal_codes_by_document_id=owner_fiscal_codes_by_document_id,
        property_docs_by_owner_cf={
            code: list(properties.values()) for code, properties in property_docs_by_owner_cf.items()
        },
    )

    if active_view == "location":
        prop_map = _build_property_map(
            page_documents,
            owner_subject_docs=owner_subject_docs,
            owner_fiscal_codes_by_document_id=owner_fiscal_codes_by_document_id,
        )
    else:
        prop_map = {"immobili": [], "soggetti": [], "altri": []}
    if logical is not None:
        total = len(logical)
        del logical
    document_page_start = page_offset + 1 if page_documents else 0
    document_page_end = page_offset + len(page_documents)
    return theme.render(
        "documents.html",
        request,
        user=user,
        tree=tree,
        prop_map=prop_map,
        total=total,
        document_file_count=document_file_count,
        active_view=active_view,
        files=page_documents,
        favorites_enabled=bool(favorite_user),
        favorites_only=favorites_only,
        page=page,
        page_count=page_count,
        page_size=_DOCUMENTS_PAGE_SIZE,
        page_start=document_page_start,
        page_end=document_page_end,
    )


# Document families shown for every parcel of the hub, in display order: (key, label, icon, document types)
_PARCEL_DOCUMENT_KINDS = (
    ("visure", "Visure", "fa-file-lines", {"visura_fabbricati", "visura_terreni", "visura", "elenco_subalterni"}),
    ("planimetrie", "Planimetrie", "fa-ruler-combined", {"planimetria", "elaborato_planimetrico", "epa"}),
    ("mappe", "Mappe", "fa-map", {"estratto_mappa", "mappa", "fiduciali", "export_mappa", "originali"}),
    ("ispezioni", "Ispezioni ipotecarie", "fa-landmark", {"ispezione_ipotecaria"}),
)
_PARCEL_REQUEST_KINDS = (
    ("ispezioni_catastali", "Ispezioni catastali", "fa-magnifying-glass", {"ispezioni", "ispezioni_cart", "ispezioni_cartacee"}),
    ("mappe", "Mappe", "fa-map", {"mappa", "export_mappa", "fiduciali"}),
)

_HUB_MISSING_LISTED = 150  # parcels named per kind in the "still to request" lists (the counts are always complete)
_HUB_OWNERSHIP_DOCUMENTS = 150  # visure parsed for the parcels of one hub page (each is a P7M read in memory)
_HUB_PAGE_PARCELS = 100_000  # every parcel of the query is listed (the comuni are folded accordions); `?page=` still works


def _hub_document(item: dict) -> dict:
    """The few fields the hub needs from a logical document."""
    formats = sorted({f.get("file_format") for f in item.get("files") or [] if f.get("file_format")})
    return {
        "id": item.get("id"),
        "title": item.get("display_name") or item.get("oggetto") or item.get("filename") or f"Documento {item.get('id')}",
        "type": item.get("document_type") or "",
        "date": item.get("situazione_al") or item.get("richiesta_del") or "",
        "subunit": item.get("subalterno") or "",
        "formats": formats,
    }


def _hub_documents(ids: set[int], indexed_by_id: dict[int, dict]) -> list[dict]:
    rows = [dict(indexed_by_id[i]) for i in ids if i in indexed_by_id]
    return [_hub_document(item) for item in _collapse_to_logical_docs(rows)]


def _hub_inspection_extraction(extraction: dict | None) -> dict | None:
    """Readable mortgage extraction details for the person and parcel views."""
    if not extraction:
        return None
    inspection = extraction.get("inspection") or {}
    title = extraction.get("title") or {}
    lien = extraction.get("lien") or {}
    note_type = str(inspection.get("note_type") or "").strip().casefold()
    kind_label = {
        "trascrizione": "Nota di trascrizione",
        "iscrizione": "Nota di iscrizione",
        "annotazione": "Nota di annotazione",
    }.get(note_type, "Ispezione ipotecaria")
    registration = " · ".join(
        part
        for part in (
            f"RG {inspection['registro_generale']}" if inspection.get("registro_generale") else "",
            f"RP {inspection['registro_particolare']}" if inspection.get("registro_particolare") else "",
        )
        if part
    )
    presentation = " · ".join(
        str(part)
        for part in (
            f"Presentazione {inspection['presentazione_numero']}" if inspection.get("presentazione_numero") else "",
            inspection.get("presentazione_data") or "",
        )
        if part
    )
    properties = []
    for unit in extraction.get("units") or []:
        for prop in unit.get("properties") or []:
            location = prop.get("municipality") or ""
            cadastre = prop.get("cadastre_type") or ""
            identifiers = []
            if prop.get("urban_section"):
                identifiers.append(f"Sez. {prop['urban_section']}")
            if prop.get("sheet") is not None:
                identifiers.append(f"Fg {prop['sheet']}")
            if prop.get("parcel") is not None:
                identifiers.append(f"P.lla {prop['parcel']}")
            if prop.get("subunit") is not None:
                identifiers.append(f"Sub {prop['subunit']}")
            reference = " · ".join(part for part in (location, cadastre, " · ".join(identifiers)) if part)
            properties.append(
                {
                    "reference": reference,
                    "address": " ".join(part for part in (prop.get("address"), prop.get("civic_number")) if part),
                    "nature": prop.get("nature_description") or prop.get("nature") or "",
                    "room_count": prop.get("room_count"),
                    "area_sqm": prop.get("area_sqm"),
                    "floor": prop.get("floor") or "",
                }
            )
    parties = []
    for party in extraction.get("parties") or []:
        name = party.get("entity_name") or " ".join(
            part for part in (party.get("given_name"), party.get("surname")) if part
        )
        parties.append(
            {
                "name": name or party.get("fiscal_code") or "Soggetto senza nominativo",
                "role": {"a_favore": "A favore", "contro": "Contro"}.get(
                    party.get("role"), party.get("role") or "Parte"
                ),
                "fiscal_code": party.get("fiscal_code") or "",
                "quality": party.get("quality") or "",
                "right": party.get("right_type") or "",
                "share": party.get("raw_share") or "",
            }
        )
    return {
        "id": (extraction.get("extraction") or {}).get("id"),
        "kind_label": kind_label,
        "registration": registration,
        "presentation": presentation,
        "inspection_date": inspection.get("inspection_date") or "",
        "office": inspection.get("office") or "",
        "property_count": len(properties),
        "properties": properties,
        "parties": parties,
        "party_count": len(parties),
        "party_favore_count": inspection.get("party_favore_count") or 0,
        "party_contro_count": inspection.get("party_contro_count") or 0,
        "title_type": title.get("title_type") or "",
        "title_description": title.get("description") or "",
        "title_date": title.get("title_date") or "",
        "title_repertory_number": title.get("repertory_number") or "",
        "title_notary": title.get("notary_name") or "",
        "lien_type": lien.get("lien_type") or "",
        "lien_principal_euro": format_amount_it(lien.get("principal_euro")),
        "lien_total_euro": format_amount_it(lien.get("total_euro")),
        "lien_interest_euro": format_amount_it(lien.get("interest_euro")),
        "lien_expenses_euro": format_amount_it(lien.get("expenses_euro")),
        "lien_annual_interest_rate": lien.get("annual_interest_rate") or "",
        "lien_duration": lien.get("duration_description")
        or (f"{lien['duration_years']} anni" if lien.get("duration_years") else ""),
        "lien_rank": lien.get("mortgage_rank"),
    }


def _subject_cadastral_group_usage(profile: dict | None) -> dict[int, list[str]]:
    """Why a document's data is actually rendered inside the current cadastral groups."""
    usage: dict[int, set[str]] = {}
    if not profile:
        return {}
    for parcel in profile.get("map_parcels") or []:
        for ownership in parcel.get("ownership") or []:
            document_id = ownership.get("document_id")
            if document_id is not None:
                usage.setdefault(int(document_id), set()).add(
                    "Dati catastali e titolarità dalla visura per immobile"
                )
        for document in (parcel.get("documents") or {}).get("ispezioni") or []:
            if document.get("id") is not None and document.get("extraction"):
                usage.setdefault(int(document["id"]), set()).add(
                    "Estrazione di ispezione o nota mostrata nella scheda della particella"
                )
    return {document_id: sorted(labels) for document_id, labels in usage.items()}


_SUBJECT_ARCHIVE_CATEGORIES = (
    ("subject_visure", "Visure per soggetto", "fa-id-card", {"visura_soggetto"}),
    (
        "property_visure",
        "Visure per immobile ed elenchi",
        "fa-file-lines",
        {"visura", "visura_fabbricati", "visura_terreni", "visura_storica", "elenco_immobili", "elenco_subalterni"},
    ),
    ("mortgage", "Ispezioni ipotecarie", "fa-landmark", {"ispezione_ipotecaria"}),
    ("plans", "Planimetrie ed elaborati", "fa-ruler-combined", {"planimetria", "elaborato_planimetrico", "epa"}),
    ("maps", "Mappe e fiduciali", "fa-map", {"estratto_mappa", "mappa", "fiduciali", "export_mappa", "originali"}),
)

_SUBJECT_ARCHIVE_SOURCE_LABELS = {
    "subject": "Soggetto del documento",
    "subject_request": "Richiesta per soggetto",
    "legacy_identity": "Codice nel documento",
    "ownership": "Titolarità",
    "mortgage_party": "Parte nell'ispezione",
    "property": "Immobile del soggetto",
}


def _subject_archive_property_label(prop: dict) -> str:
    location = " · ".join(value for value in (prop.get("municipality"), prop.get("province")) if value)
    identifiers = []
    if prop.get("section"):
        identifiers.append(f"Sez. {prop['section']}")
    if prop.get("sheet"):
        identifiers.append(f"Fg {prop['sheet']}")
    if prop.get("parcel"):
        identifiers.append(f"P.lla {prop['parcel']}")
    if prop.get("subunit"):
        identifiers.append(f"Sub {prop['subunit']}")
    return " · ".join(part for part in (location, " · ".join(identifiers)) if part)


async def _load_subject_profile_files(
    code: str,
    seed_document_ids: set[int],
    allowed_document_ids: set[int] | None,
    *,
    associations: dict[int, dict[str, Any]] | None = None,
) -> tuple[dict[int, dict[str, Any]], list[dict]]:
    """Load only documents linked to this subject and visible logical-file siblings."""
    if associations is None:
        associations = await find_subject_document_associations(code)
    if allowed_document_ids is not None:
        associations = {
            document_id: value
            for document_id, value in associations.items()
            if document_id in allowed_document_ids
        }

    document_ids = set(associations) | {int(document_id) for document_id in seed_document_ids}
    if allowed_document_ids is not None:
        document_ids &= allowed_document_ids
    rows, siblings = await asyncio.gather(
        get_documents_by_ids(document_ids),
        get_document_group_siblings(document_ids),
    )
    rows.extend(siblings)
    visible_rows = {
        int(item["id"]): item
        for item in rows
        if item.get("id") is not None
        and (allowed_document_ids is None or int(item["id"]) in allowed_document_ids)
    }
    return associations, list(visible_rows.values())


async def _subject_visura_candidates(
    code: str, subject_document_ids: set[int], indexed_files: list[dict]
) -> list[dict]:
    """Build the subject's visura list with one bounded metadata read for any missing index rows."""
    candidates = [
        dict(item)
        for item in indexed_files
        if (item.get("document_type") or "") == "visura_soggetto"
        and (item.get("id") in subject_document_ids or _extract_cf(item) == code)
    ]
    for item in candidates:
        item["queried_fiscal_code"] = code
    candidate_ids = {int(item["id"]) for item in candidates if item.get("id") is not None}
    missing_ids = subject_document_ids - candidate_ids
    if missing_ids:
        for item in await get_documents_by_ids(missing_ids):
            if (item.get("document_type") or "") == "visura_soggetto":
                candidates.append(
                    {
                        **item,
                        "response_id": None,
                        "visura_subtype": None,
                        "situazione_al": None,
                        "queried_fiscal_code": code,
                    }
                )
    return candidates


async def _load_subject_asset_documents(peer_documents: list[dict]) -> list[dict]:
    """Fetch peer XML visure together, preserving the selected profile's document order."""
    document_ids = [int(item["id"]) for item in peer_documents if item.get("id") is not None]
    if not document_ids:
        return []
    rows = await get_documents_by_ids(document_ids, include_content=True)
    rows_by_id = {int(item["id"]): item for item in rows if item.get("id") is not None}
    return [
        rows_by_id[document_id]
        for document_id in document_ids
        if document_id in rows_by_id and rows_by_id[document_id].get("xml_content")
    ]


async def _load_subject_profile_indexes(code: str) -> tuple[set[int], dict[int, dict[str, Any]]]:
    """Load the subject-visura IDs and full subject associations concurrently."""

    async def subject_visura_ids() -> set[int]:
        try:
            return await get_subject_document_ids(code)
        except Exception as exc:  # noqa: BLE001 - association-based navigation remains available without this index.
            logger.debug("Subject document index unavailable: %s", type(exc).__name__)
            return set()

    return await asyncio.gather(subject_visura_ids(), find_subject_document_associations(code))


async def _subject_document_archive(
    code: str,
    indexed_files: list[dict],
    cadastral_group_usage: dict[int, list[str]] | None = None,
    allowed_document_ids: set[int] | None = None,
    associations: dict[int, dict[str, Any]] | None = None,
) -> dict:
    """Exhaustive, provenance-labelled document inventory for one entity page.

    ``_subject_query_profile`` remains the parcel-oriented view of the newest
    subject search.  This inventory instead starts from all normalized subject,
    ownership, inspection and historical-property associations, then selects
    complete logical document groups so alternate P7M/XML/PDF files remain
    individually accessible.
    """
    associations = associations if associations is not None else await find_subject_document_associations(code)
    if allowed_document_ids is not None:
        associations = {document_id: value for document_id, value in associations.items() if document_id in allowed_document_ids}
    complete_indexed = [dict(item) for item in indexed_files]
    indexed_ids = {int(item["id"]) for item in complete_indexed if item.get("id") is not None}
    missing_associated_ids = set(associations) - indexed_ids
    if missing_associated_ids:
        complete_indexed.extend(await get_documents_by_ids(missing_associated_ids))
    all_logical = _collapse_to_logical_docs(complete_indexed)
    physical_by_id = {int(item["id"]): item for item in complete_indexed if item.get("id") is not None}
    related_ids = set(associations)
    cadastral_group_usage = cadastral_group_usage or {}

    subject_logical: list[tuple[dict, set[int]]] = []
    subject_group_file_ids: set[int] = set()
    for logical in all_logical:
        file_ids = {int(item["id"]) for item in logical.get("files") or [] if item.get("id") is not None}
        if file_ids & related_ids:
            subject_logical.append((logical, file_ids))
            subject_group_file_ids.update(file_ids)

    # Extraction presence is displayed only for the related logical groups.
    # Querying it for every indexed document made the first subject-profile
    # load scale with the entire catalogue even though unrelated results were
    # never rendered.
    extracted_ids = await find_documents_with_extraction(subject_group_file_ids)
    documents = []
    for logical, file_ids in subject_logical:
        sources: set[str] = set()
        properties: list[dict] = []
        for document_id in file_ids:
            association = associations.get(document_id) or {}
            sources.update(association.get("sources") or set())
            for prop in association.get("properties") or []:
                if prop not in properties:
                    properties.append(prop)

        # Ownership links can survive after a property disappeared from the newest saved search.
        # Retain the document's own location in that case so the historical asset is still identifiable.
        if not properties and "ownership" in sources and (logical.get("foglio") or logical.get("particella")):
            properties.append(
                {
                    "province": logical.get("provincia") or "",
                    "municipality": logical.get("comune") or "",
                    "section": logical.get("sezione_urbana") or "",
                    "sheet": logical.get("foglio") or "",
                    "parcel": logical.get("particella") or "",
                    "subunit": logical.get("subalterno") or "",
                }
            )

        files = []
        for collapsed_file in logical.get("files") or []:
            document_id = int(collapsed_file["id"])
            physical = physical_by_id.get(document_id) or {}
            file_path = physical.get("file_path")
            available = bool(file_path and Path(file_path).is_file())
            files.append(
                {
                    **collapsed_file,
                    "id": document_id,
                    "available": available,
                    "view_url": f"/documents/{document_id}/view" if available else "",
                    "download_url": f"/documents/{document_id}/download" if available else "",
                }
            )

        document_type = logical.get("document_type") or ""
        group_usage_labels = sorted(
            {
                label
                for document_id in file_ids
                for label in cadastral_group_usage.get(document_id, [])
            }
        )
        category = next(
            (key for key, _label, _icon, types in _SUBJECT_ARCHIVE_CATEGORIES if document_type in types),
            "other",
        )
        property_labels = sorted(
            {label for label in (_subject_archive_property_label(prop) for prop in properties) if label}
        )
        documents.append(
            {
                "id": int(logical["id"]),
                "title": logical.get("display_name") or logical.get("oggetto") or logical.get("filename") or f"Documento {logical['id']}",
                "type": document_type,
                "date": logical.get("situazione_al") or logical.get("richiesta_del") or logical.get("created_at") or "",
                "category": category,
                "source_labels": [
                    label for source, label in _SUBJECT_ARCHIVE_SOURCE_LABELS.items() if source in sources
                ],
                "property_labels": property_labels,
                "has_extraction": bool(file_ids & extracted_ids),
                "used_in_cadastral_groups": bool(group_usage_labels),
                "cadastral_group_usage_labels": group_usage_labels,
                "files": sorted(files, key=lambda item: item["id"]),
            }
        )

    documents.sort(key=lambda item: item["id"], reverse=True)
    category_specs = [
        (key, label, icon) for key, label, icon, _types in _SUBJECT_ARCHIVE_CATEGORIES
    ] + [("other", "Altri documenti", "fa-folder-open")]
    categories = [
        {
            "key": key,
            "label": label,
            "icon": icon,
            "documents": [document for document in documents if document["category"] == key],
        }
        for key, label, icon in category_specs
    ]
    categories = [category for category in categories if category["documents"]]
    file_count = sum(len(document["files"]) for document in documents)
    available_file_count = sum(
        1 for document in documents for file in document["files"] if file["available"]
    )
    return {
        "categories": categories,
        "document_count": len(documents),
        "file_count": file_count,
        "available_file_count": available_file_count,
        "missing_file_count": file_count - available_file_count,
    }


def _hub_request(item: dict) -> dict:
    request_type = str(item.get("request_type") or "").strip().casefold().replace("-", "_")
    label = {
        "ispezioni": "Ispezione catastale",
        "ispezioni_cart": "Ispezione catastale cartacea",
        "ispezioni_cartacee": "Ispezione catastale cartacea",
    }.get(request_type, request_type_info(request_type).label)
    return {
        "request_id": item.get("request_id"),
        "type": request_type,
        "label": label,
        "date": _format_timestamp(item.get("responded_at") or item.get("requested_at")),
        "success": item.get("success"),
        "total_results": item.get("total_results"),
    }


def _hub_owner(owner: dict, own_code: str) -> dict:
    return {
        "name": owner.get("name") or "-",
        "fiscal_code": owner.get("fiscal_code") or "",
        "right": owner.get("right") or "",
        "quota": owner.get("quota") or "",
        "period": (owner.get("period") or {}).get("label") or "",
        "birth": " · ".join(x for x in (owner.get("birth_date"), owner.get("birth_place") or owner.get("registered_office")) if x),
        "is_subject": bool(own_code) and (owner.get("fiscal_code") or "").upper() == own_code,
    }


async def _hub_parcel_ownership(documents: list[dict], own_code: str, budget: list[int]) -> list[dict]:
    """Full ownership (current owners + acts) from the newest visura of each subalterno of a parcel.

    ``documents`` are the hub's logical documents of the parcel's visure; ``budget`` is the shared count of visure
    that may still be parsed for this page.
    """
    from decimal import Decimal

    from .cadastral_values import building_cadastral_value, land_cadastral_value
    from .land_registry_kpis import _format_currency_it

    newest: dict[str, dict] = {}
    for d in documents:  # newest first
        newest.setdefault(d["subunit"] or "", d)
    out = []
    for subunit, d in newest.items():
        if budget[0] <= 0:
            break
        budget[0] -= 1
        doc = await get_document_by_id(int(d["id"]))
        view = build_visura_view((doc or {}).get("xml_content", ""))
        if not view:
            continue
        units = view.get("units") or []
        unit = next((u for u in units if (u.get("identifier") or {}).get("subunit", "") == subunit), units[0] if units else {})
        acts = [
            {
                "type": (m.get("act") or {}).get("type") or (m.get("act") or {}).get("text") or "",
                "date": (m.get("act") or {}).get("date") or "",
                "current": bool(m.get("current")),
                "start": m.get("start") or "",
                "end": m.get("end") or "",
                "owners": [_hub_owner(o, own_code) for o in m.get("owners") or []],
            }
            for m in view.get("ownership") or []
        ]
        classification = unit.get("classification") or {}
        totals = unit.get("totals") or {}
        raw_consistency = classification.get("consistency") or ""
        consistency_value, consistency_unit = parse_consistency(raw_consistency)
        consistency_sqm = format_amount_it(consistency_value) if consistency_unit == "MQ" else ""
        consistency_rooms = format_amount_it(consistency_value) if consistency_unit == "VANI" else ""
        consistency_other = (
            raw_consistency if raw_consistency and consistency_unit not in {"MQ", "VANI"} else ""
        )
        if view.get("family") == "fabbricati":
            cadastral_value = building_cadastral_value(
                classification.get("income_eur"), classification.get("category")
            )
            financial = {
                "base_label": "Rendita catastale",
                "base_value": classification.get("income_eur") or "",
                "cadastral_value": _format_currency_it(float(cadastral_value.value)) if cadastral_value else "",
                "formula": (
                    f"rendita × 1,05 × {cadastral_value.multiplier}"
                    if cadastral_value else ""
                ),
                "other_income": "",
                "note": (
                    "Valore fiscale ai fini IMU, riferito all'intera unità; non è una stima di mercato e non applica la quota di proprietà."
                    if cadastral_value else "Valore catastale non calcolabile: rendita o moltiplicatore catastale non disponibile."
                ),
            }
        else:
            land_values = [
                land_cadastral_value(row.get("dominical_income_eur"))
                for row in unit.get("classifications") or []
            ]
            land_values = [value for value in land_values if value is not None]
            taxable_base = sum((value.value for value in land_values), Decimal("0"))
            financial = {
                "base_label": "Reddito dominicale",
                "base_value": totals.get("dominical_eur") or "",
                "cadastral_value": _format_currency_it(float(taxable_base)) if land_values else "",
                "formula": "reddito dominicale × 1,25 × 135" if land_values else "",
                "other_income": totals.get("agricultural_eur") or "",
                "note": (
                    "Valore fiscale ai fini IMU, riferito all'intera particella; non è una stima di mercato e non applica la quota di proprietà. Il moltiplicatore ridotto per CD/IAP non è applicato."
                    if land_values else "Valore catastale non calcolabile: reddito dominicale non disponibile."
                ),
            }
        out.append(
            {
                "document_id": d["id"],
                "subunit": subunit,
                # Full interpreted Visura: unit facts, property history and every ownership act are rendered
                # together inside the parcel group rather than being split across the page.
                "view": view,
                "unit_view": unit,
                "financial": financial,
                # what the visura says about the unit itself, to fill what the saved query left blank
                "unit": {
                    "address": unit.get("address") or "",
                    "category": classification.get("category") or "",
                    "cadastral_class": classification.get("class") or "",
                    "consistency": raw_consistency,
                    "consistency_sqm": consistency_sqm,
                    "consistency_rooms": consistency_rooms,
                    "consistency_other": consistency_other,
                    "income": classification.get("income_eur") or "",
                    "area": (unit.get("surface") or {}).get("total") or (f"{totals['area_m2']} m²" if totals.get("area_m2") else ""),
                    "dominical_income": totals.get("dominical_eur") or "",
                    "agricultural_income": totals.get("agricultural_eur") or "",
                },
                "as_of": view.get("as_of") or d["date"],
                "current_owners": [_hub_owner(o, own_code) for o in unit.get("current_owners") or []],
                "acts": acts,
            }
        )
    return out


def _enrich_units_from_visure(units: list[dict], ownership: list[dict]) -> None:
    """Fill the fields the saved query left blank from the visura of the same subalterno (in place).

    A unit that got something from a visura carries ``enriched_from`` (the document id) and the filled field names.
    """
    by_subunit = {o["subunit"]: o for o in ownership}
    for unit in units:
        source = by_subunit.get(str(unit.get("subunit") or "").strip())
        if source is None:
            continue
        filled = [field for field, value in source["unit"].items() if value and not unit.get(field)]
        for field in filled:
            unit[field] = source["unit"][field]
        if filled:
            unit["enriched_from"] = source["document_id"]
            unit["enriched_fields"] = filled


async def _subject_query_profile(
    code: str,
    subject_documents: list[dict],
    indexed_files: list[dict],
    page: int = 1,
    *,
    exhaustive_ownership: bool = False,
    allowed_document_ids: set[int] | None = None,
) -> dict | None:
    """Hub of a subject built on its newest saved ``soggetto`` query (None when it was never queried).

    The query result is the skeleton: provinces → comuni → parcels (with their units and this subject's right).
    Everything else stored hangs off it: the subject's own visure and ipotecarie, per parcel the visure,
    planimetrie, map documents, ipotecarie, cadastral inspection and map request results, and cached map geometry.
    What is missing is shown as missing, so the page doubles as the checklist of what is still to request.
    """
    queries = await find_subject_query_requests(code)
    if not queries:
        return None
    latest = queries[0]
    request_id = latest["request_id"]
    record, response, properties, owners = await asyncio.gather(
        get_result_record(request_id),
        get_response(request_id),
        get_db_properties_for_response(request_id),
        get_db_owners_for_response(request_id),
    )
    data = (response or {}).get("data") or {}

    own_rights = {o["result_id"]: o for o in owners if (o.get("fiscal_code") or "").upper() == code}
    name = next((o["nominativo"] for o in owners if o.get("fiscal_code") == code and o.get("nominativo")), None)
    name = name or await get_subject_name(code)

    indexed_by_id = {int(item["id"]): item for item in indexed_files if item.get("id") is not None}
    keys = {parcel_key(p.get("comune_id"), p.get("section"), p.get("sheet"), p.get("parcel")) for p in properties}
    keys.discard(None)
    document_ids, subject_inspection_ids = await asyncio.gather(
        find_parcel_document_ids(keys),
        find_subject_inspection_document_ids(code),
    )
    related_ids = set(subject_inspection_ids)
    for ids in document_ids.values():
        related_ids.update(ids)
    missing_ids = related_ids - set(indexed_by_id)
    if allowed_document_ids is not None:
        missing_ids &= allowed_document_ids
    if missing_ids:
        related_rows, sibling_rows = await asyncio.gather(
            get_documents_by_ids(missing_ids),
            get_document_group_siblings(missing_ids),
        )
        related_rows.extend(sibling_rows)
        indexed_by_id.update(
            {
                int(item["id"]): item
                for item in related_rows
                if item.get("id") is not None
                and (allowed_document_ids is None or int(item["id"]) in allowed_document_ids)
            }
        )
    request_records, with_map = await asyncio.gather(
        find_parcel_request_records(keys),
        find_parcels_with_geometry(keys),
    )

    parcels: dict[tuple, dict] = {}
    for prop in properties:
        right = own_rights.get(prop.get("result_id")) or {}
        unit = {**prop, "right_type": right.get("right_type"), "ownership_share": right.get("ownership_share")}
        key = parcel_key(prop.get("comune_id"), prop.get("section"), prop.get("sheet"), prop.get("parcel"))
        group_key = key or (None, prop.get("section"), prop.get("sheet"), prop.get("parcel"), prop.get("municipality"))
        parcel = parcels.setdefault(
            group_key,
            {
                "province": prop.get("province") or "-",
                "municipality": prop.get("municipality") or "-",
                "section": prop.get("section") or "",
                "sheet": prop.get("sheet") or "-",
                "parcel": prop.get("parcel") or "-",
                "units": [],
                "key": key,
                "municipality_code": prop.get("municipality_code") or "",
                "province_code": prop.get("province_code") or "",
            },
        )
        parcel["units"].append(unit)

    for parcel in parcels.values():
        units = parcel["units"]
        parcel["is_building"] = any(u.get("property_type") == "building" for u in units)
        address = next((u.get("address") for u in units if u.get("address")), None)
        parcel["map_address"] = str(address or "").strip()
        parcel["map_query"] = f"{address}, {parcel['municipality']}" if address else ""
        ids = {i for i in document_ids.get(parcel["key"], set()) if (indexed_by_id.get(i) or {}).get("document_type") != "visura_soggetto"}
        docs = _hub_documents(ids, indexed_by_id)
        # a building's parcel holds the visure of every subalterno, most of them other owners': keep this subject's own
        own_subunits = {str(u.get("subunit") or "").strip() for u in units if u.get("subunit")}
        parcel["other_units_documents"] = sum(1 for d in docs if d["subunit"] and d["subunit"] not in own_subunits)
        docs = [d for d in docs if not d["subunit"] or d["subunit"] in own_subunits]
        grouped = {kind: [d for d in docs if d["type"] in types] for kind, _l, _i, types in _PARCEL_DOCUMENT_KINDS}
        known = set().union(*(types for *_x, types in _PARCEL_DOCUMENT_KINDS))
        grouped["altri"] = [d for d in docs if d["type"] not in known]
        for group in grouped.values():
            group.sort(key=lambda d: d["date"], reverse=True)
        parcel["documents"] = grouped
        parcel_requests = {kind: [] for kind, _label, _icon, _types in _PARCEL_REQUEST_KINDS}
        for request_record in request_records.get(parcel["key"], []):
            normalized_type = str(request_record.get("request_type") or "").casefold().replace("-", "_")
            request_kind = next(
                (kind for kind, _label, _icon, types in _PARCEL_REQUEST_KINDS if normalized_type in types),
                None,
            )
            if request_kind:
                parcel_requests[request_kind].append(_hub_request(request_record))
        parcel["requests"] = parcel_requests
        parcel["has_map"] = parcel["key"] in with_map
        parcel["map_document_id"] = next((d["id"] for d in grouped["visure"]), None)

    subject_inspections = sorted(
        _hub_documents(subject_inspection_ids, indexed_by_id), key=lambda d: d["date"], reverse=True
    )

    # Load each normalized extraction once and attach its details both to the
    # person-level inspection list and every parcel card that references it.
    inspection_instances: dict[int, list[dict]] = {}
    for document in subject_inspections:
        if document.get("id") is not None:
            inspection_instances.setdefault(int(document["id"]), []).append(document)
    for parcel in parcels.values():
        for document in parcel["documents"]["ispezioni"]:
            if document.get("id") is not None:
                inspection_instances.setdefault(int(document["id"]), []).append(document)
    if inspection_instances:
        inspection_ids = sorted(inspection_instances)

        async def load_inspection(document_id: int) -> dict | None:
            try:
                return await get_document_structured_extraction(document_id)
            except Exception as exc:  # noqa: BLE001 - keep the cadastral profile usable if one extraction is malformed.
                logger.warning("Mortgage extraction unavailable for document %s: %s", document_id, type(exc).__name__)
                return None

        extractions = await asyncio.gather(*(load_inspection(document_id) for document_id in inspection_ids))
        for document_id, extraction in zip(inspection_ids, extractions):
            summary = _hub_inspection_extraction(extraction)
            for document in inspection_instances[document_id]:
                document["extraction"] = summary

    ordered = sorted(
        parcels.values(),
        key=lambda x: (not x["is_building"], x["province"], x["municipality"], x["sheet"].zfill(5), x["parcel"].zfill(6)),
    )
    ownership_budget = [
        sum(len(parcel["units"]) for parcel in parcels.values())
        if exhaustive_ownership
        else _HUB_OWNERSHIP_DOCUMENTS
    ]
    municipality_totals: dict[tuple, int] = {}
    for parcel in ordered:
        municipality = (parcel["province"], parcel["municipality"])
        municipality_totals[municipality] = municipality_totals.get(municipality, 0) + 1

    pages = max(1, -(-len(ordered) // _HUB_PAGE_PARCELS))
    page = min(max(page, 1), pages)
    first = (page - 1) * _HUB_PAGE_PARCELS
    page_parcels = ordered[first : first + _HUB_PAGE_PARCELS]
    # ownership comes from parsing the visure, so only the parcels shown get it (and it is not capped for them)
    for parcel in page_parcels:
        parcel["ownership"] = await _hub_parcel_ownership(parcel["documents"]["visure"], code, ownership_budget)
        _enrich_units_from_visure(parcel["units"], parcel["ownership"])

    def build_tree(parcel_list: list[dict]) -> list[dict]:
        tree: list[dict] = []
        for parcel in parcel_list:
            if not tree or tree[-1]["province"] != parcel["province"]:
                tree.append({"province": parcel["province"], "municipalities": []})
            municipalities = tree[-1]["municipalities"]
            if not municipalities or municipalities[-1]["name"] != parcel["municipality"]:
                municipalities.append(
                    {
                        "name": parcel["municipality"],
                        "parcels": [],
                        "total": municipality_totals[(parcel["province"], parcel["municipality"])],
                    }
                )
            municipalities[-1]["parcels"].append(parcel)
        return tree

    tree = build_tree(page_parcels)
    # the parcels something is archived for (documents or full ownership): the enrichment section of the subject page
    documented_tree = build_tree(
        [
            p for p in page_parcels
            if p.get("ownership") or any(p["documents"].values()) or any(p["requests"].values())
        ]
    )

    subject_visure = [
        {
            "id": d.get("id"),
            "title": d.get("search_scope_label") or d.get("display_name") or d.get("oggetto") or d.get("filename"),
            "tipo_catasto": d.get("tipo_catasto") or "",
            "subtype": d.get("visura_subtype") or "",
            "date": d.get("situazione_al") or d.get("richiesta_del") or "",
            "formats": sorted({f.get("file_format") for f in d.get("files") or [] if f.get("file_format")}),
        }
        for d in subject_documents
    ]
    all_parcels = list(parcels.values())

    def missing_for(kind: str, label: str, icon: str) -> dict:
        """The parcels nothing of this kind is archived for, by comune: the checklist of what is left to request."""
        by_comune: dict[tuple, list[str]] = {}
        count = 0
        for parcel in ordered:
            if parcel["documents"][kind] or (kind == "mappe" and parcel["has_map"]):
                continue
            count += 1
            by_comune.setdefault((parcel["province"], parcel["municipality"]), []).append(
                f"{'Sez. ' + parcel['section'] + ' · ' if parcel['section'] else ''}Fg {parcel['sheet']} · P.lla {parcel['parcel']}"
            )
        shown, groups = 0, []
        for (province, municipality), refs in by_comune.items():
            room = _HUB_MISSING_LISTED - shown
            if room <= 0:
                break
            groups.append({"province": province, "municipality": municipality, "parcels": refs[:room], "total": len(refs)})
            shown += min(len(refs), room)
        return {"kind": kind, "label": label, "icon": icon, "count": count, "groups": groups, "more": count - shown}

    missing = [missing_for(kind, label, icon) for kind, label, icon, _types in _PARCEL_DOCUMENT_KINDS if kind != "mappe"]
    return {
        "code": code,
        "name": name,
        "request_id": request_id,
        "tipo_catasto": latest["tipo_catasto"],
        "requested_at_display": _format_timestamp((record or {}).get("requested_at")),
        "responded_at_display": _format_timestamp(latest["responded_at"]),
        "provinces": [p for p in (data.get("province") or []) if isinstance(p, dict)],
        "tree": tree,
        "missing": missing,
        "documented_tree": documented_tree,
        "map_parcels": ordered,  # every parcel of the query: the map is not limited to the listed page
        "subject_visure": subject_visure,
        "subject_inspections": subject_inspections,
        "kinds": [
            *((kind, label, icon) for kind, label, icon, _types in _PARCEL_DOCUMENT_KINDS),
            *((kind, label, icon) for kind, label, icon, _types in _PARCEL_REQUEST_KINDS if kind != "mappe"),
        ],
        "totals": {
            "units": len(properties),
            "buildings": sum(1 for p in properties if p.get("property_type") == "building"),
            "lands": sum(1 for p in properties if p.get("property_type") == "land"),
            "parcels": len(all_parcels),
            "with_visure": sum(1 for p in all_parcels if p["documents"]["visure"]),
            "with_planimetrie": sum(1 for p in all_parcels if p["documents"]["planimetrie"]),
            "with_ispezioni": sum(1 for p in all_parcels if p["documents"]["ispezioni"]),
            "with_ispezioni_catastali": sum(1 for p in all_parcels if p["requests"]["ispezioni_catastali"]),
            "with_map": sum(
                1 for p in all_parcels if p["has_map"] or p["documents"]["mappe"] or p["requests"]["mappe"]
            ),
        },
        "pagination": {
            "page": page,
            "pages": pages,
            "size": _HUB_PAGE_PARCELS,
            "total": len(ordered),
            "first": first + 1 if page_parcels else 0,
            "last": first + len(page_parcels),
            "query": "",  # extra query string the route keeps on the page links
        },
        "other_queries": [
            {**q, "responded_at_display": _format_timestamp(q["responded_at"])} for q in queries[1:]
        ],
    }


_HUB_MAP_CACHE: dict[str, tuple[float, dict | None]] = {}
_HUB_MAP_DATA: dict[str, dict] = {}  # map_key -> {"chunks": [map JSON per chunk], "done", "total"}, served by .../parcel-map.json?k=&after=
_HUB_MAP_CACHE_SECONDS = 3600
# A lookup that lost chunks is not trusted for an hour, but rebuilding it on every page view (3-4 s of timeouts against the same
# geometry source) helped nobody: reuse what it found for a few minutes, then try again.
_HUB_MAP_PARTIAL: dict[str, tuple[float, dict | None]] = {}
_HUB_MAP_PARTIAL_SECONDS = float(os.getenv("SISTER_HUB_MAP_PARTIAL_SECONDS", "300"))
_HUB_MAP_CACHE_ENTRIES = 64


def _hub_map_subject_xml(parcels: list[dict]) -> dict:
    """Parcels of a hub as the subject-visura tree ``_google_maps_context`` reads its parcels from."""
    groups: dict[tuple, dict] = {}
    for parcel in parcels:
        if not parcel.get("municipality_code") or not parcel.get("province_code"):
            continue  # without its codes a parcel cannot be matched against the cadastral map
        group = groups.setdefault(
            (parcel["province_code"], parcel["municipality_code"]),
            {
                "CodiceComune": parcel["municipality_code"],
                "Descrizione": parcel["municipality"],
                "Provincia": parcel["province_code"],
                "ImmobileFabbricatiS": [],
                "ImmobileTerreniS": [],
            },
        )
        identifier = {
            "Provincia": parcel["province_code"],
            "CodiceComune": parcel["municipality_code"],
            "Comune": parcel["municipality"],
            "Foglio": parcel["sheet"],
            "ParticellaNum": parcel["parcel"],
        }
        if parcel["section"]:
            identifier["SezUrbana"] = parcel["section"]
        row: dict = {"DatiIdentificativi": {"IdentificativoDefinitivo": identifier}}
        if parcel["is_building"]:
            category = next((u.get("category") for u in parcel["units"] if u.get("category")), "")
            row["DatiClassamentoF"] = {"Categoria": category}
            group["ImmobileFabbricatiS"].append(row)
        else:
            group["ImmobileTerreniS"].append(row)
    return {"VisuraSoggetto": {"GruppoUnitaImmobiliari": list(groups.values())}} if groups else {}


def _elenco_number_unit(text: Any) -> tuple[str, str]:
    """'7,5 vani' -> ('7,5', 'vani'); a bare number keeps an empty unit."""
    match = re.match(r"^\s*([\d.,]+)\s*(.*?)\s*$", str(text or ""))
    return (match.group(1), match.group(2)) if match else (str(text or "").strip(), "")


def _elenco_row_key(province_code: str, municipality_code: str, section: Any, sheet: Any, parcel: Any, subunit: Any) -> tuple:
    def plain(value: Any) -> str:
        text = str(value or "").strip().upper()
        return text.lstrip("0") or ("0" if text else "")

    return (str(municipality_code or "").strip().upper(), plain(section), plain(sheet), plain(parcel), plain(subunit))


def _elenco_groups(profile: dict, known_keys: set[tuple] = frozenset()) -> list[dict]:
    """Groups of the subject's *elenco immobili* (saved soggetto query) in the shape of a subject visura.

    One group per comune and cadastre type (F buildings / T lands), rows shaped like the visura's, so the visura layout
    renders them with its own tables. Units whose key is in ``known_keys`` (already in a stored visura) are left out.
    """
    groups: dict[tuple, dict] = {}
    for parcel in profile.get("map_parcels") or []:
        for unit in parcel["units"]:
            is_building = unit.get("property_type") == "building"
            row_key = _elenco_row_key(
                parcel.get("province_code"), parcel.get("municipality_code"), parcel.get("section"),
                parcel.get("sheet"), parcel.get("parcel"), unit.get("subunit") if is_building else "",
            )
            if row_key in known_keys:
                continue
            # like a visura, a group is the units of one comune and type that the subject holds in the same way
            right, share = str(unit.get("right_type") or "").strip(), str(unit.get("ownership_share") or "").strip()
            group = groups.setdefault(
                (parcel.get("province_code") or parcel["province"], parcel.get("municipality_code") or parcel["municipality"],
                 "F" if is_building else "T", right, share),
                {
                    "TipoCatasto": "F" if is_building else "T",
                    "CodiceComune": parcel.get("municipality_code") or "",
                    "Descrizione": parcel["municipality"],
                    "Provincia": parcel.get("province_code") or "",
                    "FonteElenco": True,  # from the saved search only: no ownership history from a visura yet
                    "TitolaritaElenco": {"Diritto": right, "Quota": share},
                    # what the search says about the subject's right, shaped like the visura's ownership heading so the
                    # valuation (valore catastale, quota, diritto) runs on it; the page shows it as a line, not a timeline
                    "IntestazioneGruppo": {"MutazioneSoggettiva": [{"Intestazione": {"Intestato": [{
                        "CF": profile.get("code") or "",
                        "Nominativo": profile.get("name") or "",
                        "DirittiReali": {"Descrizione": right, "Quota": share},
                    }]}}]},
                    "ImmobileFabbricatiS": [],
                    "ImmobileTerreniS": [],
                },
            )
            identifier = {
                "Provincia": parcel.get("province_code") or "",
                "CodiceComune": parcel.get("municipality_code") or "",
                "Comune": parcel["municipality"],
                "Foglio": parcel["sheet"],
                "ParticellaNum": parcel["parcel"],
            }
            if parcel.get("section"):
                identifier["SezUrbana"] = parcel["section"]
            if unit.get("partita"):
                identifier["Partita"] = unit["partita"]
            if is_building:
                if unit.get("subunit"):
                    identifier["Subalterno"] = unit["subunit"]
                value, unit_name = _elenco_number_unit(unit.get("consistency"))
                classamento = {
                    "ZonaCensuaria": unit.get("census_zone") or "",
                    "Categoria": unit.get("category") or "",
                    "Classe": unit.get("cadastral_class") or "",
                    "RenditaEuro": unit.get("income") or "",
                }
                if value:
                    classamento["Consistenza"] = {"Valore": value, "Unita": unit_name}
                group["ImmobileFabbricatiS"].append({
                    "DatiIdentificativi": {"IdentificativoDefinitivo": identifier},
                    "DatiIndirizzo": {"IndirizzoImm": unit.get("address") or ""},
                    "DatiClassamentoF": classamento,
                })
            else:
                group["ImmobileTerreniS"].append({
                    "DatiIdentificativi": {"IdentificativoDefinitivo": identifier},
                    "DatiClassamentoT": {"ClassamentoT": {
                        "Qualita": unit.get("quality") or "",
                        "Classe": unit.get("cadastral_class") or "",
                        "SuperficieMQ": unit.get("area") or unit.get("consistency") or "",  # a land's consistency is its m²
                        "RedditoDominicaleEuro": unit.get("dominical_income") or "",
                        "RedditoAgrarioEuro": unit.get("agricultural_income") or "",
                    }},
                })
    return list(groups.values())


def _subject_tree_with_elenco(xml_parsed: dict, profile: dict | None) -> dict:
    """The subject visura tree completed with the units of the elenco immobili the visura does not list.

    The stored visura wins for every unit it lists (it has classamento history, owners and the ownership timeline); the
    elenco adds the rest as groups of its own, so the page lists every immobile of the saved search. Without a visura
    (``xml_parsed`` empty) the elenco is the whole tree.
    """
    if not profile:
        return xml_parsed

    def items(value):
        return value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])

    key = next((k for k in xml_parsed if str(k).startswith("VisuraSoggetto") and isinstance(xml_parsed[k], dict)), None)
    body = dict(xml_parsed[key]) if key else {}
    groups = items(body.get("GruppoUnitaImmobiliari"))
    known: set[tuple] = set()
    for group in groups:
        for row_name in ("ImmobileFabbricatiS", "ImmobileTerreniS"):
            for row in items(group.get(row_name)):
                for identifier in items((row.get("DatiIdentificativi") or {}).get("IdentificativoDefinitivo")):
                    known.add(_elenco_row_key(
                        identifier.get("Provincia"), identifier.get("CodiceComune") or group.get("CodiceComune"),
                        identifier.get("SezUrbana"), identifier.get("Foglio"),
                        identifier.get("ParticellaNum") or identifier.get("Particella"),
                        identifier.get("Subalterno") if row_name == "ImmobileFabbricatiS" else "",
                    ))
    extra = _elenco_groups(profile, known)
    if not extra:
        return xml_parsed
    body["GruppoUnitaImmobiliari"] = groups + extra
    return {**xml_parsed, key or "VisuraSoggetto": body}


_HUB_MAP_CHUNK_PARCELS = 120  # parcels per spatial lookup: one query over thousands of parcels outlives its timeout
_HUB_MAP_FIRST_CHUNK_PARCELS = 40  # a small first chunk: something is on the map within seconds
_HUB_MAP_WAIT_SECONDS = float(os.getenv("SISTER_HUB_MAP_WAIT_SECONDS", "2"))  # how long the page waits for the whole map before it shows up as "loading" (and fills in)
_HUB_MAP_TASKS: dict[str, asyncio.Task] = {}


def _hub_map_sheet_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    """Stable cadastral sheet identity shared by parcel chunks and rendered features."""
    def part(value: Any) -> str:
        text = str(value or "").strip().upper()
        return str(int(text)) if text.isdigit() else text

    province = str(item.get("province_code") or item.get("province") or "").strip().upper()
    municipality = str(item.get("municipality_code") or item.get("municipality") or "").strip().upper()
    return province, municipality, part(item.get("section")), part(item.get("sheet"))


def _hub_map_geocode_query(address: Any, municipality: Any, province: Any) -> str:
    """Build a street-level geocoder query without unit details that do not locate a building."""
    street_address = str(address or "").strip()
    if not street_address:
        return ""
    # Keep "senza numero civico" and its common abbreviations out of the search
    # text. Removing only SNC leaves "n. Piano T", which Nominatim cannot resolve.
    street_address = re.sub(
        r"\b(?:n(?:umero)?\.?\s*)?(?:s\.?\s*n\.?\s*c\.?|s\s*/\s*n)\b",
        "",
        street_address,
        flags=re.IGNORECASE,
    )
    street_address = re.sub(
        r"\b(?:n(?:umero)?\.?\s*)(\d+[A-Z]?(?:/[A-Z0-9]+)?)",
        r"\1",
        street_address,
        flags=re.IGNORECASE,
    )
    street_address = re.sub(
        r"(\b\d+[A-Z]?(?:/[A-Z0-9]+)?)\s+(?:interno|int\.?|piano|p\.?|scala|sc\.?|edificio|palazzina|lotto|blocco)\b.*$",
        r"\1",
        street_address,
        flags=re.IGNORECASE,
    )
    street_address = re.sub(
        r"\s+(?:interno|int\.?|piano|scala|sc\.?|edificio|palazzina|lotto|blocco)\b.*$",
        "",
        street_address,
        flags=re.IGNORECASE,
    )
    street_address = re.sub(r"\s+", " ", street_address).strip(" ,")
    if not street_address:
        return ""

    from .provinces import province_code, province_name

    raw_province = str(province or "").strip()
    province_label = province_name(province_code(raw_province) or raw_province) or raw_province
    return ", ".join(
        part for part in (street_address, str(municipality or "").strip(), province_label, "Italia") if part
    )


def _hub_map_geocode_queries(address: Any, municipality: Any, province: Any) -> list[str]:
    """Return the full address query first, followed by useful rural-place variants."""
    primary = _hub_map_geocode_query(address, municipality, province)
    if not primary:
        return []
    queries = [primary]

    street_address = str(address or "").strip()
    locality = re.sub(
        r"^(?:contrada|c\.?\s*/?\s*da\.?|localit[aà]|frazione)\s+",
        "",
        street_address,
        flags=re.IGNORECASE,
    ).strip(" ,")
    if locality and locality.casefold() != street_address.casefold():
        municipalities = [str(municipality or "").strip()]
        # OSM often indexes an address under the inhabited centre rather than
        # the full administrative name (for example, "Lampedusa e Linosa").
        short_municipality = re.split(r"\s+e\s+", municipalities[0], maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if short_municipality and short_municipality.casefold() != municipalities[0].casefold():
            municipalities.append(short_municipality)
        for municipality_variant in municipalities:
            query = _hub_map_geocode_query(locality, municipality_variant, province)
            if query and query not in queries:
                queries.append(query)
    return queries


def _hub_map_parcel_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Stable parcel identity shared by hub profile addresses and map lookup results."""
    def part(value: Any) -> str:
        text = str(value or "").strip().upper()
        return str(int(text)) if text.isdigit() else text

    province, municipality, section, sheet = _hub_map_sheet_key(item)
    return province, municipality, section, sheet, part(item.get("parcel"))


def _hub_map_chunks(parcels: list[dict]) -> list[list[dict]]:
    """Parcels in lookup order (comune, then sheet) cut into chunks: a small one first, then the usual size."""
    ordered = sorted(parcels, key=lambda p: (p.get("province_code") or "", p.get("municipality_code") or "", p["sheet"]))
    chunks = [ordered[:_HUB_MAP_FIRST_CHUNK_PARCELS]]
    rest = ordered[_HUB_MAP_FIRST_CHUNK_PARCELS:]
    chunks.extend(rest[start : start + _HUB_MAP_CHUNK_PARCELS] for start in range(0, len(rest), _HUB_MAP_CHUNK_PARCELS))
    return [chunk for chunk in chunks if chunk]


async def _hub_map_lookup(parcels: list[dict], on_chunk=None) -> dict | None:
    """Map lookup of every parcel of a hub, run in chunks and merged (each chunk fills the geometry cache).

    ``on_chunk(lookup)`` is awaited with each chunk's own lookup as soon as it is done, before the merge.
    """
    merged: dict | None = None
    failed_chunks = 0
    for chunk in _hub_map_chunks(parcels):
        chunk_sheet_keys = {_hub_map_sheet_key(parcel) for parcel in chunk}
        xml_parsed = _hub_map_subject_xml(chunk)
        if not xml_parsed:
            continue
        lookup = None
        for attempt in (1, 2):  # a busy cadastral database cancels a query now and then: one retry
            try:
                lookup = await _google_maps_context({"id": None, "xml_parsed": xml_parsed, "view": {}})
                break
            except Exception as exc:  # noqa: BLE001 - one chunk without a map must not hide the others
                logger.warning("Hub parcel map chunk unavailable (attempt %s): %s", attempt, type(exc).__name__)
        if lookup is None:
            failed_chunks += 1
            continue
        # The cadastral source can return a municipality-level fallback outline alongside a requested sheet.
        # Keep only sheet identities that are actually referenced by this subject's owned parcels.
        lookup["sheet_features"] = [
            feature for feature in lookup.get("sheet_features") or []
            if _hub_map_sheet_key(feature.get("properties") or {}) in chunk_sheet_keys
        ]
        if on_chunk is not None:
            await on_chunk(lookup)
        if merged is None:
            merged = lookup
            merged["centroid"] = None  # the centre follows the markers of all the chunks
            continue
        for key in ("parcel_markers", "parcel_features", "unmapped_parcels", "unmapped_parcel_items"):
            merged[key] = (merged.get(key) or []) + (lookup.get(key) or [])
        known_sheets = {
            _hub_map_sheet_key(feature.get("properties") or {})
            for feature in merged.get("sheet_features") or []
        }
        fresh_sheets = []
        for feature in lookup.get("sheet_features") or []:
            key = _hub_map_sheet_key(feature.get("properties") or {})
            if key not in known_sheets:
                known_sheets.add(key)
                fresh_sheets.append(feature)
        merged["sheet_features"] = (merged.get("sheet_features") or []) + fresh_sheets
        merged["parcel_count"] = (merged.get("parcel_count") or 0) + (lookup.get("parcel_count") or 0)
    if merged is None:
        return None
    unique_sheets = {}
    for feature in merged.get("sheet_features") or []:
        unique_sheets.setdefault(_hub_map_sheet_key(feature.get("properties") or {}), feature)
    merged["sheet_features"] = list(unique_sheets.values())
    merged["sheet_count"] = len({_hub_map_sheet_key(parcel) for parcel in parcels})
    merged["failed_chunks"] = failed_chunks
    for index, marker in enumerate(merged.get("parcel_markers") or []):
        marker["selected"] = index == 0
    merged["mapped_parcel_count"] = len(merged.get("parcel_markers") or [])
    merged["mapped_sheet_count"] = len(merged.get("sheet_features") or [])
    merged["lookup_message"] = (
        f"Trovate {merged['mapped_parcel_count']} particelle tra quelle elencate nelle tabelle della visura."
        if merged["mapped_parcel_count"]
        else "Nessuna geometria particellare trovata per i riferimenti elencati."
    )
    return merged


def _hub_map_parcels(profile: dict) -> list[dict]:
    """The parcels of a hub the map can place (those with their comune and province codes)."""
    return [p for p in profile.get("map_parcels") or [] if p.get("municipality_code") and p.get("province_code")]


def _hub_map_key(parcels: list[dict]) -> str:
    return hashlib.sha1(
        json.dumps(
            sorted(
                (
                    p["province_code"],
                    p["municipality_code"],
                    p["section"],
                    p["sheet"],
                    p["parcel"],
                    p["is_building"],
                    ";".join(_hub_map_geocode_queries(
                        p.get("map_address"), p.get("municipality"), p.get("province_code") or p.get("province")
                    )),
                )
                for p in parcels
            )
        ).encode()
    ).hexdigest()


def _hub_data_url(profile: dict, map_key: str) -> str:
    code = profile["code"]
    prefix = "/companies" if len(code) == 11 and code.isdigit() else "/persons"
    return f"{prefix}/{code}/parcel-map.json?k={map_key}"


def _empty_map_payload() -> dict:
    def empty_collection() -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": []}

    return {
        "parcel_markers": [],
        "parcel_features": empty_collection(),
        "sheet_features": empty_collection(),
        "omi_zone_features": empty_collection(),
        "unmapped_parcels": [],
        "parcel_fit_points": [],
    }


async def _build_hub_map(map_key: str, parcels: list[dict], data_url: str) -> dict | None:
    """Look every parcel up chunk by chunk and return the embeddable map context.

    Each finished chunk is published in ``_HUB_MAP_DATA[map_key]`` (``chunks``) straight away, so the map script can draw
    the first parcels while the rest is still being looked up. A lookup that lost chunks still serves what it found, but
    its context is not remembered: the next page load tries again.
    """
    progress = _HUB_MAP_DATA[map_key] = {"chunks": [], "done": False, "total": len(_hub_map_chunks(parcels))}
    seen_sheets: set[tuple[str, str, str, str]] = set()
    seen_omi_zones: dict[str, bool] = {}
    parcels_by_key = {_hub_map_parcel_key(parcel): parcel for parcel in parcels}

    async def publish(lookup: dict) -> None:
        for marker in lookup.get("parcel_markers") or []:
            marker["selected"] = False  # the merged lookup selects the very first one
        address_fallbacks = []
        for item in lookup.get("unmapped_parcel_items") or []:
            source = parcels_by_key.get(_hub_map_parcel_key(item))
            queries = _hub_map_geocode_queries(
                (source or {}).get("map_address"),
                (source or {}).get("municipality") or item.get("municipality"),
                (source or {}).get("province_code") or (source or {}).get("province") or item.get("province"),
            )
            if queries:
                address_fallbacks.append((item, source, queries))
        payload = await _parcel_map_payload(lookup)
        fresh = []
        for feature in payload["sheet_features"]["features"]:  # a sheet can be cut across two chunks
            key = _hub_map_sheet_key(feature.get("properties") or {})
            if key in seen_sheets:
                continue
            seen_sheets.add(key)
            fresh.append(feature)
        payload["sheet_features"]["features"] = fresh
        fresh_omi_zones = []
        for feature in payload["omi_zone_features"]["features"]:
            properties = feature.get("properties") or {}
            feature_key = str(properties.get("omi_feature_key") or "")
            if not feature_key:
                continue
            matches_owned_parcel = bool(properties.get("matches_owned_parcel"))
            previous_match = seen_omi_zones.get(feature_key)
            if previous_match is None:
                seen_omi_zones[feature_key] = matches_owned_parcel
                fresh_omi_zones.append(feature)
            elif matches_owned_parcel and not previous_match:
                seen_omi_zones[feature_key] = True
                # Send a duplicate as a style update when a later chunk resolves an owned parcel geometry.
                fresh_omi_zones.append(feature)
        payload["omi_zone_features"]["features"] = fresh_omi_zones
        progress["chunks"].append(payload)

        # Publish cadastral results first so a slow address lookup never delays the map.
        # Geocoded points are explicitly approximate and are added only when the parcel
        # has no cadastral polygon/centroid from the cadastral source.
        for item, source, queries in address_fallbacks:
            place = None
            for query in queries:
                place = await _geocode_address(query)
                if place:
                    break
            if not place:
                continue
            try:
                latitude, longitude = float(place["lat"]), float(place["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            marker = {
                "latitude": latitude,
                "longitude": longitude,
                "reference": item.get("reference") or "Particella",
                "municipality": item.get("municipality") or source.get("municipality") or "",
                "municipality_code": item.get("municipality_code") or source.get("municipality_code") or "",
                "province": item.get("province") or source.get("province") or "",
                "section": item.get("section") or source.get("section") or "",
                "sheet": item.get("sheet") or source.get("sheet") or "",
                "parcel": item.get("parcel") or source.get("parcel") or "",
                "position_source": "osm_address",
                "geocode_label": str(place.get("label") or ""),
                "selected": False,
            }
            lookup.setdefault("parcel_markers", []).append(marker)
            fallback_payload = _empty_map_payload()
            fallback_payload["parcel_markers"].append(marker)
            progress["chunks"].append(fallback_payload)

    complete = False
    parcel_map = None
    try:
        lookup = await _hub_map_lookup(parcels, publish)
        if lookup is not None:
            zones_by_key = {}
            for chunk in progress["chunks"]:
                for feature in chunk["omi_zone_features"].get("features") or []:
                    properties = feature.get("properties") or {}
                    feature_key = str(properties.get("omi_feature_key") or properties.get("reference") or "")
                    if not feature_key:
                        continue
                    previous = zones_by_key.get(feature_key)
                    if previous is None or (
                        properties.get("matches_owned_parcel")
                        and not (previous.get("properties") or {}).get("matches_owned_parcel")
                    ):
                        zones_by_key[feature_key] = feature
            lookup["omi_zone_features"] = {
                "type": "FeatureCollection",
                "features": list(zones_by_key.values()),
            }
            lookup["data_url"] = data_url
            parcel_map = await _land_registry_map_context(lookup)
            complete = not lookup.get("failed_chunks")
    except Exception as exc:  # noqa: BLE001 - the map is an addition: the archive must open without it
        logger.warning("Hub parcel map unavailable: %s", type(exc).__name__)
    finally:
        progress["done"] = True
        _HUB_MAP_TASKS.pop(map_key, None)
    while len(_HUB_MAP_DATA) > _HUB_MAP_CACHE_ENTRIES:
        _HUB_MAP_DATA.pop(next(iter(_HUB_MAP_DATA)))
    if complete:
        if len(_HUB_MAP_CACHE) >= _HUB_MAP_CACHE_ENTRIES:
            _HUB_MAP_CACHE.pop(next(iter(_HUB_MAP_CACHE)))
        _HUB_MAP_CACHE[map_key] = (time.monotonic(), parcel_map)
    elif parcel_map is not None:
        if len(_HUB_MAP_PARTIAL) >= _HUB_MAP_CACHE_ENTRIES:
            _HUB_MAP_PARTIAL.pop(next(iter(_HUB_MAP_PARTIAL)))
        _HUB_MAP_PARTIAL[map_key] = (time.monotonic(), parcel_map)
    return parcel_map


_HUB_MAP_CHUNKS_PER_ANSWER = 4  # keeps one draw step of the browser short, also when everything is already looked up


async def _hub_map_increment(map_key: str, after: int, wait_seconds: float = 25.0) -> dict:
    """What the map script has not drawn yet: the next few chunks after the first ``after``, merged into one answer.

    Waits (long poll) for a new chunk, up to ``wait_seconds``; then answers an empty increment that says more follows.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        progress = _HUB_MAP_DATA.get(map_key)
        if progress is None or len(progress["chunks"]) > after or progress["done"] or loop.time() >= deadline:
            break
        await asyncio.sleep(0.4)
    progress = _HUB_MAP_DATA.get(map_key) or {"chunks": [], "done": True, "total": 0}
    chunks = progress["chunks"]
    fresh = chunks[after : after + _HUB_MAP_CHUNKS_PER_ANSWER]
    done = bool(progress["done"]) and after + len(fresh) >= len(chunks)
    increment = _empty_map_payload()
    for payload in fresh:
        increment["parcel_markers"].extend(payload["parcel_markers"])
        increment["parcel_features"]["features"].extend(payload["parcel_features"]["features"])
        increment["sheet_features"]["features"].extend(payload["sheet_features"]["features"])
        increment["omi_zone_features"]["features"].extend(payload["omi_zone_features"]["features"])
        increment["unmapped_parcels"].extend(payload["unmapped_parcels"])
        increment["parcel_fit_points"].extend(payload["parcel_fit_points"])
    increment["next"] = after + len(fresh)
    increment["done"] = done
    increment["progress"] = 1.0 if done else min(0.99, increment["next"] / max(1, progress.get("total") or 1))
    return increment


async def _hub_parcel_map(profile: dict) -> dict | None:
    """Map (outlines and markers) of every parcel of a hub, None when nothing can be placed.

    The lookup of thousands of parcels takes a while the first time (later ones come from the geometry cache), so it
    runs as a task: the page waits a few seconds and otherwise renders the map as loading; its data URL answers when
    the task is done (and rebuilds the map from the saved search if the service forgot it meanwhile).
    """
    parcels = _hub_map_parcels(profile)
    if not parcels:
        return None
    map_key = _hub_map_key(parcels)
    data_url = _hub_data_url(profile, map_key)
    cached = _HUB_MAP_CACHE.get(map_key)
    if cached and time.monotonic() - cached[0] < _HUB_MAP_CACHE_SECONDS and (_HUB_MAP_DATA.get(map_key) or {}).get("done"):
        return cached[1]
    partial = _HUB_MAP_PARTIAL.get(map_key)
    if partial and time.monotonic() - partial[0] < _HUB_MAP_PARTIAL_SECONDS and (_HUB_MAP_DATA.get(map_key) or {}).get("done"):
        return partial[1]
    task = _HUB_MAP_TASKS.get(map_key)
    if task is None:
        task = _HUB_MAP_TASKS[map_key] = asyncio.create_task(_build_hub_map(map_key, parcels, data_url))
    done, _pending = await asyncio.wait({task}, timeout=_HUB_MAP_WAIT_SECONDS)
    if done:
        return task.result()
    first = parcels[0]
    return await _land_registry_map_context({
        "sheet": first["sheet"],
        "parcel": first["parcel"],
        "municipality": first["municipality"],
        "province": first["province"],
        "data_url": data_url,
        "listed_only": True,
        "parcel_count": len(parcels),
        "lookup_message": f"Posizionamento di {len(parcels)} particelle in corso…",
    })


async def _hub_parcel_map_for_pdf(profile: dict) -> dict | None:
    """Wait for the complete hub map before embedding it in a downloadable report."""
    parcel_map = await _hub_parcel_map(profile)
    parcels = _hub_map_parcels(profile)
    if not parcels:
        return parcel_map
    task = _HUB_MAP_TASKS.get(_hub_map_key(parcels))
    if task is None:
        return parcel_map
    try:
        return await asyncio.shield(task) or parcel_map
    except Exception as exc:  # noqa: BLE001 - map failure must not block the report's tables and archive.
        logger.warning("Complete hub map unavailable for PDF: %s", type(exc).__name__)
        return parcel_map


async def _subject_pdf_response(
    doc: dict,
    kpis: dict | None,
    lookup: dict | None,
    code: str,
    subject_report: dict | None = None,
):
    """PDF (Typst) of a subject page from the data the page was built on."""
    from fastapi import HTTPException
    from fastapi.responses import Response

    from .pdf_typst import PdfExportError, build_context, pdf_filename, render_pdf

    try:
        context = (
            build_context(doc, kpis, lookup, subject_report=subject_report)
            if subject_report is not None
            else build_context(doc, kpis, lookup)
        )
        content = await run_in_threadpool(render_pdf, context)
    except PdfExportError as exc:
        logger.warning("PDF export of subject %s failed: %s", code, exc)
        raise HTTPException(status_code=422, detail=f"PDF export not available: {exc}") from exc
    stem = f"report_soggetto_{code}" if subject_report is not None else (
        Path(str(doc.get("filename") or "")).stem or f"elenco_immobili_{code}"
    )
    headers = {"Content-Disposition": f'attachment; filename="{pdf_filename(stem, doc.get("id") or 0)}"', "Cache-Control": "no-store"}
    return Response(content=content, media_type="application/pdf", headers=headers)


def _query_only_subject_doc(profile: dict) -> dict:
    """Page data of a subject that has a saved soggetto query but no stored visura: the elenco as visura groups."""
    return {
        "id": None,
        "query_only": True,  # the visura layout labels itself as the elenco of the saved query, not as a visura
        "tipo_catasto": profile.get("tipo_catasto") or "",
        "queried_at": profile.get("responded_at_display") or profile.get("requested_at_display") or "",
        "oggetto": profile.get("name") or profile["code"],
        "xml_parsed": _subject_tree_with_elenco(
            {
                "VisuraSoggetto": {
                    "DatiRichiesta": {
                        "TipoCatasto": profile.get("tipo_catasto") or "",
                        "SoggettoIndividuato": {
                            "SoggettoPF": {"Cognome": profile.get("name") or "", "CodiceFiscale": profile["code"]}
                        },
                    }
                }
            },
            profile,
        ),
    }


async def _render_query_only_subject(
    request,
    theme,
    user,
    profile: dict,
    route_prefix: str,
    subject_archive: dict | None = None,
    as_pdf: bool = False,
    subject_favorite_enabled: bool = False,
    subject_is_favorite: bool = False,
):
    """Render a subject that has a saved soggetto query but no structured visura on the visura layout.

    /persons and /companies share ``catasto/entity.html``: a stored visura fills the layout, here the header
    carries the subject's name and code and the archive hub (the saved query) is the content.
    """
    doc = _query_only_subject_doc(profile)

    try:
        land_registry_map = await (
            _hub_parcel_map_for_pdf(profile) if as_pdf else _hub_parcel_map(profile)
        )
    except Exception as exc:  # noqa: BLE001 - the map and OMI estimate are optional additions to the subject archive
        logger.warning("Subject parcel map unavailable: %s", type(exc).__name__)
        land_registry_map = None
    try:
        # The saved search has ownership and the hub map has parcel positions; visure may add cadastral surfaces later.
        land_registry_kpis = await _subject_land_registry_calculation(
            doc["xml_parsed"], land_registry_map, user, scope_type="request",
            scope_id=profile.get("request_id") or profile.get("code"),
        )
    except Exception as exc:  # noqa: BLE001 - the valuation is an addition: the archive must open without it
        logger.warning("Subject valuation unavailable: %s", type(exc).__name__)
        land_registry_kpis = None
    if as_pdf:
        return await _subject_pdf_response(
            doc,
            land_registry_kpis,
            land_registry_map,
            profile["code"],
            subject_report={"archive": subject_archive, "subject_hub": profile},
        )
    return theme.render(
        "catasto/entity.html",
        request,
        user=user,
        doc=doc,
        subject_fiscal_code=profile["code"],
        subject_route_prefix=route_prefix,
        subject_documents=[],
        subject_hub=profile,
        subject_archive=subject_archive,
        subject_favorite_enabled=subject_favorite_enabled,
        subject_is_favorite=subject_is_favorite,
        land_registry_kpis=land_registry_kpis,
        manual_valuation_enabled=bool(_favorite_user_key(user)),
        land_registry_map=land_registry_map,
        **_land_map_layout("soggetto", theme),
    )


async def _web_subject_profile(
    request: Request,
    fiscal_code: str,
    document: int | None = None,
    *,
    user,
    route_prefix: str,
    code_pattern: str,
    page: int = 1,
    as_pdf: bool = False,
):
    """Open a canonical subject route and allow switching between its subject visure.

    ``as_pdf`` returns the PDF of the very same page instead of rendering it.
    """
    from fastapi import HTTPException

    normalized_code = fiscal_code.strip().upper()
    if not re.fullmatch(code_pattern, normalized_code):
        raise HTTPException(status_code=404, detail="Soggetto non trovato")
    if normalized_code != fiscal_code:
        query = urlencode({k: v for k, v in (("document", document), ("page", page if page > 1 else None)) if v is not None})
        return RedirectResponse(url=f"{route_prefix}/{normalized_code}{'?' + query if query else ''}", status_code=308)

    # The subject relation provides a canonical match for imported XML. Filenames
    # remain a fallback for older/history documents that predate typed ingestion.
    visible_document_ids = await _visible_document_ids(user)
    subject_document_ids, subject_associations = await _load_subject_profile_indexes(normalized_code)
    if visible_document_ids is not None:
        subject_document_ids &= visible_document_ids

    subject_associations, indexed_files = await _load_subject_profile_files(
        normalized_code,
        subject_document_ids,
        visible_document_ids,
        associations=subject_associations,
    )
    candidates = await _subject_visura_candidates(normalized_code, subject_document_ids, indexed_files)

    subject_documents = _collapse_to_logical_docs(candidates)
    is_company = route_prefix == "/companies"
    # the saved soggetto query is the hub's skeleton; it enriches the visura layout, or stands alone without a visura
    subject_hub = None
    if visible_document_ids is None:
        subject_hub = await _subject_query_profile(
            normalized_code,
            subject_documents,
            indexed_files,
            1 if as_pdf else page,
            exhaustive_ownership=as_pdf,
            allowed_document_ids=visible_document_ids,
        )
    favorite_user_key = _favorite_user_key(user)
    subject_type = "company" if is_company else "person"
    subject_favorite_enabled = bool(favorite_user_key)
    subject_is_favorite = False
    if subject_favorite_enabled and not as_pdf:
        from .database import is_subject_favorite

        subject_is_favorite = await is_subject_favorite(favorite_user_key, subject_type, normalized_code)
    if subject_hub is not None and document is not None:
        subject_hub["pagination"]["query"] = f"&document={document}"
    if subject_hub is not None and not subject_documents:
        subject_archive = await _subject_document_archive(
            normalized_code,
            indexed_files,
            _subject_cadastral_group_usage(subject_hub),
            allowed_document_ids=visible_document_ids,
            associations=subject_associations,
        )
        return await _render_query_only_subject(
            request,
            _get_theme(request),
            user,
            subject_hub,
            route_prefix,
            subject_archive,
            as_pdf,
            subject_favorite_enabled=subject_favorite_enabled,
            subject_is_favorite=subject_is_favorite,
        )
    if not subject_documents:
        if as_pdf:
            raise HTTPException(status_code=404, detail="Nessuna visura né ricerca per soggetto archiviata")
        # a code nobody has queried yet is the normal outcome of the lookup form: stay on the form with a hint
        response = _get_theme(request).render(
            "companies_index.html" if is_company else "persons_index.html",
            request,
            user=user,
            code=normalized_code,
            error_message=(
                f"Nessuna visura soggetto salvata per {normalized_code}. "
                "Richiedila dalla pagina Query (visura per soggetto), poi riapri questo profilo."
            ),
        )
        response.status_code = 404
        return response

    # Prefer the complete nationwide E-cadastre response. If none exists, prefer
    # E-cadastre visure so same-period provincial responses can be combined into
    # one profile view. The source order is newest-first, and sort stability keeps
    # that ordering within each priority.
    def subject_doc_priority(item: dict) -> int:
        if item.get("search_scope") == "nationwide" and item.get("tipo_catasto") == "E":
            return 0
        return 1 if item.get("tipo_catasto") == "E" else 2

    subject_documents.sort(key=subject_doc_priority)

    def logical_doc_contains(logical_doc: dict, requested_id: int) -> bool:
        ids = {logical_doc.get("id")}
        ids.update(file.get("id") for file in logical_doc.get("files", []))
        return requested_id in ids

    selected = None
    if document is not None:
        selected = next((item for item in subject_documents if logical_doc_contains(item, document)), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="Visura non trovata per questo soggetto")
    else:
        selected = subject_documents[0]

    selected_id = int(selected["id"])
    doc = await get_document_by_id(selected_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Visura non trovata")
    document_code = _document_subject_fiscal_code(doc)
    if document_code and document_code != normalized_code:
        raise HTTPException(status_code=404, detail="Visura non trovata per questo soggetto")

    subject_asset_documents = []
    subject_portfolio = None
    if selected.get("tipo_catasto") == "E":
        is_nationwide_e = selected.get("search_scope") == "nationwide"
        if is_nationwide_e:
            subject_portfolio = {
                "scope_label": "Nazionale",
                "tipo_catasto": "E",
                "aggregated": False,
                "provinces": [],
            }
        elif selected.get("situazione_al") and selected.get("visura_subtype"):
            peer_documents = [
                item
                for item in subject_documents
                if item.get("id") != selected.get("id")
                and item.get("tipo_catasto") == "E"
                and item.get("search_scope") == "provincial"
                and item.get("situazione_al") == selected.get("situazione_al")
                and item.get("visura_subtype") == selected.get("visura_subtype")
            ]
            subject_asset_documents = await _load_subject_asset_documents(peer_documents)
            if subject_asset_documents:
                provinces = sorted(
                    {
                        item.get("search_scope_label", "").removeprefix("Provinciale · ")
                        for item in [selected, *peer_documents]
                        if item.get("search_scope_label", "").startswith("Provinciale · ")
                    }
                )
                subject_portfolio = {
                    "scope_label": "Nazionale",
                    "tipo_catasto": "E",
                    "aggregated": True,
                    "provinces": provinces,
                }

    document_navigation = [
        {
            "id": int(item["id"]),
            "title": item.get("oggetto") or item.get("filename") or f"Visura #{item['id']}",
            "date": item.get("situazione_al") or item.get("requested_at") or item.get("created_at") or "",
            # what the "Documento" card of the visura page used to show, now part of this visura's list entry
            "tipo_catasto": item.get("tipo_catasto") or "",
            "scope": item.get("search_scope_label") or "",
            "situazione_al": item.get("situazione_al") or "",
            "protocollo": item.get("protocollo") or "",
            "protocollo_anno": item.get("protocollo_anno") or "",
        }
        for item in subject_documents
        if item.get("id") is not None
    ]
    theme = _get_theme(request)
    cadastral_group_usage = _subject_cadastral_group_usage(subject_hub)
    if subject_hub is not None and not (doc.get("xml_content") or "").strip():
        # a visura stored only as PDF has no structured data for the visura layout: show the hub (it lists the file)
        subject_archive = await _subject_document_archive(
            normalized_code,
            indexed_files,
            cadastral_group_usage,
            allowed_document_ids=visible_document_ids,
            associations=subject_associations,
        )
        return await _render_query_only_subject(
            request,
            theme,
            user,
            subject_hub,
            route_prefix,
            subject_archive,
            as_pdf,
            subject_favorite_enabled=subject_favorite_enabled,
            subject_is_favorite=subject_is_favorite,
        )
    cadastral_group_usage.setdefault(selected_id, []).append(
        "Gruppi catastali dalla visura per soggetto selezionata"
    )
    for source in subject_asset_documents:
        if source.get("id") is not None:
            cadastral_group_usage.setdefault(int(source["id"]), []).append(
                "Gruppi aggregati da una visura per soggetto provinciale"
            )
    subject_archive = await _subject_document_archive(
        normalized_code,
        indexed_files,
        cadastral_group_usage,
        allowed_document_ids=visible_document_ids,
        associations=subject_associations,
    )
    return await _render_doc_from_db(
        doc,
        request,
        theme,
        user,
        subject_fiscal_code=normalized_code,
        subject_route_prefix=route_prefix,
        subject_documents=document_navigation,
        subject_asset_documents=subject_asset_documents,
        subject_portfolio=subject_portfolio,
        subject_hub=subject_hub,
        subject_archive=subject_archive,
        subject_favorite_enabled=subject_favorite_enabled,
        subject_is_favorite=subject_is_favorite,
        as_pdf=as_pdf,
    )


@router.get("/persons/{fiscal_code}/pdf")
async def web_person_pdf(request: Request, fiscal_code: str, document: int | None = None, user=Depends(_require_auth)):
    """Complete PDF report for the person, with the selected visura and all linked archive evidence."""
    return await _web_subject_profile(
        request, fiscal_code, document, user=user, route_prefix="/persons", code_pattern=r"[A-Z0-9]{16}", as_pdf=True
    )


@router.get("/companies/{fiscal_code}/pdf")
async def web_company_pdf(request: Request, fiscal_code: str, document: int | None = None, user=Depends(_require_auth)):
    """Complete PDF report for the company, with the selected visura and all linked archive evidence."""
    return await _web_subject_profile(
        request, fiscal_code, document, user=user, route_prefix="/companies", code_pattern=r"\d{11}", as_pdf=True
    )


@router.get("/persons/", response_class=HTMLResponse, name="persons_index")
async def web_persons_index(
    request: Request,
    code: str | None = None,
    user=Depends(_require_auth),
):
    """Search archived person profiles by full fiscal code or partial name/code text."""
    submitted = " ".join((code or "").strip().split())
    normalized_code = re.sub(r"[^A-Z0-9]", "", submitted.upper())
    if submitted and not any(character.isalnum() for character in submitted):
        response = _get_theme(request).render(
            "persons_index.html",
            request,
            user=user,
            code=submitted,
            error_message="Inserisci almeno una lettera o un numero.",
            search_results=None,
            search_has_more=False,
        )
        response.status_code = 422
        return response
    if submitted:
        if len(submitted) > 80:
            response = _get_theme(request).render(
                "persons_index.html",
                request,
                user=user,
                code=submitted,
                error_message="La ricerca può contenere al massimo 80 caratteri.",
                search_results=None,
                search_has_more=False,
            )
            response.status_code = 422
            return response
        if len(normalized_code) == 16 and re.fullmatch(r"[A-Z0-9 ./-]+", submitted.upper()):
            return RedirectResponse(url=f"/persons/{normalized_code}", status_code=303)
        search_results, search_has_more = await search_person_profiles(submitted)
        return _get_theme(request).render(
            "persons_index.html",
            request,
            user=user,
            code=submitted,
            error_message=None,
            search_results=search_results,
            search_has_more=search_has_more,
        )

    return _get_theme(request).render(
        "persons_index.html",
        request,
        user=user,
        code="",
        error_message=None,
        search_results=None,
        search_has_more=False,
    )


@router.get("/companies/", response_class=HTMLResponse, name="companies_index")
async def web_companies_index(
    request: Request,
    code: str | None = None,
    user=Depends(_require_auth),
):
    """Lookup entry point for a company's stored subject visura."""
    normalized_code = (code or "").strip()
    if normalized_code:
        if not re.fullmatch(r"\d{11}", normalized_code):
            response = _get_theme(request).render(
                "companies_index.html",
                request,
                user=user,
                code=normalized_code,
                error_message="Inserisci una partita IVA composta da 11 cifre.",
            )
            response.status_code = 422
            return response
        return RedirectResponse(url=f"/companies/{normalized_code}", status_code=303)

    return _get_theme(request).render("companies_index.html", request, user=user, code="", error_message=None)


@router.get("/persons/{fiscal_code}", response_class=HTMLResponse)
async def web_person(
    request: Request,
    fiscal_code: str,
    document: int | None = None,
    page: int = 1,
    user=Depends(_require_auth),
):
    """Open a canonical person route and switch between that person's subject visure."""
    return await _web_subject_profile(
        request,
        fiscal_code,
        document,
        user=user,
        route_prefix="/persons",
        code_pattern=r"[A-Z0-9]{16}",
        page=page,
    )


@router.get("/companies/{fiscal_code}", response_class=HTMLResponse)
async def web_company(
    request: Request,
    fiscal_code: str,
    document: int | None = None,
    page: int = 1,
    user=Depends(_require_auth),
):
    """Open a company route using its 11-digit VAT/fiscal code."""
    return await _web_subject_profile(
        request,
        fiscal_code,
        document,
        user=user,
        route_prefix="/companies",
        code_pattern=r"\d{11}",
        page=page,
    )


# ---------------------------------------------------------------------------
# Dossiers — multi/single-step query responses stored as JSON files
# ---------------------------------------------------------------------------


def _safe_dossier_path(path: str) -> "Path":
    """Resolve a dossier-relative path, guarding against traversal. Raises 403/404."""
    from fastapi import HTTPException

    base = _dossiers_base()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Dossier not found")
    return target


@router.get("/dossiers/view/{path:path}", response_class=HTMLResponse)
async def web_dossier_view(request: Request, path: str, user=Depends(_require_admin)):
    """Render a single dossier JSON via the result_detail template."""
    import json as _json

    theme = _get_theme(request)
    target = _safe_dossier_path(path.strip("/"))
    try:
        data = _json.loads(target.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return theme.render(
            "result_detail.html",
            request,
            user=user,
            result=None,
            request_id=target.name,
            not_found=True,
            error=str(exc),
        )

    if _is_batch_dossier(data):
        parsed = _parse_batch_dossier(data)
        return theme.render("dossier_batch_viewer.html", request, user=user, name=target.name, **parsed)

    result = _dossier_to_result(target.name, data)
    return theme.render("result_detail.html", request, user=user, result=result, request_id=target.name)


@router.get("/dossiers", response_class=HTMLResponse)
@router.get("/dossiers/{path:path}", response_class=HTMLResponse)
async def web_dossiers(request: Request, path: str = "", download: str = "", user=Depends(_require_admin)):
    """Dossiers hub — multi-step (workflow) and single-step query responses.

    Root lists the dossier JSON files; a file path serves the raw JSON
    (``?download=1`` forces an attachment). Use /dossiers/view/<file> for the
    rendered view.
    """
    theme = _get_theme(request)
    path = path.strip("/")
    base = _dossiers_base()

    # A specific file → serve raw JSON (inline, or as attachment with ?download=1)
    if path:
        target = _safe_dossier_path(path)
        return FileResponse(
            str(target),
            media_type="application/json",
            filename=target.name if download else None,
        )

    # Root → list dossier files with extracted metadata, off the event loop.
    dossiers = await run_in_threadpool(_load_dossier_index, base)
    groups = _build_dossier_groups(dossiers)

    n_errors = sum(
        1
        for group in groups
        for subgroup in group["subgroups"]
        for dossier in subgroup["entries"]
        if dossier.get("ok") is False
    )

    return theme.render(
        "dossiers_index.html",
        request,
        user=user,
        groups=groups,
        total=len(dossiers),
        n_errors=n_errors,
    )


# ---------------------------------------------------------------------------
# Browser session control
# ---------------------------------------------------------------------------


@router.get("/browser", response_class=HTMLResponse)
async def web_browser(request: Request, user=Depends(_require_admin)):
    """Browser session control panel."""
    from .main import visura_service

    theme = _get_theme(request)
    svc = visura_service
    status = svc.auth_status if svc else {"state": "unavailable", "message": "Service not initialized"}
    extra = {}
    if svc:
        extra = {
            "queue_size": svc.request_queue.qsize(),
            "pending_requests": len(svc.pending_request_ids),
            "last_login": (
                svc.browser_manager.last_login_time.strftime("%Y-%m-%d %H:%M:%S")
                if svc.browser_manager.last_login_time
                else None
            ),
            "mode": status.get("mode", "local"),
            "authenticated": svc.browser_manager.authenticated,
        }
    return theme.render("browser_control.html", request, user=user, status=status, **extra)


@router.get("/browser/status", response_class=JSONResponse)
async def web_browser_status(request: Request, user=Depends(_require_admin)):
    """JSON status for live polling from the control panel."""
    from .main import visura_service

    svc = visura_service
    if svc is None:
        return JSONResponse(
            {
                "state": "unavailable",
                "message": "Service not initialized",
                "queue_size": 0,
                "pending_requests": 0,
                "last_login": None,
            }
        )
    st = svc.auth_status
    return JSONResponse(
        {
            **st,
            "queue_size": svc.request_queue.qsize(),
            "pending_requests": len(svc.pending_request_ids),
            "last_login": (
                svc.browser_manager.last_login_time.strftime("%Y-%m-%d %H:%M:%S")
                if svc.browser_manager.last_login_time
                else None
            ),
            "authenticated": svc.browser_manager.authenticated,
        }
    )


@router.post("/browser/start", response_class=JSONResponse)
@_audited("browser.start")
async def web_browser_start(request: Request, user=Depends(_require_admin)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.start_browser()
    return JSONResponse(result)


@router.post("/browser/stop", response_class=JSONResponse)
@_audited("browser.stop")
async def web_browser_stop(request: Request, force: bool = False, user=Depends(_require_admin)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.stop_browser(force=force)
    return JSONResponse(result)


@router.post("/browser/restart", response_class=JSONResponse)
@_audited("browser.restart")
async def web_browser_restart(request: Request, user=Depends(_require_admin)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.restart_browser()
    return JSONResponse(result)


@router.post("/browser/launch-chrome", response_class=JSONResponse)
@_audited("browser.launch_chrome")
async def web_browser_launch_chrome(request: Request, user=Depends(_require_admin)):
    """Launch Google Chrome with CDP if not already running, then start the browser session."""
    import asyncio
    import os
    from urllib.parse import urlparse

    import httpx

    from .main import _chrome_cdp_cmd, visura_service

    cdp_endpoint = os.getenv("BROWSER_CDP_ENDPOINT", "http://localhost:9222")
    launched = False
    pid = None

    # Check if CDP endpoint is already reachable
    chrome_version = None
    chrome_status = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cdp_endpoint}/json/version")
            if resp.status_code == 200:
                chrome_version = resp.json().get("Browser", "unknown")
                chrome_status = "already_running"
    except Exception:
        pass

    if chrome_status is None:
        port = urlparse(cdp_endpoint).port or 9222
        cmd = _chrome_cdp_cmd(port)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            pid = proc.pid
            launched = True
            # Wait up to 5 s for Chrome to accept CDP connections
            for _ in range(5):
                await asyncio.sleep(1)
                try:
                    async with httpx.AsyncClient(timeout=1.0) as client:
                        resp = await client.get(f"{cdp_endpoint}/json/version")
                        if resp.status_code == 200:
                            chrome_version = resp.json().get("Browser", "unknown")
                            chrome_status = "launched"
                            break
                except Exception:
                    pass
            if chrome_status is None:
                return JSONResponse(
                    {"status": "launched", "pid": pid, "error": "Chrome started but CDP not yet reachable"}
                )
        except FileNotFoundError:
            return JSONResponse({"error": "google-chrome not found in PATH"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # Connect the sister browser session
    if visura_service is None:
        return JSONResponse(
            {
                "status": chrome_status,
                "pid": pid,
                "browser": chrome_version,
                "session": "skipped",
                "session_error": "visura_service not initialized",
            }
        )
    try:
        session_result = await visura_service.start_browser()
        return JSONResponse(
            {
                "status": chrome_status,
                "pid": pid,
                "browser": chrome_version,
                "launched": launched,
                "session": session_result,
            }
        )
    except Exception as e:
        return JSONResponse(
            {
                "status": chrome_status,
                "pid": pid,
                "browser": chrome_version,
                "launched": launched,
                "session_error": str(e),
            }
        )


# ---------------------------------------------------------------------------
# API proxy (for web form submissions)
# ---------------------------------------------------------------------------


@router.post("/api/ui/batch", response_class=JSONResponse)
async def web_api_batch(request: Request, user=Depends(_require_auth)):
    """Parse a CSV and bill each accepted row for non-admin users."""
    import httpx

    from .query_forms import command_for_path

    try:
        body = await request.json()
        command, api_path, rows, generic_commands = _parse_batch_rows(body)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    paid_user = not _is_admin(user)
    form_command = command_for_path(api_path)
    if paid_user and form_command is None:
        return JSONResponse({"detail": "Tipo di query non disponibile"}, status_code=422)
    if paid_user and len(rows) > 100:
        return JSONResponse({"detail": "Un batch può contenere al massimo 100 righe"}, status_code=422)
    unit_cost, price_path = await _query_product_price(api_path, allow_shared_fallback=True) if paid_user else (None, None)
    access_error = await _query_access_error(user)
    if access_error is not None:
        return JSONResponse({"detail": access_error.detail}, status_code=access_error.status_code)
    if paid_user and unit_cost is None:
        return JSONResponse({"detail": "Il prezzo per questo comando non è configurato"}, status_code=503)
    confirmation_error = _query_price_confirmation_error(user, body, len(rows), unit_cost, price_path)
    if confirmation_error is not None:
        return confirmation_error

    charge, error_response = await _charge_portal_query(
        request, user, len(rows), uuid4().hex, unit_cost, price_path
    )
    if error_response is not None:
        return error_response

    base = f"http://localhost:{request.url.port or 8025}"
    results = []
    failed_rows = 0
    user_key = _favorite_user_key(user) if paid_user else None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for i, row in enumerate(rows):
                try:
                    if form_command in generic_commands or command in generic_commands:
                        resp = await client.post(f"{base}{api_path}", params=row, headers=_internal_api_headers())
                    else:
                        resp = await client.post(f"{base}{api_path}", json=row, headers=_internal_api_headers())
                    data = resp.json()
                    if resp.is_success and user_key:
                        data = _query_poll_tokens(data, user_key)
                    if not resp.is_success:
                        failed_rows += 1
                    results.append(
                        {
                            "row": i + 1,
                            "status": "submitted" if resp.is_success else "error",
                            "data": data,
                            **({"http_status": resp.status_code} if not resp.is_success else {}),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    failed_rows += 1
                    results.append(
                        {"row": i + 1, "status": "error", "error": "Richiesta non completata" if paid_user else str(exc)}
                    )
    except Exception as exc:  # noqa: BLE001 - client construction/teardown must not consume unsubmitted row charges
        pending = len(rows) - len(results)
        failed_rows += pending
        logger.warning("Portal batch client failed: %s", type(exc).__name__)
        for i in range(len(results), len(rows)):
            results.append(
                {"row": i + 1, "status": "error", "error": "Richiesta non completata" if paid_user else str(exc)}
            )
    await _refund_portal_query(charge, failed_rows, "batch-rejected-rows")
    return JSONResponse({"command": command, "total_rows": len(rows), "results": results})


@router.post("/api/ui/workflow/stream")
async def web_api_workflow_stream(request: Request, user=Depends(_require_auth)):
    """Run a workflow; paid users reserve one product price per planned step."""
    body = await request.json()
    confirmed_quote = {
        key: body.pop(key, None)
        for key in ("_query_confirmed_price_path", "_query_confirmed_unit_cost", "_query_confirmed_units")
    } if isinstance(body, dict) else {}
    try:
        from .workflows import _missing_required_fields, prepare_workflow

        plan = prepare_workflow(body)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    missing = _missing_required_fields(plan.workflow)
    if missing:
        return JSONResponse({"detail": f"Campi obbligatori mancanti: {', '.join(missing)}"}, status_code=422)
    paid_user = not _is_admin(user)
    if not plan.steps:
        return JSONResponse({"detail": "La richiesta non contiene query da eseguire"}, status_code=422)
    unit_cost, price_path = await _query_product_price(_QUERY_SHARED_PRICE_PATH) if paid_user else (None, None)
    access_error = await _query_access_error(user)
    if access_error is not None:
        return JSONResponse({"detail": access_error.detail}, status_code=access_error.status_code)
    if paid_user and unit_cost is None:
        return JSONResponse({"detail": "Il prezzo condiviso per i workflow non è configurato"}, status_code=503)
    body.update(confirmed_quote)
    confirmation_error = _query_price_confirmation_error(user, body, len(plan.steps), unit_cost, price_path)
    if confirmation_error is not None:
        return confirmation_error

    charge, error_response = await _charge_portal_query(
        request, user, len(plan.steps), uuid4().hex, unit_cost, price_path
    )
    if error_response is not None:
        return error_response

    from .workflows import run_workflow_stream

    base_url = os.getenv("VISURA_API_URL") or "http://127.0.0.1:8025"
    api_key = os.getenv("VISURA_API_KEY") or os.getenv("API_KEY")

    async def stream_events():
        completed_steps = 0
        try:
            async for event in run_workflow_stream(plan, base_url=base_url, api_key=api_key):
                try:
                    parsed = json.loads(event)
                except (TypeError, ValueError):
                    parsed = {}
                if parsed.get("event") == "step" and parsed.get("status") == "completed":
                    completed_steps += 1
                yield f"data: {event}\n\n"
        finally:
            await _refund_portal_query(charge, len(plan.steps) - completed_steps, "workflow-uncompleted-steps")

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _internal_api_headers() -> dict:
    """Headers for the page's calls to the service's own API (it requires X-API-Key when API_KEY is set)."""
    api_key = os.getenv("API_KEY") or os.getenv("VISURA_API_KEY") or ""
    return {"X-API-Key": api_key} if api_key else {}


def _web_query_command(endpoint: str) -> Optional[str]:
    """The ``sister query`` command a /forms endpoint submits (None for workflows, batch, ipotecaria...)."""
    from .query_forms import command_for_path

    return command_for_path("/visura/" + endpoint.strip("/")) if endpoint.strip("/") else "search"


async def _query_access_error(user):
    """Return a safe response when a non-admin cannot use the paid query product."""
    from fastapi import HTTPException

    from . import purchases_billing

    if _is_admin(user):
        return None
    if not _favorite_user_key(user):
        return HTTPException(status_code=403, detail="Account non identificabile")
    if purchases_billing.wallet_backend() == "none":
        return HTTPException(status_code=503, detail="Le query a pagamento non sono configurate")
    prices = await _query_product_prices()
    if not prices or not any(path == "/visura" or path.startswith("/visura/") for path in prices):
        return HTTPException(status_code=503, detail="Il listino delle query non è disponibile")
    if not (os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY")):
        return HTTPException(status_code=503, detail="La consultazione dei risultati non è configurata")
    return None


async def _charge_portal_query(
    request: Request, user, units: int, reference: str, unit_cost: Decimal | None, price_path: str | None = None
):
    """Reserve credits for a non-admin query action, returning (charge, error response)."""
    from . import purchases_billing

    if _is_admin(user):
        return None, None
    access_error = await _query_access_error(user)
    if access_error is not None:
        return None, JSONResponse({"detail": access_error.detail}, status_code=access_error.status_code)
    if unit_cost is None:
        return None, JSONResponse({"detail": "Il prezzo per questa query non è configurato"}, status_code=503)
    try:
        outcome, charge, available = await purchases_billing.charge_query_credits(
            _favorite_user_key(user), units, reference, unit_cost=unit_cost
        )
    except Exception as exc:  # noqa: BLE001 - fail closed if the wallet is unavailable
        logger.error("Portal query charge failed: %s", type(exc).__name__)
        return None, JSONResponse({"detail": "Il pagamento con crediti non è disponibile"}, status_code=503)
    request.state.audit_params = {
        **(getattr(request.state, "audit_params", None) or {}),
        "query_units": units,
        "credits_per_unit": str(unit_cost),
        "credits_available_before": str(available),
        "billing_reference": reference,
        "price_path": price_path or "",
    }
    if outcome == "insufficient":
        return None, JSONResponse(
            {
                "error": "insufficient_credits",
                "detail": f"Saldo insufficiente: servono {unit_cost * units} crediti; disponibili {available}.",
                "required": str(unit_cost * units),
                "available": str(available),
            },
            status_code=402,
        )
    return charge, None


def _query_price_confirmation_error(user, body: dict, units: int, unit_cost: Decimal | None, price_path: str | None):
    """Require the client to acknowledge the exact quote; never trust its price as the charge amount."""
    confirmed_path = body.pop("_query_confirmed_price_path", None)
    confirmed_cost = body.pop("_query_confirmed_unit_cost", None)
    confirmed_units = body.pop("_query_confirmed_units", None)
    if _is_admin(user):
        return None
    from . import purchases_billing

    try:
        confirmed_cost = Decimal(str(confirmed_cost))
    except (InvalidOperation, TypeError, ValueError):
        confirmed_cost = None
    try:
        confirmed_units = int(confirmed_units)
    except (TypeError, ValueError):
        confirmed_units = None
    if (
        unit_cost is None
        or price_path is None
        or confirmed_cost != unit_cost
        or confirmed_units != units
        or not confirmed_path
        or purchases_billing.normalize_query_price_path(confirmed_path)
        != purchases_billing.normalize_query_price_path(price_path)
    ):
        return JSONResponse(
            {
                "error": "query_price_changed",
                "detail": "Il prezzo è cambiato o il preventivo non è più valido. Verifica di nuovo il costo prima di inviare.",
                "units": units,
                "unit_cost": str(unit_cost) if unit_cost is not None else None,
                "price_path": price_path,
            },
            status_code=409,
        )
    return None


async def _refund_portal_query(charge, units: int, reference: str) -> None:
    if charge is None:
        return
    from . import purchases_billing

    await purchases_billing.refund_query_credits(charge, units, reference)


def _query_poll_tokens(content: dict, user_key: str | None) -> dict:
    """Attach user-bound poll capabilities while leaving the upstream request ids intact."""
    if not user_key:
        return content
    from . import purchases_billing

    request_ids = content.get("request_ids") or ([content.get("request_id")] if content.get("request_id") else [])
    if not request_ids:
        return content
    updated = dict(content)
    updated["poll_tokens"] = {
        str(request_id): purchases_billing.query_result_token(str(request_id), user_key)
        for request_id in dict.fromkeys(request_ids)
        if str(request_id)
    }
    return updated


def _parse_batch_rows(body: dict) -> tuple[str, str, list[dict], set[str]]:
    """Validate and normalize the CSV envelope shared by billing quotes and batch submission."""
    import csv
    import io

    from .query_forms import QUERY_FORMS

    csv_data = str(body.get("csv_data") or "")
    command = str(body.get("command") or "search").strip().lower()
    lines = [line for line in csv_data.strip().splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(lines) < 2:
        raise ValueError("Il CSV deve contenere un’intestazione e almeno una riga di dati")
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = [
        {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k and v and str(v).strip()}
        for row in reader
    ]
    if not rows:
        raise ValueError("Nessuna riga valida nel CSV")

    aliases = {
        "p.iva": "identificativo", "piva": "identificativo", "partita_iva": "identificativo",
        "vat": "identificativo", "organization": "identificativo", "company": "identificativo",
        "denominazione": "identificativo", "ragione_sociale": "identificativo", "cf": "codice_fiscale",
        "tax_code": "codice_fiscale", "province": "provincia", "municipality": "comune", "city": "comune",
        "sheet": "foglio", "parcel": "particella", "sub": "subalterno", "type": "tipo_catasto",
        "catasto": "tipo_catasto", "address": "indirizzo", "via": "indirizzo",
    }
    for row in rows:
        for alias, canonical in aliases.items():
            if alias in row and canonical not in row:
                row[canonical] = row.pop(alias)

    endpoint_map = {
        "search": "/visura", "intestati": "/visura/intestati", "soggetto": "/visura/soggetto",
        "persona-giuridica": "/visura/persona-giuridica", "elenco-immobili": "/visura/elenco-immobili",
        "indirizzo": "/visura/indirizzo", "partita": "/visura/partita",
    }
    api_path = endpoint_map.get(command, f"/visura/{command}")
    generic_commands = {form.command for form in QUERY_FORMS.values() if form.method == "generic_search"}
    return command, api_path, rows, generic_commands


@router.get("/api/ui/query-billing", response_class=JSONResponse)
async def web_api_query_billing(request: Request, user=Depends(_require_auth)):
    """The forms page uses this to explain query prices and the current wallet balance."""
    from . import purchases_billing

    is_admin = _is_admin(user)
    prices = await _query_product_prices()
    access_error = None if is_admin else await _query_access_error(user)
    enabled = is_admin or access_error is None
    key = _favorite_user_key(user)
    balance = None
    if enabled and key and not is_admin:
        try:
            balance = str(await purchases_billing.credit_balance(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Query-credit balance unavailable: %s", type(exc).__name__)
    return JSONResponse(
        {
            "enabled": enabled,
            "admin": is_admin,
            "detail": access_error.detail if access_error is not None else None,
            "prices": {
                path: str(cost) for path, cost in (prices or {}).items() if path.startswith("/visura")
            },
            "shared_fallback_path": purchases_billing.normalize_query_price_path(_QUERY_SHARED_PRICE_PATH),
            "shared_fallback_cost": str(
                (prices or {}).get(purchases_billing.normalize_query_price_path(_QUERY_SHARED_PRICE_PATH))
            ) if (prices or {}).get(purchases_billing.normalize_query_price_path(_QUERY_SHARED_PRICE_PATH)) is not None else None,
            "balance": balance,
            "batch_basis": "per_row_command_price_or_shared_fallback",
            "workflow_basis": "shared_fallback_per_planned_step",
        }
    )


@router.post("/api/ui/query-quote", response_class=JSONResponse)
async def web_api_query_quote(request: Request, user=Depends(_require_auth)):
    """Validate the form shape and quote the credits before its paid submission."""
    from . import purchases_billing

    cost_path = None
    allow_shared_fallback = False
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("La richiesta deve essere un oggetto JSON")
        kind = str(body.get("kind") or "single")
        if kind == "batch":
            _command, _api_path, rows, _generic = _parse_batch_rows(body)
            from .query_forms import command_for_path

            if not _is_admin(user) and command_for_path(_api_path) is None:
                raise ValueError("Tipo di query non disponibile")
            if not _is_admin(user) and len(rows) > 100:
                raise ValueError("Un batch può contenere al massimo 100 righe")
            units = len(rows)
            cost_path = _api_path
            allow_shared_fallback = True
        elif kind == "workflow":
            from .workflows import _missing_required_fields, prepare_workflow

            plan = prepare_workflow(body.get("payload") or {})
            missing = _missing_required_fields(plan.workflow)
            if missing:
                raise ValueError(f"Campi obbligatori mancanti: {', '.join(missing)}")
            units = len(plan.steps)
            cost_path = _QUERY_SHARED_PRICE_PATH
        else:
            endpoint = str(body.get("path") or "")
            endpoint = endpoint.removeprefix("/api/ui/").removeprefix("/visura/").removeprefix("/visura").strip("/")
            command = _web_query_command(endpoint)
            if not _is_admin(user) and command is None:
                raise ValueError("Tipo di query non disponibile")
            if command:
                from .query_forms import QUERY_FORMS

                cost_path = QUERY_FORMS[command].path
            units = 1
        if not units:
            raise ValueError("La richiesta non contiene query da eseguire")
    except (ValueError, TypeError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    access_error = await _query_access_error(user)
    if access_error is not None:
        return JSONResponse({"detail": access_error.detail}, status_code=access_error.status_code)
    cost, matched_price_path = (
        await _query_product_price(cost_path, allow_shared_fallback=allow_shared_fallback) if cost_path else (None, None)
    )
    balance = None
    if not _is_admin(user):
        try:
            balance = await purchases_billing.credit_balance(_favorite_user_key(user))
        except Exception as exc:  # noqa: BLE001
            logger.error("Portal query quote failed: %s", type(exc).__name__)
            return JSONResponse({"detail": "Il saldo crediti non è disponibile"}, status_code=503)
        if cost is None:
            return JSONResponse({"detail": "Il prezzo della query non è configurato"}, status_code=503)
    return JSONResponse(
        {
            "admin": _is_admin(user),
            "units": units,
            "unit_cost": str(cost) if cost is not None else "0",
            "total_cost": str(cost * units) if cost is not None else "0",
            "price_path": matched_price_path,
            "balance": str(balance) if balance is not None else None,
        }
    )


@router.post("/api/ui/{endpoint:path}", response_class=JSONResponse)
async def web_api_proxy(endpoint: str, request: Request, user=Depends(_require_auth)):
    """Submit a supported query; non-admin requests are charged and only receive owner-bound poll tokens."""
    import os

    import httpx

    from .client import VisuraAPIError, VisuraClient

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Corpo JSON non valido"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "La richiesta deve essere un oggetto JSON"}, status_code=400)
    base = f"http://localhost:{request.url.port or 8025}"
    api_key = os.getenv("API_KEY") or os.getenv("VISURA_API_KEY") or ""

    command = _web_query_command(endpoint)
    if command:
        from .query_forms import QUERY_FORMS

        paid_user = not _is_admin(user)
        product_path = QUERY_FORMS[command].path
        unit_cost, matched_price_path = await _query_product_price(product_path) if paid_user else (None, None)
        access_error = await _query_access_error(user)
        if access_error is not None:
            return JSONResponse({"detail": access_error.detail}, status_code=access_error.status_code)
        if paid_user and unit_cost is None:
            return JSONResponse({"detail": "Il prezzo per questo comando non è configurato"}, status_code=503)
        confirmation_error = _query_price_confirmation_error(user, body, 1, unit_cost, matched_price_path)
        if confirmation_error is not None:
            return confirmation_error
        charge, error_response = await _charge_portal_query(
            request, user, 1, uuid4().hex, unit_cost, matched_price_path
        )
        if error_response is not None:
            return error_response
        force = str(body.pop("force", "")).lower() in ("1", "true", "yes")
        try:
            content = await VisuraClient(base_url=base, api_key=api_key).submit(command, body, force=force)
        except ValueError as exc:
            await _refund_portal_query(charge, 1, "single-validation-failed")
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except VisuraAPIError as exc:
            await _refund_portal_query(charge, 1, "single-upstream-rejected")
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        except Exception as exc:  # noqa: BLE001 - charge is refunded when the local API call cannot complete
            logger.error("Portal query proxy failed: %s", type(exc).__name__)
            await _refund_portal_query(charge, 1, "single-proxy-failed")
            return JSONResponse({"detail": "La richiesta non è stata completata"}, status_code=502)
        if paid_user:
            try:
                content = _query_poll_tokens(content, _favorite_user_key(user))
            except Exception as exc:  # noqa: BLE001 - do not leave a paid request without a private result path
                logger.error("Portal result token creation failed: %s", type(exc).__name__)
                await _refund_portal_query(charge, 1, "single-result-access-failed")
                return JSONResponse({"detail": "Impossibile rendere disponibile il risultato"}, status_code=503)
        return JSONResponse(content=content)

    if not _is_admin(user):
        return JSONResponse({"detail": "Tipo di query non disponibile"}, status_code=403)

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        resp = await client.post(
            f"{base}/visura/{endpoint}",
            json=body,
            headers=_internal_api_headers() or None,
        )
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text or "Empty response", "status_code": resp.status_code}
    return JSONResponse(content=content, status_code=resp.status_code)


@router.get("/api/ui/visura/{request_id}", response_class=JSONResponse)
async def web_api_poll(request_id: str, request: Request, user=Depends(_require_auth)):
    """Poll a result by admin request id or by the signed token returned to its owner."""
    import httpx

    if _is_admin(user):
        upstream_request_id = request_id
    else:
        from . import purchases_billing

        user_key = _favorite_user_key(user)
        upstream_request_id = purchases_billing.request_id_from_query_token(request_id, user_key) if user_key else None
        if upstream_request_id is None:
            # Do not reveal whether an arbitrary request id exists.
            return JSONResponse({"detail": "Risultato non trovato"}, status_code=404)

    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(f"{base}/visura/{upstream_request_id}", headers=_internal_api_headers() or None)
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text or "Empty response", "status_code": resp.status_code}
    return JSONResponse(content=content, status_code=resp.status_code)
