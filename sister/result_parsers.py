"""Pure parsers for the composite strings SISTER puts in its HTML result pages.

The portal packs several facts into one cell or one hidden ``value``: ``RA/103`` is sezione + foglio,
``Proprieta' per 1/2`` is diritto + quota, ``ROSSI MARIO a RAVENNA (RA) il 01/01/1970`` is name + birth place + birth
date, and the radio of every listed immobile carries the catasto comune code. Splitting them here (no browser, no
database) gives the extraction code and the persistence layer one normalised view of what a page returned.
"""

from __future__ import annotations

import re
from typing import Any

_SPACES = re.compile(r"\s+")
_AMOUNT = re.compile(r"\d[\d.]*(?:,\d+)?")
_PERSON_ID = re.compile(r"^[A-Z0-9]{16}$")
_COMPANY_ID = re.compile(r"^\d{11}$")
_IDENTIFIER = re.compile(r"^(?:[A-Z0-9]{16}|\d{11})$")
_DATE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_SHARE = re.compile(r"(\d+(?:[.,]\d+)?\s*/\s*\d+|\d+(?:[.,]\d+)?\s*%)")


def squeeze(value: Any) -> str:
    """Collapse whitespace (portal cells carry newlines and non-breaking spaces)."""
    return _SPACES.sub(" ", str(value if value is not None else "").replace("\xa0", " ")).strip()


def clean_amount(value: Any) -> str | None:
    """``R.Euro:557,77`` / ``Euro: 189,80`` → ``557,77`` / ``189,80`` (Italian decimal format kept); None when empty."""
    text = squeeze(value)
    if not text:
        return None
    found = _AMOUNT.search(text)
    return found.group(0) if found else None


def land_area_m2(row: dict) -> int | None:
    """The terreni list gives the area as three columns, ``ha`` / ``are`` / ``ca`` → square metres (None if absent)."""
    if not any(key in row for key in ("ha", "are", "ca")):
        return None
    try:
        return int(squeeze(row.get("ha")) or 0) * 10000 + int(squeeze(row.get("are")) or 0) * 100 + int(
            squeeze(row.get("ca")) or 0
        )
    except ValueError:
        return None


def split_foglio(value: Any) -> tuple[str, str]:
    """``RA/103`` → (``RA``, ``103``): the prefix is the sezione urbana/censuaria. ``103`` → (``""``, ``103``)."""
    text = squeeze(value)
    if "/" in text:
        section, _, sheet = text.partition("/")
        return section.strip().upper(), sheet.strip()
    return "", text


def identifier_kind(identifier: str | None) -> str | None:
    """``person`` (16-character codice fiscale), ``legal_entity`` (11-digit partita IVA / CF) or None."""
    ident = squeeze(identifier).upper()
    if _COMPANY_ID.match(ident):
        return "legal_entity"
    if _PERSON_ID.match(ident):
        return "person"
    return None


def gender_from_codice_fiscale(codice_fiscale: str | None) -> str | None:
    """The day of birth of a female codice fiscale is increased by 40 (characters 10-11)."""
    ident = squeeze(codice_fiscale).upper()
    if identifier_kind(ident) != "person" or not ident[9:11].isdigit():
        return None
    return "F" if int(ident[9:11]) > 40 else "M"


def birth_code_from_codice_fiscale(codice_fiscale: str | None) -> str | None:
    """Belfiore code of the birth place (characters 12-15), e.g. ``H501``; foreign states start with ``Z``."""
    ident = squeeze(codice_fiscale).upper()
    if identifier_kind(ident) != "person":
        return None
    code = ident[11:15]
    return code if re.fullmatch(r"[A-Z]\d{3}", code) else None


def parse_titolarita(value: Any) -> dict[str, str]:
    """``Proprieta' per 1/2`` → ``{right_type: "Proprieta'", ownership_share: "1/2"}``.

    The Intestati table has diritto and quota in separate columns; the subject/property lists combine them. Anything
    after the quota (``in regime di comunione dei beni``) is kept under ``note``.
    """
    text = squeeze(value)
    if not text:
        return {}
    match = re.match(r"^(?P<type>.*?)\s+per\s+(?P<rest>.*)$", text, flags=re.IGNORECASE)
    if not match:
        return {"right_type": text}
    share = _SHARE.search(match.group("rest"))
    if not share:
        return {"right_type": text}
    out = {"right_type": match.group("type").strip(), "ownership_share": share.group(1).replace(" ", "")}
    note = squeeze(match.group("rest").replace(share.group(0), "", 1)).strip(" ,;-")
    if note:
        out["note"] = note
    return out


