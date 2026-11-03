#!/usr/bin/env python3
"""Build a private JSONL batch manifest from the local SISTER documents.

This script only reads source files and writes job definitions. It never
submits a workflow or contacts the cadastral portal. Property locations and
valid natural-person fiscal codes are deduplicated across XML, P7M, PDF, and
ZIP documents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from lxml import etree


CF_RE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{16}(?![A-Z0-9])", re.IGNORECASE)
PROPERTY_FILENAME_RE = re.compile(
    r"(?:^|_)FG(?P<sheet>[A-Z0-9]+)_PT(?P<parcel>[A-Z0-9]+)(?:_SUB(?P<subunit>[A-Z0-9]+))?",
    re.IGNORECASE,
)
OMOCODIA = dict(zip("LMNPQRSTUV", "0123456789"))
ODD_VALUES = {
    **dict(zip("0123456789", (1, 0, 5, 7, 9, 13, 15, 17, 19, 21))),
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23))),
}
MONTHS = set("ABCDEHLMPRST")
PERSON_FIELD_NAMES = {"cf", "codfisc", "codfiscale", "codicefiscale", "fiscalcode"}
EXCLUDED_PERSON_ANCESTORS = {"richiedente", "richiesta", "operatore", "utente"}
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 3


def local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].replace("_", "").replace("-", "").casefold()


def normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def canonical_cf(value: str) -> str | None:
    cf = re.sub(r"\s+", "", value or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{16}", cf):
        return None
    if cf[8] not in MONTHS:
        return None
    # Omocodia substitutes letters for digits in numeric positions.
    decoded = list(cf)
    for index in (6, 7, 9, 10, 12, 13, 14):
        decoded[index] = OMOCODIA.get(decoded[index], decoded[index])
        if not decoded[index].isdigit():
            return None
    if not re.fullmatch(r"[A-Z]{6}[0-9]{2}[ABCDEHLMPRST][0-9]{2}[A-Z][0-9]{3}[A-Z]", cf):
        return None
    total = 0
    for index, char in enumerate(decoded[:15]):
        if index % 2 == 0:
            total += ODD_VALUES[char]
        else:
            total += int(char) if char.isdigit() else ord(char) - ord("A")
    return cf if chr(ord("A") + total % 26) == cf[-1] else None


def read_document_payloads(root: Path) -> Iterator[tuple[str, bytes, int]]:
    """Yield document content; ZIP members and P7M payloads stay in memory."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".plan.json"):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or len(data) > MAX_ARCHIVE_MEMBER_BYTES:
            continue
        ref = hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()
        yield from expand_payload(path.name, data, ref, 0)


def expand_payload(name: str, data: bytes, source_ref: str, depth: int) -> Iterator[tuple[str, bytes, int]]:
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if data.startswith(b"%PDF-") or name.casefold().endswith(".pdf"):
        yield "PDF", data, source_ref
        return
    if stripped.startswith(b"<") or name.casefold().endswith(".xml"):
        yield "XML", data, source_ref
        return
    if name.casefold().endswith(".p7m"):
        try:
            from aecs4u_crypto import extract_p7m_payload

            extracted = extract_p7m_payload(data)
            yield from expand_payload(name.rsplit(".p7m", 1)[0] + ".xml", extracted, source_ref, depth)
        except Exception:
            return
        return
    if data.startswith(b"PK\x03\x04") and depth < MAX_ARCHIVE_DEPTH:
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for item in archive.infolist():
                    if item.is_dir() or item.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        continue
                    try:
                        content = archive.read(item)
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        continue
                    child_ref = hashlib.sha256(f"{source_ref}/{item.filename}".encode()).hexdigest()
                    yield from expand_payload(item.filename, content, child_ref, depth + 1)
        except (OSError, zipfile.BadZipFile):
            return


