"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
Auth: landing page is public; authenticated UI routes require authentication.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .database import (
    count_result_rows,
    count_total_result_rows,
    find_result_rows,
    get_all_documents,
    get_cadastral_sibling_visuras,
    get_db_owners_for_response,
    get_db_properties_for_response,
    get_document_by_id,
    get_document_structured_extraction,
    get_documents_for_response,
    get_indexed_file_metadata,
    get_result_record,
    record_activity,
)
from .form_config import get_available_form_groups, get_single_step_groups, get_workflow_groups
from .visura_view import build_visura_view

# Opendata API URL — workflow runs/steps are owned by opendata, not sister.
# Sister's web UI proxies workflow list/detail requests to opendata.
_OPENDATA_API_URL = os.getenv("OPENDATA_API_URL", "http://localhost:8024")


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
    """Classify a visura along the *Fabbricati e Terreni* facet."""
    dt = d.get("document_type") or ""
    tc = (d.get("tipo_catasto") or "").upper()
    if dt == "visura_terreni" or tc == "T":
        return "terreni"
    if dt == "visura_fabbricati" or tc == "F":
        return "fabbricati"
    return None


def _soggetto_kind(d: dict) -> str:
    """Persona Fisica vs Persona Giuridica.

    11-digit VAT numbers indicate Persona Giuridica; 16-char alphanumeric CFs
    indicate Persona Fisica. Explicit keywords in the name are also checked.
    """
    blob = ((d.get("oggetto") or "") + " " + (d.get("filename") or "")).lower()
    if "giuridic" in blob or "pnf" in blob or "_pg_" in blob or "persona_giuridica" in blob:
        return "pg"
    cf = _extract_cf(d)
    if cf and len(cf) == 11 and cf.isdigit():
        return "pg"
    return "pf"


_CF_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]|\d{11})(?![A-Z0-9])", re.IGNORECASE)


def _extract_cf(d: dict) -> str | None:
    """Return the fiscal code / VAT for the queried subject of a soggetto visura.

    For soggetto documents the intestati_json holds property co-owners, not the
    queried subject — so oggetto/filename (which always embeds the CF by naming
    convention) takes priority; intestati_json is a last-resort fallback for
    docs whose name contains no CF (e.g. "VISURA LORENZIN BARBARA").
    """
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
      3. visura_soggetto: document_type + CF + visura_subtype
      4. document_type + oggetto/filename                (fallback)

    response_id is honoured first so that, once live/backfilled data links files to
    their Richiesta, attachments group correctly without code changes.
    """
    from collections import defaultdict

    def _logical_key(d: dict):
        rid = d.get("response_id")
        dt = d.get("document_type") or ""
        if dt == "ispezione_ipotecaria":
            # Keep each report/RP note independent; only pair alternate formats
            # that share the same filename stem.
            return (rid, dt, Path(d.get("filename") or str(d.get("id"))).stem.casefold())
        # Soggetto visure: group by CF + subtype regardless of coordinates.
        # Coordinates on soggetto P7Ms are the first owned property, not the identity key.
        if dt == "visura_soggetto":
            cf = _extract_cf(d)
            if cf:
                return (rid, dt, cf, d.get("visura_subtype") or "", d.get("situazione_al") or "")
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

    groups: dict[object, list[dict]] = defaultdict(list)
    for d in docs:
        groups[_logical_key(d)].append(d)

    logical: list[dict] = []
    for items in groups.values():
        ordered = sorted(items, key=lambda x: (_fmt_rank(x), x.get("id") or 0))
        primary = dict(ordered[0])  # copy — primary carries the document metadata
        primary["files"] = [
            {
                "id": it.get("id"),
                "file_format": (it.get("file_format") or "").upper(),
                "filename": it.get("filename") or "",
                "size_human": _human_size(it["file_size"]) if it.get("file_size") else "",
            }
            for it in ordered
        ]
        primary["n_files"] = len(ordered)
        primary["n_pdf"] = sum(1 for f in primary["files"] if f["file_format"] == "PDF")
        primary_structured = (primary.get("file_format") or "").upper() in ("P7M", "XML")
        is_ispezione = (primary.get("document_type") or "") == "ispezione_ipotecaria"
        primary["viewable"] = is_ispezione or (
            primary_structured and (primary.get("document_type") or "") in _VIEWABLE_DOC_TYPES
        )
        if primary.get("document_type") == "visura_soggetto":
            cf = _extract_cf(primary)
            if cf and len(cf) == 16:
                primary["cf"] = cf
            elif cf and len(cf) == 11 and cf.isdigit():
                primary["vat"] = cf
        logical.append(primary)
    return logical


def _link_related_visura_docs(docs: list[dict]) -> None:
    """Attach reciprocal links between subject and property visure by CF/VAT.

    A subject visura's ``intestati_json`` lists co-owners of its properties, so
    the queried subject is identified only from the visura's object or filename.
    Property visure identify their owners through ``intestati_json``.
    """
    import json

    subjects_by_cf: dict[str, list[dict]] = {}
    properties_by_id: dict[int, dict] = {}

    def _subject_cf(doc: dict) -> str | None:
        for source in (doc.get("oggetto") or "", doc.get("filename") or ""):
            match = _CF_RE.search(source.upper())
            if match:
                return match.group(1).upper()
        return None

    def _owner_cfs(raw: Any) -> set[str]:
        if not raw:
            return set()
        try:
            rows = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return set()
        if not isinstance(rows, list):
            return set()
        return {
            str(row["CF"]).strip().upper()
            for row in rows
            if isinstance(row, dict) and row.get("CF")
        }

    for doc in docs:
        doc_type = doc.get("document_type") or ""
        if doc_type == "visura_soggetto":
            cf = _subject_cf(doc)
            if cf:
                subjects_by_cf.setdefault(cf, []).append(doc)
        elif doc_type in _VISURA_TYPES and doc.get("id") is not None:
            properties_by_id[doc["id"]] = doc

    subject_links: dict[int, dict[int, dict[str, Any]]] = {}
    property_links: dict[int, dict[int, dict[str, Any]]] = {}
    for property_doc in properties_by_id.values():
        property_id = property_doc["id"]
        for cf in _owner_cfs(property_doc.get("intestati_json")):
            for subject_doc in subjects_by_cf.get(cf, []):
                subject_id = subject_doc.get("id")
                if subject_id is None:
                    continue
                subject_links.setdefault(subject_id, {})[property_id] = {
                    "id": property_id,
                    "label": property_doc.get("oggetto") or property_doc.get("filename") or f"Doc {property_id}",
                }
                property_links.setdefault(property_id, {})[subject_id] = {
                    "id": subject_id,
                    "label": subject_doc.get("oggetto") or subject_doc.get("filename") or f"Doc {subject_id}",
                }

    for doc in docs:
        doc_id = doc.get("id")
        if doc.get("document_type") == "visura_soggetto":
            doc["related_immobile_docs"] = list(subject_links.get(doc_id, {}).values())
        elif (doc.get("document_type") or "") in _VISURA_TYPES:
            doc["related_soggetto_docs"] = list(property_links.get(doc_id, {}).values())


def _build_property_map(docs: list[dict]) -> dict:
    """Group documents by cadastral coordinates into a nested property map.

    Visura-soggetto documents are nested as owner children under property docs
    (visura_storica / visura_fabbricati / visura_terreni) whose ``intestati_json``
    lists their CF.  Documents not linked to any property parcel appear in the
    ``soggetti`` section grouped by person.

    Returns:
      ``immobili``: provincia → foglio → particella → subalterno → docs
      ``soggetti``: list of {cf, label, docs} for unlinked soggetto documents
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

    def _cf_from_oggetto(obj: str) -> Optional[str]:
        m = re.search(r"\(([A-Z0-9]{11,16})\)", obj or "")
        return m.group(1) if m else None

    def _cfs_from_intestati(istr: str) -> list[str]:
        try:
            seen, out = set(), []
            for row in json.loads(istr):
                cf = row.get("CF")
                if cf and cf not in seen:
                    seen.add(cf)
                    out.append(cf)
            return out
        except Exception:
            return []

    # Global CF → [soggetto docs] index (ALL visura_soggetto regardless of coords)
    cf_soggetti: dict[str, list[dict]] = {}
    for d in docs:
        if (d.get("document_type") or "") == _SOGGETTO_TYPE:
            cf = _cf_from_oggetto(d.get("oggetto") or "")
            if cf:
                cf_soggetti.setdefault(cf, []).append(d)

    # Separate docs with cadastral coords from the rest
    immobili_docs = [d for d in docs if d.get("foglio") and d.get("particella") and d.get("provincia")]

    # Track doc IDs claimed as owner children under a property doc
    claimed_ids: set[int] = set()

    def _owner_docs_for(property_doc: dict) -> list[dict]:
        """Return visura_soggetto docs for all owners in this property doc."""
        istr = property_doc.get("intestati_json")
        if not istr:
            return []
        result = []
        for cf in _cfs_from_intestati(istr):
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
                            "total_docs": sum(len(s["docs"]) for s in subs_list),
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
    return {"immobili": immobili_tree, "soggetti": _build_soggetti(immobili_tree)}