def parse_period(value: Any) -> tuple[str, str]:
    """``dal 01/01/2020 al 31/12/2023`` / ``dall'impianto al 31/12/2023`` → (start, end) as DD/MM/YYYY (or "")."""
    text = squeeze(value)
    dates = _DATE.findall(text)
    if not dates:
        return "", ""
    if re.search(r"\bdal\b.*\bal\b", text) and len(dates) >= 2:
        return dates[0], dates[1]
    if re.search(r"\bdal\b", text) and "dall'impianto" not in text.lower():
        return dates[0], ""
    return "", dates[-1]


def parse_ubicazione(value: Any) -> dict[str, str]:
    """``RAVENNA(RA) VIA EL ALAMEIN n. 2 Piano T-1`` → municipality, province, address."""
    text = squeeze(value)
    match = re.match(r"^(?P<comune>.+?)\s*\((?P<prov>[A-Za-z]{2})\)\s*(?P<address>.*)$", text)
    if not match:
        return {"address": text} if text else {}
    return {
        "municipality": match.group("comune").strip(),
        "province": match.group("prov").upper(),
        "address": match.group("address").strip(),
    }


def parse_classamento(value: Any) -> dict[str, str]:
    """``Zona 1 Cat.A/4`` → census_zone ``1``, category ``A/4`` (the soggetto list combines them)."""
    text = squeeze(value)
    out: dict[str, str] = {}
    zone = re.search(r"\bZona\s+(\S+)", text, flags=re.IGNORECASE)
    if zone:
        out["census_zone"] = zone.group(1)
    category = re.search(r"\bCat\.?\s*([A-Z]\s*/?\s*\d+)", text, flags=re.IGNORECASE)
    if category:
        out["category"] = re.sub(r"\s+", "", category.group(1)).upper()
    return out


def parse_nominativo(value: Any) -> dict[str, str]:
    """Split the "Nominativo o denominazione" cell of the Intestati table.

    * persona fisica: ``ROSSI MARIO a RAVENNA (RA) il 01/01/1970`` → name, birth_place, birth_province, birth_date;
    * persona giuridica: ``ROSSI S.R.L. con sede in MONZA (MB)`` → name, registered_office.
    """
    text = squeeze(value)
    if not text:
        return {}
    person = re.match(
        r"^(?P<name>.+?)\s+a\s+(?P<place>.+?)\s*(?:\((?P<prov>[A-Za-z]{2,3})\))?\s+il\s+(?P<date>\d{2}/\d{2}/\d{4})$",
        text,
        flags=re.IGNORECASE,
    )
    if person:
        out = {"name": person.group("name").strip(), "birth_place": person.group("place").strip(),
               "birth_date": person.group("date")}
        if person.group("prov"):
            out["birth_province"] = person.group("prov").upper()
        return out
    company = re.match(
        r"^(?P<name>.+?)\s+con\s+sede\s+in\s+(?P<place>.+?)\s*(?:\((?P<prov>[A-Za-z]{2})\))?$", text, flags=re.IGNORECASE
    )
    if company:
        sede = company.group("place").strip()
        if company.group("prov"):
            sede = f"{sede} ({company.group('prov').upper()})"
        return {"name": company.group("name").strip(), "registered_office": sede}
    return {"name": text}


def split_person_name(name: str | None, *, has_birth_data: bool = True) -> tuple[str | None, str | None]:
    """Cognome / nome of a persona fisica.

    The portal prints ``COGNOME NOME`` with no separator, so a split is only safe for exactly two words; longer
    names (compound surnames, several given names) are left to the caller as the full display name.
    """
    words = squeeze(name).split(" ") if name else []
    if has_birth_data and len(words) == 2:
        return words[0], words[1]
    return None, None


def parse_place(value: Any) -> tuple[str, str]:
    """``RAVENNA (RA)`` → (municipality, province)."""
    text = squeeze(value)
    match = re.match(r"^(?P<m>.*?)\s*\((?P<p>[A-Za-z]{2,3})\)$", text)
    if match:
        return match.group("m").strip(), match.group("p").upper()
    return text, ""