def extract_xml_locations(root: etree._Element, filename: str) -> list[dict[str, str]]:
    base: dict[str, str] = {}
    location_attrs = {
        "provincia": "provincia",
        "comune": "comune",
        "foglio": "foglio",
        "particella": "particella",
        "particellanum": "particella",
        "subalterno": "subalterno",
        "sezcensuaria": "sezione",
        "sezurbana": "sezione_urbana",
        "tipocatasto": "tipo_catasto",
    }
    candidates: list[dict[str, str]] = []
    for element in root.iter():
        tag = local_name(str(element.tag))
        attrs = {location_attrs[local_name(key)]: normalize(value)
                 for key, value in element.attrib.items() if local_name(key) in location_attrs}
        if tag in {"dati richiesta", "datirichiesta"}:
            base.update(attrs)
            candidates.append({**base})
        elif attrs and any(key in attrs for key in ("foglio", "particella")):
            candidates.append({**base, **attrs})

    property_name = filename.casefold()
    all_tags = {local_name(str(element.tag)) for element in root.iter()}
    if not base.get("tipo_catasto"):
        if "terren" in property_name or "visuraterreno" in property_name:
            base["tipo_catasto"] = "T"
        elif any("terreno" in tag or "terreni" in tag for tag in all_tags):
            base["tipo_catasto"] = "T"
        elif "fabbricat" in property_name or "visurafabbricat" in property_name:
            base["tipo_catasto"] = "F"
        elif any("fabbricat" in tag for tag in all_tags):
            base["tipo_catasto"] = "F"
    match = PROPERTY_FILENAME_RE.search(Path(filename).stem)
    if match:
        base.setdefault("foglio", match.group("sheet"))
        base.setdefault("particella", match.group("parcel"))
        base.setdefault("subalterno", match.group("subunit") or "")

    if any(base.get(field) for field in ("foglio", "particella")):
        candidates.append({**base})
    if not candidates:
        candidates.append(base)

    normalized = []
    for values in candidates:
        normalized.append({
            "tipo_catasto": values.get("tipo_catasto", ""),
            "provincia": values.get("provincia", ""),
            "comune": values.get("comune", ""),
            "foglio": values.get("foglio", ""),
            "particella": values.get("particella", ""),
            "subalterno": values.get("subalterno", ""),
            "sezione": values.get("sezione", ""),
            "sezione_urbana": values.get("sezione_urbana", ""),
        })
    return normalized


def xml_person_codes(root: etree._Element) -> set[str]:
    found: set[str] = set()
    for element in root.iter():
        ancestry = {local_name(str(node.tag)) for node in element.iterancestors()}
        if ancestry & EXCLUDED_PERSON_ANCESTORS:
            continue
        candidates: list[str] = []
        for key, value in element.attrib.items():
            if local_name(key) in PERSON_FIELD_NAMES or "codicefiscale" in local_name(key):
                candidates.append(str(value))
        if local_name(str(element.tag)) in PERSON_FIELD_NAMES or "codicefiscale" in local_name(str(element.tag)):
            candidates.append(element.text or "")
        for raw in candidates:
            for match in CF_RE.findall(raw.upper()):
                normalized_cf = canonical_cf(match)
                if normalized_cf:
                    found.add(normalized_cf)
    return found


def pdf_text(data: bytes) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace").replace("\x00", "")