def _build_document_tree(docs: list[dict]) -> list[dict]:
    """Build the hierarchical document index using the unified query-type taxonomy.

    Single-step query groups (top-level), with subtypes as children:
      Visura per Immobile → Fabbricati / Terreni / (non classificate)
      Visura per Soggetto → Persona Fisica / Persona Giuridica
      Intestatari
      Planimetrie
      Elaborati Planimetrici → EPA
      Richieste

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

    # ── Visura per Immobile ────────────────────────────────────────────────
    # Subtype order and display metadata for grouping within Fabbricati / Terreni.
    _SUBTYPE_ORDER = [
        ("attuale", "Attuale", "fa-calendar-check", "success"),
        ("storica_analitica", "Storica Analitica", "fa-clock-rotate-left", "secondary"),
        ("storica_sintetica", "Storica Sintetica", "fa-clock-rotate-left", "secondary"),
        ("storica_completa", "Storica Completa", "fa-clock-rotate-left", "secondary"),
        ("storica", "Storica", "fa-clock-rotate-left", "secondary"),
        ("", "Non classificate", "fa-file-contract", "secondary"),
    ]

    def _catasto_subtype_leaves(
        items: list[dict], catasto_label: str, catasto_key: str, catasto_color: str
    ) -> list[dict]:
        """Flatten catasto type + subtype into sibling leaves: 'Fabbricati · Storica' etc."""
        from collections import defaultdict

        by_sub: dict[str, list[dict]] = defaultdict(list)
        for d in items:
            by_sub[d.get("visura_subtype") or ""].append(d)
        present_subs = [sub for sub, *_ in _SUBTYPE_ORDER if by_sub.get(sub)]
        if len(present_subs) <= 1:
            # Only one subtype — keep as simple leaf with just the catasto label
            return [
                leaf(
                    catasto_key,
                    catasto_label,
                    "fa-building" if catasto_key == "visura_fabbricati" else "fa-seedling",
                    catasto_color,
                    items,
                )
            ]
        result = []
        for sub, sub_label, icon, color in _SUBTYPE_ORDER:
            group = by_sub.get(sub, [])
            if not group:
                continue
            combined_label = f"{catasto_label} · {sub_label}" if sub else f"{catasto_label} · Non classificate"
            result.append(leaf(f"{catasto_key}_{sub or 'altro'}", combined_label, icon, color, group))
        return result

    visure = [d for d in docs if (d.get("document_type") or "") in _VISURA_TYPES]
    if visure:
        fab = [d for d in visure if _doc_catasto_ft(d) == "fabbricati"]
        ter = [d for d in visure if _doc_catasto_ft(d) == "terreni"]
        rest = [d for d in visure if _doc_catasto_ft(d) is None]
        children: list[dict] = []
        if fab:
            children.extend(_catasto_subtype_leaves(fab, "Fabbricati", "visura_fabbricati", "success"))
        if ter:
            children.extend(_catasto_subtype_leaves(ter, "Terreni", "visura_terreni", "warning"))
        if rest:
            children.append(leaf("visura_altro", "Non classificate", "fa-file-contract", "secondary", rest))
        tree.append(node("visura_immobile", "Visura per Immobile", "fa-house", "success", children, count=len(visure)))

    # ── Visura per Soggetto ────────────────────────────────────────────────
    soggetti = by_type.get("visura_soggetto", [])
    if soggetti:
        pf = [d for d in soggetti if _soggetto_kind(d) == "pf"]
        pg = [d for d in soggetti if _soggetto_kind(d) == "pg"]
        children = []
        if pf:
            children.append(leaf("soggetto_pf", "Persona Fisica", "fa-user", "info", pf, is_soggetto=True))
        if pg:
            children.append(leaf("soggetto_pg", "Persona Giuridica", "fa-building-user", "info", pg, is_soggetto=True))
        tree.append(
            node(
                "visura_soggetto",
                "Visura per Soggetto",
                "fa-user-group",
                "info",
                children,
                count=len(soggetti),
                is_soggetto=True,
            )
        )

    # ── Intestatari ────────────────────────────────────────────────────────
    if by_type.get("elenco_immobili"):
        tree.append(leaf("intestati", "Intestatari", "fa-users", "primary", by_type["elenco_immobili"]))

    # ── Planimetrie ────────────────────────────────────────────────────────
    if by_type.get("planimetria"):
        tree.append(leaf("planimetria", "Planimetrie", "fa-ruler-combined", "secondary", by_type["planimetria"]))

    # ── Elaborati Planimetrici ─────────────────────────────────────────────
    elab = by_type.get("elaborato_planimetrico", [])
    epa = by_type.get("epa", [])
    if elab or epa:
        children = [leaf("epa", "EPA", "fa-file-lines", "secondary", epa)] if epa else []
        tree.append(
            node(
                "epa",
                "Elaborati Planimetrici",
                "fa-drafting-compass",
                "secondary",
                children,
                count=len(elab) + len(epa),
                docs=elab,
            )
        )

    # ── Richieste ──────────────────────────────────────────────────────────
    if by_type.get("richieste"):
        tree.append(leaf("richieste", "Richieste", "fa-clock-rotate-left", "secondary", by_type["richieste"]))

    # ── Ispezioni ipotecarie ────────────────────────────────────────────────
    ispezioni = by_type.get("ispezione_ipotecaria", [])
    if ispezioni:
        tree.append(
            leaf(
                "ispezione_ipotecaria",
                "Ispezione ipotecaria",
                "fa-file-circle-check",
                "primary",
                ispezioni,
            )
        )

    return tree


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
    """Operators may drive the portal session (opt-in via SISTER_ADMIN_USERS; everybody is one with auth disabled)."""
    if not _auth_required():
        return True
    if not os.getenv("SISTER_ADMIN_USERS", "").strip():
        return True  # admin gating is opt-in: no admins configured -> every authenticated user (see main.py)
    from jinja2 import Undefined

    if user is None or isinstance(user, Undefined):  # templates rendered without a user (public pages)
        return False
    if getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).lower() == "admin":
        return True
    has_role = getattr(user, "has_role", None)
    return bool(callable(has_role) and has_role("admin"))


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
            try:
                response = await handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                from fastapi import HTTPException

                rejected = isinstance(exc, HTTPException) and exc.status_code < 500
                await record_activity(
                    action,
                    actor=_actor(user),
                    status="rejected" if rejected else "error",
                    params=params,
                    error=str(getattr(exc, "detail", None) or exc),
                    started_at=started,
                    client_ip=_client_ip(request),
                )
                raise
            code = getattr(response, "status_code", 200)
            await record_activity(
                action,
                actor=_actor(user),
                status="success" if code < 400 else ("rejected" if code < 500 else "error"),
                params=params,
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
    """Dashboard — service health and recent activity."""
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
    from .models import GenericSisterRequest
    from .missing_pairs import build_missing_pair_batch

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
async def web_retrieve_missing_pairs_status(request: Request, user=Depends(_require_auth)):
    async with _missing_pair_job_lock:
        return JSONResponse(_missing_pair_job_view(_missing_pair_job))


@router.get("/forms", response_class=HTMLResponse)
async def web_forms(request: Request, user=Depends(_require_auth)):
    """Query submission forms."""
    theme = _get_theme(request)
    return theme.render(
        "catasto/forms.html",
        request,
        user=user,
        form_groups=get_available_form_groups(),
        single_step_groups=get_single_step_groups(),
        workflow_groups=get_workflow_groups(),
    )


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
        limit=limit,
        offset=offset,
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
                "completed" if item.get("success") is True else "failed" if item.get("success") is False else "pending"
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
        "total": _build_url("/results", **stats_filters),
        "completed": _build_url("/results", status="completed", **stats_filters),
        "partial": _build_url("/results", status="partial", **stats_filters),
        "failed": _build_url("/results", status="failed", **stats_filters),
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
        source=source,
        status=status,
        limit=limit,
        offset=offset,
        prev_url=prev_url,
        next_url=next_url,
        stats_urls=stats_urls,
        active_filters=active_filters,
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


async def get_workflow_result_record(request_id: str) -> Optional[dict]:
    """Fetch a workflow run from opendata (returns None on miss or error)."""
    resp = await _opendata_get(f"/catasto/workflow/runs/{request_id}")
    if resp is not None and resp.status_code == 200:
        return resp.json()
    return None


@router.get("/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str, user=Depends(_require_auth)):
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
    result["documents"] = await get_documents_for_response(
        request_id,
        foglio=result.get("foglio"),
        particella=result.get("particella"),
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
    user=Depends(_require_auth),
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
async def web_workflow_detail(request: Request, workflow_id: str, user=Depends(_require_auth)):
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
    result["documents"] = await get_documents_for_response(
        workflow_id,
        foglio=result.get("foglio"),
        particella=result.get("particella"),
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
    return theme.render("profile.html", request, user=user or _get_user(request))


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
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


async def _geocode_address(query: str) -> dict[str, Any] | None:
    """Resolve an address to coordinates with OpenStreetMap Nominatim (no API key); results are cached per process."""
    key = re.sub(r"\s+", " ", query).strip().casefold()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    import httpx

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
        hit = {
            "lat": float(rows[0]["lat"]),
            "lng": float(rows[0]["lon"]),
            "label": rows[0].get("display_name", query),
            "kind": rows[0].get("type", ""),
        }
    _GEOCODE_CACHE[key] = hit
    return hit


@router.get("/3d", response_class=HTMLResponse)
async def web_building_3d(
    request: Request,
    q: str = "Via El Alamein 2, 48124 Ravenna",
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


def _file_icon(ext: str, is_dir: bool) -> tuple[str, str]:
    """Return (fa-icon-class, text-color-class) for a file extension."""
    if is_dir:
        return "fa-folder", "text-warning"
    return {
        ".pdf": ("fa-file-pdf", "text-danger"),
        ".p7m": ("fa-file-shield", "text-warning"),
        ".json": ("fa-file-code", "text-info"),
        ".xml": ("fa-file-code", "text-secondary"),
        ".csv": ("fa-file-csv", "text-success"),
        ".xlsx": ("fa-file-excel", "text-success"),
        ".xls": ("fa-file-excel", "text-success"),
        ".png": ("fa-file-image", "text-secondary"),
        ".jpg": ("fa-file-image", "text-secondary"),
        ".jpeg": ("fa-file-image", "text-secondary"),
        ".log": ("fa-scroll", "text-muted"),
        ".txt": ("fa-file-lines", "text-muted"),
        ".zip": ("fa-file-zipper", "text-secondary"),
    }.get(ext, ("fa-file", "text-muted"))


async def _render_doc_from_db(doc: dict, request, theme, user, force_template: str | None = None):
    """Finalize a doc dict fetched from the DB and render the matching visura template.

    By default the template is chosen from the XML root element. Pass
    ``force_template`` (e.g. "result_detail" or "result_detail.html") to override
    the auto-selection — used by ``?template=`` on the document route to render the
    generic, exhaustive view of any document.
    """
    doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
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
        )
    land_registry_kpis = None
    land_registry_history = None
    if land_map_kind == "soggetto":
        from .land_registry_kpis import subject_land_registry_kpis
        from .land_registry_history import subject_land_registry_history

        land_registry_kpis = await subject_land_registry_kpis(xml_p, map_lookup)
        land_registry_history = await subject_land_registry_history(xml_p)
    from .display import merge_identifiers, readable_owners, subject_for_theme

    doc = {**doc, "xml_parsed": merge_identifiers(subject_for_theme(readable_owners(xml_p)))}  # after the lookups above, which need the original tree
    return theme.render(
        "catasto/land_map.html",
        request,
        user=user,
        doc=doc,
        google_maps=map_lookup if land_map_kind == "fabbricati" else None,
        land_registry_map=doc.get("land_registry_map"),
        land_registry_kpis=land_registry_kpis,
        land_registry_history=land_registry_history,
        **_land_map_layout(land_map_kind),
    )


def _cadastral_map_lookup_key(item: dict, *, feature_type: str) -> str:
    """Build the same normalized location key used by the Sister PostGIS view."""
    fields = [
        item.get("municipality"),
        item.get("province"),
        item.get("municipality_code"),
        item.get("section"),
        item.get("sheet"),
    ]
    if feature_type == "parcel":
        fields.append(item.get("parcel"))
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
                """SELECT feature_type, lookup_key, municipality, province,
                          municipality_code, section, sheet, parcel, source_reference,
                          checked_at >= now() - interval '30 days' AS is_fresh,
                          checked_at IS NOT NULL AS is_checked,
                          ST_AsGeoJSON(geom, 6) AS geometry,
                          ST_X(ST_Centroid(geom)) AS longitude,
                          ST_Y(ST_Centroid(geom)) AS latitude
                   FROM sister.v_sister_visura_cadastral_polygons
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
                await cursor.executemany(
                    """INSERT INTO sister.cadastral_map_geometry_cache (
                           feature_type, lookup_key, municipality, province, municipality_code,
                           section, sheet, parcel, source_reference, geom, checked_at
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           ST_SetSRID(ST_GeomFromGeoJSON((%s)::text), 4326), now()
                       )
                       ON CONFLICT (feature_type, lookup_key) DO UPDATE SET
                           municipality = EXCLUDED.municipality,
                           province = EXCLUDED.province,
                           municipality_code = EXCLUDED.municipality_code,
                           section = EXCLUDED.section,
                           sheet = EXCLUDED.sheet,
                           parcel = EXCLUDED.parcel,
                           source_reference = EXCLUDED.source_reference,
                           geom = EXCLUDED.geom,
                           checked_at = now()""",
                    records,
                )
            await connection.commit()
    except Exception as exc:  # A cache write must never make the document page fail.
        logger.debug("Sister cadastral polygon cache write failed: %s: %s", type(exc).__name__, exc)