def parse_vis_imm_sel(value: Any) -> dict[str, str]:
    """Decode the ``visImmSel`` radio value of a listed immobile.

    ``634568#634568#F#RA/103#1714#H199##2# #RAVENNA`` → id, catasto (F/T), sezione, foglio, particella, the catasto
    comune code (``H199``, not the ISTAT code), subalterno and the comune name. Terreni rows leave the sub empty
    (``…#G273### #PALERMO``). Fields the radio leaves blank are omitted.
    """
    parts = [part for part in (value or "").split("#")] if isinstance(value, str) else []
    if len(parts) < 6:
        return {}
    section, sheet = split_foglio(parts[3])
    out = {
        "id_immobile": parts[0].strip(),
        "catasto": parts[2].strip(),
        "section": section,
        "sheet": sheet,
        "parcel": parts[4].strip(),
        "municipality_code": parts[5].strip(),
        "subunit": parts[7].strip() if len(parts) > 7 else "",
        "municipality": parts[9].strip() if len(parts) > 9 else "",
    }
    return {key: val for key, val in out.items() if val}


def parse_intestato_value(value: str) -> dict:
    """Decode an ``intestatoSelezionato`` radio value.

    Persona fisica: ``id#id#COGNOME NOME #CF#SESSO#LUOGO (PR)#GG/MM/AAAA``;
    persona giuridica: ``id#0#DENOMINAZIONE#SEDE (PR)#PARTITA IVA``.
    """
    parts = [part.strip() for part in (value or "").split("#")]
    ident = next((part for part in parts if _IDENTIFIER.match(part)), "")
    owner: dict = {"codice_fiscale": ident, "tipo": "azienda" if len(ident) == 11 else "persona"}
    if owner["tipo"] == "persona" and len(parts) >= 7:
        owner.update(nome=parts[2], sesso=parts[4], luogo_nascita=parts[5], data_nascita=parts[6])
    elif len(parts) >= 5:
        owner.update(nome=parts[2], sede=parts[3])
    return owner


_NOMINATIVO_KEYS = ("Nominativo o denominazione", "Nominativo", "nome", "Denominazione")
_CF_KEYS = ("Codice fiscale", "Codice Fiscale", "codice_fiscale", "CF")


