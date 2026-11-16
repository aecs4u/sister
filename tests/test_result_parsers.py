"""Pure parsers for the composite strings of SISTER result pages (values taken from tests/fixtures/single_step)."""

import json
from pathlib import Path

import pytest

from sister import result_parsers as rp


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("R.Euro:557,77", "557,77"),
        ("Euro: 189,80", "189,80"),
        ("Euro: 1.234,50", "1.234,50"),
        ("500,00", "500,00"),
        ("", None),
        ("  ", None),
        (None, None),
    ],
)
def test_clean_amount(raw, expected):
    assert rp.clean_amount(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("RA/103", ("RA", "103")), ("103", ("", "103")), (" ra / 7 ", ("RA", "7")), ("", ("", ""))]
)
def test_split_foglio(raw, expected):
    assert rp.split_foglio(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Proprieta' per 1/2", {"right_type": "Proprieta'", "ownership_share": "1/2"}),
        ("Proprietà per 1/1", {"right_type": "Proprietà", "ownership_share": "1/1"}),
        ("Proprieta'", {"right_type": "Proprieta'"}),
        (
            "Usufrutto per 1/3 in regime di comunione dei beni",
            {"right_type": "Usufrutto", "ownership_share": "1/3", "note": "in regime di comunione dei beni"},
        ),
        ("", {}),
    ],
)
def test_parse_titolarita(raw, expected):
    assert rp.parse_titolarita(raw) == expected


def test_parse_period():
    assert rp.parse_period("dal 01/01/2020 al 31/12/2023") == ("01/01/2020", "31/12/2023")
    assert rp.parse_period("dall'impianto al 31/12/2023") == ("", "31/12/2023")
    assert rp.parse_period("") == ("", "")


def test_parse_formalita_list_and_cancellation_annotation():
    page_text = """1 TRASCRIZIONE A FAVORE del 01/02/2017 - Registro Particolare 11 Registro Generale 22
Pubblico ufficiale NOTAIO ROSSI Repertorio 123 del 01/02/2017
ATTO TRA VIVI - COMPRAVENDITA
Immobili siti in RAVENNA (RA)
Nota disponibile in formato elettronico - Presenza titolo telematico
Documenti successivi correlati:
1 - ISCRIZIONE n. 55 del 03/04/2020 (ANNOTAZIONE DI CANCELLAZIONE)
Documenti in formato elaborabile
"""
    rows = rp.parse_formalita_list(page_text, ["elenco-11-0-22-1"])
    assert rows == [
        {
            "numero": 1,
            "tipo": "TRASCRIZIONE",
            "verso": "A FAVORE",
            "data_formalita": "2017-02-01",
            "registro_particolare": "11",
            "registro_generale": "22",
            "nota_disponibile_elettronica": True,
            "presenza_titolo_telematico": True,
            "documenti_correlati": [
                {"tipo": "ISCRIZIONE", "numero": "55", "data": "2020-04-03", "descrizione": "ANNOTAZIONE DI CANCELLAZIONE"}
            ],
            "pubblico_ufficiale": "NOTAIO ROSSI",
            "repertorio": "123",
            "data_repertorio": "2017-02-01",
            "immobili": "RAVENNA (RA)",
            "causale": "ATTO TRA VIVI - COMPRAVENDITA",
            "portal_selection_value": "elenco-11-0-22-1",
        }
    ]
    assert rp.is_cancellation_annotation(rows[0]["documenti_correlati"][0]["descrizione"])
    assert not rp.is_cancellation_annotation("ANNOTAZIONE DI RINNOVO")


def test_parse_anonymized_saved_formalita_capture_and_checkbox_identities():
    fixture_path = Path(__file__).parent / "fixtures/mortgage/formalita_saved_capture_anonymized.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    rows = rp.parse_formalita_list(fixture["text"], fixture["selection_values"])

    assert len(rows) == fixture["expected"]["formalities"]
    assert sum(bool(row.get("portal_selection_value")) for row in rows) == fixture["expected"]["matched_selection_values"]
    assert sum(len(row.get("documenti_correlati") or []) for row in rows) == fixture["expected"]["annotations"]
    assert sum(
        rp.is_cancellation_annotation(annotation.get("descrizione"))
        for row in rows
        for annotation in row.get("documenti_correlati") or []
    ) == fixture["expected"]["cancellations"]
    assert all(row["registro_particolare"] and row["registro_generale"] for row in rows)


