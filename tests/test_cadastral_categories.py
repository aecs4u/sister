"""Descriptions of the cadastral categories shown in the subject tables."""

import pytest

from sister import omi_quotes
from sister.cadastral_categories import CATEGORY_DESCRIPTIONS, category_description
from sister.cadastral_values import building_multiplier, normalize_category


@pytest.mark.parametrize("value, expected", [
    ("A/2", "Abitazioni di tipo civile"),
    ("A02", "Abitazioni di tipo civile"),
    ("A/2: ABITAZIONI DI TIPO CIVILE", "Abitazioni di tipo civile"),  # the portal's own wording is replaced by ours
    ("C/6", "Stalle, scuderie, rimesse e autorimesse (senza fine di lucro)"),
    ("D10", "Fabbricati per funzioni produttive connesse alle attività agricole"),
    ("F/2", "Unità collabente"),
])
def test_category_description_reads_any_form_of_the_code(value, expected):
    assert category_description(value) == expected


def test_unknown_or_missing_category_has_no_description():
    assert category_description("") == "" and category_description(None) == ""
    assert category_description("Z/9") == ""
    # a code we do not list falls back to the description the visura itself carries
    assert category_description("A/12: ABITAZIONI SPECIALI") == "Abitazioni speciali"


def test_every_category_the_valuation_knows_has_a_description():
    """The IMU multipliers and the OMI typologies are defined for these categories; the table must be able to name them."""
    for code in list(omi_quotes.OMI_TYPOLOGY_BY_CATEGORY):
        assert code in CATEGORY_DESCRIPTIONS, code
    for code, description in CATEGORY_DESCRIPTIONS.items():
        assert description and normalize_category(code) == code
        if code[0] in "ABCD":
            assert building_multiplier(code) is not None


def test_the_subject_table_still_renders_when_the_helper_is_not_registered():
    """A template newer than the running code (templates reload from disk, Python does not) must not fail to compile."""
    from jinja2 import Environment

    template = "{{ (category_description(c) if category_description is defined else '') or '—' }}"
    plain = Environment().from_string(template)
    assert plain.render(c="A/2") == "—"
    registered = Environment()
    from sister.display import register

    register(registered)
    assert registered.from_string(template).render(c="A/2") == "Abitazioni di tipo civile"
