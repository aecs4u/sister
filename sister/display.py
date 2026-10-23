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


def merge_identifiers(node: Any) -> Any:
    """Copy of a parsed visura tree where a unit with several ``IdentificativoDefinitivo`` shows them all.

    A unit that merges parcels (fusione) lists one identifier per parcel; the theme tables read a single mapping and
    printed dashes for foglio, particella and sub. Values that differ are joined in order (``522, 2206``), equal ones
    stay single. Only meant for rendering: lookups (map, OMI zone, history) keep working on the original tree.
    """
    if isinstance(node, list):
        return [merge_identifiers(item) for item in node]
    if not isinstance(node, dict):
        return node
    merged = {}
    for key, value in node.items():
        if key == "IdentificativoDefinitivo" and isinstance(value, list) and len(value) > 1:
            items = [item for item in value if isinstance(item, dict)]
            combined: dict[str, Any] = {}
            for item in items:
                for field, field_value in item.items():
                    if isinstance(field_value, (dict, list)):
                        combined.setdefault(field, merge_identifiers(field_value))
                        continue
                    seen = combined.setdefault(field, [])
                    if isinstance(seen, list) and str(field_value).strip() and field_value not in seen:
                        seen.append(field_value)
            value = {
                field: (", ".join(str(v).strip() for v in found) if isinstance(found, list) else found)
                for field, found in combined.items()
            }
        merged[key] = merge_identifiers(value)
    return merged


def subject_for_theme(tree: Any) -> Any:
    """Copy of a parsed subject visura where a legal entity reads like the person the theme expects.

    The theme prints ``Cognome Nome``, birth date, birth place and sex of ``SoggettoPF``; a company
    (``SoggettoPNF``: Denominazione, Sede) came out as "-" in every field and the title fell back to the portal's
    object string. The denomination goes in ``Cognome`` and the registered office in ``ComuneNascita`` (the page script
    relabels it "Sede"). Only for rendering: the original tree is left alone.
    """
    if isinstance(tree, list):
        return [subject_for_theme(item) for item in tree]
    if not isinstance(tree, dict):
        return tree
    out = {}
    for key, value in tree.items():
        if key == "SoggettoPNF" and isinstance(value, dict) and value.get("Denominazione") and not value.get("Cognome"):
            value = {**value, "Cognome": value["Denominazione"], "ComuneNascita": value.get("Sede", ""), "Provincia": ""}
        out[key] = subject_for_theme(value)
    return out


def owner_details(nominativo: Any, codice_fiscale: Any = "", as_of: date | None = None) -> dict[str, str]:
    """An owner's ``Nominativo`` from a visura XML as readable parts: name, sex, birth place, birth date, office.

    The XML spells a person as ``ROSSI MARIO 04/07/1974; Comune PALERMO (PA)`` or, when the cadastre has lost the
    date, ``ROSSI MARIO /0/04/ il ; Comune Nato a PALERMO (PA)``. A missing date is recovered from the codice fiscale
    when that is unambiguous (see ``birth_date_from_codice_fiscale``).
    """
    from .result_parsers import (
        birth_date_from_codice_fiscale,
        correct_century,
        gender_from_codice_fiscale,
        parse_xml_nominativo,
    )

    parsed = parse_xml_nominativo(nominativo)
    date_text = correct_century(parsed.get("birth_date") or "", codice_fiscale, as_of)
    if not date_text:
        date_text = birth_date_from_codice_fiscale(codice_fiscale, as_of, partial=parsed.get("birth_date_partial", "")) or ""
    return {
        "name": parsed.get("name") or "",
        "sex": gender_from_codice_fiscale(codice_fiscale) or parsed.get("sex", ""),
        "birth_place": parsed.get("birth_place", ""),
        "birth_date": date_text,
        "office": parsed.get("registered_office", ""),
    }


def owner_label(nominativo: Any, codice_fiscale: Any = "", as_of: date | None = None) -> str:
    """``GRISTINA Antonio, nato a PALERMO (PA) il 08/04/1962`` instead of the portal's ``... /0/08/ il ; Comune Nato a ...``."""
    details = owner_details(nominativo, codice_fiscale, as_of)
    if not details["name"]:
        return " ".join(str(nominativo or "").split())
    if details["office"]:
        return f"{details['name']}, sede {details['office']}"
    verb = {"F": "nata", "M": "nato"}.get(details["sex"], "nato/a")
    label = details["name"]
    if details["birth_place"] or details["birth_date"]:
        label += f", {verb}"
        if details["birth_place"]:
            label += f" a {details['birth_place']}"
        if details["birth_date"]:
            label += f" il {details['birth_date']}"
    return label


def readable_owners(tree: Any, as_of: date | None = None) -> Any:
    """Copy of a parsed visura where every ``Nominativo`` next to a ``CF`` reads as ``owner_label``.

    The original text stays under ``NominativoOriginale`` (the "Dati completi" dump is meant to be exhaustive). Only for
    rendering.
    """
    if isinstance(tree, list):
        return [readable_owners(item, as_of) for item in tree]
    if not isinstance(tree, dict):
        return tree
    title = tree.get("TitoloVisura")
    if isinstance(title, dict) and re.fullmatch(r"\d{8}", str(title.get("SituazioneAl") or "")):
        text = str(title["SituazioneAl"])
        as_of = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    out = {key: readable_owners(value, as_of) for key, value in tree.items()}
    if isinstance(tree.get("Nominativo"), str) and "CF" in tree:
        out["Nominativo"] = owner_label(tree["Nominativo"], tree.get("CF"), as_of)
        out["NominativoOriginale"] = tree["Nominativo"]
    return out


def register(env) -> None:
    """Attach the filters to a Jinja environment."""
    env.filters.update(
        it_date=it_date,
        it_money=it_money,
        it_enum=it_enum,
        clean_requester=clean_requester,
        inspection_ref=inspection_ref,
    )