def _first(source: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = squeeze(source.get(key))
        if value:
            return value
    return ""


def normalize_owner(raw: dict, as_of: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """One owner, whatever page or document it came from → (subject fields, right fields).

    Understands the Intestati table row (``Nominativo o denominazione`` / ``Codice fiscale`` / ``Titolarità`` /
    ``Quota``), the decoded radio (``nome`` / ``sesso`` / ``luogo_nascita`` / ``data_nascita``) and the XML
    ``Intestato`` (``Nominativo`` / ``CF`` / ``DirittiReali``). Subject keys follow ``get_or_create_subject`` plus
    ``birth_province`` / ``birth_municipality`` (resolved to a place by the caller); right keys follow
    ``get_or_create_right``. Empty values are omitted. ``as_of`` (DD/MM/YYYY) is the visura's reference date: a
    right ending on it is still held, so it gets no end date.
    """
    subject: dict[str, Any] = {}
    right: dict[str, Any] = {}

    identifier = _first(raw, _CF_KEYS).upper()
    kind = identifier_kind(identifier)
    if identifier:
        subject["fiscal_code"] = identifier

    nominativo = _first(raw, _NOMINATIVO_KEYS)
    parsed = parse_nominativo(nominativo)
    if ";" in nominativo or "DirittiReali" in raw:  # the XML spells name, birth data and place differently
        xml_parsed = parse_xml_nominativo(nominativo)
        if xml_parsed.get("birth_date") or xml_parsed.get("birth_date_partial") or xml_parsed.get("registered_office") or xml_parsed.get("note"):
            parsed = {key: value for key, value in xml_parsed.items() if key != "birth_place"}
            place, province = parse_place(xml_parsed.get("birth_place", ""))
            if place:
                parsed["birth_place"], parsed["birth_province"] = place, province
    name = parsed.get("name") or ""
    if raw.get("Cognome") or raw.get("Nome"):
        subject["last_name"] = squeeze(raw.get("Cognome")) or None
        subject["first_name"] = squeeze(raw.get("Nome")) or None
        name = name or " ".join(part for part in (squeeze(raw.get("Cognome")), squeeze(raw.get("Nome"))) if part)
    if name:
        subject["display_name"] = name

    birth_place = parsed.get("birth_place", "")
    birth_province = parsed.get("birth_province", "")
    if raw.get("luogo_nascita"):
        birth_place, birth_province = parse_place(raw["luogo_nascita"])
    birth_date = parsed.get("birth_date") or squeeze(raw.get("data_nascita")) or squeeze(raw.get("DataNascita"))
    if birth_date:
        subject["date_of_birth"] = birth_date
    if birth_place:
        subject["birth_municipality"] = birth_place
        if birth_province:
            subject["birth_province"] = birth_province
    if parsed.get("registered_office") or raw.get("sede"):
        subject["registered_office"] = parsed.get("registered_office") or squeeze(raw.get("sede"))

    gender = squeeze(raw.get("sesso")).upper()[:1] or gender_from_codice_fiscale(identifier)
    if gender in {"M", "F"} and kind != "legal_entity":
        subject["gender"] = gender
    birth_code = birth_code_from_codice_fiscale(identifier)
    if birth_code:
        subject["birth_municipality_code"] = birth_code
    if kind or birth_date or birth_place:
        subject["subject_type"] = kind or ("person" if birth_date else "unknown")
    if subject.get("subject_type") == "person" and "last_name" not in subject and birth_date:
        last, first = split_person_name(subject.get("display_name"))
        if last:
            subject["last_name"], subject["first_name"] = last, first

    # --- right -----------------------------------------------------------------------------------------------
    diritti = raw.get("DirittiReali") if isinstance(raw.get("DirittiReali"), dict) else {}
    titolarita = parse_titolarita(raw.get("Titolarità") or raw.get("Titolarita") or raw.get("titolarita"))
    if diritti:
        right["right_code"] = squeeze(diritti.get("CodiceDiritto")) or None
        right["right_type"] = squeeze(diritti.get("Descrizione")) or None
        right["right_type"] = clean_right_description(diritti.get("Descrizione")) or None
        right["ownership_share"] = clean_quota(diritti.get("Quota")) or None
        period = parse_right_period(diritti.get("FineDiritto"), as_of)
        right["start_date"], right["end_date"] = period["start"] or None, period["end"] or None
    else:
        right.update(titolarita)
        right.pop("note", None)
        quota = squeeze(raw.get("Quota") or raw.get("quota"))
        if quota:
            right["ownership_share"] = quota
    return (
        {key: value for key, value in subject.items() if value not in (None, "")},
        {key: value for key, value in right.items() if value not in (None, "")},
    )


def workflow_columns(row: dict) -> dict:
    """A subject-list row with the columns the workflow executors read (``aecs4u_workflow._normalize_property``).

    The executors expect ``Provincia`` / ``Comune`` / ``Foglio`` / ``Particella`` (+ ``Sub``, ``Tipo``);
    the owner → immobili list gives ``provincia_nome``, ``Ubicazione`` (comune + indirizzo), ``Foglio`` as ``RA/103``
    and ``Catasto``. Added only where missing; ``Foglio`` becomes the plain number (the portal's value is kept as
    ``Foglio_portale``). Rows that are not immobili (no foglio/particella) are returned unchanged.
    """
    if not (row.get("Foglio") and row.get("Particella")):
        return row
    radio = parse_vis_imm_sel(row.get("visImmSel"))
    out = dict(row)
    section, sheet = split_foglio(row["Foglio"])
    if section or sheet != row["Foglio"]:
        out["Foglio_portale"] = row["Foglio"]
        out["Foglio"] = sheet
    out.setdefault("Provincia", squeeze(row.get("provincia_nome")) or squeeze(row.get("provincia")))
    out.setdefault("Comune", parse_ubicazione(row.get("Ubicazione")).get("municipality") or radio.get("municipality", ""))
    if not out.get("Tipo"):
        out["Tipo"] = squeeze(row.get("Catasto")) or radio.get("catasto", "")
    # no "Sezione": the executors pass it to the sezione dropdown, but the RA of RA/103 is a sezione *urbana*
    return out


# ---------------------------------------------------------------------------------------------------------
# Visura XML cells (the XML spells names and periods differently from the HTML tables)
# ---------------------------------------------------------------------------------------------------------

_XML_PLACE = re.compile(
    r";\s*(?:Comune\s+(?:(?P<sex>Nat[oa])\s+a\s+)?)?(?P<place>[^;()]+?)\s*\((?P<prov>[A-Za-z]{2,3})\)\s*$"
)
_XML_DATE_END = re.compile(r"\s*(?P<date>\d{2}/\d{2}/\d{4})\s*$")
_XML_PARTIAL_DATE = re.compile(r"\s*(?P<partial>/\d{1,2}/\d{1,2}/)\s*il\s*$")


def _unescape(text: str) -> str:
    import html

    for _ in range(2):  # the portal escapes some names twice (``&amp;amp;``)
        if "&" not in text:
            break
        text = html.unescape(text)
    return text


def parse_xml_nominativo(value: Any) -> dict[str, str]:
    """Split the ``Nominativo`` of a visura XML.

    * ``ROSSI MARIO 04/07/1974; Comune PALERMO (PA)`` → name, birth_date, birth_place, birth_province;
    * ``ROSSI MARIA /0/26/ il ; Comune Nata a MILANO (MI)`` → name, partial birth date (``/mese/anno/``), sex F, place;
    * ``MONTE DI PIETA'; Comune PALERMO (PA)`` (no date) → name, registered_office;
    * ``ROSSI Anna ; Bianchi`` → name, note (the text after the first ``;``).
    """
    text = squeeze(_unescape(squeeze(value)))
    if not text:
        return {}
    out: dict[str, str] = {}
    head = text
    place = _XML_PLACE.search(text)
    if place:
        head = text[: place.start()]
        place_text = f"{place.group('place').strip()} ({place.group('prov').upper()})"
        sex = place.group("sex")
        if sex:
            out["sex"] = "F" if sex.lower() == "nata" else "M"
    else:
        place_text = ""
    head = head.strip()
    date = _XML_DATE_END.search(head)
    partial = _XML_PARTIAL_DATE.search(head)
    if date:
        out["birth_date"] = date.group("date")
        head = head[: date.start()]
    elif partial:
        out["birth_date_partial"] = partial.group("partial")
        head = head[: partial.start()]
    name, _, note = head.partition(";")
    out["name"] = squeeze(name)
    if squeeze(note):
        out["note"] = squeeze(note)
    if place_text:
        if out.get("birth_date") or out.get("birth_date_partial") or out.get("sex"):
            out["birth_place"] = place_text
        else:
            out["registered_office"] = place_text
    return out


def parse_right_period(value: Any, as_of: str = "") -> dict[str, Any]:
    """``dal 01/09/2012 al 10/10/2026`` / ``dall'impianto al 29/03/1983`` → a period with its flags.

    ``as_of`` is the visura's reference date (DD/MM/YYYY): an end equal to it means the right is still held
    (``ongoing``), not that it ended on that day. A start after the end (the right is listed as of the unit's
    creation but ended earlier) is reported as ``inverted`` with the start dropped.
    """
    text = squeeze(value)
    start, end = parse_period(text)
    from_origin = "dall'impianto" in text.lower() or "dall’impianto" in text.lower()
    ongoing = bool(end) and bool(as_of) and end == as_of
    inverted = False
    if start and end and _to_date(start) and _to_date(end) and _to_date(start) > _to_date(end):
        inverted = True
        start = ""
    return {"start": start, "end": "" if ongoing else end, "from_origin": from_origin, "ongoing": ongoing,
            "inverted": inverted, "raw": text}


def _to_date(value: str):
    from datetime import datetime

    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def clean_quota(value: Any) -> str:
    """``" per 2/9"`` → ``2/9``; ``"per 1/1"`` → ``1/1``."""
    return re.sub(r"^\s*per\s+", "", squeeze(value), flags=re.IGNORECASE)


def clean_right_description(value: Any) -> str:
    """``"Livellario per"`` → ``Livellario`` (the portal leaves the preposition of the quota in the description)."""
    return re.sub(r"\s+per$", "", squeeze(value), flags=re.IGNORECASE)
