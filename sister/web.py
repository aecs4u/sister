"""Web UI routes for sister.

Serves HTML pages via aecs4u-theme and proxies API calls for form submissions.
Auth: landing page is public; /web/* routes require authentication.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from .database import (
    count_result_rows,
    count_total_result_rows,
    find_result_rows,
    get_all_documents,
    get_document_by_id,
    get_documents_for_response,
    get_indexed_file_metadata,
    get_result_record,
)
from .form_config import get_available_form_groups, get_single_step_groups, get_workflow_groups

# Opendata API URL — workflow runs/steps are owned by opendata, not sister.
# Sister's web UI proxies workflow list/detail requests to opendata.
_OPENDATA_API_URL = os.getenv("OPENDATA_API_URL", "http://localhost:8024")


# Base directory for the document browser (/web/documents).
# Defaults to the parent of the DB data folder (the project data root).
def _files_base() -> "Path":
    from pathlib import Path

    from .database import DB_PATH

    data_root = Path(DB_PATH).parent.parent
    return Path(os.getenv("SISTER_FILES_BASE", str(data_root / "documents"))).resolve()


logger = logging.getLogger("sister")

router = APIRouter(tags=["Web UI"])


# Document types that render in a dedicated visura template (so "Apri" is meaningful);
# others only offer the exhaustive view + download.
_VIEWABLE_DOC_TYPES = {"visura_fabbricati", "visura_storica", "visura_terreni", "visura_soggetto"}

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
        primary["viewable"] = primary_structured and (primary.get("document_type") or "") in _VIEWABLE_DOC_TYPES
        if primary.get("document_type") == "visura_soggetto":
            cf = _extract_cf(primary)
            if cf and len(cf) == 16:
                primary["cf"] = cf
            elif cf and len(cf) == 11 and cf.isdigit():
                primary["vat"] = cf
        logical.append(primary)
    return logical


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
        """Soggetti not linked to any property doc, grouped by person (CF)."""
        by_cf: dict[str, dict] = {}
        for d in docs:
            if (d.get("document_type") or "") != _SOGGETTO_TYPE:
                continue
            if d.get("id") in claimed_ids:
                continue
            oggetto = d.get("oggetto") or ""
            m = re.search(r"\(([A-Z0-9]{11,16})\)", oggetto)
            cf = m.group(1) if m else "__no_cf__"
            label = oggetto[: m.start()].strip() if m else (oggetto or d.get("filename") or "—")
            if cf not in by_cf:
                by_cf[cf] = {"cf": cf if cf != "__no_cf__" else None, "label": label, "docs": []}
            by_cf[cf]["docs"].append(_decorate_one(d))
        for entry in by_cf.values():
            entry["docs"].sort(key=lambda d: d.get("filename") or "")
        return sorted(by_cf.values(), key=lambda x: x["label"])

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

    return tree


def _dossiers_base() -> "Path":
    """Filesystem root holding dossier JSON files (multi/single-step query responses)."""
    from pathlib import Path

    from .database import DB_PATH

    data_root = Path(DB_PATH).parent.parent
    return Path(os.getenv("SISTER_DOSSIERS_BASE", str(data_root / "dossiers"))).resolve()


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
        ok = data.get("success")
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
        "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
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
    """Redirect /dashboard to /web/."""
    return RedirectResponse(url="/web/")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("landing.html", request, user=user)


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
        "index.html",
        request,
        user=user,
        stats=stats,
        recent=recent,
        auth_status=_get_auth_status(),
    )


@router.get("/web/forms", response_class=HTMLResponse)
async def web_forms(request: Request, user=Depends(_require_auth)):
    """Query submission forms."""
    theme = _get_theme(request)
    return theme.render(
        "forms.html",
        request,
        user=user,
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
        "results.html",
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


@router.get("/web/results/{request_id}", response_class=HTMLResponse)
async def web_result_detail(request: Request, request_id: str, user=Depends(_require_auth)):
    """Single result detail page."""
    theme = _get_theme(request)
    result = await get_result_record(request_id)
    if result is None and request_id.startswith("wf_"):
        # Workflow runs are stored in opendata — proxy the lookup
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{_OPENDATA_API_URL}/catasto/workflow/runs/{request_id}")
                if resp.status_code == 200:
                    result = resp.json()
        except Exception as exc:
            logger.warning("Could not fetch workflow %s from opendata: %s", request_id, exc)
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
    return theme.render(
        "result_detail.html",
        request,
        user=user,
        result=result,
        request_id=request_id,
        not_found=False,
    )


@router.get("/web/workflows", response_class=HTMLResponse)
async def web_workflows(
    request: Request,
    user=Depends(_require_auth),
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Workflow runs list — proxied from opendata (which owns workflow storage)."""
    import httpx

    theme = _get_theme(request)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    runs: list[dict] = []
    try:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_OPENDATA_API_URL}/catasto/workflow/runs",
                params=params,
            )
            if resp.status_code == 200:
                runs = resp.json().get("runs", [])
    except Exception as exc:
        logger.warning("Could not fetch workflow runs from opendata: %s", exc)

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
        auth_status=_get_auth_status(),
    )


