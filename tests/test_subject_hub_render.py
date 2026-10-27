"""Render the complete property Visura inside its cadastral parcel group."""

from __future__ import annotations

from pathlib import Path

import aecs4u_theme
import pytest
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from sister import web


FIXTURE = Path(__file__).parent / "fixtures" / "visura_xml" / "fabbricati_storica.xml"


@pytest.mark.asyncio
async def test_subject_profile_attaches_cadastral_inspection_and_map_requests(monkeypatch):
    code = "RSSMRA70A01H501Z"
    request_id = "subject-search-1"
    parcel_key = (123, "RA", "103", "1714")

    async def subject_queries(_code):
        return [{"request_id": request_id, "tipo_catasto": "E", "responded_at": None}]

    async def properties(_request_id):
        return [{
            "result_id": 1,
            "property_type": "building",
            "comune_id": 123,
            "province": "Ravenna",
            "province_code": "RA",
            "municipality": "RAVENNA",
            "municipality_code": "H199",
            "section": "RA",
            "sheet": "103",
            "parcel": "1714",
            "subunit": "2",
        }]

    async def parcel_requests(_keys):
        return {
            parcel_key: [
                {"request_id": "map-123", "request_type": "mappa", "success": True, "total_results": 1},
                {"request_id": "inspection-456", "request_type": "ispezioni", "success": True, "total_results": 3},
            ]
        }

    monkeypatch.setattr(web, "find_subject_query_requests", subject_queries)
    monkeypatch.setattr(web, "get_result_record", lambda _request_id: _resolved({"requested_at": None}))
    monkeypatch.setattr(web, "get_response", lambda _request_id: _resolved({"data": {}}))
    monkeypatch.setattr(web, "get_db_properties_for_response", properties)
    monkeypatch.setattr(web, "get_db_owners_for_response", lambda _request_id: _resolved([]))
    monkeypatch.setattr(web, "get_subject_name", lambda _code: _resolved("Mario Rossi"))
    monkeypatch.setattr(web, "find_parcel_document_ids", lambda _keys: _resolved({}))
    monkeypatch.setattr(web, "find_parcel_request_records", parcel_requests)
    monkeypatch.setattr(web, "find_parcels_with_geometry", lambda _keys: _resolved(set()))
    monkeypatch.setattr(web, "find_subject_inspection_document_ids", lambda _code: _resolved(set()))

    profile = await web._subject_query_profile(code, [], [], page=1)
    parcel = profile["map_parcels"][0]

    assert {item["request_id"] for item in parcel["requests"]["mappe"]} == {"map-123"}
    assert {item["request_id"] for item in parcel["requests"]["ispezioni_catastali"]} == {"inspection-456"}
    assert parcel["requests"]["ispezioni_catastali"][0]["label"] == "Ispezione catastale"
    assert profile["totals"]["with_map"] == 1
    assert profile["totals"]["with_ispezioni_catastali"] == 1


async def _resolved(value):
    return value


