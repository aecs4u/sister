"""Reference data of the flat schema: places, cadastral units, subjects, rights and the small lookup tables.

Every writer (the response projection, the XML ingestion, the document importer) finds or creates the same real-world
things through these functions, so each exists once:

* ``get_or_create_comune``         a municipality, by Belfiore code when known, else by (province sigla, name)
* ``get_or_create_cadastral_unit`` (catasto, comune, sezione, foglio, particella, subalterno)
* ``get_or_create_subject``        a person or legal entity, by fiscal code, else by name + birth date + birth comune
* ``get_or_create_right``          a right kind plus its share (a reduced fraction) and period
* ``ensure_lookup``                a row of one of the small open vocabularies (request type, category, ...)

All of them run inside the caller's session and are safe under concurrent writers (``INSERT .. ON CONFLICT``).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .db_models import IstatMunicipality
from .provinces import province_code
from .request_types import request_type_info
from .result_parsers import parse_it_date, parse_share, squeeze

logger = logging.getLogger("sister")

_FOREIGN_PREFIX = re.compile(r"^\s*NAT[AO]\s+(?:IN|A)\s+", re.IGNORECASE)
_BELFIORE = re.compile(r"^[A-Z][0-9]{3}$")
_CADASTRE_TYPES = {"T", "F", "E"}

# the small open vocabularies a writer may extend: lookup table → description column (or None)
LOOKUP_TABLES = {
    "request_type": None,
    "document_type": None,
    "cadastral_category": "description",
    "land_quality": None,
    "activity_action": None,
    "mortgage_lien_type": None,
    "mortgage_title_type": None,
    "mortgage_note_type": None,
    "mortgage_party_quality": None,
}


def clean(value: Any) -> Optional[str]:
    """Trimmed text, or None when empty (the schema stores 'no value' as NULL, never as an empty string)."""
    text_value = squeeze(value)
    return text_value or None


def place_name(value: Any) -> Optional[str]:
    """A municipality name as stored: upper case, ``NATO IN BRASILE`` (foreign birthplace) → ``BRASILE``."""
    name = _FOREIGN_PREFIX.sub("", squeeze(value))
    return name.upper() or None


def belfiore_code(value: Any) -> Optional[str]:
    """The four-character cadastral municipality code (``H199``), or None when the value is not one."""
    code = squeeze(value).upper()
    return code if _BELFIORE.match(code) else None


def cadastre_type_code(value: Any) -> Optional[str]:
    code = squeeze(value).upper()[:1]
    return code if code in _CADASTRE_TYPES else None


def fiscal_code_key(value: Any) -> Optional[str]:
    """Fiscal code / VAT number with all whitespace removed and upper-cased; None when empty."""
    code = re.sub(r"\s+", "", str(value or "")).upper()
    return code or None


async def _one(session: AsyncSession, sql: str, **params: Any):
    return (await session.execute(text(sql), params)).first()


# ---------------------------------------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------------------------------------


async def ensure_lookup(session: AsyncSession, table: str, code: Any, description: Any = None) -> Optional[str]:
    """Make sure ``code`` exists in a lookup table; returns the stored code (None for an empty value)."""
    if table not in LOOKUP_TABLES:
        raise ValueError(f"{table!r} is not a lookup table")
    value = clean(code)
    if value is None:
        return None
    if table == "request_type":
        info = request_type_info(value)
        await session.execute(
            text(
                "INSERT INTO request_type (code, label, description) VALUES (:c, :label, :description) "
                "ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description"
            ),
            {"c": value, "label": info.label, "description": info.description},
        )
        for tag in info.tags:
            await session.execute(
                text(
                    "INSERT INTO request_type_tag (request_type, tag) VALUES (:c, :tag) "
                    "ON CONFLICT (request_type, tag) DO NOTHING"
                ),
                {"c": value, "tag": tag},
            )
        return value
    description_column = LOOKUP_TABLES[table]
    if description_column and clean(description):
        await session.execute(
            text(
                f"INSERT INTO {table} (code, {description_column}) VALUES (:c, :d) "  # noqa: S608 (whitelisted name)
                f"ON CONFLICT (code) DO UPDATE SET {description_column} = coalesce({table}.{description_column}, EXCLUDED.{description_column})"
            ),
            {"c": value, "d": clean(description)},
        )
    else:
        await session.execute(text(f"INSERT INTO {table} (code) VALUES (:c) ON CONFLICT (code) DO NOTHING"), {"c": value})  # noqa: S608
    return value


# ---------------------------------------------------------------------------------------------------------
# ISTAT municipality
# ---------------------------------------------------------------------------------------------------------


async def get_or_create_comune(
    session: AsyncSession,
    name: Any = None,
    province: Any = None,
    cadastral_code: Any = None,
) -> Optional[int]:
    """Resolve an existing ISTAT ``numeric_code`` from a name and/or Belfiore code.

    This compatibility-named helper no longer creates reference rows. A known cadastral code wins; otherwise a name
    must resolve uniquely, with a province sigla used when supplied. When the province is absent, homonyms remain
    unresolved.
    """
    place = place_name(name)
    prov = province_code(province)
    code = belfiore_code(cadastral_code)

    if code:
        row = await session.execute(
            select(IstatMunicipality.numeric_code).where(func.upper(func.trim(IstatMunicipality.cadastral_code)) == code).limit(1)
        )
        numeric_code = row.scalar_one_or_none()
        if numeric_code is not None:
            return numeric_code
    if not place:
        return None

    names = or_(
        func.upper(func.trim(IstatMunicipality.name)) == place,
        func.upper(func.trim(IstatMunicipality.italian_name)) == place,
        func.upper(func.trim(IstatMunicipality.other_language_name)) == place,
    )
    statement = select(IstatMunicipality.numeric_code).where(names)
    if prov:
        statement = statement.where(func.upper(func.trim(IstatMunicipality.province_abbreviation)) == prov)
    matches = list(dict.fromkeys((await session.execute(statement)).scalars().all()))
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------------------------------------
# Cadastral unit
# ---------------------------------------------------------------------------------------------------------


async def get_or_create_cadastral_unit(
    session: AsyncSession,
    *,
    cadastre_type: Any = None,
    province: Any = None,
    municipality: Any = None,
    municipality_code: Any = None,
    section: Any = None,
    sheet: Any = None,
    parcel: Any = None,
    subunit: Any = None,
) -> int:
    """The cadastral unit with this identity (absent parts are NULL), created when it does not exist yet."""
    comune_id = await get_or_create_comune(session, municipality, province, municipality_code)
    key = {
        "ct": cadastre_type_code(cadastre_type),
        "cid": comune_id,
        "sec": (clean(section) or "").upper() or None,
        "sh": clean(sheet),
        "pa": clean(parcel),
        "su": clean(subunit),
    }
    row = await _one(
        session,
        "INSERT INTO cadastral_unit (cadastre_type, comune_id, section, sheet, parcel, subunit) "
        "VALUES (:ct, :cid, :sec, :sh, :pa, :su) ON CONFLICT ON CONSTRAINT uq_cadastral_unit DO NOTHING RETURNING id",
        **key,
    )
    if row is None:
        row = await _one(
            session,
            "SELECT id FROM cadastral_unit WHERE cadastre_type IS NOT DISTINCT FROM :ct AND comune_id IS NOT DISTINCT FROM :cid "
            "AND section IS NOT DISTINCT FROM :sec AND sheet IS NOT DISTINCT FROM :sh AND parcel IS NOT DISTINCT FROM :pa "
            "AND subunit IS NOT DISTINCT FROM :su",
            **key,
        )
    return row.id


# ---------------------------------------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------------------------------------


async def get_or_create_subject(
    session: AsyncSession,
    *,
    fiscal_code: Any = None,
    display_name: Any = None,
    last_name: Any = None,
    first_name: Any = None,
    gender: Any = None,
    date_of_birth: Any = None,
    birth_province: Any = None,
    birth_municipality: Any = None,
    birth_municipality_code: Any = None,
    subject_type: Any = None,
) -> int:
    """Find the subject by fiscal code (else by name + birth date + birth comune) or create it; blanks get completed.

    A later, richer source (the birth data of the Intestati page, the XML) completes a bare subject but never
    overwrites what is known. ``date_of_birth`` may be ``dd/mm/yyyy`` text or a date.
    """
    code = fiscal_code_key(fiscal_code)
    last = clean(last_name)
    first = clean(first_name)
    name = clean(display_name) or clean(" ".join(part for part in (last, first) if part)) or code or "(sconosciuto)"
    born = date_of_birth if isinstance(date_of_birth, date) else parse_it_date(date_of_birth)
    birth_comune_id = None
    if clean(birth_municipality) or belfiore_code(birth_municipality_code):
        birth_comune_id = await get_or_create_comune(session, birth_municipality, birth_province, birth_municipality_code)
    birth_municipality_text = place_name(birth_municipality) if birth_comune_id is None else None
    birth_province_code = province_code(birth_province) if birth_municipality_text else None
    sex = (clean(gender) or "").upper()
    sex = sex if sex in {"M", "F"} else None
    kind = clean(subject_type)
    if kind not in {"person", "legal_entity"}:
        kind = None

    existing = None
    if code:
        existing = await _one(session, "SELECT id FROM subject WHERE fiscal_code = :c", c=code)
    else:
        existing = await _one(
            session,
            "SELECT id FROM subject WHERE fiscal_code IS NULL AND upper(name) = upper(:n) AND birth_date IS NOT DISTINCT FROM :b "
            "AND birth_comune_id IS NOT DISTINCT FROM :bc AND birth_municipality_text IS NOT DISTINCT FROM :bt "
            "AND birth_province_code IS NOT DISTINCT FROM :bp AND subject_type IS NOT DISTINCT FROM :k ORDER BY id LIMIT 1",
            n=name,
            b=born,
            bc=birth_comune_id,
            bt=birth_municipality_text,
            bp=birth_province_code,
            k=kind,
        )
    if existing is not None:
        await session.execute(
            text(
                "UPDATE subject SET last_name = coalesce(last_name, :l), first_name = coalesce(first_name, :f), "
                "gender = coalesce(gender, :g), birth_date = coalesce(birth_date, :b), "
                "birth_comune_id = coalesce(birth_comune_id, :bc), "
                "birth_municipality_text = coalesce(birth_municipality_text, :bt), "
                "birth_province_code = coalesce(birth_province_code, :bp), subject_type = coalesce(subject_type, :k), "
                "name = CASE WHEN name IN ('(sconosciuto)', fiscal_code) AND :n NOT IN ('(sconosciuto)') THEN :n ELSE name END "
                "WHERE id = :i"
            ),
            {
                "l": last,
                "f": first,
                "g": sex,
                "b": born,
                "bc": birth_comune_id,
                "bt": birth_municipality_text,
                "bp": birth_province_code,
                "k": kind,
                "n": name,
                "i": existing.id,
            },
        )
        return existing.id

    params = {
        "c": code,
        "n": name,
        "l": last,
        "f": first,
        "g": sex,
        "b": born,
        "bc": birth_comune_id,
        "bt": birth_municipality_text,
        "bp": birth_province_code,
        "k": kind,
    }
    row = await _one(
        session,
        "INSERT INTO subject (fiscal_code, name, last_name, first_name, gender, birth_date, birth_comune_id, "
        "birth_municipality_text, birth_province_code, subject_type) "
        "VALUES (:c, :n, :l, :f, :g, :b, :bc, :bt, :bp, :k) "
        "ON CONFLICT ON CONSTRAINT uq_subject_fiscal_code DO NOTHING RETURNING id",
        **params,
    )
    if row is None:  # a concurrent writer inserted the same fiscal code
        row = await _one(session, "SELECT id FROM subject WHERE fiscal_code = :c", c=code)
    return row.id


# ---------------------------------------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------------------------------------


async def get_or_create_right(
    session: AsyncSession,
    *,
    right_type: Any = None,
    right_code: Any = None,
    right_description: Any = None,
    ownership_share: Any = None,
    start_date: Any = None,
    end_date: Any = None,
) -> int:
    """The right (kind + share + period), created when it does not exist yet.

    The share is stored as a fraction in lowest terms (``500/1000`` is ``1/2``), so equal shares are one right.
    """
    kind = {"t": clean(right_type), "c": clean(right_code), "d": clean(right_description)}
    row = await _one(
        session,
        "INSERT INTO right_kind (right_type, right_code, description) VALUES (:t, :c, :d) "
        "ON CONFLICT ON CONSTRAINT uq_right_kind DO NOTHING RETURNING id",
        **kind,
    )
    if row is None:
        row = await _one(
            session,
            "SELECT id FROM right_kind WHERE right_type IS NOT DISTINCT FROM :t AND right_code IS NOT DISTINCT FROM :c "
            "AND description IS NOT DISTINCT FROM :d",
            **kind,
        )
    kind_id = row.id

    share = parse_share(ownership_share)
    if share is None and clean(ownership_share):
        logger.debug("Quota non riconosciuta: %r", ownership_share)
    key = {
        "k": kind_id,
        "n": share[0] if share else None,
        "d": share[1] if share else None,
        "s": start_date if isinstance(start_date, date) else parse_it_date(start_date),
        "e": end_date if isinstance(end_date, date) else parse_it_date(end_date),
    }
    row = await _one(
        session,
        "INSERT INTO ownership_right (right_kind_id, share_numerator, share_denominator, start_date, end_date) "
        "VALUES (:k, :n, :d, :s, :e) ON CONFLICT ON CONSTRAINT uq_ownership_right DO NOTHING RETURNING id",
        **key,
    )
    if row is None:
        row = await _one(
            session,
            "SELECT id FROM ownership_right WHERE right_kind_id = :k AND share_numerator IS NOT DISTINCT FROM :n "
            "AND share_denominator IS NOT DISTINCT FROM :d AND start_date IS NOT DISTINCT FROM :s AND end_date IS NOT DISTINCT FROM :e",
            **key,
        )
    return row.id


async def get_or_create_office(session: AsyncSession, name: Any) -> Optional[int]:
    """The land registry office (``Direzione Provinciale di Ravenna …``) a document came from."""
    value = clean(name)
    if value is None:
        return None
    row = await _one(
        session, "INSERT INTO land_registry_office (name) VALUES (:n) ON CONFLICT (name) DO NOTHING RETURNING id", n=value
    )
    if row is None:
        row = await _one(session, "SELECT id FROM land_registry_office WHERE name = :n", n=value)
    return row.id
