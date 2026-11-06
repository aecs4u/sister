"""/3d must geocode the cadastral addresses the subject pages link to."""

import pytest

from sister.web import _building_geocode_queries


@pytest.mark.parametrize(
    ("query", "first"),
    [
        # "Vista 3D" link of a subject page: unit details dropped, comune kept.
        ("VIA EL ALAMEIN n. 2 Piano T-3, RAVENNA", "VIA EL ALAMEIN 2, RAVENNA, Italia"),
        ("VIA ROMA n. SNC Piano T, 40100 BOLOGNA", "VIA ROMA, 40100 BOLOGNA, Italia"),
        ("Via Zamboni 33, Bologna", "Via Zamboni 33, Bologna, Italia"),
        ("Piazza del Popolo", "Piazza del Popolo, Italia"),
    ],
)
def test_cleaned_query_first_raw_text_as_fallback(query, first):
    queries = _building_geocode_queries(query)
    assert queries == [first, query]