@pytest.mark.asyncio
async def test_documented_parcel_renders_full_visura_and_opens_first_visura_group(monkeypatch):
    # Real SISTER Visure use Italian decimal separators for monetary amounts.
    xml_content = FIXTURE.read_text(encoding="utf-8").replace('RenditaEuro="557.77"', 'RenditaEuro="557,77"')

    async def get_document(_document_id: int):
        return {"xml_content": xml_content}

    monkeypatch.setattr(web, "get_document_by_id", get_document)
    ownership = await web._hub_parcel_ownership(
        [{"id": 1814, "subunit": "2", "date": "2026-10-06"}],
        "RSSMRA70A01H501Z",
        [1],
    )

    kinds = [
        ("visure", "Visure", "fa-file-lines"),
        ("planimetrie", "Planimetrie", "fa-ruler-combined"),
        ("mappe", "Mappe", "fa-map"),
        ("ispezioni", "Ispezioni ipotecarie", "fa-landmark"),
        ("ispezioni_catastali", "Ispezioni catastali", "fa-magnifying-glass"),
    ]
    parcel = {
        "section": "RA",
        "province_code": "RA",
        "municipality_code": "H199",
        "municipality": "RAVENNA",
        "sheet": "103",
        "parcel": "1714",
        "is_building": True,
        "units": [{
            "subunit": "2",
            "property_type": "building",
            "right_type": "Proprietà",
            "ownership_share": "1/2",
            "address": "VIA EL ALAMEIN n. 2 Piano T-1",
            "category": "A/4",
            "cadastral_class": "04",
            "consistency": "6.0 vani",
            "income": "557.77",
            "dominical_income": "",
            "agricultural_income": "",
            "enriched_from": 1814,
            "enriched_fields": ["income", "category"],
        }],
        "ownership": ownership,
        "documents": {
            "visure": [{
                "id": 1814,
                "title": "Visura fabbricati",
                "type": "visura_fabbricati",
                "date": "2026-10-06",
                "subunit": "2",
                "formats": ["P7M"],
            }],
            "planimetrie": [],
            "mappe": [],
            "ispezioni": [],
            "altri": [],
        },
        "requests": {
            "mappe": [{
                "request_id": "map-123",
                "label": "Mappa catastale",
                "date": "2026-10-01 12:00",
                "total_results": 1,
            }],
            "ispezioni_catastali": [{
                "request_id": "inspection-456",
                "label": "Ispezione catastale",
                "date": "2026-10-02 12:00",
                "total_results": 3,
            }],
        },
        "has_map": False,
        "other_units_documents": 0,
        "map_document_id": None,
        "map_query": "",
    }
    profile = {
        "kinds": kinds,
        "tree": [{"province": "RA"}],
        "documented_tree": [{
            "province": "RA",
            "municipalities": [{"name": "RAVENNA", "total": 1, "parcels": [parcel]}],
        }],
        "pagination": {"page": 1, "pages": 1, "first": 1, "last": 1, "total": 1, "query": ""},
    }

    template_root = Path(__file__).parents[1] / "sister" / "templates"
    theme_template_root = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader([FileSystemLoader(str(template_root)), FileSystemLoader(str(theme_template_root))]),
        autoescape=True,
    )
    template = env.from_string(
        '{% from "components/subject_hub.html" import documented_parcels %}'
        "{{ documented_parcels(profile) }}"
    )
    html = template.render(profile=profile)

    assert ownership[0]["financial"]["cadastral_value"] == "€ 93.705"
    assert 'class="accordion-collapse collapse show"' in html
    assert '<details class="border-bottom px-3 py-2" open>' in html
    assert "Situazione catastale" in html
    assert "Valore catastale (base imponibile IMU): € 93.705" in html
    assert "VIA EL ALAMEIN n. 2" in html
    assert "ROSSI MARIO" in html and "ROSSI ANNA" in html and "VERDI LUCA" in html
    assert "Storia dell&#39;immobile" in html or "Storia dell'immobile" in html
    assert 'href="/documents/1814"' in html
    assert 'href="/results/map-123"' in html and "Mappa catastale" in html
    assert 'href="/results/inspection-456"' in html and "Ispezione catastale" in html

    group = {
        "Provincia": "RA",
        "CodiceComune": "H199",
        "Descrizione": "RAVENNA",
        "Sezione": "RA",
        "ImmobileFabbricatiS": [{
            "DatiIdentificativi": {"IdentificativoDefinitivo": {
                "Provincia": "RA",
                "CodiceComune": "H199",
                "SezUrbana": "RA",
                "Foglio": "103",
                "ParticellaNum": "1714",
                "Subalterno": "2",
            }},
        }],
        "ImmobileTerreniS": [],
    }
    grouped_template = env.from_string(
        '{% from "components/subject_hub.html" import documented_parcels %}'
        "{{ documented_parcels(profile, group) }}"
    )
    grouped_html = grouped_template.render(profile=profile, group=group)

    assert "Schede catastali per particella" not in grouped_html
    assert 'id="hub-comune-' not in grouped_html
    assert "Fg 103 · P.lla 1714" in grouped_html
    assert "Valore catastale (base imponibile IMU): € 93.705" in grouped_html

    group["CodiceComune"] = "A547"
    group["ImmobileFabbricatiS"][0]["DatiIdentificativi"]["IdentificativoDefinitivo"]["CodiceComune"] = "A547"
    assert "Situazione catastale" not in grouped_template.render(profile=profile, group=group)