def extract_pdf_location(text: str, filename: str) -> dict[str, str]:
    low = text.casefold()
    cadastre = "T" if "terren" in low else "F" if any(word in low for word in ("fabbricat", "urbano")) else ""
    if not cadastre:
        cadastre = "T" if "_ter_" in filename.casefold() else "F" if "_fab_" in filename.casefold() else ""
    province = ""
    comune = ""
    match = re.search(r"Comune di ([A-ZÀ-ÖØ-Ý'’ .-]+?)\s*\([A-Z0-9]{4}\)\s*\(([A-Z]{2})\)", text, re.IGNORECASE)
    if match:
        comune = normalize(match.group(1)).upper()
        province = match.group(2).upper()
    else:
        match = re.search(r"Comune(?:\s+di)?\s+([A-ZÀ-ÖØ-Ý'’ .-]+?)\s*\(([A-Z]{2})\)", text, re.IGNORECASE)
        if match:
            comune = normalize(match.group(1)).upper()
            province = match.group(2).upper()
    values = {"tipo_catasto": cadastre, "provincia": province, "comune": comune}
    for field, pattern in (
        ("foglio", r"\bFoglio\s*:?\s*([A-Z0-9]+)"),
        ("particella", r"\bParticella(?:\s+Num)?\s*:?\s*([A-Z0-9]+)"),
        ("subalterno", r"\bSubalterno\s*:?\s*([A-Z0-9]+)"),
        ("sezione_urbana", r"\bSezione urbana\s+([A-Z0-9]+)"),
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        values[field] = normalize(match.group(1)) if match else ""
    values.setdefault("sezione", "")
    filename_match = PROPERTY_FILENAME_RE.search(Path(filename).stem)
    if filename_match:
        values["foglio"] = values["foglio"] or filename_match.group("sheet")
        values["particella"] = values["particella"] or filename_match.group("parcel")
        values["subalterno"] = values["subalterno"] or filename_match.group("subunit") or ""
    return values


def pdf_person_codes(text: str) -> set[str]:
    found: set[str] = set()
    folded = text.casefold()
    for match in CF_RE.finditer(text.upper()):
        # Inspection PDFs can repeat the requester's code in the header/footer;
        # that person is not a target of the investigation.
        context = folded[max(0, match.start() - 90):match.end() + 30]
        if any(label in context for label in ("richiedente", "per conto di", "operatore")):
            continue
        normalized_cf = canonical_cf(match.group(0))
        if normalized_cf:
            found.add(normalized_cf)
    return found


def target_key(target: dict[str, str]) -> tuple[str, ...]:
    return tuple(normalize(target.get(field)).casefold() for field in (
        "tipo_catasto", "provincia", "comune", "foglio", "particella", "subalterno", "sezione", "sezione_urbana"
    ))


def stable_job_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def gather_targets(documents: Path) -> tuple[dict[tuple[str, ...], dict[str, Any]], dict[str, set[str]], dict[str, int]]:
    properties: dict[tuple[str, ...], dict[str, Any]] = {}
    people: dict[str, set[str]] = defaultdict(set)
    stats = defaultdict(int)

    for name, content, source_ref in read_document_payloads(documents):
        stats["payloads_seen"] += 1
        filename = name
        parsed_locations: list[dict[str, str]] = []
        found_people: set[str] = set()
        person_context = False
        if name == "XML":
            try:
                xml_root = etree.fromstring(
                    content,
                    etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False),
                )
            except (etree.XMLSyntaxError, ValueError):
                stats["xml_unparsed"] += 1
                continue
            if xml_root is None:
                stats["xml_unparsed"] += 1
                continue
            stats["xml_parsed"] += 1
            parsed_locations = extract_xml_locations(xml_root, filename)
            found_people = xml_person_codes(xml_root)
            all_tags = {local_name(str(element.tag)) for element in xml_root.iter()}
            person_context = bool(all_tags & {"soggettopf", "soggetto", "intestato", "intestatario", "persona"})
        else:
            text_content = pdf_text(content)
            if not text_content:
                stats["pdf_unparsed"] += 1
                continue
            stats["pdf_parsed"] += 1
            parsed_locations = [extract_pdf_location(text_content, filename)]
            found_people = pdf_person_codes(text_content)
            person_context = bool(re.search(r"intestat|soggett|persona fisica|codice fiscale", text_content, re.IGNORECASE))

        # Filenames can carry a queried person's fiscal code even when the PDF
        # text layer or XML subject section is incomplete.
        for candidate in CF_RE.findall(Path(filename).stem.upper()):
            valid = canonical_cf(candidate)
            if valid:
                found_people.add(valid)
        for cf in found_people:
            people[cf].add(source_ref)
        if person_context and not found_people:
            stats["person_context_docs_without_cf"] += 1

        if not parsed_locations:
            parsed_locations = [{}]
        payload_has_property = False
        payload_has_complete_target = False
        for parsed_location in parsed_locations:
            target = {
                "tipo_catasto": normalize(parsed_location.get("tipo_catasto")).upper(),
                "provincia": normalize(parsed_location.get("provincia")).upper(),
                "comune": normalize(parsed_location.get("comune")).upper(),
                "foglio": normalize(parsed_location.get("foglio")),
                "particella": normalize(parsed_location.get("particella")),
                "subalterno": normalize(parsed_location.get("subalterno")),
                "sezione": normalize(parsed_location.get("sezione")),
                "sezione_urbana": normalize(parsed_location.get("sezione_urbana")),
            }
            has_property_data = any(target[field] for field in ("foglio", "particella")) or bool(PROPERTY_FILENAME_RE.search(Path(filename).stem))
            payload_has_property = payload_has_property or has_property_data
            if not has_property_data:
                continue
            if target["tipo_catasto"] in {"F", "T"} and all(target[field] for field in ("provincia", "comune", "foglio", "particella")):
                payload_has_complete_target = True
                key = target_key(target)
                if key not in properties:
                    properties[key] = {"target": target, "sources": set()}
                if source_ref not in properties[key]["sources"]:
                    properties[key]["sources"].add(source_ref)
            elif has_property_data:
                stats["unresolved_property_locations"] += 1
        if payload_has_property and not payload_has_complete_target:
            stats["property_payload_without_complete_target"] += 1

    stats["property_targets"] = len(properties)
    stats["person_targets"] = len(people)
    return properties, people, dict(stats)


