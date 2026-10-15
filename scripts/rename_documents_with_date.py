"""Give every stored document the ISO request date as the last part of its name (``<name>_2026-10-06.<ext>``).

Same convention as new downloads (``utils._unique_document_stem``): the date, never the time. For a file already
in the folder the date is, in this order, the ``requested_at`` of its ``visura_documents`` row, the date printed in
the document (XML ``TitoloVisura@Data``, PDF ``Data dd/mm/yyyy``), then the file's modification date. Visure
(``vi_*`` / ``vs_*``) lose their old collision suffix (``_T265733_2026``, ``_DOC_<id>``), which comes back after the
date only when two files would otherwise share a name.

Renames the files, the ``.plan.json`` sidecars, ``visura_documents.filename/file_path`` and the aliases in the links
directory. The plan is written next to the database backup, so ``--revert <plan.json>`` undoes it. Dry run unless
``--apply``.

    ../.venv/bin/python scripts/rename_documents_with_date.py --documents <dir> --links <dir>           # report
    ../.venv/bin/python scripts/rename_documents_with_date.py --documents <dir> --links <dir> --apply
    ../.venv/bin/python scripts/rename_documents_with_date.py --documents <dir> --links <dir> --revert <plan.json>
"""

from __future__ import annotations

import argparse
import asyncio
import collections
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

from sqlalchemy import text  # noqa: E402

from sister.utils import _iso_request_date, _p7m_payload  # noqa: E402

SIDECARS = (".plan.json",)
SKIPPED = (".meta",)  # migration provenance files: never renamed, wherever they are
DATED = re.compile(r"_\d{4}-\d{2}-\d{2}$")
COLLISION_SUFFIX = re.compile(r"(?:_T\d+_\d{4}|_DOC_\d+)+$")
PDF_DATE = re.compile(r"\bData\b\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})")
XML_DATE = re.compile(r'<TitoloVisura\b[^>]*\bData="(\d{8})"')


def split_name(name: str) -> tuple[str, str]:
    stem, dot, ext = name.partition(".")
    return stem, (dot + ext if dot else "")


def content_date(path: Path) -> str:
    """The date printed in the document itself, or ``""``."""
    name = path.name.lower()
    try:
        if name.endswith(".p7m") and not name.endswith(".zip.p7m"):
            payload = _p7m_payload(str(path))
            match = XML_DATE.search(payload.decode("utf-8", "replace")) if payload else None
            return _iso_request_date(match.group(1)) if match else ""
        if name.endswith(".pdf"):
            first_page = subprocess.run(
                ["pdftotext", "-l", "1", "-layout", str(path), "-"], capture_output=True, text=True, timeout=30
            ).stdout
            match = PDF_DATE.search(first_page)
            return _iso_request_date(match.group(1)) if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


def build_plan(documents: Path, requested_at: dict[str, str]) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    names = sorted(
        p.name
        for p in documents.iterdir()
        if p.is_file() and not p.is_symlink() and not p.name.startswith(".") and not p.name.endswith(SKIPPED)
    )
    existing = set(names)
    main = [n for n in names if not n.endswith(SIDECARS)]
    entries: dict[str, dict] = {}
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)

    for name in main:
        stem, ext = split_name(name)
        if DATED.search(stem):
            continue  # already follows the convention
        path = documents / name
        date, source = _iso_request_date(requested_at.get(name, "")), "db"
        if not date:
            date, source = content_date(path), "content"
        if not date:
            date, source = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"), "mtime"
        tie = ""
        core = stem
        if stem.startswith(("vi_", "vs_")):
            match = COLLISION_SUFFIX.search(stem)
            if match:
                core, tie = stem[: match.start()], match.group(0)
        entry = {"old": name, "date": date, "source": source, "core": core, "tie": tie, "ext": ext}
        entries[name] = entry
        groups[(f"{core}_{date}", ext)].append(entry)

    taken = set(existing)
    for (dated_core, ext), members in sorted(groups.items()):
        # the file that had the plain name keeps the clean one; repeats follow by their old suffix
        members.sort(key=lambda e: (bool(e["tie"]), e["tie"], e["old"]))
        for position, entry in enumerate(members):
            candidate = f"{dated_core}{ext}"
            if position or candidate in taken - {entry["old"]}:
                tie = entry["tie"] or f"_{position + 1}"
                candidate = f"{dated_core}{tie}{ext}"
                ordinal = 2
                while candidate in taken - {entry["old"]}:
                    candidate = f"{dated_core}{tie}_{ordinal}{ext}"
                    ordinal += 1
            entry["new"] = candidate
            taken.add(candidate)

    plan = [e for e in entries.values()]
    by_old = {e["old"]: e["new"] for e in plan}
    for name in names:
        if not name.endswith(SIDECARS):
            continue
        parent = next((name[: -len(s)] for s in SIDECARS if name.endswith(s)), "")
        sidecar = name[len(parent) :]
        if parent in by_old:
            plan.append({"old": name, "new": by_old[parent] + sidecar, "date": "", "source": "sidecar"})
        else:
            notes.append(f"kept {name}: its document {parent} is not renamed")
    return plan, notes