@router.get("/web/workflows/{workflow_id}", response_class=HTMLResponse)
async def web_workflow_detail(request: Request, workflow_id: str, user=Depends(_require_auth)):
    """Workflow detail page — proxied from opendata."""
    import httpx

    theme = _get_theme(request)

    result: Optional[dict] = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_OPENDATA_API_URL}/catasto/workflow/runs/{workflow_id}",
            )
            if resp.status_code == 200:
                result = resp.json()
    except Exception as exc:
        logger.warning("Could not fetch workflow %s from opendata: %s", workflow_id, exc)

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
        doc.pop("xml_content", None)
    result["page_visit_rows"] = []
    return theme.render(
        "workflow_detail.html",
        request,
        user=user,
        result=result,
        workflow_id=workflow_id,
    )


@router.get("/web/about", response_class=HTMLResponse)
async def web_about(request: Request):
    """About page (public)."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("about.html", request, user=user)


@router.get("/web/privacy", response_class=HTMLResponse)
async def web_privacy(request: Request):
    """Privacy policy (public)."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("privacy_policy.html", request, user=user)


@router.get("/web/guide", response_class=HTMLResponse)
async def web_guide(request: Request):
    """User guide for the SISTER portal."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("guide.html", request, user=user)


@router.get("/web/cheatsheet", response_class=HTMLResponse)
async def web_cheatsheet(request: Request):
    """Quick-reference cheat sheet for SISTER."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("cheatsheet.html", request, user=user)


@router.get("/web/glossary", response_class=HTMLResponse)
async def web_glossary(request: Request):
    """Glossary of document types and cadastral terms."""
    theme = _get_theme(request)
    user = _get_user(request)
    return theme.render("glossary.html", request, user=user)


# ---------------------------------------------------------------------------
# Document browser + structured viewers  (/web/documents/*)
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
        ".sqlite": ("fa-database", "text-primary"),
        ".log": ("fa-scroll", "text-muted"),
        ".txt": ("fa-file-lines", "text-muted"),
        ".zip": ("fa-file-zipper", "text-secondary"),
    }.get(ext, ("fa-file", "text-muted"))


def _render_doc_from_db(doc: dict, request, theme, user, force_template: str | None = None):
    """Finalize a doc dict fetched from the DB and render the matching visura template.

    By default the template is chosen from the XML root element. Pass
    ``force_template`` (e.g. "result_detail" or "result_detail.html") to override
    the auto-selection — used by ``?template=`` on the document route to render the
    generic, exhaustive view of any document.
    """
    doc["xml_parsed"] = _parse_xml_to_dict(doc.get("xml_content", ""))
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

    if "VisuraFabbricatiStorica" in xml_p or "VisuraFabbricati" in xml_p:
        template = "visura_fabbricati_storica.html"
    elif "VisuraSoggettoAttuale" in xml_p or "VisuraSoggettoStorica" in xml_p:
        template = "visura_soggetto_attuale.html"
    elif "VisuraTerreniAttuale" in xml_p or "VisuraTerrenoStorica" in xml_p or "VisuraTerreno" in xml_p:
        template = "visura_terreni_attuale.html"
    else:
        template = "result_detail.html"
    if template == "result_detail.html":
        result = _doc_as_result(doc)
        return theme.render(
            "result_detail.html",
            request,
            user=user,
            result=result,
            request_id=str(doc.get("id") or doc.get("filename") or ""),
        )
    return theme.render(template, request, user=user, doc=doc)


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
        "page_visit_rows": [],
    }


@router.get("/web/documents/view/{path:path}", response_class=HTMLResponse)
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
        from .utils import _extract_p7m

        extracted = _extract_p7m(str(target))
        if extracted and Path(extracted).exists():
            xml_content = Path(extracted).read_text(encoding="utf-8", errors="ignore")
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
    if "VisuraFabbricatiStorica" in xml_parsed or "VisuraFabbricati" in xml_parsed:
        template = "visura_fabbricati_storica.html"
    elif "VisuraSoggettoAttuale" in xml_parsed or "VisuraSoggettoStorica" in xml_parsed:
        template = "visura_soggetto_attuale.html"
    elif "VisuraTerreniAttuale" in xml_parsed or "VisuraTerrenoStorica" in xml_parsed or "VisuraTerreno" in xml_parsed:
        template = "visura_terreni_attuale.html"
    else:
        template = "result_detail.html"

    return theme.render(template, request, user=user, doc=doc)


