"""Bring the stored visure in line with the current naming convention and collapse repeated requests.

For every ``vi_*`` / ``vs_*`` document the name is derived again from its content with ``utils._descriptive_filename``
(office and comune of a subject visura, ``_sto``/``_att`` and ``_ter`` of a property visura, the "situazione al" date of a
storica/sintetica one) and keeps its request date: ``<canonical>_<YYYY-MM-DD>.<ext>``. Files that end up with the same
name are grouped by *content* (request id, time and practice number are ignored):

* same content: one file stays, the others are redundant repeats. They are moved to a quarantine folder (not deleted),
  their database rows and everything below them are removed, and aliases in the links directory are re-pointed.
* different content: all stay, the later ones with their practice id after the date. They are listed with what
  differs, because the name alone cannot tell them apart.

PDFs of properties keep their name (the PDF parser cannot tell terreni from fabbricati reliably); subject PDFs are
re-derived. Dry run unless ``--apply``; ``--apply`` writes a database backup, a plan and the quarantine folder.

    ../.venv/bin/python scripts/normalize_documents.py --documents <dir> --links <dir>           # report
    ../.venv/bin/python scripts/normalize_documents.py --documents <dir> --links <dir> --apply
    ../.venv/bin/python scripts/normalize_documents.py --documents <dir> --links <dir> --only <name> [<name> ...]  # just these
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from cleanup_extracted_xml import backup, delete_tree  # noqa: E402
from sister.utils import (  # noqa: E402
    _descriptive_filename,
    _p7m_payload,
    _parse_visura_pdf,
    _parse_visura_xml,
)

NAME = re.compile(
    r"^(?P<core>.+?)(?P<date>_\d{4}-\d{2}-\d{2})(?P<tail>(?:_T\d+_\d{4}|_DOC_\d+)*(?:_\d+)?)(?P<ext>\.(?:p7m|pdf|xml))$"
)
XML_NOISE = re.compile(rb'\s(Data|Ora|Protocollo|Anno)="[^"]*"')  # SituazioneAl is a real parameter
PDF_NOISE = [
    re.compile(p)
    for p in (r"Ora:?\s*\d{1,2}:\d{2}:\d{2}", r"Numero Pratica:?\s*T?\d+/\d{4}", r"\bData\b\s*:?\s*\d{1,2}/\d{1,2}/\d{4}")
]


def pdf_text(path: Path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60).stdout


def content_key(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".p7m"):
        payload = _p7m_payload(str(path))
        if payload:
            return "xml:" + hashlib.sha256(XML_NOISE.sub(b"", payload)).hexdigest()
    elif name.endswith(".pdf"):
        content = pdf_text(path)
        for rx in PDF_NOISE:
            content = rx.sub("", content)
        return "pdf:" + hashlib.sha256(" ".join(content.split()).encode()).hexdigest()
    return "raw:" + hashlib.sha256(path.read_bytes()).hexdigest()


def differentiators(path: Path) -> str:
    """What tells the files of one name apart: office, scope and situation date."""
    if path.name.lower().endswith(".p7m"):
        payload = _p7m_payload(str(path))
        if not payload:
            return "?"
        t = payload.decode("utf-8", "replace")

        def get(tag: str, attr: str) -> str:
            m = re.search(rf'<{tag}\b[^>]*\b{attr}="([^"]*)"', t)
            return m.group(1).strip() if m else ""

        office = re.search(r"Direzione Provinciale di (.*?) Ufficio", t)
        return (
            f"office={office.group(1) if office else '-'} prov={get('DatiRichiesta', 'Provincia') or '-'} "
            f"comune={get('DatiRichiesta', 'CodiceComune') or '-'} {get('TitoloVisura', 'Titolo')} "
            f"al {get('TitoloVisura', 'SituazioneAl') or '-'}"
        )
    t = pdf_text(path)
    office = re.search(r"Direzione Provinciale di (\S+)", t)
    situation = re.search(r"Situazione degli atti informatizzati[^\n]*?\bal\s+(\d{2}/\d{2}/\d{4})", t)
    area = re.search(r"siti in ([^\n]*?)(?:\s{3,}|$)", t)
    return (
        f"office={office.group(1) if office else '-'} {(area.group(1) if area else '').strip()} "
        f"al {situation.group(1) if situation else '-'}"
    )


SUBJECT = re.compile(
    r"^vs_(?P<view>att|sto|sin)_(?P<id>[A-Z0-9]{11,16})(?:_(?P<prov>[A-Z]{2}))?(?:_(?P<comune>[A-Z]\d{3}))?(?:_C(?P<cat>[EFT]))?(?:_al\d{4}-\d{2}-\d{2})?$"
)
KIND = re.compile(r"^(?:vi|vs)_(?:att|sto|sin)(?:_ter)?")


CATEGORY = re.compile(r"_C[EFT](?=_al\d{4}-|$)")


def derived_core(path: Path, core: str, date: str) -> str:
    """``_derived_core`` plus the guarantee that a subject name always names its cadastre (``CE`` when unknown)."""
    result = _derived_core(path, core, date)
    if result.startswith("vs_") and not CATEGORY.search(result):
        position = re.search(r"_al\d{4}-", result)
        index = position.start() if position else len(result)
        result = f"{result[:index]}_CE{result[index:]}"
    return result


def _derived_core(path: Path, core: str, date: str) -> str:
    """The name the current convention gives the document, keeping what the old name says that content cannot.

    A subject visura gains its office (and comune, and "situazione al" date) and always names its cadastre
    (``CE``/``CF``/``CT``: from the document, else from the old name, else ``CE``); every other name only has its kind
    corrected (``vi``/``vs``, ``att``/``sto``/``sin``, ``_ter``): location tokens such as the comune of an old name
    (``SAVIO``) or the owner's partita IVA are kept.
    """
    if not core.startswith(("vi_", "vs_")):
        return core
    suffix = path.suffix.lower()
    if suffix in (".p7m", ".xml"):
        parsed = _parse_visura_xml(str(path))
    elif suffix == ".pdf" and core.startswith("vs_"):
        parsed = _parse_visura_pdf(str(path))
    else:
        return core
    if not parsed or parsed.get("tipo") not in {"visura_soggetto", "visura_fabbricati", "visura_terreni"}:
        return core
    requested = date.lstrip("_")
    subject = SUBJECT.match(core)
    if subject and parsed["tipo"] == "visura_soggetto":
        if not (parsed.get("codice_fiscale") or parsed.get("identificativo")):
            parsed["identificativo"] = subject["id"]
        if not parsed.get("tipo_catasto") and subject["cat"]:
            parsed["tipo_catasto"] = subject["cat"]
        derived = _descriptive_filename(parsed, requested_at=requested)
        return derived if derived.startswith("vs_") else core
    derived = _descriptive_filename(parsed, requested_at=requested)
    if parsed["tipo"] == "visura_soggetto" and derived.startswith("vs_") and core.startswith("vi_"):
        # a subject visura that was named after its first property (the old parser did not know companies)
        return derived
    new_kind, old_kind = KIND.match(derived), KIND.match(core)
    return new_kind.group(0) + core[old_kind.end() :] if new_kind and old_kind else core


def build_plan(documents: Path) -> dict:
    names = sorted(p.name for p in documents.iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith("."))
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for name in names:
        m = NAME.match(name)
        if not m or not m.group("core").startswith(("vi_", "vs_")):
            continue
        core = derived_core(documents / name, m.group("core"), m.group("date"))
        groups[(core + m.group("date"), m.group("ext"))].append(
            {"old": name, "tail": m.group("tail"), "from_core": m.group("core"), "to_core": core}
        )

    renames: list[dict] = []
    redundant: list[dict] = []
    distinct: list[dict] = []
    for (base, ext), members in sorted(groups.items()):
        classes: dict[str, list[dict]] = collections.defaultdict(list)
        for member in members:
            classes[content_key(documents / member["old"])].append(member)
        ordered = []
        for files in classes.values():
            files.sort(key=lambda f: (f["tail"] != "", f["old"]))
            ordered.append(files)
        ordered.sort(key=lambda files: (files[0]["tail"] != "", files[0]["old"]))
        used: set[str] = set()
        for index, files in enumerate(ordered):
            keeper = files[0]
            tail = "" if index == 0 else (keeper["tail"] or f"_{index + 1}")
            new = f"{base}{tail}{ext}"
            while new in used:
                tail += "_2"
                new = f"{base}{tail}{ext}"
            used.add(new)
            keeper["new"] = new
            if new != keeper["old"]:
                renames.append(
                    {"old": keeper["old"], "new": new, "renamed_core": keeper["from_core"] != keeper["to_core"]}
                )
            for extra in files[1:]:
                redundant.append({"remove": extra["old"], "keep": keeper["old"], "keep_as": new})
        if len(ordered) > 1:
            distinct.append(
                {
                    "name": f"{base}{ext}",
                    "classes": [
                        {"files": [f["new"] if "new" in f else f["old"] for f in files], "differs": differentiators(documents / files[0]["old"])}
                        for files in ordered
                    ],
                }
            )
    return {"renames": renames, "redundant": redundant, "distinct": distinct}


async def update_database(plan: dict, commit: bool) -> dict[str, int]:
    from sister.database import _get_engine

    counts: dict[str, int] = {"_relinked": 0, "_renamed": 0}
    engine = _get_engine()
    async with engine.connect() as conn:
        transaction = await conn.begin()
        wanted = {e["old"] for e in plan["renames"]} | {e["remove"] for e in plan["redundant"]}
        wanted |= {e["keep"] for e in plan["redundant"]}
        rows = {
            r.filename: r
            for r in (
                await conn.execute(
                    text("SELECT id, file_name AS filename, request_id AS response_id FROM document WHERE file_name = ANY(:n)"),
                    {"n": sorted(wanted)},
                )
            ).all()
        }
        ids: list[int] = []
        for entry in plan["redundant"]:
            gone, keeper = rows.get(entry["remove"]), rows.get(entry["keep"])
            if gone is None:
                continue
            ids.append(gone.id)
            if gone.response_id and keeper is not None and not keeper.response_id:
                await conn.execute(
                    text("UPDATE document SET request_id = :r WHERE id = :id"),
                    {"r": gone.response_id, "id": keeper.id},
                )
                counts["_relinked"] += 1
        if ids:
            await delete_tree(conn, "document", "id = ANY(:ids)", {"ids": ids}, counts)
        for entry in plan["renames"]:
            row = rows.get(entry["old"])
            if row is None:
                continue
            await conn.execute(  # the database keeps the file name only: the directory is configuration
                text("UPDATE document SET file_name = :f WHERE id = :id"),
                {"f": entry["new"], "id": row.id},
            )
            counts["_renamed"] += 1
        await (transaction.commit() if commit else transaction.rollback())
    await engine.dispose()
    return counts


def repoint_links(plan: dict, documents: Path, links: Path) -> int:
    target = {e["old"]: e["new"] for e in plan["renames"]}
    target.update({e["remove"]: e["keep_as"] for e in plan["redundant"]})
    documents_real = os.path.realpath(documents)
    changed = 0
    for link in sorted(links.iterdir()):
        if not link.is_symlink() or link.name.endswith(".meta"):
            continue
        current = os.readlink(link)
        new = target.get(os.path.basename(current))
        if new is None or os.path.dirname(os.path.realpath(os.path.join(links, current))) != documents_real:
            continue
        temporary = link.with_name(f".{link.name}.tmp")
        os.symlink(posixpath.join(posixpath.dirname(current), new), temporary)
        os.replace(temporary, link)
        changed += 1
    return changed


def move_files(plan: dict, documents: Path, quarantine: Path, stamp: str) -> list[tuple[Path, Path]]:
    """Quarantine the redundant files, then rename through temporary names so chains of renames cannot collide."""
    done: list[tuple[Path, Path]] = []

    def move(source: Path, target: Path) -> None:
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        os.rename(source, target)
        done.append((source, target))

    try:
        for entry in plan["redundant"]:
            move(documents / entry["remove"], quarantine / entry["remove"])
        temporaries = []
        for index, entry in enumerate(plan["renames"]):
            temporary = documents / f".normalize_{stamp}_{index}"
            move(documents / entry["old"], temporary)
            temporaries.append((temporary, documents / entry["new"]))
        for temporary, final in temporaries:
            move(temporary, final)
    except Exception:
        for source, target in reversed(done):
            os.rename(target, source)
        raise
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--links", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=None, help="default: <documents>/../backups")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--only", nargs="+", metavar="NAME",
        help="rename just these files (current names); no other rename and no duplicate is touched",
    )
    args = parser.parse_args()

    documents = args.documents.resolve()
    plan = build_plan(documents)
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {entry["old"] for entry in plan["renames"]}
        if unknown:
            parser.error(f"nothing to rename for: {', '.join(sorted(unknown))}")
        plan = {"renames": [e for e in plan["renames"] if e["old"] in wanted], "redundant": [], "distinct": []}
    renames, redundant, distinct = plan["renames"], plan["redundant"], plan["distinct"]
    recomputed = [e for e in renames if e["renamed_core"]]
    print(f"names re-derived from content: {len(recomputed)}")
    for entry in recomputed[:8]:
        print(f"   {entry['old']}\n     -> {entry['new']}")
    print(f"files renamed in total (re-derived, or suffix dropped): {len(renames)}")
    print(f"redundant repeats (same name, same content): {len(redundant)}")
    for entry in redundant[:6]:
        print(f"   {entry['remove']}\n     = {entry['keep_as']}")
    print(f"\nnames that still cover DIFFERENT contents (their practice id stays after the date): {len(distinct)}")
    for group in distinct:
        print(f"  {group['name']}")
        for cls in group["classes"]:
            print(f"      {len(cls['files'])} file(s) | {cls['differs']}")
            for file in cls["files"]:
                print(f"           {file}")

    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    if not args.apply:
        counts = asyncio.run(update_database(plan, commit=False))
        print(
            "\ndatabase: rows renamed", counts["_renamed"], "| rows removed",
            {k: v for k, v in sorted(counts.items()) if not k.startswith("_")},
        )
        print("dry run: nothing changed (use --apply)")
        return

    backup_dir = args.backup_dir or documents.parent / "backups"
    quarantine = backup_dir / f"removed_duplicates_{stamp}"
    quarantine.mkdir(parents=True, exist_ok=True)
    print("\nbackup:", backup(backup_dir))
    (backup_dir / f"normalize_plan_{stamp}.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    done = move_files(plan, documents, quarantine, stamp)
    try:
        counts = asyncio.run(update_database(plan, commit=True))
    except Exception:
        for source, target in reversed(done):
            os.rename(target, source)
        raise
    print("database rows renamed:", counts["_renamed"], "| removed:", {k: v for k, v in counts.items() if not k.startswith("_")})
    print("responses re-linked to the kept document:", counts["_relinked"])
    if args.links and args.links.is_dir():
        print("aliases re-pointed:", repoint_links(plan, documents, args.links))
    print(f"quarantined {len(redundant)} files in {quarantine}; renamed {len(renames)}")


if __name__ == "__main__":
    main()