def test_capture_page_parse_attaches_checkbox_values_without_browser_actions():
    from scripts.capture_page import parse_capture_formalities

    capture = {
        "text": """1 TRASCRIZIONE A FAVORE del 01/02/2017 - Registro Particolare 11 Registro Generale 22
Pubblico ufficiale NOTAIO ROSSI Repertorio 123 del 01/02/2017
ATTO TRA VIVI - COMPRAVENDITA
Nota disponibile in formato elettronico
Documenti in formato elaborabile""",
        "form_fields": [
            {"name": "noteSelezionate", "type": "checkbox", "value": "elenco-11-0-22-1"},
            {"name": "altro", "type": "text", "value": "ignored"},
        ],
    }

    parsed = parse_capture_formalities(capture)

    assert parsed == capture["parsed_formalities"]
    assert parsed[0]["portal_selection_value"] == "elenco-11-0-22-1"


def test_parse_mortgage_inspection_page_requires_portal_identity_and_normalizes_projection():
    text = """Ispezione ipotecaria n. T1 91777 del 11/11/2026
1 TRASCRIZIONE A FAVORE del 29/12/2011 - Registro Particolare 15128 Registro Generale 23828
Pubblico ufficiale ROSSI Repertorio 123 del 01/01/2011
COMPRAVENDITA
Immobili siti in RAVENNA (RA)
Nota disponibile in formato elettronico - Presenza titolo telematico
Documenti in formato elaborabile
"""
    parsed = rp.parse_mortgage_inspection_page(
        text,
        ["x-15128-0-23828-x"],
        subject_fiscal_code="rssmra70a01h501z",
        office="RAVENNA (RA)",
        requester_fiscal_code="RSSMRA70A01H501Z",
        motivation="Esplorazione",
    )

    assert parsed["portal_capture"]["inspection"]["number"] == "T 91777"
    assert parsed["portal_capture"]["inspection"]["date"] == "2026-11-11"
    assert parsed["portal_capture"]["inspection"]["subject_tax_code"] == "RSSMRA70A01H501Z"
    assert parsed["formalities"][0]["registro_particolare"] == "15128"
    assert parsed["formalities"][0]["portal_selection_value"] == "x-15128-0-23828-x"
    assert rp.parse_mortgage_inspection_page(
        "A page without a T number and date",
        subject_fiscal_code="RSSMRA70A01H501Z",
        office="RAVENNA (RA)",
    ) is None


def test_parse_ubicazione_and_classamento():
    assert rp.parse_ubicazione("RAVENNA(RA) VIA EL ALAMEIN n. 2 Piano T-1") == {
        "municipality": "RAVENNA",
        "province": "RA",
        "address": "VIA EL ALAMEIN n. 2 Piano T-1",
    }
    assert rp.parse_ubicazione("VIA ROMA 1") == {"address": "VIA ROMA 1"}
    assert rp.parse_ubicazione("PISOGNE(BS) Sez: A") == {
        "municipality": "PISOGNE",
        "province": "BS",
        "section": "A",
        "address": "",
    }
    assert rp.parse_classamento("Zona 1 Cat.A/4") == {"census_zone": "1", "category": "A/4"}
    assert rp.parse_classamento("") == {}


def test_parse_nominativo_person_and_company():
    assert rp.parse_nominativo("ROSSI MARIO a RAVENNA (RA) il 01/01/1970") == {
        "name": "ROSSI MARIO",
        "birth_place": "RAVENNA",
        "birth_province": "RA",
        "birth_date": "01/01/1970",
    }
    assert rp.parse_nominativo("ROSSI S.R.L. con sede in MONZA (MB)") == {
        "name": "ROSSI S.R.L.",
        "registered_office": "MONZA (MB)",
    }
    assert rp.parse_nominativo("SOLO NOME") == {"name": "SOLO NOME"}


def test_parse_vis_imm_sel_fabbricati_and_terreni():
    assert rp.parse_vis_imm_sel("634568#634568#F#RA/103#1714#H199##2# #RAVENNA") == {
        "id_immobile": "634568",
        "catasto": "F",
        "section": "RA",
        "sheet": "103",
        "parcel": "1714",
        "municipality_code": "H199",
        "subunit": "2",
        "municipality": "RAVENNA",
    }
    land = rp.parse_vis_imm_sel("29466#29466#T#25#266#G273### #PALERMO")
    assert land["catasto"] == "T" and land["municipality_code"] == "G273" and "subunit" not in land
    assert rp.parse_vis_imm_sel(None) == {}
    assert rp.parse_vis_imm_sel("bad") == {}


