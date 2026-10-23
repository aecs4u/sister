"""Importing an Ocular mortgage-inspection extraction into the flat tables and reading it back for the page."""

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from sister import database
from sister.db_models import Document

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_ocular_extraction.py"

pytestmark = pytest.mark.usefixtures("fresh_db")

EXTRACTION = {
    "source_file": "isp_test.pdf",
    "$schema_version": "1.2",
    "ispezione": {
        "data": "2025-01-30", "ora": "08:57:03", "numero": "T1 72614", "data_numero": "2025-01-30",
        "inizio_ispezione": "2025-01-30T08:50:57", "richiedente": "STUDIO ROSSI", "tassa_versata_euro": 4, "pagine": 3,
        "ufficio": {"ufficio_provinciale": "RAVENNA", "servizio": "Servizi Catastali"},
    },
    "nota": {
        "tipo_nota": "iscrizione", "utc_timestamp": "2017-12-22T17:31:51+01:00", "registro_generale": 1,
        "registro_particolare": 2, "presentazione_numero": 3, "presentazione_data": "2017-12-27",
        "sezione_a": {
            "altri_dati": "altri",
            "dati_titolo": {
                "tipo_titolo": "atto notarile", "descrizione": "mutuo", "data": "2017-12-21", "numero_repertorio": "55",
                "notaio": {"nome": "NOTAIO", "codice_fiscale": "NTRXXX70A01H501Z", "sede": "RAVENNA"},
            },
            "ipoteca": {
                "specie": "ipoteca_volontaria", "capitale_euro": 52000, "tasso_interesse_annuo_pct": 4.25, "interessi_euro": 100,
                "spese_euro": 10, "totale_euro": 78000, "importi_variabili": False, "valuta_estera": "CHF", "durata_anni": 20,
                "grado_ipoteca": 1,
            },
        },
        "sezione_b": {"unita_negoziali": [{"numero": 1, "immobili": [{
            "numero_immobile": 1, "comune_codice": "H199", "comune_denominazione": "RAVENNA (RA)", "tipo_catasto": "fabbricati",
            "sezione_urbana": "RA", "foglio": 103, "particella": 1714, "subalterno": 8, "natura": "A2", "consistenza_vani": 5,
            "consistenza_mq": 90.5, "indirizzo": "VIA X", "numero_civico": "2",
        }]}]},
        "sezione_c": {
            "a_favore": [{
                "numero_soggetto": 1, "qualita": "creditore_ipotecario", "denominazione": "BANCA SPA", "codice_fiscale": "01188860397",
                "sede": "RAVENNA", "tipo_diritto": "proprieta", "quota_numeratore": 1, "quota_denominatore": 1,
            }],
            "contro": [{
                "numero_soggetto": 2, "qualita": "debitore_ipotecario", "cognome": "ROSSI", "nome": "MARIO", "data_nascita": "1970-01-01",
                "luogo_nascita": "ROMA (RM)", "sesso": "M", "codice_fiscale": "RSSMRA70A01H501Z", "tipo_diritto": "proprieta",
                "quota_numeratore": 1, "quota_denominatore": 1,
            }],
        },
        "sezione_d": {"testo": "testo libero"},
    },
}


