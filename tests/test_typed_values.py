"""Typed-value parsers (dates, shares, amounts, consistency, categories) and the province table."""

from datetime import date
from decimal import Decimal

import pytest

from sister.provinces import PROVINCES, province_code, province_name
from sister.result_parsers import (
    format_amount_it,
    format_consistency,
    format_it_date,
    format_share,
    normalize_category,
    normalize_class,
    parse_amount_it,
    parse_consistency,
    parse_it_date,
    parse_share,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("27/12/2001", date(2001, 12, 27)),
        ("  01/02/1999 ", date(1999, 2, 1)),
        ("2026-06-04", date(2026, 6, 4)),
        ("2026-06-04T08:50:57", date(2026, 6, 4)),
        ("20260604", date(2026, 6, 4)),
        ("31/02/2020", None),  # impossible date
        ("2026-13-01", None),
        ("", None),
        (None, None),
        ("ieri", None),
        ("12/2026", None),
    ],
)
def test_parse_it_date(text, expected):
    assert parse_it_date(text) == expected


def test_date_round_trip():
    assert format_it_date(parse_it_date("04/10/2022")) == "04/10/2022"
    assert format_it_date(None) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1/1", (1, 1)),
        ("1000/1000", (1, 1)),
        ("500/1000", (1, 2)),
        ("2/9", (2, 9)),
        ("0/5", (0, 1)),
        (" 3 / 12 ", (1, 4)),
        ("50%", (1, 2)),
        ("100%", (1, 1)),
        ("1/0", None),
        ("", None),
        (None, None),
        ("proprieta", None),
    ],
)
def test_parse_share_reduces_to_lowest_terms(text, expected):
    assert parse_share(text) == expected


def test_format_share():
    assert format_share(1, 2) == "1/2"
    assert format_share(None, None) == ""
    assert format_share(1, None) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R.Euro:1704,31", Decimal("1704.31")),
        ("R.Euro:1.704,31", Decimal("1704.31")),  # point = thousands
        ("557,77", Decimal("557.77")),
        ("Euro: 189,80", Decimal("189.80")),
        ("12", Decimal("12")),
        ("R.Euro:", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_amount_it(text, expected):
    assert parse_amount_it(text) == expected


def test_format_amount_it_is_the_old_stored_form():
    assert format_amount_it(Decimal("1704.31")) == "1704,31"
    assert format_amount_it(Decimal("0.50")) == "0,50"
    assert format_amount_it(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("11 vani", (Decimal("11"), "VANI")),
        ("8,5 vani", (Decimal("8.5"), "VANI")),
        ("4.5 vani", (Decimal("4.5"), "VANI")),
        ("254 m2", (Decimal("254"), "MQ")),
        ("57  m2", (Decimal("57"), "MQ")),
        ("12 mc", (Decimal("12"), "MC")),
        ("254", (Decimal("254"), None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_consistency(text, expected):
    assert parse_consistency(text) == expected


def test_format_consistency_for_display():
    assert format_consistency(Decimal("11"), "VANI") == "11 vani"
    assert format_consistency(Decimal("8.5"), "VANI") == "8,5 vani"
    assert format_consistency(Decimal("254"), "MQ") == "254 m2"
    assert format_consistency(None, "VANI") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A02", ("A/2", None)),
        ("A10", ("A/10", None)),
        ("C06", ("C/6", None)),
        ("A/2", ("A/2", None)),
        ("A/2: ABITAZIONI DI TIPO CIVILE", ("A/2", "ABITAZIONI DI TIPO CIVILE")),
        ("D/8: FABBRICATI COSTRUITI PER SPECIALI ESIGENZE", ("D/8", "FABBRICATI COSTRUITI PER SPECIALI ESIGENZE")),
        (": ", (None, None)),
        ("", (None, None)),
        ("ente urbano", (None, None)),
    ],
)
def test_normalize_category(text, expected):
    assert normalize_category(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("01", "1"), ("10", "10"), ("U", "U"), ("", None), ("0", None), (None, None)])
def test_normalize_class(text, expected):
    assert normalize_class(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Roma", "RM"),
        ("ROMA", "RM"),
        ("rm", "RM"),
        ("Forlì-Cesena", "FC"),
        ("FORLI'-CESENA", "FC"),
        ("Reggio Emilia", "RE"),
        ("Reggio nell'Emilia", "RE"),
        ("Bolzano", "BZ"),
        ("Valle d'Aosta", "AO"),
        ("Massa Carrara", "MS"),
        ("Pesaro e Urbino", "PU"),
        ("Monza e della Brianza", "MB"),
        ("SU", "SU"),  # the portal still prints the retired sigla; stored places keep it
        ("NAZIONALE", None),
        ("", None),
        (None, None),
        ("Atlantide", None),
    ],
)
def test_province_code(text, expected):
    assert province_code(text) == expected


def test_every_province_name_resolves_to_its_own_code():
    assert len(PROVINCES) == 110
    assert [code for code, name in PROVINCES.items() if province_code(name) != code] == []
    assert province_name("rm") == "Roma"