def test_parse_intestato_value_person_and_company():
    person = rp.parse_intestato_value("1#1#ROSSI MARIO #RSSMRA70A01H501Z#M#ROMA (RM)#01/01/1970")
    assert person == {
        "codice_fiscale": "RSSMRA70A01H501Z",
        "tipo": "persona",
        "nome": "ROSSI MARIO",
        "sesso": "M",
        "luogo_nascita": "ROMA (RM)",
        "data_nascita": "01/01/1970",
    }
    company = rp.parse_intestato_value("2#0#ROSSI S.R.L.#MONZA (MB)#00000000001")
    assert company["tipo"] == "azienda" and company["sede"] == "MONZA (MB)"


def test_codice_fiscale_decoding():
    assert rp.gender_from_codice_fiscale("RSSMRA70A01H501Z") == "M"
    assert rp.gender_from_codice_fiscale("RSSMRA70A41H501Z") == "F"
    assert rp.gender_from_codice_fiscale("00000000001") is None
    assert rp.birth_code_from_codice_fiscale("RSSMRA70A01H501Z") == "H501"
    assert rp.identifier_kind("00000000001") == "legal_entity"
    assert rp.identifier_kind("xx") is None


def test_normalize_owner_from_intestati_table_row():
    subject, right = rp.normalize_owner(
        {
            "Nominativo o denominazione": "ROSSI MARIO a RAVENNA (RA) il 01/01/1970",
            "Codice fiscale": "RSSMRA70A01H501Z",
            "Titolarità": "Proprieta'",
            "Quota": "1/2",
        }
    )

    assert subject["display_name"] == "ROSSI MARIO"
    assert (subject["last_name"], subject["first_name"]) == ("ROSSI", "MARIO")
    assert subject["fiscal_code"] == "RSSMRA70A01H501Z"
    assert (subject["birth_municipality"], subject["birth_province"], subject["date_of_birth"]) == (
        "RAVENNA",
        "RA",
        "01/01/1970",
    )
    assert subject["gender"] == "M" and subject["subject_type"] == "person"
    assert subject["birth_municipality_code"] == "H501"
    assert right == {"right_type": "Proprieta'", "ownership_share": "1/2"}


def test_normalize_owner_from_radio_and_company():
    subject, right = rp.normalize_owner(
        {
            "codice_fiscale": "RSSMRA70A41H501Z",
            "nome": "ROSSI MARIA",
            "sesso": "F",
            "luogo_nascita": "ROMA (RM)",
            "data_nascita": "01/01/1970",
            "titolarita": "Proprieta' per 1/1",
        }
    )
    assert subject["gender"] == "F" and subject["birth_municipality"] == "ROMA"
    assert right == {"right_type": "Proprieta'", "ownership_share": "1/1"}

    company, _ = rp.normalize_owner({"Nominativo o denominazione": "ROSSI S.R.L.", "Codice fiscale": "00000000001"})
    assert company["subject_type"] == "legal_entity" and "gender" not in company


def test_normalize_owner_from_xml_intestato_with_period():
    subject, right = rp.normalize_owner(
        {
            "Nominativo": "ROSSI MARIO",
            "CF": "RSSMRA70A01H501Z",
            "DirittiReali": {
                "CodiceDiritto": "1",
                "Descrizione": "Proprieta'",
                "Quota": "1/2",
                "FineDiritto": "dal 01/01/2020 al 31/12/2023",
            },
        }
    )
    assert subject["fiscal_code"] == "RSSMRA70A01H501Z"
    assert right == {
        "right_code": "1",
        "right_type": "Proprieta'",
        "ownership_share": "1/2",
        "start_date": "01/01/2020",
        "end_date": "31/12/2023",
    }


def test_workflow_columns_adds_what_the_executors_read():
    row = {"Catasto": "F", "Titolarità": "Proprieta' per 1/2", "Ubicazione": "RAVENNA(RA) VIA EL ALAMEIN n. 2",
           "Foglio": "RA/103", "Particella": "1714", "Sub": "2", "provincia": "RA", "provincia_nome": "RAVENNA",
           "visImmSel": "634568#634568#F#RA/103#1714#H199##2# #RAVENNA"}

    out = rp.workflow_columns(row)

    assert (out["Provincia"], out["Comune"], out["Foglio"], out["Particella"], out["Sub"]) == (
        "RAVENNA", "RAVENNA", "103", "1714", "2")
    assert (out["Tipo"], out["Foglio_portale"]) == ("F", "RA/103") and "Sezione" not in out
    assert row["Foglio"] == "RA/103"  # input untouched


def test_workflow_columns_leaves_entity_rows_alone():
    row = {"Denominazione": "ROSSI S.R.L.", "Sede": "MONZA (MB)", "Codice Fiscale": "00000000001"}

    assert rp.workflow_columns(row) is row
