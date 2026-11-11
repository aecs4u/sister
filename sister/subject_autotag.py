"""Tags for persons and companies derived from data the archive already has.

Four families, each a rule over a source (see ``docs/subject_tags.md``):

* **PVP** — the code is a person of the *Portale Vendite Pubbliche* (``pvp.pvp_persons``, or the ``custodian_fiscal_code``
  of a PVP sale): ``PVP`` plus the role(s) as ``PVP: delegato`` / ``custode`` / ``curatore`` / ``giudice`` / ``liquidatore`` /
  ``commissario`` / ``altro``. These are the professionals of a sale, not its owners.
* **Aste** — ``Aste: proprietario immobile``: owner of a property of a PVP sale, i.e. of a result of a request tagged ``PVP``
  (the property visure the PVP lists in ``inputs/pvp_sales`` asked for).
* **Politici** — the code is in ``inputs/politici_siciliani.md``: ``Politici`` plus the level of its section
  (Regione / Libero consorzio / Città metropolitana / Comune). The list's codes were computed from name and birth data,
  so a match is accepted only when the archive's name agrees with the list's (``names_agree``); the others are reported.
* **Catasto** — ``Non presente in catasto``: a company whose searches succeeded but found no property and no name, i.e. a
  code SISTER does not know.

Only subjects that are in the archive tables (``/persons``, ``/companies``) are tagged. Everything here only *adds* tags
(``database.tag_subjects`` never overwrites), so it is safe to run again; ``scripts/tag_subjects.py`` is the command.
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from . import database
from .database_config import database_dsn_setting
from .subject_names import CODE_COLUMNS, _cells, _header_key

AUTOTAG_USER = "script:tag_subjects"
TAG_PVP = "PVP"
TAG_AUCTION_OWNER = "Aste: proprietario immobile"
TAG_POLITICIAN = "Politici"
TAG_NOT_IN_CADASTRE = "Non presente in catasto"

# PVP role codes (``pvp.pvp_persons.role``) -> the tag of the role
PVP_ROLE_TAGS = {
    "DELEGATO": "PVP: delegato",
    "CUSTODE": "PVP: custode",
    "CURATOR": "PVP: curatore",
    "GIUDICE": "PVP: giudice",
    "LIQUGIU": "PVP: liquidatore",
    "LIQUIDAT": "PVP: liquidatore",
    "COMMLIQUID": "PVP: liquidatore",
    "GESTLIQU": "PVP: liquidatore",
    "COMMGIU": "PVP: commissario",
    "COMISIO": "PVP: commissario",
}
TAG_PVP_OTHER = "PVP: altro"  # SOGGSPV, IVG and any role not listed above

# a heading of politici_siciliani.md -> the tag of its level (first match; accents and apostrophes do not matter)
POLITICIAN_LEVELS = (
    ("regione siciliana", "Politici: Regione"),
    ("liberi consorzi", "Politici: Libero consorzio"),
    ("citta metropolitane", "Politici: Città metropolitana"),
    ("comuni", "Politici: Comune"),
)
_PERSON_CODE = re.compile(r"^[A-Z0-9]{16}$")


def kind_of(code: str) -> Optional[str]:
    """``person`` for a 16-character codice fiscale, ``company`` for an 11-digit partita IVA."""
    return "person" if len(code) == 16 else "company" if len(code) == 11 and code.isdigit() else None


def pvp_tags(roles: Iterable[str]) -> set[str]:
    """``PVP`` and one tag per distinct role of a person in the PVP (unknown roles become ``PVP: altro``)."""
    tags = {TAG_PVP}
    for role in roles:
        tags.add(PVP_ROLE_TAGS.get(str(role or "").strip().upper(), TAG_PVP_OTHER))
    return tags


def _level_tag(heading: str) -> Optional[str]:
    plain = re.sub(r"[^a-z ]", "", heading.lower().replace("à", "a").replace("'", "")).strip()
    return next((tag for prefix, tag in POLITICIAN_LEVELS if plain.startswith(prefix)), None)


def _name_tokens(name: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    return sorted(re.sub(r"[^A-Z ]", " ", plain).split())


def names_agree(first: str, second: str) -> Optional[bool]:
    """Whether two spellings of a person's name can be the same person; ``None`` when one of them is empty.

    Every word of the shorter name must be in the longer one, up to a small spelling difference (a middle name more or
    less, ``IMPELLIZZERI`` / ``Impellizzieri``); ``SEVERO ROSALIA`` and ``Saverino Ersilia`` are two people.
    """
    one, other = _name_tokens(first), _name_tokens(second)
    if not one or not other:
        return None
    short, long_ = (one, other) if len(one) <= len(other) else (other, one)
    return all(
        any(word == candidate or difflib.SequenceMatcher(None, word, candidate).ratio() >= 0.85 for candidate in long_)
        for word in short
    )


NAME_COLUMNS_POLITICIANS = ("intestatario", "nome", "name", "nominativo")


def politicians_from_markdown(source: str | Path) -> dict[str, tuple[str, set[str]]]:
    """Code -> (name in the list, tags: ``Politici`` + the levels it is listed under) from the batch list of politicians.

    A table counts when its heading names a level (see ``POLITICIAN_LEVELS``); the section of people without a computable
    codice fiscale and any code that is not 16 characters are skipped.
    """
    body = Path(source).read_text(encoding="utf-8") if isinstance(source, Path) else str(source)
    found: dict[str, tuple[str, set[str]]] = {}
    level: Optional[str] = None
    code_at: Optional[int] = None
    name_at: Optional[int] = None
    expect_header = True
    for line in body.splitlines():
        if line.startswith("#"):
            level, code_at, expect_header = _level_tag(line.lstrip("#").strip()), None, True
            continue
        row = _cells(line)
        if row is None:
            code_at, expect_header = None, True
            continue
        if expect_header:
            keys = [_header_key(cell) for cell in row]
            code_at = next((keys.index(k) for k in CODE_COLUMNS if k in keys), None)
            name_at = next((keys.index(k) for k in NAME_COLUMNS_POLITICIANS if k in keys), None)
            expect_header = False
            continue
        if level is None or code_at is None or code_at >= len(row):
            continue
        code = re.sub(r"\s", "", row[code_at]).upper()
        if _PERSON_CODE.match(code):
            name = " ".join(row[name_at].split()) if name_at is not None and name_at < len(row) else ""
            known_name, tags = found.get(code, (name, set()))
            tags.update({TAG_POLITICIAN, level})
            found[code] = (known_name or name, tags)
    return found


# ---------------------------------------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------------------------------------


def pvp_dsn() -> str:
    """The ``pvp`` database next to the application's one (same server and login), or ``PVP_DATABASE_DSN``."""
    explicit = os.getenv("PVP_DATABASE_DSN")
    if explicit:
        return explicit
    dsn = database.DATABASE_DSN
    if not dsn:
        raise RuntimeError(f"{database_dsn_setting()} (or PVP_DATABASE_DSN) must be set")
    return make_url(dsn).set(database="pvp", query={}).render_as_string(hide_password=False)


