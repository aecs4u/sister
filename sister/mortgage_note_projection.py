"""Mortgage notes (``Nota di trascrizione|iscrizione|annotazione``) → typed ``mortgage_inspection*`` tables.

Two sources feed the same typed rows:

* an Ocular structured JSON (``import_ocular_extraction``), already shaped as ``{"ispezione", "nota": {...}}``;
* the text of a *Ispezione ipotecaria* PDF (``DocumentExtractionPayload.structured_data["raw_text"]``), which
  :func:`parse_note_text` turns into that same shape. Those payloads only kept the text, so the inspection, its parties
  and its properties were never projected and the subject page could not tell who sold or bought what.

``project_note_data`` writes one ``mortgage_inspection`` row, its parties (``subject`` found by fiscal code, else by name
and birth date) and its properties (``cadastral_unit``); ``backfill_mortgage_notes`` runs it on stored payloads.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete
from sqlmodel import select

from .reference import clean, ensure_lookup, get_or_create_cadastral_unit
from .result_parsers import parse_it_date

logger = logging.getLogger(__name__)

_PLACE = re.compile(r"^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$")
_NOTE_KINDS = ("trascrizione", "iscrizione", "annotazione")
_SECTION = re.compile(r"^\s*Sezione ([A-D]) - .*$", re.M)
# Header repeated on every page: from "Ispezione ipotecaria" to the "Registro particolare ... Presentazione" line.
_PAGE_HEADER = re.compile(r"[ \t]*Ispezione ipotecaria[ \t]*\n[ \t]*Ufficio Provinciale.*?\n[ \t]*Registro particolare n\.[^\n]*\n", re.S)
_ROW_LABEL = re.compile(r"^\s*(Immobile|Comune|Catasto|Sezione|Natura|Consistenza|Indirizzo|Piano|Unità|Porzione)\b")


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _time(value: Any):
    try:
        return time.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _datetime(value: Any):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def slug(value: Any) -> str | None:
    """``PROPRIETA'`` → ``proprieta``: the lookup codes of the typed tables."""
    value = clean(value)
    if not value or value == "-":
        return None
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_") or None


def _money_eur(value: str | None) -> int | None:
    """``€ 469.687,09`` → 469687 (the column holds whole euro)."""
    match = re.search(r"([\d.]+(?:,\d+)?)", value or "")
    if not match:
        return None
    number = _decimal(match.group(1).replace(".", ""))
    return int(number.to_integral_value()) if number is not None else None


def _dash(value: str | None) -> str | None:
    value = clean(value)
    return None if value in (None, "-") else value


# -- text → Ocular-shaped dict ------------------------------------------------------------------------------------------


def _find(pattern: str, source: str, flags: int = 0) -> str | None:
    match = re.search(pattern, source, flags)
    return clean(match.group(1)) if match else None


def _fiscal_code(value: str | None) -> str | None:
    value = re.sub(r"\s+", "", value or "").upper()
    return value or None


def _parse_property(unit: int | None, number: int | None, block: str) -> dict[str, Any]:
    comune = re.search(r"Comune\s+(\w{4}) - (.+?)\s*\((\w{2})\)", block)
    catasto = (_find(r"Catasto\s+(\w+)", block) or "").lower()
    lines = block.splitlines()
    nature_lines: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"\s*Natura\s+(.*)$", line)
        if not match:
            continue
        nature_lines.append(match.group(1))
        for follower in lines[index + 1 :]:
            if not follower.strip() or _ROW_LABEL.match(follower):
                if not follower.strip():
                    continue
                break
            nature_lines.append(follower.strip())
        break
    nature_text = " ".join(re.sub(r"\s*\bConsistenza\b.*$", "", part).strip() for part in nature_lines).strip()
    nature_match = re.match(r"(\w+)\s+-\s+(.*)$", nature_text)
    nature_code = nature_match.group(1) if nature_match else None
    nature_description = clean(nature_match.group(2) if nature_match else nature_text)
    # a description continued on the next page keeps its word order: "A2 - ABITAZIONE DI TIPO" + "CIVILE"
    vani = _find(r"(\d+(?:[.,]\d+)?)\s*vani", block)
    sqm = _find(r"(\d+(?:[.,]\d+)?)\s*metri quadri", block)
    address = re.search(r"Indirizzo\s+(.+?)(?:\s{2,}N\. civico\s+(\S+))?\s*$", block, re.M)
    return {
        "numero_immobile": number,
        "unita_negoziale": unit,
        "comune_codice": comune.group(1) if comune else None,
        "comune_denominazione": f"{comune.group(2)} ({comune.group(3)})" if comune else None,
        "tipo_catasto": catasto or None,
        "sezione_urbana": _dash(_find(r"Sezione urbana\s+(\S+)", block)),
        "foglio": _dash(_find(r"Foglio\s+(\S+)", block)),
        "particella": _dash(_find(r"Particella\s+(\S+)", block)),
        "subalterno": _dash(_find(r"Subalterno\s+(\S+)", block)),
        "natura": nature_code,
        "natura_descrizione": nature_description,
        "consistenza_vani": vani.replace(",", ".") if vani else None,
        "consistenza_mq": sqm.replace(",", ".") if sqm else None,
        "piano": _dash(_find(r"^\s*Piano\s+(.+)$", block, re.M)),
        "indirizzo": clean(address.group(1)) if address else None,
        "numero_civico": clean(address.group(2)) if address and address.group(2) else None,
    }


