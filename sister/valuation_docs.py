"""Data for the /valuation documentation page, taken from the code that does the valuation.

Tables and worked examples are computed here from `cadastral_values` and `omi_quotes`, never typed into the template, so
the page cannot say something the calculation does not do.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .cadastral_values import (
    BUILDING_MULTIPLIER_TABLE,
    BUILDING_REVALUATION,
    LAND_MULTIPLIER,
    LAND_MULTIPLIER_FARMER,
    LAND_REVALUATION,
    building_cadastral_value,
    land_cadastral_value,
)
from .omi_quotes import OMI_TYPOLOGY_BY_CATEGORY, OMI_TYPOLOGY_LABELS

# Cadastral categories that get no OMI estimate, with the reason shown on the page.
OMI_UNVALUED_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("A/9", "castelli e palazzi di eminente pregio: valutazione caso per caso"),
    ("B/*", "collegi, ospedali, scuole, uffici pubblici: non quotati da OMI"),
    ("C/4, C/5", "locali per esercizi sportivi e stabilimenti balneari: nessuna tipologia OMI corrispondente"),
    ("D/*", "fabbricati a destinazione speciale: rendita a stima diretta, non quotati per zona da OMI"),
    ("E/*, F/*", "categorie a destinazione particolare (E) e unità senza rendita (F): non valutate"),
)


def _eur(value: Decimal | float, decimals: int = 0) -> str:
    text = f"{value:,.{decimals}f}"
    whole, _, fraction = text.partition(".")
    whole = whole.replace(",", ".")
    return f"€ {whole},{fraction}" if decimals else f"€ {whole}"


def _it(value: Decimal | float | int) -> str:
    text = f"{value:,}".replace(",", "_").replace(".", ",").replace("_", ".")
    return text.rstrip("0").rstrip(",") if "," in text else text


def _category_label(code: str) -> str:
    return f"{code[0]}/{code[1:]}"


def _category_sort_key(code: str) -> tuple[str, int]:
    return code[0], int(code[1:])


def valuation_page_context() -> dict[str, Any]:
    """Template variables of `valuation.html`."""
    building_example = building_cadastral_value("1000", "A02")
    land_example = land_cadastral_value("10")
    assert building_example is not None and land_example is not None  # fixed, valid inputs

    omi_area, omi_low, omi_high = 100, 1400, 1800  # a made-up zone quote and unit, only to show the arithmetic
    return {
        "building_revaluation": _it(BUILDING_REVALUATION),
        "land_revaluation": _it(LAND_REVALUATION),
        "land_multiplier": _it(LAND_MULTIPLIER),
        "land_multiplier_farmer": _it(LAND_MULTIPLIER_FARMER),
        "multipliers": [
            {"multiplier": multiplier, "label": label, "examples": ", ".join(_category_label(c) for c in examples)}
            for multiplier, label, examples in BUILDING_MULTIPLIER_TABLE
        ],
        "omi_map": [
            {
                "category": _category_label(category),
                "typologies": [OMI_TYPOLOGY_LABELS[code] for code in codes],
            }
            for category, codes in sorted(OMI_TYPOLOGY_BY_CATEGORY.items(), key=lambda item: _category_sort_key(item[0]))
        ],
        "omi_unvalued": OMI_UNVALUED_CATEGORIES,
        "example_building": {
            "rendita": _eur(building_example.base, 2),
            "multiplier": int(building_example.multiplier),
            "value": _eur(building_example.value),
        },
        "example_land": {"reddito": _eur(land_example.base, 2), "value": _eur(land_example.value, 2)},
        "example_omi": {
            "area": omi_area,
            "low": _eur(omi_low),
            "high": _eur(omi_high),
            "mid": _eur((omi_low + omi_high) / 2),
            "value_low": _eur(omi_area * omi_low),
            "value_high": _eur(omi_area * omi_high),
            "value_mid": _eur(omi_area * (omi_low + omi_high) / 2),
        },
    }
