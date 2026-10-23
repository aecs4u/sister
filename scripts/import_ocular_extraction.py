"""Import an Ocular structured JSON result into Sister's flat and normalized tables.

The inspection, its title and lien are one ``mortgage_inspection`` row; every party becomes a ``subject`` (found by fiscal
code, else by name and birth date) linked by ``mortgage_inspection_party``; every property is a ``cadastral_unit`` linked
by ``mortgage_inspection_property``. The JSON as extracted is kept in ``sister_archive.document_extraction_payload``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete
from sqlmodel import select

from sister.database import _get_session_factory, document_file_path, _resolve_subject
from sister.db_models import (
    Document,
    DocumentExtraction,
    DocumentExtractionPayload,
    MortgageInspection,
    MortgageInspectionParty,
    MortgageInspectionProperty,
)
from sister.reference import clean, ensure_lookup, get_or_create_cadastral_unit
from sister.result_parsers import parse_it_date

_PLACE = re.compile(r"^\s*(.*?)\s*\(([A-Za-z]{2})\)\s*$")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string(value: Any) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None


def _date(value: Any):
    return parse_it_date(value)


def _time(value: Any):
    from datetime import time

    try:
        return time.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _datetime(value: Any):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _completed_at(run: dict[str, Any]) -> datetime | None:
    value = run.get("completed_at") or run.get("updated_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_hash_prefix(path: Path, length: int = 8) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:length]


async def import_extraction(document_id: int, structured_path: Path) -> int:
    structured_path = structured_path.resolve()
    data = _read_json(structured_path)
    base = structured_path.parent
    provenance_path = base / "provenance.json"
    provenance = _read_json(provenance_path) if provenance_path.is_file() else {}
    run_id = str(provenance.get("run_id") or "")
    if not run_id:
        raise ValueError("Ocular provenance.json does not contain run_id")

    run_path = structured_path.parents[1] / "runs" / run_id / "run.json"
    run = _read_json(run_path) if run_path.is_file() else {}
    schema_name = str(provenance.get("schema_name") or "ocular_structured")
    source_file = Path(str(data.get("source_file") or "")).name
    is_mortgage_inspection = schema_name == "ispezione_ipotecaria_schema" or (
        "ispezione" in data and "nota" in data
    )
    schema_metadata = provenance.get("schema_metadata") or {}
    schema_version = data.get("$schema_version") or schema_metadata.get("version")
    workflow_id = run.get("workflow_id") or run.get("mode")
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

    session_factory = _get_session_factory()
    async with session_factory() as session:
        document = (await session.execute(
            select(Document).where(Document.id == document_id)
        )).scalar_one_or_none()
        if document is None:
            raise ValueError(f"Sister document {document_id} does not exist")
        if source_file and Path(document.file_name or "").name != source_file:
            output_root_hash = structured_path.parents[1].name
            stored_path = document_file_path(document.file_name)
            content_hash_matches = (
                stored_path
                and Path(stored_path).is_file()
                and output_root_hash == _file_hash_prefix(Path(stored_path))
            )
            if not content_hash_matches:
                raise ValueError(
                    f"Ocular source file {source_file!r} does not match Sister document filename "
                    f"{document.file_name!r} or its content hash"
                )

        extraction = (await session.execute(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document_id,
                DocumentExtraction.schema_name == schema_name,
                DocumentExtraction.run_id == run_id,
            )
        )).scalar_one_or_none()
        if extraction is None:
            extraction = DocumentExtraction(document_id=document_id, schema_name=schema_name, run_id=run_id)
            session.add(extraction)
            await session.flush()
        else:
            await session.execute(delete(MortgageInspectionProperty).where(MortgageInspectionProperty.inspection_id == extraction.id))
            await session.execute(delete(MortgageInspectionParty).where(MortgageInspectionParty.inspection_id == extraction.id))
            await session.execute(delete(MortgageInspection).where(MortgageInspection.extraction_id == extraction.id))

        extraction.provider = provenance.get("provider")
        extraction.model = _string(provenance.get("model") or run.get("structure_models"))
        extraction.schema_version = _string(schema_version)
        extraction.workflow_id = _string(workflow_id)
        extraction.output_path = str(structured_path)
        extraction.extracted_at = _completed_at(run)
        session.add(extraction)
        await session.flush()
        extraction_id = extraction.id
        await session.execute(delete(DocumentExtractionPayload).where(DocumentExtractionPayload.extraction_id == extraction_id))
        session.add(DocumentExtractionPayload(extraction_id=extraction_id, structured_data=data))

        if is_mortgage_inspection:
            for table, value in (
                ("mortgage_note_type", note.get("tipo_nota")),
                ("mortgage_title_type", title_data.get("tipo_titolo")),
                ("mortgage_lien_type", lien_data.get("specie")),
            ):
                await ensure_lookup(session, table, value)
            rate = _decimal(lien_data.get("tasso_interesse_annuo_pct"))
            session.add(MortgageInspection(
                extraction_id=extraction_id,
                inspection_date=_date(inspection_data.get("data")),
                inspection_time=_time(inspection_data.get("ora")),
                inspection_number=_string(inspection_data.get("numero")),
                inspection_number_date=_date(inspection_data.get("data_numero")),
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
                presentazione_data=_date(note.get("presentazione_data")),
                section_a_other_data=_string(section_a.get("altri_dati")),
                section_d_text=_string(section_d.get("testo")),
                lien_type=clean(lien_data.get("specie")),
                lien_derived_from=_string(lien_data.get("derivante_da")),
                lien_derived_from_code=_string(lien_data.get("derivante_da_codice_ufficiale")),
                lien_derived_from_description=_string(lien_data.get("derivante_da_descrizione")),
                lien_principal_eur=_integer(lien_data.get("capitale_euro")),
                lien_annual_interest_rate_pct=rate,
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
                title_date=_date(title_data.get("data")),
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
                session.add(MortgageInspectionProperty(
                    inspection_id=extraction_id,
                    unit_number=unit_number,
                    property_number=_integer(property_data.get("numero_immobile")),
                    cadastral_unit_id=unit_id,
                    nature=_string(property_data.get("natura")),
                    nature_description=_string(property_data.get("natura_descrizione")),
                    room_count=_integer(property_data.get("consistenza_vani")),
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
                    "date_of_birth": _date(party_data.get("data_nascita")),
                    "birth_municipality": place.group(1) if place else _string(party_data.get("luogo_nascita")),
                    "birth_province": place.group(2) if place else None,
                    "subject_type": "legal_entity" if entity else "person",
                })
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

        await session.commit()
    return extraction_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_id", type=int)
    parser.add_argument("structured_json", type=Path)
    args = parser.parse_args()
    extraction_id = asyncio.run(import_extraction(args.document_id, args.structured_json))
    print(f"Imported structured extraction {extraction_id} for document {args.document_id}")


if __name__ == "__main__":
    main()
