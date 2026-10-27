"""Visura XML documents → the typed document tables.

``_persist_flattened_xml`` keeps every element in the archive (``sister_archive.document_xml_node``); this module reads
the same XML into the relational tables designed for it (``visura_xml_models``): the header on ``document``, the queried
party in ``document_subject``, the properties (``building_unit``, ``land_parcel`` + classifications, ``property_group``)
and the ownership acts with their owners (``ownership_mutation`` / ``mutation_owner``).

Five document shapes come out of SISTER (root child of ``<Visura>``):

=========================  ====================================================================================
``VisuraFabbricatiAttuale``  one building unit + its current owners
``VisuraFabbricatiStorica``  current state, address history, current + historical owners
``VisuraTerreniAttuale``     one land parcel (several classification rows) + owners
``VisuraTerreniStorica``     same, plus the history of the parcel
``VisuraSoggettoAttuale``    a subject and its groups of properties, each group with its owners
=========================  ====================================================================================

``parse_visura_xml`` is pure (string → dict); ``ingest_visura_xml`` writes inside the caller's session.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree

from .database import document_file_path
from .result_parsers import normalize_owner, squeeze

logger = logging.getLogger("sister")

KINDS = {
    "VisuraFabbricatiAttuale": "fabbricati_attuale",
    "VisuraFabbricatiStorica": "fabbricati_storica",
    "VisuraTerreniAttuale": "terreni_attuale",
    "VisuraTerreniStorica": "terreni_storica",
    "VisuraTerrenoAttuale": "terreni_attuale",
    "VisuraTerrenoStorica": "terreni_storica",
    "VisuraSoggettoAttuale": "soggetto_attuale",
}

# ---------------------------------------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------------------------------------


def _attrs(element) -> dict[str, str]:
    if element is None:
        return {}
    return {key: value for key, value in element.attrib.items() if not key.startswith("{")}


def _text(element) -> str:
    return squeeze(element.text) if element is not None else ""


def _identifier(element) -> dict[str, str]:
    a = _attrs(element)
    return {
        "province": a.get("Provincia", ""),
        "municipality": a.get("Comune", ""),
        "sheet": a.get("Foglio", ""),
        "parcel": a.get("ParticellaNum") or a.get("Particella", ""),
        "subunit": a.get("Subalterno", ""),
        "section": a.get("SezUrbana") or a.get("SezCensuaria") or a.get("Sezione", ""),
        "municipality_code": a.get("CodiceComune", ""),
        "sequence_id": a.get("ProgrId", ""),
        "status": a.get("StatoImmobile", ""),
        "partita": _text(element.find("Partita")) if element is not None else "",
    }


def _owner(element) -> dict[str, Any]:
    out: dict[str, Any] = {"IndiceIntestato": element.get("IndiceIntestato", ""), "Nominativo": _text(element.find("Nominativo")),
                           "CF": _text(element.find("CF"))}
    diritti = element.find("DirittiReali")
    if diritti is not None:
        out["DirittiReali"] = _attrs(diritti)
    return out


def _mutation(element, index: str | None = None) -> dict[str, Any]:
    a = _attrs(element)
    return {
        "index": index if index is not None else a.get("IndiceMutazione", ""),
        "date": a.get("DataMutazione", ""),
        "source": _text(element.find("DatiDerivantiDaMutazSogg")),
        "reference": _identifier(element.find("IdentificativoDefinitivoRiferimento")),
        "owners": [_owner(o) for o in element.iter("Intestato")],
    }


def _building(element) -> dict[str, Any]:
    """A building unit from ImmobileFabbricati / SituazioneAttualeFabbricati / ImmobileFabbricatiS."""
    classification = element.find("DatiClassamentoF")
    consistency = classification.find("Consistenza") if classification is not None else None
    surface = element.find("SuperficieF")
    address = element.find("DatiIndirizzo/IndirizzoImm")
    if address is None:
        address = element.find("IndirizzoImm")
    return {
        "kind": "building",
        "index": element.get("IndiceImmobile", ""),
        "identifier": _identifier(element.find("DatiIdentificativi/IdentificativoDefinitivo")),
        "classification": {**_attrs(classification), "Consistenza": _attrs(consistency)} if classification is not None else {},
        "surface": {**_attrs(surface), "Planimetria": _attrs(surface.find("Planimetria"))} if surface is not None else {},
        "address": _text(address),
        "related": [_attrs(r) for r in element.findall("MappaliCorrelati/IdentificativoCorrelato")],
    }


def _land(element) -> dict[str, Any]:
    """A land parcel from ImmobileTerreni / SituazioneAttualeTerreni / ImmobileTerreniS."""
    return {
        "kind": "land",
        "index": element.get("IndiceImmobile", ""),
        "identifier": _identifier(element.find("DatiIdentificativi/IdentificativoDefinitivo")),
        "classifications": [_attrs(c) for c in element.findall("DatiClassamentoT/ClassamentoT")],
        "related": [_attrs(r) for r in element.findall("MappaliCorrelati/IdentificativoCorrelato")],
    }


def _subject(request) -> dict[str, Any] | None:
    if request is None:
        return None
    individual = request.find("SoggettoIndividuato")
    if individual is None:
        return None
    pf, pnf = individual.find("SoggettoPF"), individual.find("SoggettoPNF")
    if pf is not None:
        a = _attrs(pf)
        return {"type": "person", **a}
    if pnf is not None:
        return {"type": "legal_entity", **_attrs(pnf)}
    return None


def parse_visura_xml(content: str | bytes) -> dict[str, Any] | None:
    """Read a visura XML into plain dicts (None when it is not one of the known document shapes)."""
    raw = content.encode("utf-8", "replace") if isinstance(content, str) else content
    try:
        root = etree.fromstring(
            raw.replace(b"\x00", b""), etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        )
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None
    body = next((child for child in root if isinstance(child.tag, str) and child.tag in KINDS), None)
    if body is None:
        return None
    kind = KINDS[body.tag]
    request = body.find("DatiRichiesta")
    parsed: dict[str, Any] = {
        "kind": kind,
        "header": _attrs(body.find("TitoloVisura")),
        "request": _attrs(request),
        "liquidation": _attrs(body.find("DatiLiquidazione")),
        "requester": _attrs(body.find("Richiedente")).get("Descrizione", ""),
        "subject": _subject(request),
        "groups": [],
        "units": [],
        "mutations": [],
        "history_addresses": [],
    }

    if kind == "soggetto_attuale":
        for group in body.findall("GruppoUnitaImmobiliari"):
            units = [_building(u) for u in group.findall("ImmobileFabbricatiS")]
            units += [_land(u) for u in group.findall("ImmobileTerreniS")]
            parsed["groups"].append(
                {
                    "attrs": _attrs(group),
                    "units": units,
                    "mutations": [_mutation(m) for m in group.findall("IntestazioneGruppo/MutazioneSoggettiva")],
                }
            )
        return parsed

    if kind.startswith("fabbricati"):
        state = body.find("SituazioneAttualeFabbricati")
        if state is None:
            state = body.find("ImmobileFabbricati")
        if state is not None:
            parsed["units"].append(_building(state))
            current = state.find("IntestazioneAttuale")
            if current is not None:
                parsed["mutations"].append(_mutation(current, index="current"))
            parsed["mutations"] += [_mutation(m) for m in state.findall("MutazioneSoggettiva")]
        for address in body.findall("StoriaImmobileFabbricati/DatiIndirizzo"):
            parsed["history_addresses"].append(
                {"address": _text(address.find("IndirizzoImm")), "situation": _text(address.find("Situazione"))}
            )
    else:
        state = body.find("SituazioneAttualeTerreni")
        if state is None:
            state = body.find("ImmobileTerreni")
        if state is not None:
            land = _land(state)
            current = state.find("IntestazioneAttuale")
            land["mutations"] = ([_mutation(current, index="current")] if current is not None else []) + [
                _mutation(m) for m in state.findall("MutazioneSoggettiva")
            ]
            parsed["units"].append(land)
    history = [_mutation(m) for m in body.findall("StoriaIntestazione/MutazioneSoggettiva")]
    if kind.startswith("terreni") and parsed["units"]:
        parsed["units"][0].setdefault("mutations", []).extend(history)
    else:
        parsed["mutations"] += history
    return parsed


# ---------------------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------------------

_PLANIMETRIA_DATE = re.compile(r"presentazione\s+(\d{2}/\d{2}/\d{4})")
_PLANIMETRIA_PROTOCOL = re.compile(r"prot\.\s*n\.\s*(\S+)")
_MUTATION_STATUS = {"attuale", "predecessore"}
_CONSISTENCY_UNITS = {"VANI", "VANO", "MQ", "MC"}


def _decimal(value: Any) -> Decimal | None:
    text = squeeze(value).replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _lire(value: Any) -> Decimal | None:
    """Legacy lire amounts are printed with thousands points: ``1.200`` → 1200."""
    digits = squeeze(value).replace(".", "")
    return Decimal(digits) if digits.isdigit() else None


def _int(value: Any) -> int | None:
    text = squeeze(value)
    return int(text) if text.isdigit() else None


async def _unit(session, identifier: dict[str, str], cadastre_type: str) -> int:
    """The cadastral unit named by an ``IdentificativoDefinitivo`` (the Belfiore code identifies the comune)."""
    from .reference import get_or_create_cadastral_unit

    return await get_or_create_cadastral_unit(
        session,
        cadastre_type=cadastre_type,
        province=identifier["province"],
        municipality=identifier["municipality"],
        municipality_code=identifier["municipality_code"],
        sheet=identifier["sheet"],
        parcel=identifier["parcel"],
        subunit=identifier["subunit"],
        section=identifier["section"],
    )


async def _subject_id(session, fields: dict[str, Any]) -> int | None:
    from .database import _resolve_subject

    return await _resolve_subject(session, fields) if fields else None


async def _clear(session, document_id: int) -> None:
    """Remove what an earlier ingestion of this document wrote (re-ingesting is idempotent per document)."""
    from sqlalchemy import text

    params = {"d": document_id}
    for statement in (
        "DELETE FROM mutation_owner WHERE mutation_id IN (SELECT id FROM ownership_mutation WHERE document_id = :d)",
        "DELETE FROM ownership_mutation WHERE document_id = :d",
        "DELETE FROM land_parcel_classification WHERE land_parcel_id IN (SELECT id FROM land_parcel WHERE document_id = :d)",
        "DELETE FROM land_parcel WHERE document_id = :d",
        "DELETE FROM building_unit_related_parcel WHERE building_unit_id IN (SELECT id FROM building_unit WHERE document_id = :d)",
        "DELETE FROM building_unit WHERE document_id = :d",
        "DELETE FROM property_group WHERE document_id = :d",
        "DELETE FROM document_address WHERE document_id = :d",
        "DELETE FROM document_subject WHERE document_id = :d",
    ):
        await session.execute(text(statement), params)


async def _write_mutations(
    session,
    document_id: int,
    mutations: list[dict],
    parent: dict[str, int],
    cadastre_type: str,
    as_of: str = "",
) -> int:
    """The ownership acts of one place in a document and their owners.

    ``parent`` is ``{"property_group_id": …}`` or ``{"land_parcel_id": …}`` (empty for the document itself); the
    document is always set. ``IntestazioneAttuale`` (index ``current``) becomes ``is_current`` with no index.
    """
    from .reference import get_or_create_right
    from .result_parsers import parse_it_date
    from .visura_xml_models import MutationOwner, OwnershipMutation

    owners = 0
    for mutation in mutations:
        ref = mutation["reference"]
        is_current = mutation["index"] == "current"
        status = (ref["status"] or "").strip().lower()
        row = OwnershipMutation(
            document_id=document_id,
            **parent,
            mutation_index=None if is_current else _int(mutation["index"]),
            is_current=is_current,
            mutation_date=parse_it_date(mutation["date"]),
            source_description=mutation["source"] or None,
            status=status if status in _MUTATION_STATUS else None,
            reference_cadastral_unit_id=await _unit(session, ref, cadastre_type)
            if (ref.get("sheet") or ref.get("parcel") or ref.get("municipality_code"))
            else None,
        )
        session.add(row)
        await session.flush()
        seen: set[tuple] = set()
        for owner in mutation["owners"]:
            subject_fields, right_fields = normalize_owner(owner, as_of)
            if right_fields.get("right_type") and not right_fields.get("right_description"):
                right_fields["right_description"] = right_fields["right_type"]
            subject_id = await _subject_id(session, subject_fields)
            if subject_id is None:  # an Intestato with neither name nor fiscal code identifies nobody
                continue
            right_id = await get_or_create_right(session, **right_fields) if right_fields else None
            if (subject_id, right_id) in seen:
                continue
            seen.add((subject_id, right_id))
            session.add(MutationOwner(mutation_id=row.id, position=_int(owner.get("IndiceIntestato")), subject_id=subject_id, right_id=right_id))
            owners += 1
    return owners


async def _write_building(
    session,
    document_id: int,
    unit: dict,
    cadastre_type: str,
    *,
    group_id: int | None = None,
    is_current: bool = False,
    address: str | None = None,
) -> int:
    """One building unit: identifier, classification and surface are columns of the row, related parcels its children.

    ``group_id`` is set for a unit inside a subject's group; ``is_current`` marks the current state of a single-property
    document.
    """
    from .reference import clean, ensure_lookup
    from .result_parsers import normalize_category, normalize_class, parse_it_date
    from .visura_xml_models import BuildingUnit, BuildingUnitRelatedParcel

    ident = unit["identifier"]
    c = unit["classification"] or {}
    cons = c.get("Consistenza", {})
    category, description = normalize_category(c.get("Categoria"))
    if category:
        await ensure_lookup(session, "cadastral_category", category, description)
    surface = unit["surface"] or {}
    planimetria = surface.get("Planimetria", {}).get("Descrizione") or ""
    filed = _PLANIMETRIA_DATE.search(planimetria)
    protocol = _PLANIMETRIA_PROTOCOL.search(planimetria)
    unit_of_measure = (cons.get("Unita") or "").strip().upper()
    consistency_value = _decimal(cons.get("Valore"))
    consistency_unit = unit_of_measure if unit_of_measure in _CONSISTENCY_UNITS else None
    row = BuildingUnit(
        document_id=document_id,
        property_group_id=group_id,
        unit_index=_int(unit["index"]),
        is_current=is_current,
        address_text=address,
        cadastral_unit_id=await _unit(session, ident, cadastre_type),
        sequence_id=ident["sequence_id"] or None,
        census_zone=clean(c.get("ZonaCensuaria")),
        category=category,
        cadastral_class=normalize_class(c.get("Classe")),
        cadastral_income_eur=_decimal(c.get("RenditaEuro")),
        consistency_value=consistency_value,
        consistency_unit=consistency_unit,
        consistency_sqm=consistency_value if consistency_unit == "MQ" else None,
        consistency_rooms=consistency_value if consistency_unit in {"VANI", "VANO"} else None,
        total_area_sqm=_decimal(surface.get("Totale")),
        excluded_area_sqm=_decimal(surface.get("TotaleE")),
        planimetria_filed_on=parse_it_date(filed.group(1)) if filed else None,
        planimetria_protocol=protocol.group(1) if protocol else None,
    )
    session.add(row)
    await session.flush()
    seen: set[tuple] = set()
    for related in unit["related"]:
        rel_ident = {
            "province": ident["province"], "municipality": ident["municipality"], "municipality_code": ident["municipality_code"],
            "subunit": "", "sheet": related.get("Foglio", ""), "parcel": related.get("ParticellaNum", ""),
            "section": related.get("SezCensuaria", ""),
        }
        parcel_id = await _unit(session, rel_ident, "T")
        key = (parcel_id, related.get("ProgrId") or None)
        if key in seen:
            continue
        seen.add(key)
        session.add(BuildingUnitRelatedParcel(building_unit_id=row.id, cadastral_unit_id=parcel_id, sequence_id=key[1]))
    return row.id


async def _write_land(session, document_id: int, unit: dict, cadastre_type: str) -> int:
    from .reference import clean, ensure_lookup
    from .result_parsers import normalize_class
    from .visura_xml_models import LandParcel, LandParcelClassification

    ident = unit["identifier"]
    parcel = LandParcel(
        document_id=document_id,
        cadastral_unit_id=await _unit(session, ident, cadastre_type),
        sequence_id=ident["sequence_id"] or None,
        partita=ident["partita"] or None,
    )
    session.add(parcel)
    await session.flush()
    for c in unit["classifications"]:
        quality = (clean(c.get("Qualita")) or "").upper() or None
        if quality:
            await ensure_lookup(session, "land_quality", quality)
        session.add(
            LandParcelClassification(
                land_parcel_id=parcel.id,
                land_quality=quality,
                cadastral_class=normalize_class(c.get("Classe")),
                area_sqm=_decimal(c.get("SuperficieMQ")),
                deduction_symbol=clean(c.get("SimboloDeduzione")),
                dominical_income_eur=_decimal(c.get("RedditoDominicaleEuro")),
                agricultural_income_eur=_decimal(c.get("RedditoAgrarioEuro")),
                dominical_income_lire=_lire(c.get("RedditoDominicaleLire")),
                agricultural_income_lire=_lire(c.get("RedditoAgrarioLire")),
            )
        )
    return parcel.id


def _subject_fields(subject: dict[str, Any]) -> dict[str, Any]:
    """The queried party of a Visura per Soggetto → ``get_or_create_subject`` fields."""
    identifier = squeeze(subject.get("CodiceFiscale")).upper()
    if subject["type"] == "legal_entity":
        return {
            "fiscal_code": identifier or None,
            "display_name": squeeze(subject.get("Denominazione")) or None,
            "subject_type": "legal_entity",
        }
    raw = {
        "codice_fiscale": identifier,
        "Cognome": subject.get("Cognome"),
        "Nome": subject.get("Nome"),
        "sesso": subject.get("Sesso"),
        "DataNascita": subject.get("DataNascita"),
        "luogo_nascita": f"{squeeze(subject.get('ComuneNascita'))} ({squeeze(subject.get('Provincia'))})"
        if subject.get("ComuneNascita") and subject.get("Provincia")
        else subject.get("ComuneNascita"),
    }
    fields, _ = normalize_owner(raw)
    if subject.get("CodiceComune"):
        fields["birth_municipality_code"] = squeeze(subject["CodiceComune"])
    return fields


def _as_of(parsed: dict[str, Any]) -> str:
    """The visura's reference date (``SituazioneAl``, YYYYMMDD) as DD/MM/YYYY."""
    value = squeeze(parsed.get("header", {}).get("SituazioneAl", ""))
    return f"{value[6:8]}/{value[4:6]}/{value[0:4]}" if len(value) == 8 and value.isdigit() else ""


