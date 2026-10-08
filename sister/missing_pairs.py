"""Find incomplete PDF/P7M pairs and map them to free SISTER requests."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


_IDENTIFIER_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{16}|\d{11})(?![A-Z0-9])", re.IGNORECASE)
_PROVINCE_NAME_RE = re.compile(r"Direzione Provinciale di\s+([A-ZÀÈÉÌÒÙ][A-Za-zÀ-ÿ '\-]+)", re.IGNORECASE)


def _parse_file(path: Path) -> dict[str, Any] | None:
    """Parse the document metadata needed to reproduce a SISTER visura request."""
    from .utils import _extract_p7m, _parse_visura_pdf, _parse_visura_xml

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        parsed = _parse_visura_pdf(str(path))
        if parsed:
            try:
                text = subprocess.check_output(
                    ["pdftotext", str(path), "-"], stderr=subprocess.DEVNULL, text=True, timeout=15
                )
                province_match = _PROVINCE_NAME_RE.search(text)
                if province_match:
                    parsed["provincia"] = province_match.group(1).strip()
            except Exception:
                pass
        return parsed
    if suffix == ".xml":
        return _parse_visura_xml(str(path))
    if suffix == ".p7m":
        sibling_xml = path.with_suffix(".xml")
        if sibling_xml.is_file():
            return _parse_visura_xml(str(sibling_xml))
        # Parse outside the documents folder so a scan never changes the source files.
        with tempfile.TemporaryDirectory(prefix="sister-pair-scan-") as temp_dir:
            temp_p7m = Path(temp_dir) / path.name
            temp_p7m.write_bytes(path.read_bytes())
            extracted = _extract_p7m(str(temp_p7m))
            return _parse_visura_xml(extracted) if extracted else None
    return None


def _subject_identifier(parsed: dict[str, Any], stem: str) -> str | None:
    value = parsed.get("codice_fiscale") or parsed.get("identificativo")
    if value:
        match = _IDENTIFIER_RE.search(str(value).upper())
        if match:
            return match.group(1).upper()
    match = _IDENTIFIER_RE.search(stem.upper())
    return match.group(1).upper() if match else None


def _subject_cadastre(stem: str, parsed: dict[str, Any]) -> str:
    match = re.search(r"_C([EFT])(?:_|$)", stem.upper())
    if match:
        return match.group(1)
    value = str(parsed.get("tipo_catasto") or "").upper()
    if value in {"T", "F", "E"}:
        return value
    return "E"


def _query_for(stem: str, parsed: dict[str, Any]) -> tuple[tuple, dict[str, Any]] | None:
    tipo = str(parsed.get("tipo") or "").lower()
    if tipo == "visura_soggetto":
        identifier = _subject_identifier(parsed, stem)
        # The SISTER subject-document form is for natural persons (16 character CF).
        if not identifier or len(identifier) != 16:
            return None
        cadastre_type = _subject_cadastre(stem, parsed)
        query = {
            "search_type": "soggetto_documento",
            "province": "NAZIONALE",
            "municipality": None,
            "cadastre_type": cadastre_type,
            "params": {"codice_fiscale": identifier, "vista": "analitica"},
            "label": f"Visura per Soggetto analitica {identifier} ({cadastre_type})",
        }
        return ("soggetto", identifier, cadastre_type), query

    if tipo not in {"visura_fabbricati", "visura_terreni"}:
        return None

    province = str(parsed.get("provincia") or "").strip()
    municipality = str(parsed.get("comune") or "").strip()
    sheet = str(parsed.get("foglio") or "").strip()
    parcel = str(parsed.get("particella") or "").strip()
    cadastre_type = str(parsed.get("tipo_catasto") or ("T" if tipo == "visura_terreni" else "F")).upper()
    if not all((province, municipality, sheet, parcel)) or cadastre_type not in {"T", "F"}:
        return None

    subunit = str(parsed.get("subalterno") or "").strip()
    section = str(parsed.get("sezione_urbana") or "").strip()
    params = {"sheet": sheet, "parcel": parcel}
    if subunit:
        params["subunit"] = subunit
    if section:
        params["section"] = section
    query = {
        "search_type": "visura_storica",
        "province": province,
        "municipality": municipality,
        "cadastre_type": cadastre_type,
        "params": params,
        "label": f"Storica Analitica {province}/{municipality} F.{sheet} P.{parcel}"
        + (f" Sub.{subunit}" if subunit else ""),
    }
    key = ("immobile", province.casefold(), municipality.casefold(), cadastre_type, sheet, parcel, subunit, section)
    return key, query


def build_missing_pair_batch(documents_dir: Path) -> dict[str, Any]:
    """Return one free SISTER request per distinct supported PDF/P7M pair gap."""
    if not documents_dir.is_dir():
        return {
            "missing_pairs": [],
            "requests": [],
            "skipped": [],
            "excluded_paid": 0,
            "error": f"Documents directory not found: {documents_dir}",
        }

    grouped: dict[str, dict[str, Path]] = defaultdict(dict)
    for path in documents_dir.iterdir():
        if path.is_file() and path.suffix.lower() in {".pdf", ".p7m", ".xml"}:
            grouped[path.stem][path.suffix.lower()] = path

    requests: dict[tuple, dict[str, Any]] = {}
    missing_pairs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    excluded_paid = 0

    for stem, files in sorted(grouped.items()):
        missing_formats = [fmt for fmt in (".pdf", ".p7m") if fmt not in files]
        if not missing_formats:
            continue
        if stem.lower().startswith(("isp_", "ispezione_", "ipotecaria_")):
            excluded_paid += 1
            continue
        if not stem.lower().startswith(("vi_", "vs_")):
            skipped.append({"stem": stem, "reason": "Not a supported cadastral visura file"})
            continue

        source = files.get(".pdf") or files.get(".xml") or files.get(".p7m")
        try:
            parsed = _parse_file(source) if source else None
        except Exception as exc:
            skipped.append({"stem": stem, "reason": f"Could not parse source file: {exc}"})
            continue
        query_info = _query_for(stem, parsed or {}) if parsed else None
        if not query_info:
            skipped.append({"stem": stem, "reason": "Unsupported document type or incomplete request metadata"})
            continue

        key, query = query_info
        pair = {"stem": stem, "source_files": sorted(p.name for p in files.values()), "missing_formats": missing_formats}
        missing_pairs.append(pair)
        if key not in requests:
            requests[key] = {**query, "source_files": [], "missing_pair_count": 0}
        requests[key]["source_files"].append(stem)
        requests[key]["missing_pair_count"] += 1

    return {
        "missing_pairs": missing_pairs,
        "requests": list(requests.values()),
        "skipped": skipped,
        "excluded_paid": excluded_paid,
    }
