"""Pure parsers for the composite strings of SISTER result pages (values taken from tests/fixtures/single_step)."""

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


def test_parse_ubicazione_and_classamento():
    assert rp.parse_ubicazione("RAVENNA(RA) VIA EL ALAMEIN n. 2 Piano T-1") == {
        "municipality": "RAVENNA",
        "province": "RA",
        "address": "VIA EL ALAMEIN n. 2 Piano T-1",
    }
    assert rp.parse_ubicazione("VIA ROMA 1") == {"address": "VIA ROMA 1"}
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