async def pvp_roles(codes: list[str]) -> dict[str, set[str]]:
    """Code -> roles in the PVP: every role of ``pvp.pvp_persons``, and ``CUSTODE`` for a sale's custodian code."""
    engine = create_async_engine(pvp_dsn())
    roles: dict[str, set[str]] = defaultdict(set)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            for code, role in await conn.execute(
                text("SELECT upper(btrim(fiscal_code)), role FROM pvp.pvp_persons WHERE upper(btrim(fiscal_code)) = ANY(:c)"),
                {"c": codes},
            ):
                roles[code].add(str(role or ""))
            for (code,) in await conn.execute(
                text(
                    "SELECT DISTINCT upper(btrim(custodian_fiscal_code)) FROM modelview.modelview_sales "
                    "WHERE upper(btrim(custodian_fiscal_code)) = ANY(:c)"
                ),
                {"c": codes},
            ):
                roles[code].add("CUSTODE")
    finally:
        await engine.dispose()
    return dict(roles)


async def auction_owner_codes(codes: list[str]) -> set[str]:
    """Codes that own a property of a PVP sale: owners in the results of the requests tagged ``PVP``."""
    async with database._get_session_factory()() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = await session.execute(
            text(
                "SELECT DISTINCT upper(btrim(s.fiscal_code)) FROM request_tag t "
                "JOIN result_owner o ON o.request_id = t.request_id JOIN subject s ON s.id = o.subject_id "
                "WHERE t.tag = 'PVP' AND upper(btrim(s.fiscal_code)) = ANY(:c)"
            ),
            {"c": codes},
        )
        return {code for (code,) in rows}