def workflow_payload(kind: str, target: dict[str, str] | None = None, cf: str | None = None) -> dict[str, Any]:
    common = {
        "depth": "full",
        # Paid steps are represented in the review queue; this base manifest
        # can run without auto-confirming a fee in the portal.
        "include_paid_steps": False,
        "auto_confirm": False,
        "max_fanout": 100,
        "max_owners": 50,
        "max_properties_per_owner": 100,
        "max_historical_properties": 50,
        "max_paid_steps": 20,
        "max_total_steps": 500,
    }
    if kind == "property_due_diligence":
        return {
            **common,
            "preset": "due-diligence",
            "include_history": True,
            "provincia": target["provincia"],
            "comune": target["comune"],
            "foglio": target["foglio"],
            "particella": target["particella"],
            "tipo_catasto": target["tipo_catasto"],
            "subalterno": target["subalterno"] or None,
            "sezione": target["sezione"] or None,
            "sezione_urbana": target["sezione_urbana"] or None,
        }
    return {**common, "preset": "portfolio", "codice_fiscale": cf}


def build_manifest(properties: dict[tuple[str, ...], dict[str, Any]], people: dict[str, set[str]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for key, item in sorted(properties.items(), key=lambda entry: entry[0]):
        target = item["target"]
        identity = "|".join(key)
        jobs.append({
            "job_id": stable_job_id("property-dd", identity),
            "job_type": "property_due_diligence",
            "target": target,
            "workflow_payload": workflow_payload("property_due_diligence", target=target),
            "source_document_count": len(item["sources"]),
            "paid_steps_deferred": ["ispezione_ipotecaria", "portfolio_ipotecaria"],
            "paid_step_approval_required": True,
        })
    for cf, sources in sorted(people.items()):
        jobs.append({
            "job_id": stable_job_id("person-portfolio", cf),
            "job_type": "person_portfolio",
            "target": {"codice_fiscale": cf},
            "workflow_payload": workflow_payload("person_portfolio", cf=cf),
            "source_document_count": len(sources),
            "paid_steps_deferred": ["portfolio_ipotecaria"],
            "paid_step_approval_required": True,
        })
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True, help="Root directory containing source documents")
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/document_workflow_batch.jsonl"),
        help="Private JSONL output path (default: outputs/document_workflow_batch.jsonl)",
    )
    args = parser.parse_args()
    documents = args.documents.expanduser().resolve()
    if not documents.is_dir():
        parser.error(f"documents directory does not exist: {documents}")

    properties, people, stats = gather_targets(documents)
    jobs = build_manifest(properties, people)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text("".join(json.dumps(job, ensure_ascii=False, sort_keys=True) + "\n" for job in jobs), encoding="utf-8")
    try:
        args.output.chmod(0o600)
        args.output.parent.chmod(0o700)
    except OSError:
        pass

    print(json.dumps({
        "manifest": str(args.output.resolve()),
        "jobs": len(jobs),
        "property_due_diligence_jobs": len(properties),
        "person_portfolio_jobs": len(people),
        "paid_steps_deferred": True,
        **stats,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