async def update_database(plan: list[dict], documents: Path, commit: bool) -> dict[str, int]:
    from sister.database import _get_engine

    mapping = {e["old"]: e["new"] for e in plan}
    counts = {"visura_documents": 0, "document_structured_extractions": 0}
    engine = _get_engine()
    async with engine.connect() as conn:
        transaction = await conn.begin()
        rows = (
            await conn.execute(
                text("SELECT id, filename, file_path FROM visura_documents WHERE filename = ANY(:names)"),
                {"names": list(mapping)},
            )
        ).all()
        for row in rows:
            new = mapping[row.filename]
            new_path = posixpath.join(posixpath.dirname(row.file_path), new) if row.file_path else None
            await conn.execute(
                text("UPDATE visura_documents SET filename = :f, file_path = :p WHERE id = :id"),
                {"f": new, "p": new_path, "id": row.id},
            )
            counts["visura_documents"] += 1
        extractions = (
            await conn.execute(
                text("SELECT id, output_path FROM document_structured_extractions WHERE output_path IS NOT NULL")
            )
        ).all()
        for row in extractions:
            new = mapping.get(posixpath.basename(row.output_path))
            if new:
                await conn.execute(
                    text("UPDATE document_structured_extractions SET output_path = :p WHERE id = :id"),
                    {"p": posixpath.join(posixpath.dirname(row.output_path), new), "id": row.id},
                )
                counts["document_structured_extractions"] += 1
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()
    await engine.dispose()
    return counts


def rename_files(plan: list[dict], documents: Path) -> list[tuple[Path, Path]]:
    done: list[tuple[Path, Path]] = []
    try:
        for entry in plan:
            source, target = documents / entry["old"], documents / entry["new"]
            if target.exists() or target.is_symlink():
                raise FileExistsError(target)
            os.rename(source, target)
            done.append((source, target))
    except Exception:
        for source, target in reversed(done):
            os.rename(target, source)
        raise
    return done


def repoint_links(plan: list[dict], documents: Path, links: Path) -> int:
    mapping = {e["old"]: e["new"] for e in plan}
    documents_real = os.path.realpath(documents)
    changed = 0
    for link in sorted(links.iterdir()):
        if not link.is_symlink() or link.name.endswith(SKIPPED):
            continue
        target = os.readlink(link)
        resolved_dir = os.path.dirname(os.path.realpath(os.path.join(links, target)))
        new = mapping.get(os.path.basename(target))
        if new is None or resolved_dir != documents_real:
            continue
        new_target = os.path.join(os.path.dirname(target), new)
        temporary = link.with_name(f".{link.name}.tmp")
        os.symlink(new_target, temporary)
        os.replace(temporary, link)
        changed += 1
    return changed


def backup(directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"sister_schema_{stamp}.dump"
    subprocess.run(
        ["pg_dump", "-h", "127.0.0.1", "-U", "postgres", "-d", "sister", "-n", "sister", "-Fc", "-f", str(target)],
        check=True,
    )
    return target


async def load_requested_at() -> dict[str, str]:
    from sister.database import _get_engine

    engine = _get_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT filename, requested_at FROM visura_documents"))).all()
    await engine.dispose()
    return {r.filename: r.requested_at for r in rows if r.requested_at}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--links", type=Path, help="document_links directory whose aliases are re-pointed")
    parser.add_argument("--backup-dir", type=Path, default=None, help="default: <documents>/../backups")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revert", type=Path, help="plan.json of a previous run: restore the old names")
    args = parser.parse_args()

    documents = args.documents.resolve()
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    backup_dir = args.backup_dir or documents.parent / "backups"

    if args.revert:
        plan = [{"old": e["new"], "new": e["old"]} for e in json.loads(args.revert.read_text())]
        notes: list[str] = []
    else:
        plan, notes = build_plan(documents, asyncio.run(load_requested_at()))

    by_source = collections.Counter(e["source"] for e in plan)
    print(f"{documents}: {len(plan)} files to rename  ({dict(by_source)})")
    for note in notes:
        print("  ", note)
    for entry in plan[:12]:
        print(f"   {entry['old']}\n     -> {entry['new']}  [{entry.get('source', '')}]")
    if not args.apply and not args.revert:
        counts = asyncio.run(update_database(plan, documents, commit=False))
        print("database rows that would change:", counts)
        print("dry run: nothing changed (use --apply)")
        return

    plan_file = backup_dir / f"rename_plan_{stamp}.json"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not args.revert:
        print("backup:", backup(backup_dir, stamp))
        plan_file.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
        print("plan:", plan_file, "(revert with --revert)")

    # files first, then the database commit: if the database step fails the renames are undone
    done = rename_files(plan, documents)
    try:
        counts = asyncio.run(update_database(plan, documents, commit=True))
    except Exception:
        for source, target in reversed(done):
            os.rename(target, source)
        raise
    print("database rows updated:", counts)
    if args.links and args.links.is_dir():
        print("aliases re-pointed:", repoint_links(plan, documents, args.links))
    print(f"renamed {len(done)} files")


if __name__ == "__main__":
    main()
