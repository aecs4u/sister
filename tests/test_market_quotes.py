"""Market (asking-price) reference figures: slugs, the per-source totals and the PDF table, no database needed."""

import asyncio

from sister import land_registry_kpis as kpis_module
from sister import market_quotes


def test_town_slugs_match_immobiliare_geography():
    assert market_quotes.slug("Canicattì") == "canicatti"
    assert market_quotes.slug("LAMPEDUSA E LINOSA") == "lampedusa-e-linosa"
    assert market_quotes.slug("Sant'Agata li Battiati") == "sant-agata-li-battiati"


def test_market_valuation_sums_area_times_price_per_source(monkeypatch):
    async def immobiliare(comune, province=""):
        return {"source": "immobiliare", "label": "Immobiliare.it", "low": 1000.0, "mid": 1500.0, "high": 2000.0,
                "scope": "comune di Palermo", "as_of": "2024-02-05"}

    async def idealista(comune, latitude=None, longitude=None):
        return None  # comune not collected

    monkeypatch.setattr(market_quotes, "immobiliare_quote", immobiliare)
    monkeypatch.setattr(market_quotes, "idealista_quote", idealista)
    buildings = [
        {"category": "A/2", "omi_area": 100, "municipality": "PALERMO", "province": "PA"},
        {"category": "A/2", "omi_area": 50, "municipality": "PALERMO", "province": "PA", "duplicate": True},
        {"category": "C/6", "omi_area": 20, "municipality": "PALERMO", "province": "PA"},  # not residential
    ]

    result = asyncio.run(kpis_module._market_valuation(buildings, []))

    assert [item["source"] for item in result] == ["immobiliare"]
    assert result[0]["units"] == 1 and result[0]["of_units"] == 1
    assert result[0]["range"] == "€ 100.000 – € 200.000" and result[0]["central"] == "€ 150.000"


def test_codice_fiscale_gives_birth_date_and_sex_for_the_report():
    from sister.fiscal_code import decode_codice_fiscale

    assert decode_codice_fiscale("MSSLNE82D48Z211J") == {"birth_date": "08/04/1982", "sex": "F", "birth_place_code": "Z211"}
    assert decode_codice_fiscale("SCRFNC60T29G273W")["birth_date"] == "29/12/1960"
    assert decode_codice_fiscale("SCRFNC60T29G273X") is None  # wrong check character


def test_pdf_carries_the_omi_breakdown_views_when_they_differ():
    from sister.pdf_typst import _assessment_items

    row = {"label": "AG", "value": "€ 1 – € 2", "valued_assets": 1, "unvalued_assets": 0, "unvalued_reasons": ""}
    assessment = {
        "has_rights": True, "tiles": [], "rights": [], "show_omi_breakdown": True,
        "omi_breakdown_views": [
            {"key": "province", "label": "Provincia", "rows": [row, {**row, "label": "PA"}]},
            {"key": "right_type", "label": "Tipo di diritto", "rows": [{**row, "label": "Piena proprietà"}]},
        ],
    }

    tables = [item for item in _assessment_items(assessment) if item.get("title") == "Ripartizione della stima attribuibile"]

    assert len(tables) == 1
    assert [r[0] for r in tables[0]["rows"]] == ["Provincia", "", "Tipo di diritto"]
    assert [r[1] for r in tables[0]["rows"]] == ["AG", "PA", "Piena proprietà"]


def test_pdf_summary_has_the_four_headline_figures_and_the_limits():
    from sister.pdf_typst import _summary_items

    kpis = {
        "owner_assessment": {"tiles": [{"label": "Totale diritti stimati OMI", "value": "€ 1 – € 2", "missing": False}]},
        "cadastral_value": "€ 1.000", "building_units": 11, "land_rows": 1, "distinct_comuni": 3,
        "asset_value_coverage": {"valued_units": 9}, "asset_value_coverage_note": "Stima su 9 di 11 fabbricati.",
    }

    tiles, note = _summary_items(kpis)

    assert [t["label"] for t in tiles["tiles"]] == [
        "Stima OMI attribuibile", "Valore catastale (base IMU)", "Patrimonio in visura", "Copertura della stima OMI"]
    assert tiles["tiles"][3]["value"] == "9 di 11 fabbricati"
    assert "9 di 11" in note["text"]
    assert _summary_items({}) == []
