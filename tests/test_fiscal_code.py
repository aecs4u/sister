"""Check character of the codice fiscale."""

import pytest

from sister.fiscal_code import check_character, valid_codice_fiscale, valid_partita_iva


@pytest.mark.parametrize("code", ["RSSMRA85T10A562S", "CNNMNL74L04G273K", "rssmra85t10a562s", " RSSMRA85T10A562S "])
def test_valid_codes(code):
    assert valid_codice_fiscale(code)


@pytest.mark.parametrize(
    "code",
    [
        "RSSMRI85E28H501E",  # the documentation example: SISTER itself answers "Codice Fiscale non valido"
        "RSSMRA85T10A562X",  # wrong check character
        "RSSMRA85T10A562",  # 15 characters
        "00000000018",  # a partita IVA is not a codice fiscale
        "",
        None,
    ],
)
def test_invalid_codes(code):
    assert not valid_codice_fiscale(code)


def test_check_character_of_the_first_fifteen():
    assert check_character("RSSMRA85T10A562") == "S"


@pytest.mark.parametrize("code, expected", [("00000000018", True), ("00000000042", True), ("12345678903", True), ("12345678904", False), ("123", False), ("", False)])
def test_partita_iva_check_digit(code, expected):
    assert valid_partita_iva(code) is expected
