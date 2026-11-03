#!/usr/bin/env python3
"""Build a SISTER JSON-query batch for PDFs without parseable companions.

The batch re-runs SISTER searches and stores their structured JSON responses.
It does not download official XML/P7M files; those portal downloads require a
human CAPTCHA.  The inventory is filename-exact so historic PDFs are not
silently treated as paired with a newer XML for the same property/person.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from collections import defaultdict
from pathlib import Path

DEFAULT_DOCUMENTS = Path("/mnt/mobile/data/aecs4u.it/sister/documents")
DEFAULT_BATCH = Path("inputs/pdf_parseable_batch.csv")
DEFAULT_INVENTORY = Path("inputs/pdf_parseable_inventory.csv")
PARSEABLE_SUFFIXES = {".xml", ".json", ".geojson", ".p7m"}

_CF_RE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{16}(?![A-Z0-9])", re.IGNORECASE)
_VAT_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")
_PROVINCE_CODE_RE = re.compile(r"\(([A-Z]{2})\)\s*(?:Sezione|Foglio|Particella|$)", re.IGNORECASE)
_MUNICIPALITY_RE = re.compile(
    r"Comune di\s+([A-ZÀ-Ÿ][A-ZÀ-Ÿ '\-]+?)\s*\(([A-Z0-9]{4,6})\)\s*\(([A-Z]{2})\)",
    re.IGNORECASE,
)
_REQUEST_MUNICIPALITY_RE = re.compile(
    r"siti nel comune di\s+([A-ZÀ-Ÿ][A-ZÀ-Ÿ '\-]+?)\s*\(([A-Z0-9]{4,6})\)\s*\(([A-Z]{2})\)",
    re.IGNORECASE,
)
_COORDINATES_RE = re.compile(
    r"Foglio\s*:?\s*(\d+)\s+Particella\s*:?\s*(\d+)(?:\s+Sub(?:alterno)?\s*:?\s*(\d+))?",
    re.IGNORECASE,
)
_FILENAME_COORDINATES_RE = re.compile(r"FG(\d+).*?PT(\d+)(?:_SUB(\d+))?", re.IGNORECASE)
_FILE_PROVINCE_RE = re.compile(r"(?:^|_)([A-Z]{2})_(?:FG|[A-Z]+_FG)", re.IGNORECASE)
_SITUATION_ID_RE = re.compile(r"_T\d+_(20\d{2})$", re.IGNORECASE)


def pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def province_name(content: str) -> str:
    for line in content.splitlines():
        marker = "Direzione Provinciale di"
        if marker.lower() in line.lower():
            remainder = line[line.lower().index(marker.lower()) + len(marker) :].strip()
            return re.split(r"\s{2,}", remainder, maxsplit=1)[0].strip().title()
    return ""


def document_info(path: Path, content: str) -> dict:
    name = path.stem
    info: dict[str, str] = {}
    province = province_name(content)
    if province:
        info["provincia"] = province

    municipality_match = _MUNICIPALITY_RE.search(content) or _REQUEST_MUNICIPALITY_RE.search(content)
    if municipality_match:
        info["comune"] = municipality_match.group(1).strip().upper()
        info["comune_codice"] = municipality_match.group(2).upper()
        info["provincia_codice"] = municipality_match.group(3).upper()
    elif info.get("provincia"):
        province_code_match = _PROVINCE_CODE_RE.search(content)
        if province_code_match:
            info["provincia_codice"] = province_code_match.group(1).upper()

    coord_match = _COORDINATES_RE.search(content)
    if coord_match:
        info["foglio"], info["particella"] = coord_match.group(1, 2)
        if coord_match.group(3):
            info["subalterno"] = coord_match.group(3)
    filename_coord_match = _FILENAME_COORDINATES_RE.search(name)
    if filename_coord_match:
        info.setdefault("foglio", filename_coord_match.group(1))
        info.setdefault("particella", filename_coord_match.group(2))
        if filename_coord_match.group(3):
            info.setdefault("subalterno", filename_coord_match.group(3))

    filename_province_match = _FILE_PROVINCE_RE.search(name)
    if filename_province_match:
        info.setdefault("provincia_codice", filename_province_match.group(1).upper())

    code = _CF_RE.search(name) or _CF_RE.search(content)
    if code:
        info["codice_fiscale"] = code.group(0).upper()
    vat = _VAT_RE.search(name) or _VAT_RE.search(content)
    if vat:
        info["identificativo"] = vat.group(0)

    text_lower = content.lower()
    # A Fabbricati visura also mentions "Particelle corrispondenti al catasto terreni" further down, so the
    # first catasto named in the document (its header) decides, not the mere presence of "catasto terreni".
    terreni_pos = text_lower.find("catasto terreni")
    fabbricati_pos = min(
        (pos for pos in (text_lower.find("catasto fabbricati"), text_lower.find("catasto dei fabbricati")) if pos >= 0),
        default=-1,
    )
    if name.lower().startswith(("vi_att_ter_", "vi_sto_ter_")):
        info["tipo_catasto"] = "T"
    elif terreni_pos >= 0 and (fabbricati_pos < 0 or terreni_pos < fabbricati_pos):
        info["tipo_catasto"] = "T"
    elif fabbricati_pos >= 0 or "fabbricati" in text_lower:
        info["tipo_catasto"] = "F"
    elif name.lower().startswith(("vi_att_", "vi_sto_", "el_imm_", "el_sub_", "elab_plan_", "mappa_")):
        info["tipo_catasto"] = "F"
    else:
        info["tipo_catasto"] = "E"
    return info


def exact_companions(path: Path, directory: Path) -> list[str]:
    return sorted(
        sibling.name
        for sibling in directory.glob(f"{path.stem}.*")
        if sibling.is_file()
        and sibling.stem.lower() == path.stem.lower()
        and sibling.suffix.lower() in PARSEABLE_SUFFIXES
    )


def province_lookup(paths: list[Path], texts: dict[Path, str]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    names_by_code: dict[str, str] = {}
    municipalities_by_sheet: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in paths:
        info = document_info(path, texts[path])
        code = info.get("provincia_codice")
        province = info.get("provincia")
        sheet = info.get("foglio")
        municipality = info.get("comune")
        if code and province:
            names_by_code.setdefault(code, province)
        if code and sheet and municipality:
            municipalities_by_sheet[(code, sheet)].add(municipality)
    unique_municipalities = {
        key: next(iter(values)) for key, values in municipalities_by_sheet.items() if len(values) == 1
    }
    return names_by_code, unique_municipalities


def request_for(path: Path, info: dict, names_by_code: dict, municipalities_by_sheet: dict) -> tuple[dict | None, str]:
    name = path.stem.lower()
    province = info.get("provincia") or names_by_code.get(info.get("provincia_codice", ""), "")
    municipality = info.get("comune")
    if not municipality and info.get("provincia_codice") and info.get("foglio"):
        municipality = municipalities_by_sheet.get((info["provincia_codice"], info["foglio"]))

    if name.startswith("vs_"):
        if info.get("codice_fiscale"):
            request = {
                "command": "soggetto",
                "codice_fiscale": info["codice_fiscale"],
                "tipo_catasto": "E",
            }
            if province:
                request["provincia"] = province
            historic = name.startswith(("vs_sin_", "vs_sto_"))
            note = "Fresh SISTER subject JSON; it does not reproduce the PDF's historical snapshot." if historic else "Fresh SISTER subject JSON; source PDF may be a dated export."
            return request, note
        if info.get("identificativo"):
            request = {
                "command": "azienda",
                "identificativo": info["identificativo"],
                "tipo_catasto": "E",
            }
            if province:
                request["provincia"] = province
            return request, "Fresh SISTER company JSON; it does not reproduce a dated PDF snapshot."
        return None, "No codice fiscale or 11-digit VAT number could be identified."

    if name.startswith("vi_"):
        required = (province, municipality, info.get("foglio"), info.get("particella"))
        if not all(required):
            return None, "Could not reliably extract province, municipality, sheet and parcel."
        request = {
            "command": "search",
            "provincia": province,
            "comune": municipality,
            "foglio": info["foglio"],
            "particella": info["particella"],
            "tipo_catasto": info.get("tipo_catasto", "F"),
        }
        if info.get("subalterno"):
            request["subalterno"] = info["subalterno"]
        note = "Fresh SISTER property JSON; a historical PDF's original date/period is not replayed." if "_sto_" in name else "Fresh SISTER property JSON."
        return request, note

    if name.startswith(("mappa_", "elab_plan_")):
        if not all((province, municipality, info.get("foglio"))):
            return None, "Could not reliably extract province, municipality and sheet."
        command = "export-mappa" if name.startswith("mappa_") else "elaborato-planimetrico"
        request = {
            "command": command,
            "provincia": province,
            "comune": municipality,
            "foglio": info["foglio"],
            "tipo_catasto": "F",
        }
        note = "SISTER returns extracted result data as JSON, not the original map/plan document."
        return request, note

    if name.startswith(("el_imm_", "el_sub_")):
        if not all((province, municipality, info.get("foglio"))):
            return None, "Could not reliably extract province, municipality and sheet."
        request = {
            "command": "elenco",
            "provincia": province,
            "comune": municipality,
            "foglio": info["foglio"],
            "tipo_catasto": "F",
        }
        note = "Elenco JSON is filtered by sheet; the current SISTER API cannot filter this query by parcel."
        return request, note

    return None, "No SISTER batch query mapping for this PDF type."


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents-dir", type=Path, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--batch-csv", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--inventory-csv", type=Path, default=DEFAULT_INVENTORY)
    args = parser.parse_args()

    if not args.documents_dir.is_dir():
        parser.error(f"documents directory not found: {args.documents_dir}")
    pdfs = sorted(args.documents_dir.glob("*.pdf"))
    texts = {path: pdf_text(path) for path in pdfs}
    names_by_code, municipalities_by_sheet = province_lookup(pdfs, texts)

    batch_by_key: dict[tuple, dict] = {}
    source_notes: dict[tuple, set[str]] = defaultdict(set)
    source_files: dict[tuple, list[str]] = defaultdict(list)
    inventory: list[dict] = []

    for path in pdfs:
        companions = exact_companions(path, args.documents_dir)
        if companions:
            inventory.append(
                {
                    "pdf": path.name,
                    "status": "has_parseable_companion",
                    "companion_files": "; ".join(companions),
                    "batch_row": "",
                    "command": "",
                    "scope_note": "",
                }
            )
            continue

        info = document_info(path, texts[path])
        request, note = request_for(path, info, names_by_code, municipalities_by_sheet)
        if request is None:
            inventory.append(
                {
                    "pdf": path.name,
                    "status": "manual_review",
                    "companion_files": "",
                    "batch_row": "",
                    "command": "",
                    "scope_note": note,
                }
            )
            continue

        key = tuple(sorted(request.items()))
        batch_by_key.setdefault(key, request)
        source_files[key].append(path.name)
        source_notes[key].add(note)
        inventory.append(
            {
                "pdf": path.name,
                "status": "queued_in_batch",
                "companion_files": "",
                "batch_row": "",
                "command": request["command"],
                "scope_note": note,
                "_batch_key": key,
            }
        )

    ordered_keys = sorted(batch_by_key, key=lambda key: (batch_by_key[key]["command"], key))
    batch_rows: list[dict] = []
    row_by_key: dict[tuple, int] = {}
    for index, key in enumerate(ordered_keys, start=1):
        request = dict(batch_by_key[key])
        notes = sorted(source_notes[key])
        request["source_files"] = "; ".join(sorted(source_files[key]))
        request["scope_note"] = " | ".join(notes)
        batch_rows.append(request)
        row_by_key[key] = index

    for row in inventory:
        key = row.pop("_batch_key", None)
        if key is not None:
            row["batch_row"] = row_by_key[key]

    batch_fields = [
        "command",
        "provincia",
        "comune",
        "foglio",
        "particella",
        "subalterno",
        "tipo_catasto",
        "sezione",
        "codice_fiscale",
        "identificativo",
        "source_files",
        "scope_note",
    ]
    inventory_fields = ["pdf", "status", "companion_files", "batch_row", "command", "scope_note"]
    write_csv(args.batch_csv, batch_fields, batch_rows)
    write_csv(args.inventory_csv, inventory_fields, inventory)

    matched = sum(row["status"] == "has_parseable_companion" for row in inventory)
    missing = len(inventory) - matched
    queued = sum(row["status"] == "queued_in_batch" for row in inventory)
    review = sum(row["status"] == "manual_review" for row in inventory)
    print(
        f"PDFs: {len(pdfs)}; already paired: {matched}; missing companion: {missing}; "
        f"covered by {len(batch_rows)} deduplicated SISTER JSON queries ({queued} PDFs); "
        f"manual review: {review}"
    )
    print(f"Batch CSV: {args.batch_csv}")
    print(f"Inventory CSV: {args.inventory_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