def _parse_properties(section_b: str) -> list[dict[str, Any]]:
    units: dict[int | None, list[dict[str, Any]]] = {}
    unit: int | None = None
    number: int | None = None
    block: list[str] = []

    def flush() -> None:
        if block and number is not None:
            units.setdefault(unit, []).append(_parse_property(unit, number, "\n".join(block)))
        block.clear()

    for line in section_b.splitlines():
        unit_match = re.match(r"\s*Unità negoziale n\.\s*(\d+)", line)
        property_match = re.match(r"\s*Immobile n\.\s*(\d+)", line)
        if unit_match:
            flush()
            unit, number = int(unit_match.group(1)), None
        elif property_match:
            flush()
            number = int(property_match.group(1))
        elif number is not None:
            block.append(line)
    flush()
    return [{"numero": key, "immobili": value} for key, value in units.items()]


def _parse_subject(role_text: str, header: re.Match[str]) -> dict[str, Any]:
    entity = _find(r"Denominazione o ragione sociale\s+(.+?)\s*$", role_text, re.M)
    surname_match = re.search(r"Cognome\s+(.+?)\s{2,}Nome\s+(.+?)\s*$", role_text, re.M)
    birth = re.search(r"Nat[oa] il\s+(\d{2}/\d{2}/\d{4})\s+a\s+(.+?)\s*$", role_text, re.M)
    domicile = re.search(r"Domicilio ipotecario eletto\s+(.*?)\n\s*Relativamente", role_text, re.S)
    reference = re.search(r"unità negoziale n\.\s*(\d+)\s+Per il diritto di\s+(.+?)(?:\s{2,}|$)", role_text, re.M)
    share = re.search(r"Per la quota di\s+(\d+)\s*/\s*(\d+)", role_text)
    return {
        "numero_soggetto": int(header.group(1)),
        "qualita": slug(header.group(2)),
        "denominazione": entity,
        "cognome": clean(surname_match.group(1)) if surname_match else None,
        "nome": clean(surname_match.group(2)) if surname_match else None,
        "data_nascita": birth.group(1) if birth else None,
        "luogo_nascita": clean(birth.group(2)) if birth else None,
        "sesso": _find(r"Sesso\s+([MF])\b", role_text),
        "codice_fiscale": _fiscal_code(_find(r"Codice fiscale\s+([A-Z0-9 ]+?)(?:\s{3,}|\s*$)", role_text, re.M)),
        "sede": _find(r"^\s*Sede\s+(.+?)\s*$", role_text, re.M),
        "domicilio_ipotecario": re.sub(r"\s+", " ", domicile.group(1)).strip() if domicile else None,
        "unita_negoziale_riferimento": int(reference.group(1)) if reference else None,
        "tipo_diritto": slug(reference.group(2)) if reference else None,
        "quota_numeratore": int(share.group(1)) if share else None,
        "quota_denominatore": int(share.group(2)) if share else None,
    }