def _generated_at(date_text: str, time_text: str):
    """``Data`` (YYYYMMDD) + ``Ora`` (HHMMSS) of the header in Italian local time; None when the date is absent."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not (date_text.isdigit() and len(date_text) == 8):
        return None
    clock = time_text if time_text.isdigit() and len(time_text) == 6 else "000000"
    try:
        return datetime(
            int(date_text[:4]), int(date_text[4:6]), int(date_text[6:]), int(clock[:2]), int(clock[2:4]), int(clock[4:]),
            tzinfo=ZoneInfo("Europe/Rome"),
        )
    except ValueError:
        return None


async def ingest_visura_xml(session, document_id: int, content: str | bytes | None) -> dict[str, int]:
    """Fill the typed tables from one document's XML; returns row counts (empty when the XML is not a visura).

    Runs inside the caller's transaction and replaces whatever an earlier ingestion of the same document wrote.
    """
    from .db_models import Document
    from .reference import get_or_create_comune, get_or_create_office
    from .visura_xml_models import DocumentAddress, DocumentSubject, PropertyGroup

    parsed = parse_visura_xml(content) if content else None
    if parsed is None:
        return {}
    await _clear(session, document_id)
    counts = {"units": 0, "parcels": 0, "mutations": 0, "owners": 0}
    kind = parsed["kind"]
    cadastre_type = "T" if kind.startswith("terreni") else "F"

    # --- header -----------------------------------------------------------------------------------------
    document = await session.get(Document, document_id)
    if document is not None:
        header, request, liquidation = parsed["header"], parsed["request"], parsed["liquidation"]
        if kind.startswith("soggetto"):
            from .document_scope import subject_search_scope

            scope = subject_search_scope(document.file_name, request_province=request.get("Provincia"))
            document.subject_search_scope = (
                scope["search_scope"] if scope["search_scope"] != "unknown" else None
            )
            document.subject_search_province = scope["search_province"] or None
        document.title = header.get("Titolo") or document.title
        document.registry_view_type = header.get("TipoVisura") or document.registry_view_type
        document.service_type = header.get("TipoServizio") or document.service_type
        document.generated_at = _generated_at(header.get("Data", ""), header.get("Ora", "")) or document.generated_at
        document.land_registry_office_id = await get_or_create_office(session, header.get("Provenienza")) or document.land_registry_office_id
        document.protocol = request.get("Protocollo") or document.protocol
        document.protocol_year = _int(request.get("Anno")) or document.protocol_year
        document.liquidation_protocol = liquidation.get("Protocollo") or document.liquidation_protocol
        document.liquidation_year = _int(liquidation.get("Anno")) or document.liquidation_year
        document.liquidation_units = _int(liquidation.get("UnitaImmobiliari")) if liquidation.get("UnitaImmobiliari") else document.liquidation_units
        document.requester = parsed["requester"] or document.requester
        session.add(document)

    # --- queried party ----------------------------------------------------------------------------------
    if parsed["subject"]:
        subject_id = await _subject_id(session, _subject_fields(parsed["subject"]))
        if subject_id:
            session.add(DocumentSubject(document_id=document_id, subject_id=subject_id))
            await session.flush()

    # --- properties and owners --------------------------------------------------------------------------
    as_of = _as_of(parsed)
    if kind == "soggetto_attuale":
        for group in parsed["groups"]:
            a = group["attrs"]
            comune_id = await get_or_create_comune(session, None, None, a.get("CodiceComune"))
            row = PropertyGroup(
                document_id=document_id,
                group_index=_int(a.get("IndiceGruppo")),
                cadastre_type=(a.get("TipoCatasto") or "")[:1].upper() or None,
                comune_id=comune_id,
                description=a.get("Descrizione") or None,
            )
            session.add(row)
            await session.flush()
            for unit in group["units"]:
                if unit["kind"] == "building":
                    await _write_building(session, document_id, unit, "F", group_id=row.id)
                    counts["units"] += 1
                else:
                    await _write_land(session, document_id, unit, "T")
                    counts["parcels"] += 1
            counts["mutations"] += len(group["mutations"])
            counts["owners"] += await _write_mutations(
                session, document_id, group["mutations"], {"property_group_id": row.id},
                group["attrs"].get("TipoCatasto") or "F", as_of,
            )
    else:
        for unit in parsed["units"]:
            if unit["kind"] == "building":
                await _write_building(session, document_id, unit, cadastre_type, is_current=True, address=unit["address"] or None)
                counts["units"] += 1
            else:
                parcel_id = await _write_land(session, document_id, unit, cadastre_type)
                counts["parcels"] += 1
                mutations = unit.get("mutations", [])
                counts["mutations"] += len(mutations)
                counts["owners"] += await _write_mutations(session, document_id, mutations, {"land_parcel_id": parcel_id}, "T", as_of)
        for entry in parsed["history_addresses"]:
            if entry["address"]:
                session.add(DocumentAddress(document_id=document_id, address_text=entry["address"], situation=entry["situation"] or None))
        counts["mutations"] += len(parsed["mutations"])
        counts["owners"] += await _write_mutations(session, document_id, parsed["mutations"], {}, cadastre_type, as_of)
    await session.flush()
    return counts


# ---------------------------------------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------------------------------------

_LEGACY_CUT = 50_000  # documents saved before XML_CONTENT_LIMIT was raised were cut at this length


def _complete_content(content: str, file_path: str | None) -> str:
    """The stored XML, or the whole file when the stored copy is one of the old 50 kB cuts."""
    if len(content) < _LEGACY_CUT or not file_path:
        return content
    from pathlib import Path

    from .utils import _p7m_payload

    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix not in (".xml", ".p7m") or not path.is_file():
        return content
    try:
        if suffix == ".p7m":
            # the signed content is read in memory; no extracted .xml is kept next to the .p7m
            payload = _p7m_payload(str(path))
            return payload.decode("utf-8", errors="ignore") if payload else content
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return content


async def backfill_documents(force: bool = False, limit: int | None = None, batch: int = 50) -> dict[str, int]:
    """Fill the typed tables for documents saved before the typed reader existed (from the XML kept in the archive).

    Skips documents that already have typed rows unless ``force``. Safe to re-run.
    """
    from sqlalchemy import text

    from .database import DATABASE_ARCHIVE_SCHEMA, _get_session_factory
    from .db_models import Document, DocumentContent

    session_factory = _get_session_factory()
    stats = {"documents": 0, "not_visura": 0, "failed": 0, "units": 0, "parcels": 0, "owners": 0}
    archive = f'"{DATABASE_ARCHIVE_SCHEMA}"'
    # map archives (``mappa_*.zip.p7m``) keep zip bytes as their content: not XML, nothing to read
    query = (
        f"SELECT c.document_id FROM {archive}.document_content c "  # noqa: S608 (quoted schema name)
        "WHERE c.content <> '' AND ltrim(c.content) LIKE '<%'"
    )
    if not force:
        query += (
            " AND NOT EXISTS (SELECT 1 FROM building_unit x WHERE x.document_id = c.document_id)"
            " AND NOT EXISTS (SELECT 1 FROM document_subject x WHERE x.document_id = c.document_id)"
            " AND NOT EXISTS (SELECT 1 FROM property_group x WHERE x.document_id = c.document_id)"
            " AND NOT EXISTS (SELECT 1 FROM land_parcel x WHERE x.document_id = c.document_id)"
        )
    query += " ORDER BY c.document_id" + (f" LIMIT {int(limit)}" if limit else "")
    async with session_factory() as session:
        ids = (await session.execute(text(query))).scalars().all()

    for start in range(0, len(ids), batch):
        async with session_factory() as session:
            for document_id in ids[start : start + batch]:
                stored = await session.get(DocumentContent, document_id)
                document = await session.get(Document, document_id)
                content = _complete_content(stored.content or "", document_file_path(document.file_name) if document else None)
                if content != stored.content:
                    stored.content = content
                    session.add(stored)
                try:
                    async with session.begin_nested():
                        counts = await ingest_visura_xml(session, document_id, content)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Backfill documento %s non riuscito: %s", document_id, exc)
                    stats["failed"] += 1
                    continue
                if not counts:
                    stats["not_visura"] += 1
                    continue
                stats["documents"] += 1
                for key in ("units", "parcels", "owners"):
                    stats[key] += counts[key]
            await session.commit()
    return stats