async def _google_maps_context(doc: dict) -> dict[str, Any] | None:
    """Build verified parcel coordinates plus Google Maps and Street View links."""
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
            return value
        return [value] if isinstance(value, dict) else []

    # Keep this source in sync with the two tables rendered by the subject
    # visura template: only current Fabbricati/Terreni rows are shown there.
    subject_table_identifiers = []
    for group in as_items(subject_body.get("GruppoUnitaImmobiliari")):
        for row_key in ("ImmobileFabbricatiS", "ImmobileTerreniS"):
            for property_row in as_items(group.get(row_key)):
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
        candidate_category = parcel_field(candidate, "category", "Categoria")
        if not (candidate_municipality and candidate_province and candidate_sheet and candidate_parcel):
            continue
        key = (
            candidate_municipality.casefold(), candidate_province.upper(), candidate_code.upper(),
            candidate_section.upper(), candidate_sheet.upper(), candidate_parcel.upper(),
        )
        if any(item["key"] == key for item in parcel_identifiers):
            continue
        parcel_identifiers.append({
            "key": key,
            "municipality": candidate_municipality,
            "province": candidate_province,
            "municipality_code": candidate_code.upper(),
            "section": candidate_section.upper(),
            "sheet": candidate_sheet,
            "parcel": candidate_parcel,
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
                    "reference": f"{item['municipality']} ({item['municipality_code']}) · Foglio {item['sheet']}",
                    "sheet": item["sheet"],
                    "section": item["section"],
                    "municipality": item["municipality"],
                    "municipality_code": item["municipality_code"],
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
        if use_cached_map_features(cached_map_rows, require_fresh=True):
            return context

    dsn = os.getenv("CADASTRAL_POSTGRES_DSN", "").strip()
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
                AS r(sheet_index, sheet_reference, section_rank)
        ), matched AS (
            SELECT w.sheet_index, r.section_rank, s.sheet_reference, s.geom
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
            SELECT *, max(section_rank) OVER (PARTITION BY sheet_index) AS best_section_rank
            FROM matched
        )
        SELECT sheet_index,
               count(DISTINCT sheet_reference) AS reference_count,
               min(ST_SRID(geom)) AS srid,
               max(ST_SRID(geom)) AS max_srid,
               ST_AsGeoJSON(ST_UnaryUnion(ST_Collect(geom)), 6) AS geometry
        FROM ranked
        WHERE section_rank = best_section_rank
        GROUP BY sheet_index
    """
    rows = []
    adjacent_rows = []
    marker_rows = []
    sheet_rows = []
    try:
        import psycopg

        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        async with await psycopg.AsyncConnection.connect(
            dsn,
            connect_timeout=2,
            options="-c default_transaction_read_only=on -c statement_timeout=2500",
        ) as connection:
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
            if parcel_identifiers:
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
                        logger.warning("Cadastral parcel markers unavailable: %s: %s", type(exc).__name__, exc)

                if is_subject_visura:
                    sheet_wanted = []
                    sheet_reference_wanted = []
                    for index, item in enumerate(sheet_identifiers):
                        sheet_values = {item["sheet"].upper()}
                        if item["sheet"].isdigit():
                            normalized_sheet = str(int(item["sheet"]))
                            sheet_values.update(normalized_sheet.zfill(width) for width in (2, 3, 4))
                            if int(normalized_sheet) >= 100 and int(normalized_sheet) % 100 == 0:
                                normalized_sheet = str(int(normalized_sheet) // 100)
                                sheet_values.add(normalized_sheet)
                                sheet_values.update(normalized_sheet.zfill(width) for width in (2, 3, 4))

                        section = item["section"].upper()
                        section_prefixes = []
                        if section:
                            section_prefixes.append((section, 2))
                            if len(section) > 1:
                                section_prefixes.append((section[:1], 1))
                        section_prefixes.append(("", 0))

                        reference_candidates = set()
                        for sheet_value in sheet_values:
                            numeric_sheet = str(int(sheet_value)) if sheet_value.isdigit() else sheet_value
                            padded_sheet = numeric_sheet.zfill(4) if numeric_sheet.isdigit() else numeric_sheet
                            for section_prefix, section_rank in section_prefixes:
                                for suffix in ("", "00"):
                                    reference = f"{item['municipality_code']}{section_prefix}{padded_sheet}{suffix}"
                                    reference_candidates.add((reference, section_rank))
                                    if section_prefix:
                                        reference_candidates.add((
                                            f"{item['municipality_code']}{section_prefix}_{padded_sheet}{suffix}",
                                            section_rank,
                                        ))
                                    else:
                                        reference_candidates.add((
                                            f"{item['municipality_code']}_{padded_sheet}{suffix}", section_rank,
                                        ))
                                        reference_candidates.add((
                                            f"{item['municipality_code']}{padded_sheet}{suffix}", section_rank,
                                        ))
                        sheet_wanted.append((
                            index, item["municipality"], item["province"], item["municipality_code"], section,
                        ))
                        sheet_reference_wanted.extend(
                            (index, reference, section_rank)
                            for reference, section_rank in sorted(reference_candidates)
                        )

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
                            logger.warning("Cadastral sheet geometries unavailable: %s: %s", type(exc).__name__, exc)
            if len(rows) == 1 and rows[0][0]:
                try:
                    adjacent_cursor = await connection.execute(adjacent_sql, (rows[0][0], rows[0][0]))
                    adjacent_rows = await adjacent_cursor.fetchall()
                except Exception as exc:
                    logger.warning("Adjacent cadastral parcels unavailable: %s", type(exc).__name__)
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
                    "reference": f"{item['municipality']} ({item['municipality_code']}) · Foglio {item['sheet']}",
                    "sheet": item["sheet"],
                    "section": item["section"],
                    "municipality": item["municipality"],
                    "municipality_code": item["municipality_code"],
                },
                "geometry": geometry,
            })
        context["mapped_sheet_count"] = len(context["sheet_features"])

    if document_id is not None:
        cache_records = []
        for index, item in enumerate(parcel_identifiers):
            geometry = parcel_geometries.get(index)
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
# `catasto/land_map.html` extends the parent and places the map in the slot.
_LAND_MAP_LAYOUTS: dict[str, tuple[str, str]] = {
    "fabbricati": ("catasto/visura_fabbricati.html", "after_query"),
    "terreni": ("catasto/visura_terreni.html", "after_query"),
    "soggetto": ("catasto/visura_soggetto.html", "sidecar"),
    "ipotecaria": ("catasto/ispezione_ipotecaria.html", "content"),
}


def _land_map_layout(kind: str) -> dict[str, str]:
    """Template variables for `catasto/land_map.html` for one document kind."""
    parent, slot = _LAND_MAP_LAYOUTS[kind]
    return {"land_map_parent": parent, "land_map_slot": slot}


async def _land_registry_map_context(lookup: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build an embeddable map, using a municipality center if parcel coordinates are unknown."""
    if not lookup or not (lookup.get("sheet") and lookup.get("parcel")):
        return None

    context: dict[str, Any] = {
        "available": False,
        "data_url": (
            f"/documents/{int(lookup['document_id'])}/parcel-map.json"
            if lookup.get("document_id") is not None else ""
        ),
        "sheet": lookup["sheet"],
        "parcel": lookup["parcel"],
        "municipality": lookup.get("municipality") or "",
        "province": lookup.get("province") or "",
        "parcel_markers": lookup.get("parcel_markers") or [],
        "parcel_features": lookup.get("parcel_features") or [],
        "sheet_features": lookup.get("sheet_features") or [],
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
    from pathlib import Path

    theme = _get_theme(request)
    base = _files_base()
    target = (base / path).resolve()

    if not str(target).startswith(str(base)):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists() or not target.is_file():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")

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
    if any(
        key in xml_parsed
        for key in ("VisuraFabbricatiStorica", "VisuraFabbricatiAttuale", "VisuraFabbricatiSintetica", "VisuraFabbricati")
    ):
        template = "catasto/visura_fabbricati.html"
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
        template = "catasto/visura_terreni.html"
    else:
        template = "result_detail.html"

    return theme.render(
        template,
        request,
        user=user,
        doc=doc,
        google_maps=await _google_maps_context(doc) if template == "catasto/visura_fabbricati.html" else None,
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

    from .database import get_all_documents

    base = _files_base()
    dest_dir = base / "named"
    dest_dir.mkdir(exist_ok=True)

    docs = await get_all_documents(limit=10000)
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

    from .land_registry_kpis import subject_land_registry_kpis
    from .pdf_typst import PdfExportError, build_context, pdf_filename, render_pdf

    doc = await get_document_by_id(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    xml_content = doc.pop("xml_content", "") or ""
    doc["xml_parsed"] = _parse_xml_to_dict(xml_content)
    doc["view"] = build_visura_view(xml_content)
    lookup = await _google_maps_context(doc)
    kpis = None
    if any(key in doc["xml_parsed"] for key in ("VisuraSoggettoAttuale", "VisuraSoggettoStorica",
                                                "VisuraSoggettoSintetica", "VisuraSoggetto")):
        kpis = await subject_land_registry_kpis(doc["xml_parsed"], lookup)
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
    from aecs4u_crypto import extract_p7m_payload, P7MError

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
    lines = [l.strip() for l in content.replace("\r", "").split("\n")]
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
            x = float(lines[i]); i += 1
            y = float(lines[i]); i += 1
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
    raw = [l.strip() for l in content.replace("\r", "").split("\n") if l.strip()]
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
            lower_names = {n.lower(): n for n in names}

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

    import asyncio as _asyncio

    result = await _asyncio.get_event_loop().run_in_executor(None, _build_plan_data, doc["file_path"])
    if result is None:
        raise HTTPException(status_code=422, detail="Plan data not available for this document")
    return JSONResponse(result)


@router.get("/documents/{doc_id}/geojson")
async def web_document_geojson(doc_id: int, request: Request, user=Depends(_require_auth)):
    """Return the GeoJSON payload extracted from a planimetria P7M document."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    doc = await get_document_by_id(doc_id)
    if doc is None or not doc.get("file_path"):
        raise HTTPException(status_code=404, detail="Document not found")

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
async def web_document_parcel_map_data(doc_id: int, user=Depends(_require_auth)):
    """Return the parcel geometries and markers for an authenticated document map."""
    from fastapi import HTTPException

    doc = await get_document_by_id(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    xml_content = doc.get("xml_content", "")
    doc["xml_parsed"] = _parse_xml_to_dict(xml_content)
    doc["view"] = build_visura_view(xml_content)
    lookup = await _google_maps_context(doc)
    if lookup is None:
        raise HTTPException(status_code=422, detail="Parcel map data is not available")

    return JSONResponse(
        {
            "parcel_markers": lookup.get("parcel_markers") or [],
            "parcel_features": {
                "type": "FeatureCollection",
                "features": lookup.get("parcel_features") or [],
            },
            "sheet_features": {
                "type": "FeatureCollection",
                "features": lookup.get("sheet_features") or [],
            },
            "unmapped_parcels": lookup.get("unmapped_parcel_items") or [],
        },
        headers={"Cache-Control": "private, max-age=300"},
    )


async def _backfill_document_metadata(base: Path, parsed_by_stem: dict) -> None:
    """Backfill document_metadata for existing visura_documents that lack it.

    Also repairs stale file_path values: pre-migration rows stored paths with an
    'outputs/documents/' prefix that no longer exists; the canonical location is
    under SISTER_FILES_BASE (= base).
    """
    import asyncio

    from sqlalchemy import text

    from .database import _get_session_factory, get_or_create_location, is_db_writable
    from .db_models import DocumentMetadata, VisuraDocument
    from .utils import _document_key, _parse_ispezione_ipotecaria_pdf, _parse_visura_xml, _persist_flattened_xml

    if not is_db_writable():
        return

    session_factory = _get_session_factory()

    # Older scans guessed these filenames as generic visure. Correct their type
    # even when their metadata rows already exist.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE visura_documents SET document_type = 'ispezione_ipotecaria' "
                "WHERE substr(lower(filename), 1, 4) = 'isp_' "
                "AND document_type <> 'ispezione_ipotecaria'"
            )
        )
        await session.commit()

    # Step 1: collect all docs with a file_path but missing document_metadata.
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT vd.id, vd.file_path, vd.filename, vd.document_type"
                    " FROM visura_documents vd"
                    " LEFT JOIN document_metadata dm ON vd.id = dm.id"
                    " WHERE vd.file_path IS NOT NULL AND dm.id IS NULL"
                )
            )
        ).fetchall()

    if not rows:
        return

    # Step 2: build a filename → absolute path map from disk to fix stale paths.
    name_to_path: dict[str, Path] = {}
    for fpath in base.rglob("*"):
        if fpath.is_file():
            name_to_path[fpath.name] = fpath

    fixed = 0
    backfilled = 0

    for doc_id, stored_path, filename, doc_type in rows:
        actual: Path | None = None

        # First try the stored path as-is.
        if stored_path and Path(stored_path).is_file():
            actual = Path(stored_path)
        elif filename and filename in name_to_path:
            actual = name_to_path[filename]

        if actual is None:
            logger.debug("Backfill: file not found for doc %d (%s)", doc_id, filename)
            continue

        # Fix stale path if necessary.
        correct_path = str(actual)
        if stored_path != correct_path:
            async with session_factory() as session:
                await session.execute(
                    text("UPDATE visura_documents SET file_path = :p WHERE id = :id"),
                    {"p": correct_path, "id": doc_id},
                )
                await session.commit()
            fixed += 1

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
            location_id = await get_or_create_location(
                session,
                cadastre_type=parsed.get("tipo_catasto") or "",
                province=parsed.get("provincia") or "",
                municipality=parsed.get("comune") or "",
                sheet=foglio,
                parcel=particella,
                subunit=parsed.get("subalterno") or "",
                section=parsed.get("sezione_urbana") or "",
            )
            meta = DocumentMetadata(
                id=doc_id,
                location_id=location_id,
                view_subtype=parsed.get("visura_subtype") or None,
                reference_date=parsed.get("situazione_al") or None,
                content=xml_content,
            )
            session.add(meta)
            await session.flush()
            await _persist_flattened_xml(session, doc_id, xml_content)
            await session.commit()
        backfilled += 1

    if fixed or backfilled:
        logger.info("Rescan backfill: %d path(s) fixed, %d metadata row(s) created", fixed, backfilled)


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

    _EXT_FORMAT = {".pdf": "PDF", ".xml": "XML", ".p7m": "P7M"}
    _NAME_TYPE = [
        ("isp_", "ispezione_ipotecaria"),
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
        else:
            # PDF: try to inherit metadata from a paired P7M/XML with the same stem (or the same without the date)
            parsed = parsed_by_stem.get(fpath.stem) or parsed_by_stem.get(_document_key(fpath.stem))
            if fpath.name.casefold().startswith("isp_"):
                parsed = await asyncio.to_thread(_parse_ispezione_ipotecaria_pdf, str(fpath))
            elif parsed is None:
                # No paired structured file — parse PDF content directly
                parsed = await asyncio.to_thread(_parse_visura_pdf, str(fpath))

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

    # Backfill document_metadata for existing docs that are missing it.
    # Also fix stale file_path values (old path had 'outputs/documents/' prefix
    # which no longer exists; actual files are under SISTER_FILES_BASE).
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


@router.get("/documents", response_class=HTMLResponse)
@router.get("/documents/{path:path}", response_class=HTMLResponse)
async def web_documents(
    request: Request, path: str = "", template: str = "", view: str = "", user=Depends(_require_auth)
):
    """Documents hub — single-step query responses.

    Root (``/documents``) shows the category, location, and filesystem views in
    one tabbed hub. ``?view=map`` and ``?view=files`` select the matching tab;
    filesystem paths keep their location while browsing. Pure-integer paths
    dispatch to the single-document viewer.

    Query params:
        view: ``map`` to select the location tab or ``files`` to select the filesystem tab.
        template: force a viewer template, e.g. ``?template=result_detail`` for
            the exhaustive field-by-field view.
    """

    theme = _get_theme(request)

    # Normalize trailing slash (e.g. /documents/21/ → "21")
    path = path.strip("/")

    # Integer path → DB-backed structured single-document viewer
    if path and path.isdigit():
        doc = await get_document_by_id(int(path))
        if doc is None:
            return theme.render("result_detail.html", request, user=user, result=None, request_id=path, not_found=True)
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
            return theme.render(
                "catasto/land_map.html",
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
        files = await get_all_documents(limit=10000)
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

    base = _files_base()
    target = (base / path).resolve() if path else base

    # Prevent path traversal
    if not str(target).startswith(str(base)):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found")

    # Serve files directly
    if target.is_file():
        return FileResponse(str(target), filename=target.name)

    # Build indexed-file map (file_path → {id, oggetto}) for linking files to DB rows
    try:
        indexed = await get_indexed_file_metadata()
    except Exception:
        indexed = {}

    entries = []
    total_size = 0
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        rel_path = str(child.relative_to(base))
        is_dir = child.is_dir()
        ext = child.suffix.lower() if not is_dir else ""
        size_bytes = stat.st_size if not is_dir else 0
        total_size += size_bytes

        # Sub-directory: count direct children
        child_count: Optional[int] = None
        if is_dir:
            try:
                child_count = sum(1 for _ in child.iterdir())
            except OSError:
                pass

        icon, icon_color = _file_icon(ext, is_dir)
        meta = indexed.get(str(child)) if not is_dir else None
        doc_id = meta["id"] if meta else None
        new_name = meta["oggetto"] if meta else None

        entries.append(
            {
                "name": child.name,
                "new_name": new_name or "",
                "path": rel_path,
                "is_dir": is_dir,
                "ext": ext,
                "size_bytes": size_bytes,
                "size_human": (
                    _human_size(size_bytes)
                    if not is_dir
                    else (f"{child_count} items" if child_count is not None else "")
                ),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "icon": icon,
                "icon_color": icon_color,
                "doc_id": doc_id,
            }
        )

    # Breadcrumbs
    parts = [p for p in path.split("/") if p] if path else []
    breadcrumbs = [{"name": base.name, "path": ""}]
    for i, part in enumerate(parts):
        breadcrumbs.append({"name": part, "path": "/".join(parts[: i + 1])})

    n_dirs = sum(1 for e in entries if e["is_dir"])
    n_files = len(entries) - n_dirs

    active_view = (
        "files"
        if path or view == "files"
        else "location"
        if view in ("map", "tree")
        else "categories"
    )
    try:
        document_files = await get_all_documents(limit=10000)
    except Exception:
        if active_view != "files":
            raise
        logger.exception("Document index unavailable while opening filesystem browser")
        document_files = []
    logical = _collapse_to_logical_docs(document_files)
    _link_related_visura_docs(logical)
    tree = _build_document_tree(logical)
    prop_map = _build_property_map(logical)
    return theme.render(
        "documents.html",
        request,
        user=user,
        tree=tree,
        prop_map=prop_map,
        total=len(logical),
        document_file_count=len(document_files),
        active_view=active_view,
        entries=entries,
        current_path=path,
        breadcrumbs=breadcrumbs,
        n_dirs=n_dirs,
        fs_n_files=n_files,
        total_size=_human_size(total_size) if total_size else None,
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
async def web_dossier_view(request: Request, path: str, user=Depends(_require_auth)):
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
async def web_dossiers(request: Request, path: str = "", download: str = "", user=Depends(_require_auth)):
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
async def web_browser_status(request: Request, user=Depends(_require_auth)):
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
    """Parse CSV text and submit each row as a separate API request."""
    import csv
    import io

    import httpx

    from .query_forms import QUERY_FORMS

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
        "p.iva": "identificativo",
        "piva": "identificativo",
        "partita_iva": "identificativo",
        "vat": "identificativo",
        "organization": "identificativo",
        "company": "identificativo",
        "denominazione": "identificativo",
        "ragione_sociale": "identificativo",
        "cf": "codice_fiscale",
        "tax_code": "codice_fiscale",
        "province": "provincia",
        "municipality": "comune",
        "city": "comune",
        "sheet": "foglio",
        "parcel": "particella",
        "sub": "subalterno",
        "type": "tipo_catasto",
        "catasto": "tipo_catasto",
        "address": "indirizzo",
        "via": "indirizzo",
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
    generic_commands = {
        form.command for form in QUERY_FORMS.values() if form.method == "generic_search"
    }

    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i, row in enumerate(rows):
            try:
                if command in generic_commands:
                    resp = await client.post(f"{base}{api_path}", params=row, headers=_internal_api_headers())
                else:
                    resp = await client.post(f"{base}{api_path}", json=row, headers=_internal_api_headers())
                result = {
                    "row": i + 1,
                    "status": "submitted" if resp.is_success else "error",
                    "data": resp.json(),
                }
                if not resp.is_success:
                    result["http_status"] = resp.status_code
                results.append(result)
            except Exception as e:
                results.append({"row": i + 1, "status": "error", "error": str(e)})

    return JSONResponse(
        {
            "command": command,
            "total_rows": len(rows),
            "results": results,
        }
    )


@router.post("/api/ui/workflow/stream")
async def web_api_workflow_stream(request: Request, user=Depends(_require_auth)):
    """Run a multi-step workflow through Sister's own query API and stream progress."""
    body = await request.json()
    try:
        from .workflows import prepare_workflow

        plan = prepare_workflow(body)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    from .workflows import run_workflow_stream

    base_url = os.getenv("VISURA_API_URL") or "http://127.0.0.1:8025"
    api_key = os.getenv("VISURA_API_KEY") or os.getenv("API_KEY")

    async def stream_events():
        async for event in run_workflow_stream(plan, base_url=base_url, api_key=api_key):
            yield f"data: {event}\n\n"

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _internal_api_headers() -> dict:
    """Headers for the page's calls to the service's own API (it requires X-API-Key when API_KEY is set)."""
    api_key = os.getenv("API_KEY") or os.getenv("VISURA_API_KEY") or ""
    return {"X-API-Key": api_key} if api_key else {}


def _web_query_command(endpoint: str) -> Optional[str]:
    """The ``sister query`` command a /forms endpoint submits (None for workflows, batch, ipotecaria...)."""
    from .query_forms import command_for_path

    return command_for_path("/visura/" + endpoint.strip("/")) if endpoint.strip("/") else "search"


@router.post("/api/ui/{endpoint:path}", response_class=JSONResponse)
async def web_api_proxy(endpoint: str, request: Request, user=Depends(_require_auth)):
    """Submit /forms requests: single-step queries go through the CLI's client code, the rest is proxied."""
    import os

    import httpx

    from .client import VisuraAPIError, VisuraClient

    body = await request.json()
    base = f"http://localhost:{request.url.port or 8025}"
    api_key = os.getenv("API_KEY") or os.getenv("VISURA_API_KEY") or ""

    command = _web_query_command(endpoint)
    if command:
        force = str(body.pop("force", "")).lower() in ("1", "true", "yes")
        try:
            content = await VisuraClient(base_url=base, api_key=api_key).submit(command, body, force=force)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except VisuraAPIError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return JSONResponse(content=content)

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
    """Poll for result status (proxy)."""
    import httpx

    base = f"http://localhost:{request.url.port or 8025}"

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(f"{base}/visura/{request_id}", headers=_internal_api_headers() or None)
    try:
        content = resp.json()
    except Exception:
        content = {"error": resp.text or "Empty response", "status_code": resp.status_code}
    return JSONResponse(content=content, status_code=resp.status_code)
