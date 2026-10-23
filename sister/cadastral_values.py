"""Statutory cadastral values (valore catastale) of buildings and agricultural land.

This is the tax base used for IMU (art. 13, c. 4, D.L. 201/2011, conv. L. 214/2011), *not* a market value:

- buildings: ``rendita catastale x 1,05 x moltiplicatore`` (the multiplier depends on the cadastral category);
- agricultural land: ``reddito dominicale x 1,25 x 135`` (75 for coltivatori diretti / IAP, which the visura does not say).

Pure functions, no I/O. Ownership shares are not applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BUILDING_REVALUATION = Decimal("1.05")
LAND_REVALUATION = Decimal("1.25")
LAND_MULTIPLIER = Decimal("135")
# Reduced multiplier for coltivatori diretti / IAP. Documented for reference; never applied automatically.
LAND_MULTIPLIER_FARMER = Decimal("75")

# Human-readable form of `building_multiplier`, shown on the /valuation page: (multiplier, who gets it, example
# categories). tests/test_valuation_docs.py checks every example against `building_multiplier`, so the page cannot
# drift from the calculation. Categories of group E and F have no multiplier and are not valued.
BUILDING_MULTIPLIER_TABLE: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (160, "Gruppo A (escluso A/10) e categorie C/2, C/6, C/7", ("A1", "A2", "A3", "A4", "A7", "C2", "C6", "C7")),
    (140, "Gruppo B e categorie C/3, C/4, C/5", ("B1", "B4", "C3", "C4", "C5")),
    (80, "Categorie A/10 (uffici e studi privati) e D/5 (istituti di credito e assicurazioni)", ("A10", "D5")),
    (65, "Gruppo D, escluso D/5 (65 dal 2013, prima 60)", ("D1", "D7", "D8")),
    (55, "Categoria C/1 (negozi e botteghe)", ("C1",)),
)

_CENTS = Decimal("0.01")
_CATEGORY = re.compile(r"^\s*([A-F])\s*[/:]?\s*0*(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class CadastralValue:
    """A computed cadastral value together with the figures it was derived from."""

    value: Decimal
    base: Decimal  # rendita catastale (buildings) or reddito dominicale (land)
    revaluation: Decimal
    multiplier: Decimal


def normalize_category(category: Any) -> str:
    """Canonical category code from the visura's ``Categoria``.

    Visure give the code followed by its description (``"A/2: ABITAZIONI DI TIPO CIVILE"``), other sources a bare code
    (``"A02"``, ``"C/6"``): all reduce to ``"A2"`` / ``"C6"``. ``""`` when no category code leads the text.
    """
    match = _CATEGORY.match(str(category or "").upper())
    return f"{match.group(1)}{int(match.group(2))}" if match else ""


def building_multiplier(category: Any) -> int | None:
    """IMU multiplier of a building category, or ``None`` for categories without one (E, F, unknown)."""
    code = normalize_category(category)
    if not code:
        return None
    if code in ("A10", "D5"):
        return 80
    if code[0] == "A" or code in ("C2", "C6", "C7"):
        return 160
    if code[0] == "B" or code in ("C3", "C4", "C5"):
        return 140
    if code == "C1":
        return 55
    if code[0] == "D":
        return 65
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:  # noqa: BLE001 - any unparsable input simply has no value
        return None
    return number if number.is_finite() and number > 0 else None


def building_cadastral_value(rendita: Any, category: Any) -> CadastralValue | None:
    """Cadastral value of a building from its rendita and category (``None`` when either is unusable)."""
    base = _decimal(rendita)
    multiplier = building_multiplier(category)
    if base is None or multiplier is None:
        return None
    value = (base * BUILDING_REVALUATION * multiplier).quantize(_CENTS, ROUND_HALF_UP)
    return CadastralValue(value, base, BUILDING_REVALUATION, Decimal(multiplier))


def land_cadastral_value(reddito_dominicale: Any) -> CadastralValue | None:
    """Cadastral value of agricultural land from its reddito dominicale (``None`` when missing or not positive)."""
    base = _decimal(reddito_dominicale)
    if base is None:
        return None
    value = (base * LAND_REVALUATION * LAND_MULTIPLIER).quantize(_CENTS, ROUND_HALF_UP)
    return CadastralValue(value, base, LAND_REVALUATION, LAND_MULTIPLIER)
