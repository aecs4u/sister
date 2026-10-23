"""Birth data of owners: the portal's degraded date, the codice fiscale as the source of truth, readable names."""

from datetime import date

import pytest

from sister.display import owner_label, readable_owners
from sister.result_parsers import (
    birth_date_from_codice_fiscale,
    normalize_owner,
    parse_nominativo,
    parse_xml_nominativo,
)

AS_OF = date(2026, 10, 22)


@pytest.mark.parametrize(
    "cf, partial, expected",
    [
        ("GRSNTN62D08G273W", "/0/08/", "08/04/1962"),  # day 8 of the portal date agrees with the code
        ("DFRLCU64R70G273M", "/1/30/", "30/10/1964"),  # a woman: the code adds 40 to the day
        ("GRSNTN62D08G273W", "/0/09/", None),  # the document and the code describe different people
        ("RSSMRA10A01H501X", "", "01/01/2010"),
        ("RSSMRA27A01H501X", "", "01/01/1927"),
        ("RSSMRA23A01H501X", "", None),  # 2023 (newborn) or 1923 (103 years): not safe to say
        ("12345678901", "", None),  # a company
        ("", "", None),
    ],
)
def test_birth_date_from_the_codice_fiscale(cf, partial, expected):
    assert birth_date_from_codice_fiscale(cf, AS_OF, partial=partial) == expected


def test_the_partial_date_is_month_tens_and_day_not_month_and_year():
    parsed = parse_xml_nominativo("GRISTINA Antonio  /0/08/ il ; Comune  Nato a PALERMO (PA)")

    assert parsed == {"sex": "M", "birth_date_partial": "/0/08/", "name": "GRISTINA Antonio", "birth_place": "PALERMO (PA)"}


def test_owner_with_a_partial_date_gets_birth_date_and_names():
    subject, _right = normalize_owner(
        {"Nominativo": "GRISTINA Antonio  /0/08/ il ; Comune  Nato a PALERMO (PA)", "CF": "GRSNTN62D08G273W",
         "DirittiReali": {"Quota": " per 1/2", "CodiceDiritto": "01", "Descrizione": "Proprieta'", "FineDiritto": "dal 21/04/1999 al 22/10/2026"}},
        "22/10/2026",
    )

    assert subject["date_of_birth"] == "08/04/1962"
    assert (subject["last_name"], subject["first_name"]) == ("GRISTINA", "Antonio")
    assert subject["gender"] == "M" and subject["birth_municipality_code"] == "G273"


def test_nato_and_nata_stay_out_of_the_name_and_do_not_decide_the_sex():
    parsed = parse_nominativo("ZAFFUTO EDOARDO nato a PALERMO (PA) il 19/09/1975")
    assert parsed["name"] == "ZAFFUTO EDOARDO" and parsed["sex"] == "M"
    assert parse_nominativo("LORENZIN BARBARA nata a CROAZIA (EE) il 21/01/1974")["name"] == "LORENZIN BARBARA"
    subject, _ = normalize_owner({"Nominativo": "ZAFFUTO EDOARDO nato a PALERMO (PA) il 19/09/1975", "CF": "ZFFDRD75P19G273X"})
    assert subject["gender"] == "M"  # the codice fiscale decides


@pytest.mark.parametrize(
    "nominativo, cf, label",
    [
        ("GRISTINA Antonio  /0/08/ il ; Comune  Nato a PALERMO (PA)", "GRSNTN62D08G273W", "GRISTINA Antonio, nato a PALERMO (PA) il 08/04/1962"),
        ("DE FRANCISCI Lucia  /1/30/ il ; Comune  Nata a PALERMO (PA)", "DFRLCU64R70G273M", "DE FRANCISCI Lucia, nata a PALERMO (PA) il 30/10/1964"),
        ("CIMMINO Lorenzo  17/01/1977; Comune  NAPOLI (NA)", "CMMLNZ77A17F839A", "CIMMINO Lorenzo, nato a NAPOLI (NA) il 17/01/1977"),
        ("S.I.A.T. S.P.A.; Comune  PALERMO (PA)", "00110110822", "S.I.A.T. S.P.A., sede PALERMO (PA)"),
    ],
)
def test_owner_labels_replace_the_portal_notation(nominativo, cf, label):
    assert owner_label(nominativo, cf, AS_OF) == label


def test_readable_owners_keeps_the_original_text_for_the_full_dump():
    tree = {"V": {"TitoloVisura": {"SituazioneAl": "20261022"}, "Intestato": {
        "Nominativo": "GRISTINA Antonio  /0/08/ il ; Comune  Nato a PALERMO (PA)", "CF": "GRSNTN62D08G273W"}}}

    result = readable_owners(tree)["V"]["Intestato"]

    assert result["Nominativo"] == "GRISTINA Antonio, nato a PALERMO (PA) il 08/04/1962"
    assert result["NominativoOriginale"].startswith("GRISTINA Antonio  /0/08/")
    assert "NominativoOriginale" not in tree["V"]["Intestato"]


@pytest.mark.parametrize(
    "printed, cf, expected",
    [
        ("01/04/1908", "SCRPTR08D01G273F", "01/04/2008"),  # born in 2008, printed as 19yy: a 118-year-old owner
        ("01/04/2008", "SCRPTR08D01G273F", "01/04/2008"),
        ("11/09/1918", "CRDGNN18P12G273G", "11/09/1918"),  # the day differs: the document and the code disagree, no guess
        ("08/04/1962", "GRSNTN62D08G273W", "08/04/1962"),
        ("06/01/1926", "RSSMRA26A06H501X", "06/01/1926"),  # 100 years old is possible: only impossible ages are corrected
    ],
)
def test_a_century_off_birth_date_is_corrected_only_when_the_age_is_impossible(printed, cf, expected):
    from sister.result_parsers import correct_century

    assert correct_century(printed, cf, AS_OF) == expected


def test_plan_repairs_what_the_audit_found():
    from sister.subject_repair import plan_subject

    base = {"fiscal_code": "ZFFDRD75P19G273X", "display_name": "ZAFFUTO EDOARDO nato", "last_name": None, "first_name": None,
            "gender": "F", "date_of_birth": "19/09/1975", "birth_municipality_code": None}

    changes, notes = plan_subject(base, AS_OF)

    assert changes == {
        "display_name": ("ZAFFUTO EDOARDO nato", "ZAFFUTO EDOARDO"), "gender": ("F", "M"),
        "birth_municipality_code": (None, "G273"), "last_name": (None, "ZAFFUTO"), "first_name": (None, "EDOARDO"),
    }
    assert notes == []
    iso, _ = plan_subject({**base, "display_name": "ISMAELE LA VARDERA", "gender": "M", "date_of_birth": "1993-06-08",
                           "fiscal_code": "LVRSML93H08G273J", "last_name": "X"}, AS_OF)
    assert iso["date_of_birth"] == ("1993-06-08", "08/06/1993")
    century, century_notes = plan_subject({**base, "fiscal_code": "SCRPTR08D01G273F", "display_name": "SCARPITTA TOR",
                                           "date_of_birth": "01/04/1908", "gender": "M", "last_name": "X"}, AS_OF)
    assert century["date_of_birth"] == ("01/04/1908", "01/04/2008") and "century off" in century_notes[0]
    bare, _ = plan_subject({**base, "fiscal_code": "GRSMRP69P59G273B", "display_name": "GRISTINA Maria Pia", "gender": None,
                            "date_of_birth": None}, AS_OF)
    assert bare["date_of_birth"] == (None, "19/09/1969") and bare["gender"] == (None, "F") and "last_name" not in bare  # 3 words: not split
