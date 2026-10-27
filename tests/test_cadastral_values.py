"""Statutory IMU cadastral values: category multipliers and the building / land formulas."""

from decimal import Decimal

import pytest

from sister.cadastral_values import (
    building_cadastral_value,
    building_multiplier,
    land_cadastral_value,
    normalize_category,
)


@pytest.mark.parametrize(
    "raw, expected",
    [("C06", "C6"), ("C/6", "C6"), (" c 6 ", "C6"), ("A10", "A10"), ("A/10", "A10"), ("A01", "A1"), ("D:01", "D1"),
     ("A/2: ABITAZIONI DI TIPO CIVILE", "A2"), ("C/6: STALLE, SCUDERIE, RIMESSE E AUTORIMESSE", "C6"),
     ("A/10: UFFICI E STUDI PRIVATI", "A10"), ("C/2: MAGAZZINI E LOCALI DI DEPOSITO", "C2"), ("D/1 OPIFICI", "D1"),
     ("", ""), (None, ""), ("XYZ", ""), ("G01", ""), ("A123", ""), ("ABITAZIONI A/2", "")],
)
def test_normalize_category(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "category, multiplier",
    [
        ("A02", 160), ("A01", 160), ("A09", 160), ("C02", 160), ("C06", 160), ("C07", 160),  # gruppo A (not A/10), C/2, C/6, C/7
        ("A10", 80), ("D05", 80),  # offices and banks
        ("B01", 140), ("B08", 140), ("C03", 140), ("C04", 140), ("C05", 140),  # gruppo B, C/3-C/5
        ("C01", 55),  # shops
        ("D01", 65), ("D07", 65), ("D08", 65),  # gruppo D except D/5
        ("E01", None), ("F01", None), ("F04", None), ("", None), (None, None),  # no multiplier
    ],
)
def test_building_multiplier(category, multiplier):
    assert building_multiplier(category) == multiplier


def test_building_value_is_rendita_times_105_times_multiplier():
    value = building_cadastral_value("1000", "A02")  # 1000 x 1.05 x 160
    assert value.value == Decimal("168000.00")
    assert (value.base, value.revaluation, value.multiplier) == (Decimal("1000"), Decimal("1.05"), Decimal("160"))


def test_building_value_accepts_floats_and_rounds_to_cents():
    assert building_cadastral_value(123.45, "C06").value == Decimal("20739.60")  # 123.45 x 1.05 x 160
    assert building_cadastral_value(0.01, "C01").value == Decimal("0.58")  # 0.01 x 1.05 x 55 = 0.5775, rounded half up


@pytest.mark.parametrize(
    "rendita, expected_base",
    [("650,74", "650.74"), ("1.234,56", "1234.56"), ("1,234.56", "1234.56")],
)
def test_building_value_accepts_localized_rendita(rendita, expected_base):
    value = building_cadastral_value(rendita, "A/2")

    assert value.base == Decimal(expected_base)
    assert value.value == (Decimal(expected_base) * Decimal("1.05") * Decimal(160)).quantize(Decimal("0.01"))


@pytest.mark.parametrize("rendita, category", [(None, "A02"), (0, "A02"), (-5, "A02"), ("abc", "A02"), (100, "F01"),
                                               (100, None), (float("nan"), "A02"), (True, "A02")])
def test_building_value_is_none_when_an_input_is_unusable(rendita, category):
    assert building_cadastral_value(rendita, category) is None


def test_land_value_is_reddito_dominicale_times_125_times_135():
    value = land_cadastral_value("10")  # 10 x 1.25 x 135
    assert value.value == Decimal("1687.50")
    assert (value.revaluation, value.multiplier) == (Decimal("1.25"), Decimal("135"))


@pytest.mark.parametrize("income", [None, 0, -1, "", "x"])
def test_land_value_is_none_without_a_positive_reddito_dominicale(income):
    assert land_cadastral_value(income) is None