def unknown_company_codes(companies: list[dict]) -> set[str]:
    """Companies searched successfully that have neither a name nor a property: SISTER does not know the code."""
    return {
        c["fiscal_code"] for c in companies if not c.get("name") and not c.get("properties") and c.get("searches")
    }


# ---------------------------------------------------------------------------------------------------------
# plan / apply / revert
# ---------------------------------------------------------------------------------------------------------

FAMILIES = ("pvp", "aste", "politici", "catasto")


async def build_plan(
    politicians: Optional[Path] = None, only: Iterable[str] = FAMILIES
) -> tuple[dict[tuple[str, str], set[str]], list[dict]]:
    """``(plan, skipped)``: ``(kind, code) -> tags`` for the subjects of the archive tables by the rules of the families in
    ``only``, and the politician matches left out because the two names disagree (``code``, ``archive``, ``list``)."""
    only = set(only)
    skipped: list[dict] = []
    people, _ = await database.query_subject_profiles("person", limit=None)
    companies, _ = await database.query_subject_profiles("company", limit=None)
    codes = [p["fiscal_code"] for p in people + companies]
    wanted: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add(code: str, tags: Iterable[str]) -> None:
        kind = kind_of(code)
        if kind:
            wanted[(kind, code)].update(tags)

    if "pvp" in only:
        for code, roles in (await pvp_roles(codes)).items():
            add(code, pvp_tags(roles))
    if "aste" in only:
        for code in await auction_owner_codes(codes):
            add(code, [TAG_AUCTION_OWNER])
    if "politici" in only and politicians is not None:
        archive_names = {p["fiscal_code"]: p["name"] for p in people}
        for code, (listed_name, tags) in politicians_from_markdown(politicians).items():
            if code not in archive_names:
                continue
            if names_agree(archive_names[code], listed_name) is False:
                skipped.append({"code": code, "archive": archive_names[code], "list": listed_name})
            else:
                add(code, tags)
    if "catasto" in only:
        for code in unknown_company_codes(companies):
            add(code, [TAG_NOT_IN_CADASTRE])
    return dict(wanted), skipped


async def existing_tags() -> set[tuple[str, str, str]]:
    """``(kind, code, tag_key)`` of every tag already stored."""
    async with database._get_session_factory()() as session:
        rows = await session.execute(text("SELECT subject_type, subject_code, tag_key FROM subject_tag"))
        return {(kind, code, key) for kind, code, key in rows}


async def new_rows(plan: dict[tuple[str, str], set[str]]) -> list[dict]:
    """The ``{kind, code, tag}`` rows of the plan that are not stored yet, sorted."""
    have = await existing_tags()
    rows = [
        {"kind": kind, "code": code, "tag": tag}
        for (kind, code), tags in plan.items()
        for tag in tags
        if (kind, code, database.normalize_tag(tag)[0]) not in have
    ]
    return sorted(rows, key=lambda r: (r["tag"], r["kind"], r["code"]))


async def apply_rows(rows: list[dict]) -> int:
    """Store the rows (one bulk insert per tag and kind); returns how many were really added."""
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        grouped[(row["kind"], row["tag"])].append(row["code"])
    added = 0
    for (kind, tag), codes in grouped.items():
        added += await database.tag_subjects(kind, codes, tag, AUTOTAG_USER)
    return added


async def revert_rows(rows: list[dict]) -> int:
    """Remove the rows a previous run added — only those still written by this script (a tag a user re-created stays)."""
    removed = 0
    async with database._get_session_factory()() as session:
        for row in rows:
            result = await session.execute(
                text(
                    "DELETE FROM subject_tag WHERE subject_type = :kind AND subject_code = :code "
                    "AND tag_key = :key AND created_by = :user"
                ),
                {"kind": row["kind"], "code": row["code"], "key": database.normalize_tag(row["tag"])[0], "user": AUTOTAG_USER},
            )
            removed += int(result.rowcount or 0)
        await session.commit()
    return removed