def _parse_parties(section_c: str) -> dict[str, list[dict[str, Any]]]:
    parties: dict[str, list[dict[str, Any]]] = {"a_favore": [], "contro": []}
    role: str | None = None
    chunk: list[str] = []
    header: re.Match[str] | None = None

    def flush() -> None:
        if role and header is not None:
            parties[role].append(_parse_subject("\n".join(chunk), header))
        chunk.clear()

    for line in section_c.splitlines():
        stripped = line.strip()
        subject = re.match(r"\s*Soggetto n\.\s*(\d+)\s+In qualità di\s*(.*)$", line)
        if stripped in ("A favore", "Contro"):
            flush()
            header, role = None, "a_favore" if stripped == "A favore" else "contro"
        elif stripped.startswith("Debitori non datori"):  # not parties of the title: they hold no right on the property
            flush()
            header, role = None, None
        elif subject:
            flush()
            header = subject
        elif header is not None:
            chunk.append(line)
    flush()
    return parties


def parse_note_text(raw_text: str) -> dict[str, Any] | None:
    """The text of an *Ispezione ipotecaria* note PDF as the Ocular ``ispezione``/``nota`` structure.

    Returns None when the text is not a mortgage note (no ``Nota di ...`` heading or no *Sezione A*).
    """
    first_page = raw_text.split("Pag. 2", 1)[0]
    kind = _find(r"Nota di (trascrizione|iscrizione|annotazione)", first_page, re.I)
    if not kind or kind.lower() not in _NOTE_KINDS or "Sezione A" not in raw_text:
        return None
    pages = [int(n) for n in re.findall(r"Pag\.\s*(\d+)", raw_text)]
    body = _PAGE_HEADER.sub("", raw_text)
    marks = list(_SECTION.finditer(body))
    sections = {m.group(1): body[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(body))] for i, m in enumerate(marks)}
    a, b, c, d = (sections.get(letter, "") for letter in "ABCD")

    date_time = re.search(r"Data\s+(\d{2}/\d{2}/\d{4})\s+Ora\s+(\d{2}:\d{2}:\d{2})", first_page)
    number = re.search(r"n\.\s*(T\d?\s*\d+)\s+del\s+(\d{2}/\d{2}/\d{4})", first_page)
    started = re.search(r"Inizio ispezione\s+(\d{2})/(\d{2})/(\d{4})\s+(\d{2}:\d{2}:\d{2})", first_page)
    presentation = re.search(r"Presentazione n\.\s*(\d+)\s+del\s+(\d{2}/\d{2}/\d{4})", first_page)
    title_block = a.split("Dati relativi alla convenzione")[0].split("Dati relativi all'ipoteca")[0]
    title_date = _find(r"^\s*Data\s+(\d{2}/\d{2}/\d{4})", title_block, re.M)
    official = re.search(r"^\s*(?:Notaio|Pubblico ufficiale|Autorità emittente)\s+(.+?)(?:\s{2,}Codice fiscale\s+(.+?))?\s*$", title_block, re.M)
    lien: dict[str, Any] = {}
    if "Dati relativi all'ipoteca" in a:
        lien_text = a.split("Dati relativi all'ipoteca", 1)[1].split("Altri dati")[0]
        derived = _find(r"Derivante da\s+(.+?)\s*$", lien_text, re.M)
        derived_code = re.match(r"(\d{3,4})\s+(.*)$", derived or "")
        rate = _find(r"Tasso interesse annuo\s+([\d.,]+)\s*%", lien_text)
        years = _find(r"Durata\s+(\d+)\s*anni", lien_text)
        lien = {
            "specie": slug(_find(r"Specie dell'ipoteca o del privilegio\s+(.+?)\s*$", lien_text, re.M)),
            "derivante_da": slug(derived_code.group(2) if derived_code else derived),
            "derivante_da_codice_ufficiale": derived_code.group(1) if derived_code else None,
            "derivante_da_descrizione": clean(derived_code.group(2) if derived_code else derived),
            "capitale_euro": _money_eur(_find(r"Capitale\s+(€\s*[\d.,]+)", lien_text)),
            "tasso_interesse_annuo_pct": rate.replace(",", ".") if rate else None,
            "interessi_euro": _money_eur(_find(r"Interessi\s+(€\s*[\d.,]+)", lien_text)),
            "spese_euro": _money_eur(_find(r"Spese\s+(€\s*[\d.,]+)", lien_text)),
            "totale_euro": _money_eur(_find(r"Totale\s+(€\s*[\d.,]+)", lien_text)),
            "durata_anni": int(years) if years else None,
            "durata_descrizione": f"{years} anni" if years else None,
        }
    description = _find(r"^\s*Descrizione\s+(.+?)\s*$", title_block, re.M)
    return {
        "ispezione": {
            "data": date_time.group(1) if date_time else None,
            "ora": date_time.group(2) if date_time else None,
            "numero": clean(number.group(1)) if number else None,
            "data_numero": number.group(2) if number else None,
            "inizio_ispezione": f"{started.group(3)}-{started.group(2)}-{started.group(1)}T{started.group(4)}" if started else None,
            "richiedente": _find(r"Richiedente\s+(\S+)", first_page),
            "tassa_versata_euro": _integer(_find(r"Tassa versata\s+€\s*(\d+)", first_page)),
            "pagine": max(pages) if pages else None,
            "ufficio": {
                "ufficio_provinciale": _find(r"Ufficio Provinciale di (.+?)\s+- Territorio", first_page),
                "servizio": _find(r"(Servizio di Pubblicità Immobiliare)", first_page),
            },
        },
        "nota": {
            "tipo_nota": kind.lower(),
            "utc_timestamp": _find(r"UTC:\s*(\S+)", first_page),
            "registro_generale": _find(r"Registro generale n\.\s*(\d+)", first_page),
            "registro_particolare": _find(r"Registro particolare n\.\s*(\d+)", first_page),
            "presentazione_numero": presentation.group(1) if presentation else None,
            "presentazione_data": presentation.group(2) if presentation else None,
            "sezione_a": {
                "altri_dati": _find(r"Altri dati\s*\n\s*(.+?)\s*$", a, re.M),
                "dati_titolo": {
                    "tipo_titolo": slug(description),
                    "descrizione": description,
                    "data": title_date,
                    "numero_repertorio": _find(r"Numero di repertorio\s+(\S+)", title_block),
                    "notaio": {
                        "nome": clean(official.group(1)) if official else None,
                        "codice_fiscale": _fiscal_code(official.group(2)) if official and official.group(2) else None,
                        "sede": _find(r"^\s*Sede\s+(.+?)\s*$", title_block, re.M),
                    },
                },
                "ipoteca": lien,
            },
            "sezione_b": {"unita_negoziali": _parse_properties(b)},
            "sezione_c": _parse_parties(c),
            "sezione_d": {"testo": clean(d)},
        },
    }


