"""Presentation helpers (Jinja filters) for values shown in the web UI.

Extracted data keeps its raw form (ISO dates, enum codes, floats); these helpers only change how it is shown.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_PLACEHOLDER = "–"

_ENUM_LABELS = {
    "proprieta": "Proprietà",
    "nuda_proprieta": "Nuda proprietà",
    "usufrutto": "Usufrutto",
    "creditore_ipotecario": "Creditore ipotecario",
    "debitore_ipotecario": "Debitore ipotecario",
    "terzo_datore": "Terzo datore",
    "atto_notarile_pubblico": "Atto notarile pubblico",
    "scrittura_privata_autenticata": "Scrittura privata autenticata",
    "iscrizione": "Iscrizione",
    "trascrizione": "Trascrizione",
    "annotazione": "Annotazione",
    "fabbricati": "Fabbricati",
    "terreni": "Terreni",
}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_ODD_CASE = re.compile(r"[A-Z]{2,}[a-z]|[a-z][A-Z]{2,}")  # e.g. "IPOTeca VOLONTARIA"
_REQUESTER_TAIL = re.compile(r"(?:^|\s+)Tassa\s+versata\b.*$", re.IGNORECASE | re.DOTALL)


def it_date(value: Any, with_time: bool = True) -> str:
    """ISO date/datetime (or ``datetime``/``date``) -> ``dd/mm/yyyy[ HH:MM[:SS]]``; unknown shapes pass through."""
    if value in (None, ""):
        return _PLACEHOLDER
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S" if with_time else "%d/%m/%Y")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    text = str(value).strip()
    match = _ISO_DATE.match(text)
    if not match:
        return text
    year, month, day, hour, minute, second, tz = match.groups()
    out = f"{day}/{month}/{year}"
    if with_time and hour is not None:
        out += f" {hour}:{minute}" + (f":{second}" if second else "")
        if tz in ("Z", "+00:00", "+0000"):
            out += " UTC"
    return out


def it_money(value: Any) -> str:
    """Number -> ``€ 52.000,00`` (Italian grouping); ``None``/empty -> en dash."""
    if value in (None, ""):
        return _PLACEHOLDER
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {text}"


def it_enum(value: Any) -> str:
    """Enum code / shouting text -> readable label (``atto_notarile_pubblico`` -> ``Atto notarile pubblico``)."""
    if value in (None, ""):
        return _PLACEHOLDER
    text = str(value).strip()
    key = text.casefold()
    if key in _ENUM_LABELS:
        return _ENUM_LABELS[key]
    if "_" in text or text.isupper() or _ODD_CASE.search(text):
        sentence = text.replace("_", " ").casefold()
        return sentence[:1].upper() + sentence[1:]
    return text


def clean_requester(value: Any) -> str:
    """Drop the neighbouring "Tassa versata ..." field that the PDF text puts on the same line."""
    if value in (None, ""):
        return _PLACEHOLDER
    return _REQUESTER_TAIL.sub("", str(value)).strip() or _PLACEHOLDER


# the "1" is dropped only when it is a separate "T1 " prefix (so T12345 stays T12345)
_INSPECTION_REF = re.compile(r"^\s*T\s*(?:1\s+)?(\d+)\s*$", re.IGNORECASE)


def inspection_ref(value: Any) -> str:
    """``T1 72614`` / ``T 72614`` / ``t72614`` -> ``T72614`` (the form used in the page header and in file names)."""
    if value in (None, ""):
        return _PLACEHOLDER
    match = _INSPECTION_REF.match(str(value))
    return f"T{match.group(1)}" if match else str(value).strip()


def register(env) -> None:
    """Attach the filters to a Jinja environment."""
    env.filters.update(
        it_date=it_date,
        it_money=it_money,
        it_enum=it_enum,
        clean_requester=clean_requester,
        inspection_ref=inspection_ref,
    )
