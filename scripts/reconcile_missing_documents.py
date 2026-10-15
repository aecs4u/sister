"""Clean up after documents were removed by hand: database rows and aliases that point at a file that is gone.

For every ``visura_documents`` row (and ``document_links`` alias) whose file no longer exists, the surviving file of
the same document is looked up: the old name, or the name without its practice-id suffix, followed through the renames
recorded by ``rename_documents_with_date.py`` / ``normalize_documents.py``. It counts as the same document only when
its content equals the XML kept in the database (the PDFs have no stored content: their rows are only reported).

* row with a surviving equivalent that has its own row: the row is removed with everything below it (children first,
  one transaction) and a response link is moved to the surviving row;
* alias with a surviving equivalent: re-pointed; otherwise only reported.

Dry run unless ``--apply`` (which writes a database backup first).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import posixpath
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from cleanup_extracted_xml import backup, delete_tree  # noqa: E402
from sister.utils import _p7m_payload  # noqa: E402

XML_NOISE = re.compile(rb'\s(Data|Ora|Protocollo|Anno)="[^"]*"')
TAIL = re.compile(r"(?<=_\d{4}-\d{2}-\d{2})(?:_T\d+_\d{4}|_DOC_\d+)+(?:_\d+)?(?=\.)")


def xml_key(data: bytes) -> str:
    return hashlib.sha256(XML_NOISE.sub(b"", data)).hexdigest()


def rename_chain(backup_dir: Path) -> dict[str, str]:
    """old name -> newest name, from the plans written by the rename / normalise scripts (oldest first)."""
    mapping: dict[str, str] = {}
    plans = sorted(backup_dir.glob("rename_plan_*.json")) + sorted(backup_dir.glob("normalize_plan_*.json"))
    for path in sorted(plans, key=lambda p: p.stem.rsplit("_", 2)[-2:]):
        data = json.loads(path.read_text())
        entries = data["renames"] if isinstance(data, dict) else data
        for entry in entries:
            mapping[entry["old"]] = entry["new"]
    return mapping


def candidates(name: str, chain: dict[str, str]) -> list[str]:
    found: list[str] = []
    for start in dict.fromkeys([name, TAIL.sub("", name)]):
        current, seen = start, set()
        while current in chain and current not in seen:
            seen.add(current)
            current = chain[current]
        found.extend([start, current])
    return list(dict.fromkeys(found))


async def extraction_counts() -> dict[int, int]:
    """Structured extractions (OCR/LLM results, costly to redo) per document id."""
    from sister.database import _get_engine

    engine = _get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT document_id, count(*) FROM document_structured_extractions GROUP BY document_id")
            )
        ).all()
    await engine.dispose()
    return {r[0]: r[1] for r in rows}


async def load_rows() -> list:
    from sister.database import _get_engine

    engine = _get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT d.id, d.filename, d.response_id, m.content FROM visura_documents d"
                    " LEFT JOIN document_metadata m ON m.id = d.id"
                )
            )
        ).all()
    await engine.dispose()
    return rows


async def remove_rows(pairs: list[tuple[int, int]], commit: bool) -> dict[str, int]:
    from sister.database import _get_engine

    counts: dict[str, int] = {}
    engine = _get_engine()
    async with engine.connect() as conn:
        transaction = await conn.begin()
        for gone_id, keeper_id in pairs:
            await conn.execute(
                text(
                    "UPDATE visura_documents SET response_id = (SELECT response_id FROM visura_documents WHERE id = :g)"
                    " WHERE id = :k AND response_id IS NULL"
                ),
                {"g": gone_id, "k": keeper_id},
            )
        if pairs:
            await delete_tree(conn, "visura_documents", "id = ANY(:ids)", {"ids": [g for g, _ in pairs]}, counts)
        await (transaction.commit() if commit else transaction.rollback())
    await engine.dispose()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--links", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    documents = args.documents.resolve()
    backup_dir = args.backup_dir or documents.parent / "backups"
    chain = rename_chain(backup_dir)
    existing = {p.name for p in documents.iterdir() if p.is_file()}
    keys: dict[str, str] = {}

    def file_key(name: str) -> str:
        if name not in keys:
            payload = _p7m_payload(str(documents / name)) if name.lower().endswith(".p7m") else None
            keys[name] = xml_key(payload) if payload else ""
        return keys[name]

    rows = asyncio.run(load_rows())
    by_name = {r.filename: r for r in rows}
    # content -> the surviving signed files that have a row of their own (the name is only a tie-breaker)
    by_content: dict[str, list[str]] = {}
    for name in sorted(existing):
        if name.lower().endswith(".p7m") and name in by_name and file_key(name):
            by_content.setdefault(file_key(name), []).append(name)

    def find_survivor(name: str, content_key: str) -> str | None:
        if not content_key or content_key not in by_content:
            return None
        named = [c for c in candidates(name, chain) if c in by_content[content_key]]
        return (named or by_content[content_key])[0]

    extractions = asyncio.run(extraction_counts())
    removable: list[tuple[int, int]] = []
    unmatched: list[str] = []
    kept_for_extractions: list[str] = []
    for row in rows:
        if row.filename in existing:
            continue
        # a PDF row also carries the XML of its signed pair: only a .p7m row can be matched to a .p7m survivor
        is_signed = row.filename.lower().endswith(".p7m")
        content_key = xml_key(row.content.encode("utf-8")) if row.content and is_signed else ""
        survivor = find_survivor(row.filename, content_key)
        if survivor and extractions.get(row.id, 0) > extractions.get(by_name[survivor].id, 0):
            kept_for_extractions.append(row.filename)
        elif survivor:
            removable.append((row.id, by_name[survivor].id))
        else:
            unmatched.append(row.filename)
    print(f"rows whose file is gone: {len(removable) + len(unmatched)}")
    print(f"  same document survives (row can go): {len(removable)}")
    print(f"  has extraction results its equivalent lacks (kept): {len(kept_for_extractions)}")
    for name in kept_for_extractions:
        print("     ", name)
    print(f"  no provable equivalent (kept, reported): {len(unmatched)}")
    for name in unmatched[:15]:
        print("     ", name)

    repoint: list[tuple[Path, str]] = []
    orphan_links: list[str] = []
    if args.links and args.links.is_dir():
        for link in sorted(args.links.iterdir()):
            if not link.is_symlink() or link.exists() or link.name.endswith(".meta"):
                continue
            target = os.readlink(link)
            name = os.path.basename(target)
            row = by_name.get(name)
            content_key = xml_key(row.content.encode("utf-8")) if row is not None and row.content else ""
            survivor = find_survivor(name, content_key)
            if survivor:
                repoint.append((link, posixpath.join(posixpath.dirname(target), survivor)))
            else:
                orphan_links.append(f"{link.name} -> {name}")
    print(f"dangling aliases: {len(repoint) + len(orphan_links)} | re-pointable: {len(repoint)}")
    for entry in orphan_links[:10]:
        print("      no equivalent:", entry)

    counts = asyncio.run(remove_rows(removable, commit=False)) if not args.apply else None
    if not args.apply:
        print("rows that would be removed:", {k: v for k, v in sorted(counts.items()) if v})
        print("dry run: nothing changed (use --apply)")
        return
    print("backup:", backup(backup_dir))
    counts = asyncio.run(remove_rows(removable, commit=True))
    print("rows removed:", {k: v for k, v in sorted(counts.items()) if v})
    for link, new_target in repoint:
        temporary = link.with_name(f".{link.name}.tmp")
        os.symlink(new_target, temporary)
        os.replace(temporary, link)
    print(f"aliases re-pointed: {len(repoint)}")


if __name__ == "__main__":
    main()