# -- typed rows ---------------------------------------------------------------------------------------------------------


async def project_note_data(session, extraction_id: int, data: dict[str, Any], *, is_mortgage_inspection: bool = True) -> None:
    """Replace the typed rows of ``extraction_id`` with those of ``data`` (Ocular ``ispezione``/``nota`` structure)."""
    from .database import _resolve_subject
    from .db_models import MortgageInspection, MortgageInspectionParty, MortgageInspectionProperty

    inspection_data = data.get("ispezione") or {}
    note = data.get("nota") or {}
    section_a = note.get("sezione_a") or {}
    title_data = section_a.get("dati_titolo") or {}
    notary = title_data.get("notaio") or {}
    lien_data = section_a.get("ipoteca") or {}
    office = inspection_data.get("ufficio") or {}
    section_b = note.get("sezione_b") or {}
    section_c = note.get("sezione_c") or {}
    section_d = note.get("sezione_d") or {}

    await session.execute(delete(MortgageInspectionProperty).where(MortgageInspectionProperty.inspection_id == extraction_id))
    await session.execute(delete(MortgageInspectionParty).where(MortgageInspectionParty.inspection_id == extraction_id))
    await session.execute(delete(MortgageInspection).where(MortgageInspection.extraction_id == extraction_id))

    if is_mortgage_inspection:
        for table, value in (
            ("mortgage_note_type", note.get("tipo_nota")),
            ("mortgage_title_type", title_data.get("tipo_titolo")),
            ("mortgage_lien_type", lien_data.get("specie")),
        ):
            await ensure_lookup(session, table, value)
        session.add(MortgageInspection(
            extraction_id=extraction_id,
            inspection_date=parse_it_date(inspection_data.get("data")),
            inspection_time=_time(inspection_data.get("ora")),
            inspection_number=_string(inspection_data.get("numero")),
            inspection_number_date=parse_it_date(inspection_data.get("data_numero")),
            inspection_started_at=_datetime(inspection_data.get("inizio_ispezione")),
            requester=_string(inspection_data.get("richiedente")),
            tax_paid_eur=_integer(inspection_data.get("tassa_versata_euro")),
            page_count=_integer(inspection_data.get("pagine")),
            office=_string(office.get("ufficio_provinciale")),
            service=_string(office.get("servizio")),
            note_type=clean(note.get("tipo_nota")),
            note_at=_datetime(note.get("utc_timestamp")),
            registro_generale=_integer(note.get("registro_generale")),
            registro_particolare=_integer(note.get("registro_particolare")),
            presentazione_numero=_integer(note.get("presentazione_numero")),
            presentazione_data=parse_it_date(note.get("presentazione_data")),
            section_a_other_data=_string(section_a.get("altri_dati")),
            section_d_text=_string(section_d.get("testo")),
            lien_type=clean(lien_data.get("specie")),
            lien_derived_from=_string(lien_data.get("derivante_da")),
            lien_derived_from_code=_string(lien_data.get("derivante_da_codice_ufficiale")),
            lien_derived_from_description=_string(lien_data.get("derivante_da_descrizione")),
            lien_principal_eur=_integer(lien_data.get("capitale_euro")),
            lien_annual_interest_rate_pct=_decimal(lien_data.get("tasso_interesse_annuo_pct")),
            lien_semiannual_interest_rate=_string(lien_data.get("tasso_interesse_semestrale")),
            lien_interest_eur=_integer(lien_data.get("interessi_euro")),
            lien_expenses_eur=_integer(lien_data.get("spese_euro")),
            lien_total_eur=_integer(lien_data.get("totale_euro")),
            lien_variable_amounts=lien_data.get("importi_variabili"),
            lien_foreign_currency=_string(lien_data.get("valuta_estera")),
            lien_automatic_increase=lien_data.get("somma_aumento_automatico"),
            lien_resolutive_condition=lien_data.get("condizione_risolutiva"),
            lien_duration_years=_integer(lien_data.get("durata_anni")),
            lien_duration_description=_string(lien_data.get("durata_descrizione")),
            lien_mortgage_rank=_integer(lien_data.get("grado_ipoteca")),
            title_type=clean(title_data.get("tipo_titolo")),
            title_description=_string(title_data.get("descrizione")),
            title_date=parse_it_date(title_data.get("data")),
            title_repertory_number=_string(title_data.get("numero_repertorio")),
            title_notary_name=_string(notary.get("nome")),
            title_notary_fiscal_code=_string(notary.get("codice_fiscale")),
            title_notary_location=_string(notary.get("sede")),
        ))
        await session.flush()

    for unit_data in section_b.get("unita_negoziali") or []:
        unit_number = _integer(unit_data.get("numero"))
        for property_data in unit_data.get("immobili") or []:
            municipality = _string(property_data.get("comune_denominazione"))
            province = None
            match = _PLACE.match(municipality or "")
            if match:  # 'RAVENNA (RA)' → name + sigla
                municipality, province = match.group(1), match.group(2)
            catasto = (_string(property_data.get("tipo_catasto")) or "").strip().lower()
            unit_id = await get_or_create_cadastral_unit(
                session,
                cadastre_type={"fabbricati": "F", "terreni": "T"}.get(catasto),
                province=province,
                municipality=municipality,
                municipality_code=_string(property_data.get("comune_codice")),
                section=_string(property_data.get("sezione_urbana")),
                sheet=_string(property_data.get("foglio")),
                parcel=_string(property_data.get("particella")),
                subunit=_string(property_data.get("subalterno")),
            )
            vani = _decimal(property_data.get("consistenza_vani"))
            session.add(MortgageInspectionProperty(
                inspection_id=extraction_id,
                unit_number=unit_number,
                property_number=_integer(property_data.get("numero_immobile")),
                cadastral_unit_id=unit_id,
                nature=_string(property_data.get("natura")),
                nature_description=_string(property_data.get("natura_descrizione")),
                # the column is an integer: a half room (8,5 vani) is not stored rather than rounded
                room_count=int(vani) if vani is not None and vani == vani.to_integral_value() else None,
                area_sqm=_decimal(property_data.get("consistenza_mq")),
                floor=_string(property_data.get("piano")),
                address=_string(property_data.get("indirizzo")),
                civic_number=_string(property_data.get("numero_civico")),
            ))

    for role in ("a_favore", "contro"):
        for ordinal, party_data in enumerate(section_c.get(role) or []):
            place = _PLACE.match(_string(party_data.get("luogo_nascita")) or "")
            entity = clean(party_data.get("denominazione"))
            surname, given = clean(party_data.get("cognome")), clean(party_data.get("nome"))
            subject_id = await _resolve_subject(session, {
                "fiscal_code": _string(party_data.get("codice_fiscale")),
                "display_name": entity or " ".join(part for part in (surname, given) if part) or None,
                "last_name": None if entity else surname,
                "first_name": None if entity else given,
                "gender": _string(party_data.get("sesso")),
                "date_of_birth": parse_it_date(party_data.get("data_nascita")),
                "birth_municipality": place.group(1) if place else _string(party_data.get("luogo_nascita")),
                "birth_province": place.group(2) if place else None,
                "subject_type": "legal_entity" if entity else "person",
            })
            if subject_id is None:
                continue
            quality = await ensure_lookup(session, "mortgage_party_quality", party_data.get("qualita"))
            session.add(MortgageInspectionParty(
                inspection_id=extraction_id,
                role=role,
                ordinal=ordinal,
                subject_id=subject_id,
                subject_number=_integer(party_data.get("numero_soggetto")),
                quality=quality,
                registered_office=_string(party_data.get("sede")),
                mortgage_domicile=_string(party_data.get("domicilio_ipotecario")),
                negotiation_unit_reference=_integer(party_data.get("unita_negoziale_riferimento")),
                right_type=_string(party_data.get("tipo_diritto")),
                share_numerator=_integer(party_data.get("quota_numeratore")),
                share_denominator=_integer(party_data.get("quota_denominatore")),
            ))


