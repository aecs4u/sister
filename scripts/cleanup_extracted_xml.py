"""Remove the XML copies extracted next to the signed ``.p7m`` documents (files and database rows).

The ``.p7m`` is the only stored copy; its XML is read in memory (``sister.utils._p7m_payload``). Older versions
wrote ``<stem>.xml`` and, through ``_parse_visura_xml``, an extensionless ``<stem>`` next to every ``.p7m``, and the
folder imports indexed all three as separate documents.

A file is removed only when its bytes equal the payload of ``<stem>.p7m``. A ``document`` row is removed
(with everything that hangs off it, children first, in one transaction) only when its file is such a copy and the
``.p7m`` has its own row with the XML content. Dry run unless ``--apply``.

    ../.venv/bin/python scripts/cleanup_extracted_xml.py --documents <dir>           # report
    ../.venv/bin/python scripts/cleanup_extracted_xml.py --documents <dir> --apply   # back up, then clean
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from sister.utils import _p7m_payload  # noqa: E402


def scan_files(documents: Path) -> tuple[dict[str, Path], list[str]]:
    """Return ``{file name: path}`` of the verified copies and the notes about files that were left alone."""
    copies: dict[str, Path] = {}
    notes: list[str] = []
    for path in sorted(documents.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in (".xml", ""):
            continue
        p7m = path.with_name(f"{path.stem if path.suffix else path.name}.p7m")
        if not p7m.is_file():
            if not path.suffix:
                notes.append(f"kept {path.name}: no .p7m next to it")
            continue  # a standalone .xml is a real download
        payload = _p7m_payload(str(p7m))
        if payload is None:
            notes.append(f"kept {path.name}: {p7m.name} is unreadable")
        elif path.read_bytes() != payload:
            notes.append(f"kept {path.name}: differs from the payload of {p7m.name}")
        else:
            copies[path.name] = path
    return copies, notes


async def fk_children(conn, table: str) -> list[tuple[str, str, str]]:
    """Foreign keys that point at ``table``: ``(child table, child column, referenced column)``."""
    rows = await conn.execute(
        text(
            """
            SELECT c.conrelid::regclass::text, ca.attname, pa.attname
            FROM pg_constraint c
            JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = c.conkey[1]
            JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[1]
            WHERE c.contype = 'f' AND c.confrelid = CAST(:t AS regclass) AND array_length(c.conkey, 1) = 1
            """
        ),
        {"t": table},
    )
    return [(r[0], r[1], r[2]) for r in rows]  # keep the schema: the archive tables are not on the search path


async def delete_tree(conn, table: str, where: str, params: dict, counts: dict[str, int]) -> None:
    """Delete the rows of ``table`` matching ``where`` after the rows that reference them."""
    for child, child_col, parent_col in await fk_children(conn, table):
        if child == table:
            continue  # a tree inside one table (document_xml_nodes.parent_id) goes away in the same DELETE
        await delete_tree(conn, child, f"{child_col} IN (SELECT {parent_col} FROM {table} WHERE {where})", params, counts)
    result = await conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    counts[table] = counts.get(table, 0) + (result.rowcount or 0)


async def clean_database(copies: dict[str, Path], apply: bool) -> dict[str, int]:
    from sister.database import DATABASE_ARCHIVE_SCHEMA, _get_engine, document_file_path

    engine = _get_engine()
    counts: dict[str, int] = {}
    # A dry run executes the same deletes and rolls them back, so the counts are exact.
    async with engine.connect() as conn:
        transaction = await conn.begin()
        rows = (await conn.execute(text("SELECT id, file_name AS filename FROM document"))).all()
        archive = f'"{DATABASE_ARCHIVE_SCHEMA}"'
        with_content = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT d.file_name FROM document d"  # noqa: S608 (quoted schema name)
                    f" JOIN {archive}.document_content c ON c.document_id = d.id"
                    " WHERE d.file_name LIKE '%.p7m' AND c.content <> ''"
                )
            )
        }
        duplicate_ids: list[int] = []
        skipped: list[str] = []
        for row in rows:
            name = row.filename
            if name.endswith(".p7m"):
                continue
            stem = name[:-4] if name.endswith(".xml") else name
            if name.endswith(".xml") or "." not in name:
                if f"{stem}.p7m" in with_content:
                    # the row is a copy when its file is a verified copy (or is already gone)
                    stored = document_file_path(name)
                    path = Path(stored) if stored else None
                    if name in copies or path is None or not path.exists():
                        duplicate_ids.append(row.id)
                        continue
                skipped.append(name)
        counts["_skipped_rows"] = len(skipped)
        if skipped:
            print("rows kept (no complete .p7m row or file not a verified copy):", *skipped[:10], sep="\n  ")
        if duplicate_ids:
            await delete_tree(conn, "document", "id = ANY(:ids)", {"ids": duplicate_ids}, counts)
        counts["_documents"] = len(duplicate_ids)
        await (transaction.commit() if apply else transaction.rollback())
    await engine.dispose()
    return counts


def backup(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"sister_schema_{datetime.now():%Y%m%d_%H%M%S}.dump"
    subprocess.run(
        ["pg_dump", "-h", "127.0.0.1", "-U", "postgres", "-d", "sister", "-n", "sister", "-Fc", "-f", str(target)],
        check=True,
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--links", type=Path, help="document_links directory: aliases of deleted XML are removed")
    parser.add_argument("--backup-dir", type=Path, default=None, help="where pg_dump writes (default: <documents>/../backups)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-db", action="store_true", help="files only")
    args = parser.parse_args()

    documents = args.documents.resolve()
    copies, notes = scan_files(documents)
    print(f"verified extracted copies in {documents}: {len(copies)}")
    print(f"  .xml: {sum(1 for n in copies if n.endswith('.xml'))}, extensionless: {sum(1 for n in copies if '.' not in n)}")
    for note in notes:
        print("  ", note)

    targets = {Path(os.path.realpath(path)) for path in copies.values()}
    aliases = []
    if args.links and args.links.is_dir():
        for link in args.links.iterdir():
            if link.is_symlink():
                resolved = Path(os.path.realpath(os.path.join(link.parent, os.readlink(link))))
                if resolved in targets:
                    aliases.append(link)
    print(f"document_links aliases pointing at those copies: {len(aliases)}")

    if not args.skip_db:
        if args.apply:
            dump = backup(args.backup_dir or documents.parent / "backups")
            print(f"backup written: {dump}")
        counts = asyncio.run(clean_database(copies, args.apply))
        print("database rows " + ("deleted" if args.apply else "that would be deleted") + ":")
        for table, count in sorted(counts.items()):
            if not table.startswith("_"):
                print(f"  {table}: {count}")

    if args.apply:
        for path in copies.values():
            path.unlink()
        for link in aliases:
            link.unlink()
        print(f"removed {len(copies)} files and {len(aliases)} aliases")
    else:
        print("dry run: nothing changed (use --apply)")


if __name__ == "__main__":
    main()