def _script():
    spec = importlib.util.spec_from_file_location("import_ocular_extraction_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_run(root: Path) -> Path:
    """The layout the importer reads: <out>/<hash>/structured/…json, provenance.json beside it, runs/<id>/run.json."""
    out = root / "abcd1234"
    (out / "structured").mkdir(parents=True)
    (out / "runs" / "run-1").mkdir(parents=True)
    structured = out / "structured" / "isp_test.json"
    structured.write_text(json.dumps(EXTRACTION), encoding="utf-8")
    (out / "structured" / "provenance.json").write_text(
        json.dumps({"run_id": "run-1", "schema_name": "ispezione_ipotecaria_schema", "provider": "ocular", "model": "m1"}), encoding="utf-8"
    )
    (out / "runs" / "run-1" / "run.json").write_text(json.dumps({"workflow_id": "wf", "completed_at": "2026-10-01T10:00:00Z"}), encoding="utf-8")
    return structured


async def _document() -> int:
    async with database._get_session_factory()() as session:
        row = Document(file_format="PDF", file_name="isp_test.pdf")
        session.add(row)
        await session.commit()
        return row.id


async def _one(sql: str, **params):
    async with database._get_session_factory()() as session:
        return [tuple(r) for r in (await session.execute(text(sql), params)).all()]


async def test_extraction_fills_the_flat_tables(tmp_path):
    document_id = await _document()
    extraction_id = await _script().import_extraction(document_id, _write_run(tmp_path))

    (inspection,) = await _one(
        "SELECT inspection_date, inspection_time, note_type, presentazione_data, lien_type, lien_annual_interest_rate_pct, "
        "lien_interest_eur, lien_expenses_eur, lien_foreign_currency, title_type, title_date, title_notary_name "
        "FROM mortgage_inspection WHERE extraction_id = :e", e=extraction_id)
    assert inspection[0] == date(2025, 1, 30) and str(inspection[1]) == "08:57:03"
    assert inspection[2:5] == ("iscrizione", date(2017, 12, 27), "ipoteca_volontaria")
    assert inspection[5] == Decimal("4.2500") and inspection[6:9] == (100, 10, "CHF")
    assert inspection[9:] == ("atto notarile", date(2017, 12, 21), "NOTAIO")

    parties = await _one(
        "SELECT p.role, p.ordinal, p.quality, s.name, s.subject_type, s.fiscal_code, s.birth_date, upper(c.italian_name), c.province_abbreviation "
        "FROM mortgage_inspection_party p JOIN subject s ON s.id = p.subject_id LEFT JOIN municipalities c ON c.numeric_code = s.birth_comune_id "
        "WHERE p.inspection_id = :e ORDER BY p.role", e=extraction_id)
    assert parties == [
        ("a_favore", 0, "creditore_ipotecario", "BANCA SPA", "legal_entity", "01188860397", None, None, None),
        ("contro", 0, "debitore_ipotecario", "ROSSI MARIO", "person", "RSSMRA70A01H501Z", date(1970, 1, 1), "ROMA", "RM"),
    ]

    (prop,) = await _one(
        "SELECT p.unit_number, p.area_sqm, u.cadastre_type, u.section, u.sheet, u.parcel, u.subunit, upper(c.italian_name), c.province_abbreviation, c.cadastral_code "
        "FROM mortgage_inspection_property p JOIN cadastral_unit u ON u.id = p.cadastral_unit_id JOIN municipalities c ON c.numeric_code = u.comune_id "
        "WHERE p.inspection_id = :e", e=extraction_id)
    assert prop == (1, Decimal("90.5"), "F", "RA", "103", "1714", "8", "RAVENNA", "RA", "H199")

    async with database._get_session_factory()() as session:
        from sister.db_models import DocumentExtractionPayload

        stored = await session.get(DocumentExtractionPayload, extraction_id)
    assert stored.structured_data["ispezione"]["numero"] == "T1 72614"  # the JSON as extracted is archived


async def test_importing_the_same_run_twice_replaces_instead_of_duplicating(tmp_path):
    document_id = await _document()
    structured = _write_run(tmp_path)
    first = await _script().import_extraction(document_id, structured)
    second = await _script().import_extraction(document_id, structured)

    assert first == second
    assert (await _one("SELECT count(*) FROM document_extraction"))[0][0] == 1
    assert (await _one("SELECT count(*) FROM mortgage_inspection_party"))[0][0] == 2
    assert (await _one("SELECT count(*) FROM mortgage_inspection_property"))[0][0] == 1
    assert (await _one("SELECT count(*) FROM subject"))[0][0] == 2  # the parties are the same two subjects


async def test_a_document_that_is_not_the_extracted_file_is_refused(tmp_path):
    async with database._get_session_factory()() as session:
        other = Document(file_format="PDF", file_name="something_else.pdf")
        session.add(other)
        await session.commit()
        document_id = other.id
    with pytest.raises(ValueError, match="does not match"):
        await _script().import_extraction(document_id, _write_run(tmp_path))


async def test_the_inspection_page_still_gets_the_shape_it_was_written_for(tmp_path):
    document_id = await _document()
    await _script().import_extraction(document_id, _write_run(tmp_path))

    page = await database.get_document_structured_extraction(document_id)

    assert set(page) == {"extraction", "inspection", "title", "lien", "units", "parties"}
    assert page["extraction"]["run_id"] == "run-1" and page["extraction"]["provider"] == "ocular"
    assert page["inspection"]["inspection_date"] == "2025-01-30" and page["inspection"]["unit_count"] == 1
    assert page["inspection"]["party_favore_count"] == 1 and page["inspection"]["party_contro_count"] == 1
    assert page["title"]["notary_name"] == "NOTAIO" and page["title"]["title_date"] == "2017-12-21"
    assert page["lien"]["annual_interest_rate"] == "4,25%" and page["lien"]["interest_euro"] == 100
    assert page["lien"]["foreign_currency"] == "CHF" and page["lien"]["lien_type_original"] == "ipoteca_volontaria"
    (unit,) = page["units"]
    (prop,) = unit["properties"]
    assert unit["unit_number"] == 1
    assert (prop["municipality"], prop["municipality_code"], prop["cadastre_type"], prop["sheet"], prop["parcel"], prop["subunit"]) == (
        "RAVENNA", "H199", "fabbricati", 103, 1714, 8)
    assert prop["urban_section"] == "RA" and prop["area_sqm"] == 90.5 and prop["room_count"] == 5
    by_role = {p["role"]: p for p in page["parties"]}
    assert by_role["contro"]["surname"] == "ROSSI" and by_role["contro"]["birth_place"] == "ROMA (RM)"
    assert by_role["contro"]["birth_date"] == "1970-01-01" and by_role["contro"]["raw_share"] == "1/1"
    assert by_role["a_favore"]["entity_name"] == "BANCA SPA" and by_role["a_favore"]["surname"] is None
