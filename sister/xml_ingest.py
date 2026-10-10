"""Visura XML documents → the typed document tables.

``_persist_flattened_xml`` keeps every element in ``document_xml_nodes``; this module reads the same XML into the
relational tables designed for it (``visura_xml_models``): the header in ``document_metadata``, the queried party in
``document_subjects``, the properties (``building_*``, ``land_*``, ``property_groups``/``building_units``) and the
ownership acts with their owners (``ownership_mutations`` / ``property_owners``).

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
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree

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

# the typed tables a document fills, in delete order (children first)
_TYPED_TABLES = (
    "property_owners",
    "ownership_mutations",
    "land_classifications",
    "land_parcels",
    "related_parcels",
    "building_surfaces",
    "building_classifications",
    "building_identifiers",
    "building_units",
    "property_groups",
    "building_addresses",
    "building_current_states",
    "document_subjects",
)


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


def _decimal(value: Any) -> Decimal | None:
    text = squeeze(value).replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


async def _location(session, identifier: dict[str, str], cadastre_type: str) -> int | None:
    from .database import get_or_create_location

    if not (identifier.get("sheet") or identifier.get("parcel")):
        return None
    return await get_or_create_location(
        session,
        cadastre_type=cadastre_type,
        province=identifier["province"],
        municipality=identifier["municipality"],
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
    scopes = {
        "property_owners": "mutation_id IN (SELECT id FROM ownership_mutations WHERE document_id = :d"
        " OR property_group_id IN (SELECT id FROM property_groups WHERE document_id = :d)"
        " OR land_parcel_id IN (SELECT id FROM land_parcels WHERE document_id = :d))",
        "ownership_mutations": "document_id = :d OR property_group_id IN (SELECT id FROM property_groups"
        " WHERE document_id = :d) OR land_parcel_id IN (SELECT id FROM land_parcels WHERE document_id = :d)",
        "land_classifications": "parcel_id IN (SELECT id FROM land_parcels WHERE document_id = :d)",
        "land_parcels": "document_id = :d",
        "related_parcels": "current_state_id = :d OR building_unit_id IN (SELECT u.id FROM building_units u"
        " JOIN property_groups g ON g.id = u.group_id WHERE g.document_id = :d)",
        "building_surfaces": "current_state_id = :d OR building_unit_id IN (SELECT u.id FROM building_units u"
        " JOIN property_groups g ON g.id = u.group_id WHERE g.document_id = :d)",
        "building_classifications": "current_state_id = :d OR history_document_id = :d OR building_unit_id IN"
        " (SELECT u.id FROM building_units u JOIN property_groups g ON g.id = u.group_id WHERE g.document_id = :d)",
        "building_identifiers": "current_state_id = :d OR history_document_id = :d OR building_unit_id IN"
        " (SELECT u.id FROM building_units u JOIN property_groups g ON g.id = u.group_id WHERE g.document_id = :d)",
        "building_units": "group_id IN (SELECT id FROM property_groups WHERE document_id = :d)",
        "property_groups": "document_id = :d",
        "building_addresses": "document_id = :d",
        "building_current_states": "id = :d",
        "document_subjects": "id = :d",
    }
    for table in _TYPED_TABLES:
        await session.execute(text(f"DELETE FROM {table} WHERE {scopes[table]}"), params)  # noqa: S608 (fixed names)


async def _write_mutations(
    session, mutations: list[dict], parent: dict[str, int], cadastre_type: str, as_of: str = ""
) -> int:
    from .database import get_or_create_right
    from .visura_xml_models import OwnershipMutation, PropertyOwner

    owners = 0
    for mutation in mutations:
        ref = mutation["reference"]
        row = OwnershipMutation(
            **parent,
            mutation_index=mutation["index"] or None,
            mutation_date=mutation["date"] or None,
            source_description=mutation["source"] or None,
            reference_location_id=await _location(session, ref, cadastre_type),
            ref_municipality_code=ref["municipality_code"] or None,
            ref_property_status=ref["status"] or None,
        )
        session.add(row)
        await session.flush()
        for owner in mutation["owners"]:
            subject_fields, right_fields = normalize_owner(owner, as_of)
            if right_fields.get("right_type") and not right_fields.get("right_description"):
                right_fields["right_description"] = right_fields["right_type"]
            session.add(
                PropertyOwner(
                    mutation_id=row.id,
                    owner_index=owner.get("IndiceIntestato") or None,
                    subject_id=await _subject_id(session, subject_fields),
                    right_id=await get_or_create_right(session, **right_fields) if right_fields else None,
                )
            )
            owners += 1
    return owners


async def _write_building(session, unit: dict, parent: dict[str, int], cadastre_type: str) -> None:
    """Identifier, classification, surface and related parcels of one building; ``parent`` is the polymorphic key
    (``building_unit_id`` within a subject's group, ``current_state_id`` for a single-property document)."""
    from .visura_xml_models import BuildingClassification, BuildingIdentifier, BuildingSurface, RelatedParcel

    ident = unit["identifier"]
    session.add(
        BuildingIdentifier(
            **parent,
            location_id=await _location(session, ident, cadastre_type),
            municipality_code=ident["municipality_code"] or None,
            sequence_id=ident["sequence_id"] or None,
        )
    )
    if unit["classification"]:
        c = unit["classification"]
        cons = c.get("Consistenza", {})
        session.add(
            BuildingClassification(
                **parent,
                census_zone=c.get("ZonaCensuaria") or None,
                category=c.get("Categoria") or None,
                cadastral_class=c.get("Classe") or None,
                cadastral_income=_decimal(c.get("RenditaEuro")),
                consistency_value=_decimal(cons.get("Valore")),
                consistency_unit=cons.get("Unita") or None,
            )
        )
    if unit["surface"]:
        s = unit["surface"]
        session.add(
            BuildingSurface(
                **parent,
                total_area=_decimal(s.get("Totale")),
                excluded_area=_decimal(s.get("TotaleE")),
                planimetria=s.get("Planimetria", {}).get("Descrizione") or None,
            )
        )
    for related in unit["related"]:
        rel_ident = {
            "province": ident["province"], "municipality": ident["municipality"], "subunit": "",
            "sheet": related.get("Foglio", ""), "parcel": related.get("ParticellaNum", ""),
            "section": related.get("SezCensuaria", ""),
        }
        session.add(
            RelatedParcel(
                **parent,
                location_id=await _location(session, rel_ident, "T"),
                sequence_id=related.get("ProgrId") or None,
            )
        )


async def _write_land(session, document_id: int, unit: dict, cadastre_type: str) -> int:
    from .visura_xml_models import LandClassification, LandParcel

    ident = unit["identifier"]
    parcel = LandParcel(
        document_id=document_id,
        location_id=await _location(session, ident, cadastre_type),
        municipality_code=ident["municipality_code"] or None,
        sequence_id=ident["sequence_id"] or None,
        partita=ident["partita"] or None,
    )
    session.add(parcel)
    await session.flush()
    for c in unit["classifications"]:
        session.add(
            LandClassification(
                parcel_id=parcel.id,
                quality=c.get("Qualita") or None,
                cadastral_class=c.get("Classe") or None,
                area=_decimal(c.get("SuperficieMQ")),
                deduction_symbol=c.get("SimboloDeduzione") or None,
                dominical_income=_decimal(c.get("RedditoDominicaleEuro")),
                agricultural_income=_decimal(c.get("RedditoAgrarioEuro")),
                dominical_income_lire=c.get("RedditoDominicaleLire") or None,
                agricultural_income_lire=c.get("RedditoAgrarioLire") or None,
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


async def ingest_visura_xml(session, document_id: int, content: str | bytes | None) -> dict[str, int]:
    """Fill the typed tables from one document's XML; returns row counts (empty when the XML is not a visura).

    Runs inside the caller's transaction and replaces whatever an earlier ingestion of the same document wrote.
    """
    from .db_models import DocumentMetadata
    from .visura_xml_models import (
        BuildingAddress,
        BuildingCurrentState,
        BuildingUnit,
        DocumentSubject,
        PropertyGroup,
    )

    parsed = parse_visura_xml(content) if content else None
    if parsed is None:
        return {}
    await _clear(session, document_id)
    counts = {"units": 0, "parcels": 0, "mutations": 0, "owners": 0}
    kind = parsed["kind"]
    cadastre_type = "T" if kind.startswith("terreni") else "F"

    # --- header -----------------------------------------------------------------------------------------
    meta = await session.get(DocumentMetadata, document_id)
    if meta is not None:
        header, request, liquidation = parsed["header"], parsed["request"], parsed["liquidation"]
        meta.title = header.get("Titolo") or meta.title
        meta.registry_view_type = header.get("TipoVisura") or meta.registry_view_type
        meta.service_type = header.get("TipoServizio") or meta.service_type
        meta.generation_date = header.get("Data") or meta.generation_date
        meta.generation_time = header.get("Ora") or meta.generation_time
        meta.source_system = header.get("Provenienza") or meta.source_system
        meta.municipality_code = request.get("CodiceComune") or meta.municipality_code
        meta.protocol = request.get("Protocollo") or meta.protocol
        meta.year = request.get("Anno") or meta.year
        meta.liquidation_protocol = liquidation.get("Protocollo") or meta.liquidation_protocol
        meta.liquidation_year = liquidation.get("Anno") or meta.liquidation_year
        meta.requester = parsed["requester"] or meta.requester
        if liquidation.get("UnitaImmobiliari", "").isdigit():
            meta.liquidation_units = int(liquidation["UnitaImmobiliari"])

    # --- queried party ----------------------------------------------------------------------------------
    if parsed["subject"]:
        subject_id = await _subject_id(session, _subject_fields(parsed["subject"]))
        if subject_id:
            session.add(DocumentSubject(id=document_id, subject_id=subject_id))
            await session.flush()

    # --- properties and owners --------------------------------------------------------------------------
    if kind == "soggetto_attuale":
        for group in parsed["groups"]:
            a = group["attrs"]
            row = PropertyGroup(
                document_id=document_id,
                group_index=a.get("IndiceGruppo") or None,
                cadastre_type=a.get("TipoCatasto") or None,
                municipality_code=a.get("CodiceComune") or None,
                description=a.get("Descrizione") or None,
            )
            session.add(row)
            await session.flush()
            for unit in group["units"]:
                if unit["kind"] == "building":
                    unit_row = BuildingUnit(group_id=row.id, unit_index=unit["index"] or None)
                    session.add(unit_row)
                    await session.flush()
                    await _write_building(session, unit, {"building_unit_id": unit_row.id}, "F")
                    counts["units"] += 1
                else:
                    await _write_land(session, document_id, unit, "T")
                    counts["parcels"] += 1
            counts["mutations"] += len(group["mutations"])
            counts["owners"] += await _write_mutations(
                session,
                group["mutations"],
                {"property_group_id": row.id},
                group["attrs"].get("TipoCatasto") or "F",
                _as_of(parsed),
            )
    else:
        for unit in parsed["units"]:
            if unit["kind"] == "building":
                session.add(BuildingCurrentState(id=document_id, address_text=unit["address"] or None))
                await session.flush()
                await _write_building(session, unit, {"current_state_id": document_id}, cadastre_type)
                counts["units"] += 1
            else:
                parcel_id = await _write_land(session, document_id, unit, cadastre_type)
                counts["parcels"] += 1
                mutations = unit.get("mutations", [])
                counts["mutations"] += len(mutations)
                counts["owners"] += await _write_mutations(
                    session, mutations, {"land_parcel_id": parcel_id}, "T", _as_of(parsed)
                )
        for entry in parsed["history_addresses"]:
            session.add(
                BuildingAddress(
                    document_id=document_id, address_text=entry["address"] or None, situation=entry["situation"] or None
                )
            )
        counts["mutations"] += len(parsed["mutations"])
        counts["owners"] += await _write_mutations(
            session, parsed["mutations"], {"document_id": document_id}, cadastre_type, _as_of(parsed)
        )
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

    for candidate in (Path(file_path), Path(file_path).with_suffix(".xml")):
        if candidate.suffix.lower() == ".xml" and candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    return content


async def backfill_documents(force: bool = False, limit: int | None = None, batch: int = 50) -> dict[str, int]:
    """Fill the typed tables for documents saved before the typed reader existed (from the XML kept in the DB).

    Skips documents that already have typed rows unless ``force``. Safe to re-run.
    """
    from sqlalchemy import text

    from .database import _get_session_factory

    session_factory = _get_session_factory()
    stats = {"documents": 0, "not_visura": 0, "failed": 0, "units": 0, "parcels": 0, "owners": 0}
    query = "SELECT m.id FROM document_metadata m WHERE m.content IS NOT NULL AND m.content <> ''"
    if not force:
        query += (
            " AND NOT EXISTS (SELECT 1 FROM building_current_states x WHERE x.id = m.id)"
            " AND NOT EXISTS (SELECT 1 FROM document_subjects x WHERE x.id = m.id)"
            " AND NOT EXISTS (SELECT 1 FROM property_groups x WHERE x.document_id = m.id)"
            " AND NOT EXISTS (SELECT 1 FROM land_parcels x WHERE x.document_id = m.id)"
        )
    query += " ORDER BY m.id" + (f" LIMIT {int(limit)}" if limit else "")
    async with session_factory() as session:
        ids = (await session.execute(text(query))).scalars().all()
    from .db_models import DocumentMetadata, VisuraDocument

    for start in range(0, len(ids), batch):
        async with session_factory() as session:
            for document_id in ids[start : start + batch]:
                meta = await session.get(DocumentMetadata, document_id)
                document = await session.get(VisuraDocument, document_id)
                content = _complete_content(meta.content or "", document.file_path if document else None)
                if content != meta.content:
                    meta.content = content
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
