"""Names for subjects SISTER only knows by code.

A subject search by codice fiscale / partita IVA returns the provinces and the properties, never the name, so a
subject first met that way is stored with its code as name (``/persons`` then shows no name). The batch lists that
queued those searches (``inputs/**/*.md``: ``| id | codice_fiscale | intestatario | fonte |`` tables, also the
backups, which keep the rows already pruned) do carry the name. :func:`names_from_markdown` collects them;
``database.fill_subject_names`` writes them where the subject has no real name yet.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

CODE_COLUMNS = ("codice_fiscale", "identificativo", "partita_iva", "codice", "cf", "piva")
NAME_COLUMNS = ("intestatario", "meta_nome", "nome_completo", "denominazione", "nominativo", "name", "nome")
_CODE = re.compile(r"^(?:[A-Z]{6}[0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]|\d{11})$")


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return None
    return [cell.strip().strip("`").strip() for cell in line[1:-1].split("|")]


def _header_key(cell: str) -> str:
    return cell.lower().replace("-", "_").replace(" ", "_")


def names_in_text(text: str) -> Iterable[tuple[str, str]]:
    """(code, name) pairs from every Markdown table of ``text`` that has a code column and a name column."""
    code_at = name_at = None
    expect_header = True
    for line in text.splitlines():
        row = _cells(line)
        if row is None:
            code_at = name_at = None
            expect_header = True
            continue
        if expect_header:
            keys = [_header_key(cell) for cell in row]
            code_at = next((keys.index(k) for k in CODE_COLUMNS if k in keys), None)
            name_at = next((keys.index(k) for k in NAME_COLUMNS if k in keys), None)
            expect_header = False
            continue
        if code_at is None or name_at is None or all(set(cell) <= set("-: ") for cell in row):
            continue
        if max(code_at, name_at) >= len(row):
            continue
        code = re.sub(r"\s", "", row[code_at]).upper()
        name = " ".join(row[name_at].split())
        if _CODE.match(code) and name and name not in ("-", "—") and name.upper() != code:
            yield code, name


def names_from_markdown(paths: Iterable[Path]) -> dict[str, str]:
    """Code → name from the given Markdown files; a code listed with different names keeps the most frequent one."""
    seen: dict[str, Counter] = defaultdict(Counter)
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for code, name in names_in_text(text):
            seen[code][name] += 1
    return {code: counts.most_common(1)[0][0] for code, counts in seen.items()}


def default_list_paths(root: Path) -> list[Path]:
    """Every Markdown batch list under ``<root>/inputs`` (subfolders and backups included)."""
    folder = root / "inputs"
    return sorted(folder.rglob("*.md")) if folder.is_dir() else []


OPENDATA_NAMES_SQL = (
    "SELECT upper(btrim(tax_code)) AS code, btrim(surname) AS last_name, btrim(name) AS first_name "
    "FROM opendata.companies_managers WHERE upper(btrim(tax_code)) = ANY(CAST(:codes AS text[])) "
    "AND coalesce(btrim(surname), '') <> '' "
    "UNION ALL "
    "SELECT upper(btrim(tax_code)), btrim(surname), btrim(name) "
    "FROM opendata.companies_shareholders WHERE upper(btrim(tax_code)) = ANY(CAST(:codes AS text[])) "
    "AND coalesce(btrim(surname), '') <> ''"
)


async def names_from_opendata(codes: Iterable[str], dsn: str | None = None) -> dict[str, tuple[str, str, str]]:
    """Code → (name, last name, first name) of company managers / shareholders in the ``opendata`` database.

    Optional source: read-only, used only when ``OPENDATA_DATABASE_DSN`` (or ``dsn``) is set; ``{}`` otherwise or
    when the database cannot be reached.
    """
    import logging
    import os

    dsn = dsn or os.getenv("OPENDATA_DATABASE_DSN")
    wanted = sorted({str(c or "").strip().upper() for c in codes if str(c or "").strip()})
    if not dsn or not wanted:
        return {}
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn, pool_pre_ping=False)
    found: dict[str, Counter] = defaultdict(Counter)
    try:
        async with engine.connect() as conn:
            for start in range(0, len(wanted), 5000):
                rows = await conn.execute(text(OPENDATA_NAMES_SQL), {"codes": wanted[start : start + 5000]})
                for code, last_name, first_name in rows:
                    last_name = " ".join(str(last_name or "").split()).upper()
                    first_name = " ".join(str(first_name or "").split()).upper()
                    if last_name:
                        found[code][(last_name, first_name)] += 1
    except Exception:  # noqa: BLE001 - an optional source must never break the caller
        logging.getLogger("sister").warning("Nomi da opendata non disponibili", exc_info=True)
        return {}
    finally:
        await engine.dispose()
    names: dict[str, tuple[str, str, str]] = {}
    for code, counts in found.items():
        last_name, first_name = counts.most_common(1)[0][0]
        names[code] = (" ".join(p for p in (last_name, first_name) if p), last_name, first_name)
    return names
