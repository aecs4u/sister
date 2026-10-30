"""The /valuation documentation page must say what the valuation code does."""

import pytest
from fastapi.testclient import TestClient

from sister.cadastral_values import (
    BUILDING_MULTIPLIER_TABLE,
    building_cadastral_value,
    building_multiplier,
    normalize_category,
)
from sister.omi_quotes import OMI_TYPOLOGY_BY_CATEGORY, OMI_TYPOLOGY_LABELS, select_quote
from sister.valuation_docs import OMI_UNVALUED_CATEGORIES, valuation_page_context


@pytest.mark.parametrize(
    "multiplier, examples",
    [(row[0], row[2]) for row in BUILDING_MULTIPLIER_TABLE],
    ids=[str(row[0]) for row in BUILDING_MULTIPLIER_TABLE],
)
def test_every_documented_multiplier_matches_the_calculation(multiplier, examples):
    assert all(building_multiplier(category) == multiplier for category in examples)


def test_documented_multipliers_cover_every_category_the_code_values():
    documented = {category for _, _, examples in BUILDING_MULTIPLIER_TABLE for category in examples}
    valued = {
        f"{letter}{number}"
        for letter in "ABCDEF"
        for number in range(1, 12)
        if building_multiplier(f"{letter}{number}")
    }
    # each valued category is either listed or follows from its listed group (A/x, B/x, D/x)
    for category in valued - documented:
        assert category[0] in "ABD", category


def test_every_omi_typology_used_for_a_category_has_a_name():
    assert {code for codes in OMI_TYPOLOGY_BY_CATEGORY.values() for code in codes} <= set(OMI_TYPOLOGY_LABELS)


def test_unvalued_categories_listed_on_the_page_really_get_no_omi_typology():
    listed = {"A9", "B1", "C4", "C5", "D1", "E1", "F1"}  # one per row of OMI_UNVALUED_CATEGORIES
    assert len(OMI_UNVALUED_CATEGORIES) == 5
    for category in listed:
        assert normalize_category(category) not in OMI_TYPOLOGY_BY_CATEGORY, category
        chosen, reason = select_quote([], category)
        assert chosen is None and "tipologia OMI corrispondente" in reason


def test_worked_examples_are_computed_by_the_real_functions():
    context = valuation_page_context()
    assert building_cadastral_value("1000", "A02").value == 168000
    assert context["example_building"] == {"rendita": "€ 1.000,00", "multiplier": 160, "value": "€ 168.000"}
    assert context["example_land"] == {"reddito": "€ 10,00", "value": "€ 1.687,50"}
    assert context["example_omi"]["value_low"] == "€ 140.000" and context["example_omi"]["value_high"] == "€ 180.000"
    assert context["building_revaluation"] == "1,05" and context["land_revaluation"] == "1,25"
    assert (context["land_multiplier"], context["land_multiplier_farmer"]) == ("135", "75")


def test_omi_map_is_sorted_and_labelled():
    rows = valuation_page_context()["omi_map"]
    assert [row["category"] for row in rows] == [
        "A/1", "A/2", "A/3", "A/4", "A/5", "A/6", "A/7", "A/8", "A/10", "A/11", "C/1", "C/2", "C/3", "C/6", "C/7",
    ]
    assert rows[-2]["typologies"] == ["Box", "Autorimesse"]
    assert rows[-1]["typologies"] == ["Posti auto coperti", "Posti auto scoperti"]


def test_page_renders_publicly_with_every_section_and_the_computed_figures():
    from sister.main import app

    response = TestClient(app, raise_server_exceptions=False).get("/valuation")
    assert response.status_code == 200
    html = response.text.replace("\xa0", " ").replace("&nbsp;", " ")
    for anchor in ("panoramica", "valore-catastale", "stima-omi", "terreni", "lettura", "limiti", "fonti"):
        assert f'id="{anchor}"' in html, anchor
    assert "€ 168.000" in html and "€ 1.687,50" in html and "€ 140.000–€ 180.000" in html
    assert "non una perizia" in html and "Non esistono quotazioni OMI per i terreni agricoli" in html
    assert 'href="/valuation"' in html  # sidebar entry (with authentication off every reader is an operator)


def test_residential_categories_follow_the_agenzia_entrate_correspondence():
    """Manuale OMI 2025, Tabella 1: signorili A/1 A/8, civili A/2 A/3, economiche A/4-A/6, ville A/7, tipiche A/11,
    box e posti auto C/6 C/7."""
    labels = {category: [OMI_TYPOLOGY_LABELS[code] for code in codes] for category, codes in OMI_TYPOLOGY_BY_CATEGORY.items()}
    assert labels["A1"] == ["Abitazioni signorili"] and labels["A8"][0] == "Abitazioni signorili"
    assert labels["A2"] == labels["A3"] == ["Abitazioni civili"]  # A/3 is civili, not economico
    assert labels["A4"] == labels["A5"] == labels["A6"] == ["Abitazioni di tipo economico"]
    assert labels["A7"] == ["Ville e villini"] and labels["A11"] == ["Abitazioni tipiche dei luoghi"]
    assert labels["C6"][0] == "Box" and labels["C7"] == ["Posti auto coperti", "Posti auto scoperti"]
    assert labels["A8"][1] == "Ville e villini"  # our fallback: signorili is quoted in very few comuni