async def backfill_mortgage_notes(
    *, force: bool = False, limit: int | None = None, extraction_ids: list[int] | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Project stored ``ispezione_ipotecaria`` text payloads that have no ``mortgage_inspection`` row yet (``force`` = all)."""
    from .database import _get_session_factory
    from .db_models import DocumentExtractionPayload, MortgageInspection

    stats = {"candidates": 0, "projected": 0, "not_a_note": 0, "failed": 0, "parties": 0, "properties": 0}
    session_factory = _get_session_factory()
    async with session_factory() as session:
        stmt = select(DocumentExtractionPayload.extraction_id, DocumentExtractionPayload.structured_data).order_by(
            DocumentExtractionPayload.extraction_id
        )
        if extraction_ids:
            stmt = stmt.where(DocumentExtractionPayload.extraction_id.in_(extraction_ids))
        done = set((await session.execute(select(MortgageInspection.extraction_id))).scalars())
        rows = [
            (extraction_id, payload)
            for extraction_id, payload in (await session.execute(stmt)).all()
            if isinstance(payload, dict) and payload.get("raw_text") and "ispezione" not in payload
            and (force or extraction_id not in done)
        ]
    if limit:
        rows = rows[: int(limit)]
    stats["candidates"] = len(rows)
    for extraction_id, payload in rows:
        parsed = parse_note_text(str(payload["raw_text"]))
        if parsed is None:
            stats["not_a_note"] += 1
            continue
        if dry_run:
            stats["projected"] += 1
            continue
        try:
            async with session_factory() as session:
                await project_note_data(session, extraction_id, parsed)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - one malformed note must not stop the others
            logger.warning("Mortgage note %s not projected: %s", extraction_id, exc)
            stats["failed"] += 1
            continue
        stats["projected"] += 1
        note = parsed["nota"]
        stats["parties"] += sum(len(note["sezione_c"][role]) for role in ("a_favore", "contro"))
        stats["properties"] += sum(len(unit["immobili"]) for unit in note["sezione_b"]["unita_negoziali"])
    return stats
