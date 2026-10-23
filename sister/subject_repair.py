"""Repair of ``cadastral_subjects`` rows imported with incomplete or wrong birth data.

What the audit of 2026-10-23 found (see ``scripts/repair_subject_data.py``): owners whose date the portal degraded
(``/0/08/ il``) were stored without birth date and without first/last name, some display names still carried the
Intestati-page text (``ROSSI MARIO nato a ROMA (RM) il 01/01/1970``), a few men were stored as "F", and one date used
the ISO format. A personal codice fiscale states sex, birth date (day, month, two-digit year) and birth place, so it is
the source of truth wherever the row is silent or contradicts it.

``plan_subject`` is pure: it returns what would change and what is suspicious, and never guesses (an ambiguous century
or a date that contradicts the code is reported, not written).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .result_parsers import (
    birth_code_from_codice_fiscale,
    birth_date_from_codice_fiscale,
    correct_century,
    gender_from_codice_fiscale,
    identifier_kind,
    parse_nominativo,
    split_person_name,
    squeeze,
)

_NATO_TAIL = re.compile(r"\s+nat[oa]$", re.IGNORECASE)
_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def plan_subject(row: dict[str, Any], today: date | None = None) -> tuple[dict[str, tuple[Any, Any]], list[str]]:
    """``({column: (old, new)}, notes)`` for one subject row.

    ``row`` has the columns ``fiscal_code``, ``display_name``, ``last_name``, ``first_name``, ``gender``,
    ``date_of_birth`` and ``birth_municipality_code``.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    notes: list[str] = []
    cf = squeeze(row.get("fiscal_code")).upper()
    person = identifier_kind(cf) == "person"

    def set_(column: str, new: Any) -> None:
        old = row.get(column)
        if new and new != old:
            changes[column] = (old, new)

    # --- display name: the Intestati page's "NAME nato a PLACE (PR) il DATE" is not a name ------------------------
    display = squeeze(row.get("display_name"))
    junk_date = ""
    if display:
        parsed = parse_nominativo(display)
        clean = parsed.get("name") or display
        clean = _NATO_TAIL.sub("", clean)
        junk_date = parsed.get("birth_date", "")
        if clean != display:
            set_("display_name", clean)
        display = clean

    # --- birth date ----------------------------------------------------------------------------------------------
    current = squeeze(row.get("date_of_birth"))
    iso = _ISO.fullmatch(current)
    if iso:
        current = f"{iso.group(3)}/{iso.group(2)}/{iso.group(1)}"
        set_("date_of_birth", current)
    derived = birth_date_from_codice_fiscale(cf, today) if person else None
    if not current:
        chosen = junk_date or derived or ""
        if junk_date and derived and junk_date != derived:
            notes.append(f"date in the name ({junk_date}) contradicts the codice fiscale ({derived}): kept the name's")
        set_("date_of_birth", chosen)
        current = chosen
        if person and not chosen:
            notes.append("birth date not derivable (ambiguous century or malformed codice fiscale)")
    elif person and derived and current != derived:
        fixed = correct_century(current, cf, today)
        if fixed != current:  # the portal prints 19yy for someone born in 20yy: 118 years old is not a person
            set_("date_of_birth", fixed)
            notes.append(f"date_of_birth {current} was a century off: {fixed}")
        else:
            notes.append(f"date_of_birth {current} contradicts the codice fiscale ({derived}): left as is")

    # --- sex and birth place from the codice fiscale ---------------------------------------------------------------
    if person:
        sex = gender_from_codice_fiscale(cf)
        if sex and row.get("gender") != sex:
            set_("gender", sex)
        code = birth_code_from_codice_fiscale(cf)
        if code and not row.get("birth_municipality_code"):
            set_("birth_municipality_code", code)

    # --- first / last name (only for exactly two words, as split_person_name says) -----------------------------------
    if person and display and not squeeze(row.get("last_name")) and current:
        last, first = split_person_name(display)
        if last:
            set_("last_name", last)
            if not squeeze(row.get("first_name")):
                set_("first_name", first)
    return changes, notes