@router.get("/web/files")
@router.get("/web/files/{path:path}")
async def web_files_redirect(request: Request, path: str = ""):
    target = "/web/documents" + (f"/{path}" if path else "")
    return RedirectResponse(url=target, status_code=301)


@router.post("/web/documents/export-named")
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


@router.get("/web/documents/{doc_id}/view")
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


@router.get("/web/documents/{doc_id}/download")
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


@router.post("/web/documents/rescan", response_class=HTMLResponse)
async def web_documents_rescan(request: Request, user=Depends(_require_auth)):
    """Scan the documents directory for files not yet indexed in the DB and register them."""
    import asyncio

    from .database import get_indexed_file_paths, get_indexed_filenames
    from .utils import _parse_visura_pdf, _parse_visura_xml, _save_documents_to_db

    base = _files_base()
    if not base.exists():
        return RedirectResponse("/web/documents", status_code=303)

    indexed_paths = set((await get_indexed_file_paths()).keys())
    indexed_names = await get_indexed_filenames()

    _EXT_FORMAT = {".pdf": "PDF", ".xml": "XML", ".p7m": "P7M"}
    _NAME_TYPE = [
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
        else:
            # PDF: try to inherit metadata from a paired P7M/XML with the same stem
            parsed = parsed_by_stem.get(fpath.stem)
            if parsed is None:
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

    return RedirectResponse("/web/documents", status_code=303)


@router.get("/web/documents", response_class=HTMLResponse)
@router.get("/web/documents/{path:path}", response_class=HTMLResponse)
async def web_documents(
    request: Request, path: str = "", template: str = "", view: str = "", user=Depends(_require_auth)
):
    """Documents hub — single-step query responses.

    Root (``/web/documents``) shows the hierarchical per-type index built from the
    indexed ``visura_documents`` rows. ``?view=files`` browses the raw documents
    filesystem instead. Pure-integer paths dispatch to the single-document viewer.

    Query params:
        view: ``files`` to show the raw filesystem browser at the root.
        template: force a viewer template, e.g. ``?template=result_detail`` for
            the exhaustive field-by-field view.
    """

    theme = _get_theme(request)

    # Normalize trailing slash (e.g. /web/documents/21/ → "21")
    path = path.strip("/")

    # Integer path → DB-backed structured single-document viewer
    if path and path.isdigit():
        doc = await get_document_by_id(int(path))
        if doc is None:
            return theme.render("result_detail.html", request, user=user, result=None, request_id=path, not_found=True)
        return _render_doc_from_db(doc, request, theme, user, force_template=template or None)

    # Root with no explicit path → hierarchical document index (unless browsing files)
    if not path and view != "files":
        files = await get_all_documents(limit=10000)
        logical = _collapse_to_logical_docs(files)
        if view == "map":
            prop_map = _build_property_map(logical)
            return theme.render(
                "documents_map.html",
                request,
                user=user,
                prop_map=prop_map,
                total=len(logical),
                n_files=len(files),
            )
        tree = _build_document_tree(logical)
        return theme.render(
            "documents_index.html",
            request,
            user=user,
            tree=tree,
            total=len(logical),
            n_files=len(files),
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

    return theme.render(
        "files_browser.html",
        request,
        user=user,
        entries=entries,
        current_path=path,
        breadcrumbs=breadcrumbs,
        base_name=base.name,
        n_dirs=n_dirs,
        n_files=n_files,
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


@router.get("/web/dossiers/view/{path:path}", response_class=HTMLResponse)
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


@router.get("/web/dossiers", response_class=HTMLResponse)
@router.get("/web/dossiers/{path:path}", response_class=HTMLResponse)
async def web_dossiers(request: Request, path: str = "", download: str = "", user=Depends(_require_auth)):
    """Dossiers hub — multi-step (workflow) and single-step query responses.

    Root lists the dossier JSON files; a file path serves the raw JSON
    (``?download=1`` forces an attachment). Use /web/dossiers/view/<file> for the
    rendered view.
    """
    import json as _json

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

    # Root → list dossier files with extracted metadata
    dossiers: list[dict] = []
    if base.exists():
        for child in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True):
            if not child.is_file() or child.suffix.lower() != ".json":
                continue
            try:
                stat = child.stat()
                data = _json.loads(child.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            dossiers.append(_dossier_meta(child.name, data, stat.st_size, stat.st_mtime))

    # Group by query type (unified taxonomy), then by subtype within each group
    from collections import defaultdict

    def _collapse_pairs(entries: list[dict]) -> list[dict]:
        """Collapse wf_pair entries sharing the same group_key into one paired card."""
        pair_groups: dict[str, list[dict]] = defaultdict(list)
        for d in entries:
            pair_groups[d.get("group_key", f"solo:{d['name']}")].append(d)
        out: list[dict] = []
        for gk, peers in pair_groups.items():
            if gk.startswith("wf_pair:") and len(peers) > 1:
                base_name = gk[len("wf_pair:") :]
                primary = sorted(peers, key=lambda p: p["mtime"])[0]
                total_results = sum(p.get("n_results", 0) for p in peers)
                merged = dict(primary)
                merged.update(
                    {
                        "title": base_name.replace("_", " "),
                        "paired": True,
                        "peers": sorted(peers, key=lambda p: p["subtype"]),
                        "ok": all(p.get("ok") is not False for p in peers),
                        "n_results": total_results,
                        "badges": [f"{total_results} risultati"] if total_results else [],
                        "response_meta": {
                            "n_results": total_results,
                            "exported_at": primary.get("response_meta", {}).get("exported_at", ""),
                        },
                    }
                )
                out.append(merged)
            else:
                out.extend(peers)
        return out

    # Bucket dossiers by query group
    q_buckets: dict[str, list[dict]] = defaultdict(list)
    for d in dossiers:
        q_buckets[_dossier_query_group(d["kind"], d["subtype"])].append(d)

    groups = []
    for qkey in (
        "visura_immobile",
        "visura_soggetto",
        "intestati",
        "planimetria",
        "epa",
        "richieste",
        "workflow",
        "batch",
        "altro",
    ):
        entries = q_buckets.get(qkey)
        if not entries:
            continue
        label, icon, color = _QUERY_GROUP_META[qkey]

        collapsed = _collapse_pairs(entries)

        # Subgroup by subtype (+ kind for multi_response disambiguation)
        sub_buckets: dict[str, list[dict]] = defaultdict(list)
        for d in collapsed:
            skey = d["subtype"] if d["kind"] != "multi_response" else "multi_response"
            sub_buckets[skey].append(d)

        subgroups = []
        for skey, sentries in sub_buckets.items():
            smeta = _QUERY_SUBGROUP_META.get(skey)
            if smeta:
                slabel, sicon, scolor = smeta
            else:
                slabel = skey.replace("-", " ").replace("_", " ").title()
                sicon = "fa-file"
                scolor = color
            subgroups.append(
                {
                    "key": f"{qkey}_{skey}",
                    "label": slabel,
                    "icon": sicon,
                    "color": scolor,
                    "count": len(sentries),
                    "entries": sentries,
                }
            )
        subgroups.sort(key=lambda g: (g["label"] == "Altro", g["label"]))

        groups.append(
            {
                "key": qkey,
                "label": label,
                "icon": icon,
                "color": color,
                "count": len(collapsed),
                "subgroups": subgroups,
            }
        )

    return theme.render(
        "dossiers_index.html",
        request,
        user=user,
        groups=groups,
        total=len(dossiers),
    )


# ---------------------------------------------------------------------------
# Browser session control
# ---------------------------------------------------------------------------


@router.get("/web/browser", response_class=HTMLResponse)
async def web_browser(request: Request, user=Depends(_require_auth)):
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


@router.get("/web/browser/status", response_class=JSONResponse)
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


@router.post("/web/browser/start", response_class=JSONResponse)
async def web_browser_start(request: Request, user=Depends(_require_auth)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.start_browser()
    return JSONResponse(result)


@router.post("/web/browser/stop", response_class=JSONResponse)
async def web_browser_stop(request: Request, force: bool = False, user=Depends(_require_auth)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.stop_browser(force=force)
    return JSONResponse(result)


@router.post("/web/browser/restart", response_class=JSONResponse)
async def web_browser_restart(request: Request, user=Depends(_require_auth)):
    from .main import visura_service

    if visura_service is None:
        return JSONResponse({"error": "Service not initialized"}, status_code=503)
    result = await visura_service.restart_browser()
    return JSONResponse(result)


@router.post("/web/browser/launch-chrome", response_class=JSONResponse)
async def web_browser_launch_chrome(request: Request, user=Depends(_require_auth)):
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

    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i, row in enumerate(rows):
            try:
                resp = await client.post(f"{base}{api_path}", json=row)
                results.append({"row": i + 1, "status": "submitted", "data": resp.json()})
            except Exception as e:
                results.append({"row": i + 1, "status": "error", "error": str(e)})

    return JSONResponse(
        {
            "command": command,
            "total_rows": len(rows),
            "results": results,
        }
    )


@router.post("/web/api/workflow/stream")
async def web_api_workflow_stream(request: Request, user=Depends(_require_auth)):
    """SSE proxy for workflow streaming — forwards to opendata's workflow engine."""
    import httpx

    body = await request.json()

    async def stream_events():
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream(
                "POST",
                f"{_OPENDATA_API_URL}/catasto/workflow/stream",
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
