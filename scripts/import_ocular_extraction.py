"""Import an Ocular structured JSON result into Sister's flat and normalized tables."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete
from sqlmodel import select

from sister.database import _get_session_factory
from sister.db_models import (
    MortgageInspection,
    MortgageInspectionLien,
    MortgageInspectionParty,
    MortgageInspectionProperty,
    MortgageInspectionTitle,
    MortgageInspectionUnit,
    StructuredDocumentExtraction,
    VisuraDocument,
)


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


def _completed_at(run: dict[str, Any]) -> datetime | None:
    value = run.get("completed_at") or run.get("updated_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


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
    source_file = Path(str(data.get("source_file") or "")).name
    if not source_file:
        raise ValueError("Structured JSON does not contain source_file")

    schema_name = str(provenance.get("schema_name") or "ocular_structured")
    schema_metadata = provenance.get("schema_metadata") or {}
    schema_version = data.get("$schema_version") or schema_metadata.get("version")
    workflow_id = run.get("workflow_id") or run.get("mode")
    inspection_data = data.get("ispezione") or {}
    note = data.get("nota") or {}
    section_a = note.get("sezione_a") or {}
    title_data = section_a.get("dati_titolo") or {}
    notary = title_data.get("notaio") or {}
    lien_data = section_a.get("ipoteca") or {}
    summary = section_a.get("dati_riepilogativi") or {}
    office = inspection_data.get("ufficio") or {}
    section_b = note.get("sezione_b") or {}
    section_c = note.get("sezione_c") or {}
    section_c_raw = note.get("sezione_c_raw") or {}
    section_d = note.get("sezione_d") or {}

    session_factory = _get_session_factory()
    async with session_factory() as session:
        document = (await session.execute(
            select(VisuraDocument).where(VisuraDocument.id == document_id)
        )).scalar_one_or_none()
        if document is None:
            raise ValueError(f"Sister document {document_id} does not exist")
        if Path(document.filename or "").name != source_file:
            raise ValueError(
                f"Ocular source file {source_file!r} does not match Sister document filename "
                f"{document.filename!r}"
            )

        extraction = (await session.execute(
            select(StructuredDocumentExtraction).where(
                StructuredDocumentExtraction.document_id == document_id,
                StructuredDocumentExtraction.schema_name == schema_name,
                StructuredDocumentExtraction.run_id == run_id,
            )
        )).scalar_one_or_none()
        if extraction is None:
            extraction = StructuredDocumentExtraction(
                document_id=document_id,
                schema_name=schema_name,
                run_id=run_id,
                structured_data=data,
            )
            session.add(extraction)
            await session.flush()
        else:
            extraction.provider = provenance.get("provider")
            extraction.model = provenance.get("model") or run.get("structure_models")
            extraction.schema_version = _string(schema_version)
            extraction.workflow_id = _string(workflow_id)
            extraction.output_path = str(structured_path)
            extraction.extracted_at = _completed_at(run)
            extraction.structured_data = data
            await session.execute(delete(MortgageInspection).where(MortgageInspection.extraction_id == extraction.id))
            await session.execute(delete(MortgageInspectionTitle).where(MortgageInspectionTitle.extraction_id == extraction.id))
            await session.execute(delete(MortgageInspectionLien).where(MortgageInspectionLien.extraction_id == extraction.id))
            await session.execute(delete(MortgageInspectionParty).where(MortgageInspectionParty.extraction_id == extraction.id))
            old_units = (await session.execute(
                select(MortgageInspectionUnit.id).where(MortgageInspectionUnit.extraction_id == extraction.id)
            )).scalars().all()
            if old_units:
                await session.execute(delete(MortgageInspectionProperty).where(MortgageInspectionProperty.unit_id.in_(old_units)))
                await session.execute(delete(MortgageInspectionUnit).where(MortgageInspectionUnit.id.in_(old_units)))

        extraction.provider = provenance.get("provider")
        extraction.model = _string(provenance.get("model") or run.get("structure_models"))
        extraction.schema_version = _string(schema_version)
        extraction.workflow_id = _string(workflow_id)
        extraction.output_path = str(structured_path)
        extraction.extracted_at = _completed_at(run)
        extraction.structured_data = data
        await session.flush()
        extraction_id = extraction.id

        session.add(MortgageInspection(
            extraction_id=extraction_id,
            inspection_date=_string(inspection_data.get("data")),
            inspection_time=_string(inspection_data.get("ora")),
            inspection_number=_string(inspection_data.get("numero")),
            inspection_date_number=_string(inspection_data.get("data_numero")),
            inspection_start=_string(inspection_data.get("inizio_ispezione")),
            requester=_string(inspection_data.get("richiedente")),
            tax_paid_euro=_integer(inspection_data.get("tassa_versata_euro")),
            page_count=_integer(inspection_data.get("pagine")),
            office=_string(office.get("ufficio_provinciale")),
            service=_string(office.get("servizio")),
            note_type=_string(note.get("tipo_nota")),
            note_timestamp=_string(note.get("utc_timestamp")),
            registro_generale=_integer(note.get("registro_generale")),
            registro_particolare=_integer(note.get("registro_particolare")),
            presentazione_numero=_integer(note.get("presentazione_numero")),
            presentazione_data=_string(note.get("presentazione_data")),
            section_a_other_data=_string(section_a.get("altri_dati")),
            section_d_text=_string(section_d.get("testo")),
            unit_count=_integer(summary.get("unita_negoziali")),
            party_favore_count=_integer(summary.get("soggetti_a_favore")),
            party_contro_count=_integer(summary.get("soggetti_contro")),
        ))

        if title_data:
            session.add(MortgageInspectionTitle(
                extraction_id=extraction_id,
                title_type=_string(title_data.get("tipo_titolo")),
                description=_string(title_data.get("descrizione")),
                title_date=_string(title_data.get("data")),
                repertory_number=_string(title_data.get("numero_repertorio")),
                notary_name=_string(notary.get("nome")),
                notary_fiscal_code=_string(notary.get("codice_fiscale")),
                notary_location=_string(notary.get("sede")),
            ))
        if lien_data:
            session.add(MortgageInspectionLien(
                extraction_id=extraction_id,
                lien_type=_string(lien_data.get("specie")),
                lien_type_original=_string(lien_data.get("specie_descrizione_originale")),
                derived_from=_string(lien_data.get("derivante_da")),
                derived_from_code=_string(lien_data.get("derivante_da_codice_ufficiale")),
                derived_from_description=_string(lien_data.get("derivante_da_descrizione")),
                principal_euro=_integer(lien_data.get("capitale_euro")),
                annual_interest_rate=_string(lien_data.get("tasso_interesse_annuo")),
                annual_interest_rate_pct=_float(lien_data.get("tasso_interesse_annuo_pct")),
                semiannual_interest_rate=_string(lien_data.get("tasso_interesse_semestrale")),
                interest_euro=_integer(lien_data.get("interessi_euro")),
                expenses_euro=_integer(lien_data.get("spese_euro")),
                total_euro=_integer(lien_data.get("totale_euro")),
                variable_amounts=lien_data.get("importi_variabili"),
                foreign_currency=_string(lien_data.get("valuta_estera")),
                automatic_increase=lien_data.get("somma_aumento_automatico"),
                resolutive_condition=lien_data.get("condizione_risolutiva"),
                duration_years=_integer(lien_data.get("durata_anni")),
                duration_description=_string(lien_data.get("durata_descrizione")),
                mortgage_rank=_integer(lien_data.get("grado_ipoteca")),
            ))

        for unit_data in section_b.get("unita_negoziali") or []:
            unit = MortgageInspectionUnit(
                extraction_id=extraction_id,
                unit_number=_integer(unit_data.get("numero")),
            )
            session.add(unit)
            await session.flush()
            for property_data in unit_data.get("immobili") or []:
                session.add(MortgageInspectionProperty(
                    unit_id=unit.id,
                    property_number=_integer(property_data.get("numero_immobile")),
                    municipality_code=_string(property_data.get("comune_codice")),
                    municipality=_string(property_data.get("comune_denominazione")),
                    cadastre_type=_string(property_data.get("tipo_catasto")),
                    urban_section=_string(property_data.get("sezione_urbana")),
                    sheet=_integer(property_data.get("foglio")),
                    parcel=_integer(property_data.get("particella")),
                    subunit=_integer(property_data.get("subalterno")),
                    nature=_string(property_data.get("natura")),
                    nature_description=_string(property_data.get("natura_descrizione")),
                    room_count=_integer(property_data.get("consistenza_vani")),
                    area_sqm=_float(property_data.get("consistenza_mq")),
                    floor=_string(property_data.get("piano")),
                    address=_string(property_data.get("indirizzo")),
                    civic_number=_string(property_data.get("numero_civico")),
                ))

        for role in ("a_favore", "contro"):
            normalized_rows = section_c.get(role) or []
            raw_rows = section_c_raw.get(role) or []
            raw_by_number = {
                _integer(row.get("numero_soggetto")): row
                for row in raw_rows
                if _integer(row.get("numero_soggetto")) is not None
            }
            for ordinal, party_data in enumerate(normalized_rows):
                raw_data = raw_by_number.get(_integer(party_data.get("numero_soggetto")))
                if raw_data is None and ordinal < len(raw_rows):
                    raw_data = raw_rows[ordinal]
                raw_data = raw_data or {}
                session.add(MortgageInspectionParty(
                    extraction_id=extraction_id,
                    role=role,
                    ordinal=ordinal,
                    subject_number=_integer(party_data.get("numero_soggetto")),
                    quality=_string(party_data.get("qualita")),
                    surname=_string(party_data.get("cognome")),
                    given_name=_string(party_data.get("nome")),
                    birth_date=_string(party_data.get("data_nascita")),
                    birth_place=_string(party_data.get("luogo_nascita")),
                    gender=_string(party_data.get("sesso")),
                    fiscal_code=_string(party_data.get("codice_fiscale")),
                    entity_name=_string(party_data.get("denominazione")),
                    registered_office=_string(party_data.get("sede")),
                    mortgage_domicile=_string(party_data.get("domicilio_ipotecario")),
                    negotiation_unit_reference=_integer(party_data.get("unita_negoziale_riferimento")),
                    right_type=_string(party_data.get("tipo_diritto")),
                    share_numerator=_integer(party_data.get("quota_numeratore")),
                    share_denominator=_integer(party_data.get("quota_denominatore")),
                    raw_quality=_string(raw_data.get("qualita_testuale")),
                    raw_share=_string(raw_data.get("quota_testuale")),
                    raw_right_type=_string(raw_data.get("tipo_diritto_testuale")),
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
