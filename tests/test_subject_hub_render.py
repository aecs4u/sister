"""Render the complete property Visura inside its cadastral parcel group."""

from __future__ import annotations

from pathlib import Path

import aecs4u_theme
import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from sister import web

FIXTURE = Path(__file__).parent / "fixtures" / "visura_xml" / "fabbricati_storica.xml"


@pytest.mark.asyncio
async def test_subject_profile_attaches_cadastral_inspection_and_map_requests(monkeypatch):
    import asyncio

    code = "RSSMRA70A01H501Z"
    request_id = "subject-search-1"
    parcel_key = (123, "RA", "103", "1714")
    initial_reads_started = set()
    initial_reads_ready = asyncio.Event()

    async def concurrent_read(name, value):
        initial_reads_started.add(name)
        if len(initial_reads_started) == 4:
            initial_reads_ready.set()
        await asyncio.wait_for(initial_reads_ready.wait(), timeout=1)
        return value

    async def subject_queries(_code):
        return [{"request_id": request_id, "tipo_catasto": "E", "responded_at": None}]

    async def parcel_requests(_keys):
        return {
            parcel_key: [
                {"request_id": "map-123", "request_type": "mappa", "success": True, "total_results": 1},
                {"request_id": "inspection-456", "request_type": "ispezioni", "success": True, "total_results": 3},
            ]
        }

    monkeypatch.setattr(web, "find_subject_query_requests", subject_queries)

    async def result_record(_request_id):
        return await concurrent_read("result", {"requested_at": None})

    async def response(_request_id):
        return await concurrent_read("response", {"data": {}})

    async def owners(_request_id):
        return await concurrent_read("owners", [])

    async def profile_properties(_request_id):
        return await concurrent_read(
            "properties",
            [{
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
            }],
        )

    monkeypatch.setattr(web, "get_result_record", result_record)
    monkeypatch.setattr(web, "get_response", response)
    monkeypatch.setattr(web, "get_db_properties_for_response", profile_properties)
    monkeypatch.setattr(web, "get_db_owners_for_response", owners)
    monkeypatch.setattr(web, "get_subject_name", lambda _code: _resolved("Mario Rossi"))
    monkeypatch.setattr(web, "find_parcel_document_ids", lambda _keys: _resolved({parcel_key: {381}}))
    monkeypatch.setattr(web, "find_parcel_request_records", parcel_requests)
    monkeypatch.setattr(web, "find_parcels_with_geometry", lambda _keys: _resolved(set()))
    monkeypatch.setattr(web, "find_subject_inspection_document_ids", lambda _code: _resolved(set()))
    monkeypatch.setattr(
        web,
        "get_document_structured_extraction",
        lambda _document_id: _resolved({
            "extraction": {"id": 5, "schema_name": "ispezione_ipotecaria_schema"},
            "inspection": {
                "note_type": "trascrizione",
                "registro_generale": 17025,
                "registro_particolare": 12336,
                "party_favore_count": 1,
                "party_contro_count": 1,
            },
            "title": {"title_type": "compravendita", "description": "Atto notarile"},
            "lien": None,
            "units": [{"properties": [{"sheet": 103, "parcel": 1714}]}],
            "parties": [{"role": "a_favore"}, {"role": "contro"}],
        }),
    )
    indexed_document = {
        "id": 381,
        "document_type": "ispezione_ipotecaria",
        "file_format": "PDF",
        "filename": "isp_RA_FG103_PT1714_SUB2_T127709_2026_RP12336.pdf",
        "file_size": 100,
        "created_at": "2026-10-02T12:00:00",
        "foglio": "103",
        "particella": "1714",
        "subalterno": "2",
        "sezione_urbana": "RA",
        "provincia": "RA",
        "comune": "RAVENNA",
    }

    async def documents_by_ids(ids):
        assert ids == {381}
        return [indexed_document]

    monkeypatch.setattr(web, "get_documents_by_ids", documents_by_ids)
    monkeypatch.setattr(web, "get_document_group_siblings", lambda _ids: _resolved([]))

    profile = await web._subject_query_profile(code, [], [], page=1)
    parcel = profile["map_parcels"][0]

    assert {item["request_id"] for item in parcel["requests"]["mappe"]} == {"map-123"}
    assert {item["request_id"] for item in parcel["requests"]["ispezioni_catastali"]} == {"inspection-456"}
    assert parcel["requests"]["ispezioni_catastali"][0]["label"] == "Ispezione catastale"
    assert profile["totals"]["with_map"] == 1
    assert profile["totals"]["with_ispezioni_catastali"] == 1
    assert profile["totals"]["with_ispezioni"] == 1
    assert parcel["documents"]["ispezioni"][0]["extraction"]["kind_label"] == "Nota di trascrizione"
    assert web._subject_cadastral_group_usage(profile) == {
        381: ["Estrazione di ispezione o nota mostrata nella scheda della particella"]
    }


async def _resolved(value):
    return value


@pytest.mark.asyncio
async def test_subject_profile_file_loader_avoids_catalog_scan_and_filters_siblings(monkeypatch):
    import asyncio

    id_lookup = {}
    sibling_lookup = {}
    lookups_started = set()
    lookups_ready = asyncio.Event()

    async def concurrent_lookup(name):
        lookups_started.add(name)
        if len(lookups_started) == 2:
            lookups_ready.set()
        await asyncio.wait_for(lookups_ready.wait(), timeout=1)

    async def associations(_code):
        return {
            101: {"sources": {"subject"}, "properties": []},
            202: {"sources": {"ownership"}, "properties": []},
        }

    async def documents_by_ids(ids):
        await concurrent_lookup("documents")
        id_lookup["ids"] = set(ids)
        return [{"id": document_id, "filename": f"doc-{document_id}.p7m"} for document_id in ids]

    async def document_siblings(ids):
        await concurrent_lookup("siblings")
        sibling_lookup["ids"] = set(ids)
        return [
            {"id": 101, "filename": "doc-101.p7m"},
            {"id": 102, "filename": "doc-101.pdf"},
            {"id": 202, "filename": "doc-202.p7m"},
        ]

    monkeypatch.setattr(web, "find_subject_document_associations", associations)
    monkeypatch.setattr(web, "get_documents_by_ids", documents_by_ids)
    monkeypatch.setattr(web, "get_document_group_siblings", document_siblings)

    found_associations, files = await web._load_subject_profile_files(
        "RSSMRA70A01H501Z",
        {101, 202},
        {101, 102},
    )

    assert set(found_associations) == {101}
    assert id_lookup["ids"] == {101}
    assert sibling_lookup["ids"] == {101}
    assert {item["id"] for item in files} == {101, 102}


@pytest.mark.asyncio
async def test_subject_visura_candidates_fetch_missing_metadata_in_one_batch(monkeypatch):
    calls = []
    indexed = [
        {"id": 101, "document_type": "visura_soggetto", "filename": "indexed.xml"},
        {"id": 102, "document_type": "other", "filename": "unrelated.pdf"},
    ]

    async def documents_by_ids(document_ids):
        calls.append(set(document_ids))
        return [
            {"id": 103, "document_type": "visura_soggetto", "filename": "missing.xml"},
            {"id": 104, "document_type": "other", "filename": "not-a-visura.pdf"},
        ]

    monkeypatch.setattr(web, "get_documents_by_ids", documents_by_ids)
    found = await web._subject_visura_candidates("RSSMRA70A01H501Z", {101, 103, 104}, indexed)

    assert calls == [{103, 104}]
    assert [item["id"] for item in found] == [101, 103]
    assert all(item["queried_fiscal_code"] == "RSSMRA70A01H501Z" for item in found)
    assert found[1]["response_id"] is None


@pytest.mark.asyncio
async def test_subject_asset_documents_fetch_xml_in_one_batch_and_keep_profile_order(monkeypatch):
    calls = []

    async def documents_by_ids(document_ids, *, include_content=False):
        calls.append((list(document_ids), include_content))
        return [
            {"id": 202, "xml_content": "<second />"},
            {"id": 201, "xml_content": "<first />"},
            {"id": 203, "xml_content": ""},
        ]

    monkeypatch.setattr(web, "get_documents_by_ids", documents_by_ids)
    found = await web._load_subject_asset_documents([{"id": 201}, {"id": 202}, {"id": 203}])

    assert calls == [([201, 202, 203], True)]
    assert [item["id"] for item in found] == [201, 202]
    assert [item["xml_content"] for item in found] == ["<first />", "<second />"]


@pytest.mark.asyncio
async def test_subject_profile_index_lookups_run_concurrently(monkeypatch):
    import asyncio

    started = set()
    ready = asyncio.Event()

    async def wait_for_both(name):
        started.add(name)
        if len(started) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=1)

    async def subject_ids(_code):
        await wait_for_both("subject_ids")
        return {11, 12}

    async def associations(_code):
        await wait_for_both("associations")
        return {11: {"sources": {"subject"}, "properties": []}}

    monkeypatch.setattr(web, "get_subject_document_ids", subject_ids)
    monkeypatch.setattr(web, "find_subject_document_associations", associations)

    ids, found_associations = await web._load_subject_profile_indexes("RSSMRA70A01H501Z")

    assert ids == {11, 12}
    assert set(found_associations) == {11}


@pytest.mark.asyncio
async def test_subject_document_archive_keeps_every_physical_file_and_provenance(monkeypatch, tmp_path):
    p7m = tmp_path / "vi_sto_RA_FG103_PT1714_SUB8_2026-10-16.p7m"
    p7m.write_bytes(b"signed")
    missing_pdf = tmp_path / "vi_sto_RA_FG103_PT1714_SUB8_2026-10-16.pdf"
    indexed = [
        {
            "id": 101,
            "document_type": "visura_fabbricati",
            "file_format": "P7M",
            "filename": p7m.name,
            "file_path": str(p7m),
            "file_size": 6,
            "oggetto": "Visura storica",
            "foglio": "103",
            "particella": "1714",
            "subalterno": "8",
            "sezione_urbana": "RA",
            "provincia": "Ravenna",
            "comune": "RAVENNA",
        },
        {
            "id": 102,
            "document_type": "visura_fabbricati",
            "file_format": "PDF",
            "filename": missing_pdf.name,
            "file_path": str(missing_pdf),
            "file_size": 12,
            "oggetto": "Visura storica",
            "foglio": "103",
            "particella": "1714",
            "subalterno": "8",
            "sezione_urbana": "RA",
            "provincia": "Ravenna",
            "comune": "RAVENNA",
        },
        {
            "id": 999,
            "document_type": "ispezione_ipotecaria",
            "file_format": "PDF",
            "filename": "isp_unrelated.pdf",
            "file_path": "",
            "file_size": 12,
            "oggetto": "Ispezione non correlata",
        },
    ]
    monkeypatch.setattr(
        web,
        "find_subject_document_associations",
        lambda _code: _resolved({
            101: {
                "sources": {"ownership", "property"},
                "properties": [{
                    "province": "RA",
                    "municipality": "RAVENNA",
                    "section": "RA",
                    "sheet": "103",
                    "parcel": "1714",
                    "subunit": "8",
                }],
            }
        }),
    )
    extraction_lookup_ids = []

    async def find_extractions(document_ids):
        extraction_lookup_ids.extend(document_ids)
        return {101}

    monkeypatch.setattr(web, "find_documents_with_extraction", find_extractions)

    archive = await web._subject_document_archive(
        "RSSMRA70A01H501Z",
        indexed,
        {101: ["Dati catastali e titolarità dalla visura per immobile"]},
    )

    assert archive["document_count"] == 1
    assert set(extraction_lookup_ids) == {101, 102}
    assert archive["file_count"] == 2
    assert archive["available_file_count"] == 1
    assert archive["missing_file_count"] == 1
    document = archive["categories"][0]["documents"][0]
    assert document["source_labels"] == ["Titolarità", "Immobile del soggetto"]
    assert document["property_labels"] == ["RAVENNA · RA · Sez. RA · Fg 103 · P.lla 1714 · Sub 8"]
    assert document["has_extraction"] is True
    assert document["used_in_cadastral_groups"] is True
    assert document["cadastral_group_usage_labels"] == [
        "Dati catastali e titolarità dalla visura per immobile"
    ]
    assert [(item["id"], item["available"]) for item in document["files"]] == [(101, True), (102, False)]


def test_subject_document_archive_template_lists_available_and_database_only_files():
    template_root = Path(__file__).parents[1] / "sister" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_root)), autoescape=True)
    template = env.from_string(
        '{% from "components/subject_document_archive.html" import subject_document_archive %}'
        "{{ subject_document_archive(archive) }}"
    )
    archive = {
        "document_count": 1,
        "file_count": 2,
        "available_file_count": 1,
        "missing_file_count": 1,
        "categories": [{
            "key": "property_visure",
            "label": "Visure per immobile ed elenchi",
            "icon": "fa-file-lines",
            "documents": [{
                "id": 101,
                "title": "Visura storica",
                "type": "visura_fabbricati",
                "date": "16/10/2026",
                "source_labels": ["Titolarità", "Immobile del soggetto"],
                "property_labels": ["RAVENNA · RA · Sez. RA · Fg 103 · P.lla 1714 · Sub 8"],
                "has_extraction": True,
                "used_in_cadastral_groups": True,
                "cadastral_group_usage_labels": [
                    "Dati catastali e titolarità dalla visura per immobile"
                ],
                "files": [
                    {
                        "id": 101,
                        "filename": "visura.p7m",
                        "file_format": "P7M",
                        "size_human": "6 B",
                        "available": True,
                        "view_url": "/documents/101/view",
                        "download_url": "/documents/101/download",
                    },
                    {
                        "id": 102,
                        "filename": "visura.pdf",
                        "file_format": "PDF",
                        "size_human": "12 B",
                        "available": False,
                        "view_url": "",
                        "download_url": "",
                    },
                ],
            }],
        }],
    }

    html = template.render(archive=archive)

    assert "Tutti i documenti del soggetto e degli immobili" in html
    assert "incluse quelle precedenti" in html
    assert 'href="/documents/101/view"' in html
    assert 'href="/documents/101/download"' in html
    assert 'aria-label="Scarica visura.p7m"' in html
    assert "visura.p7m" in html and "visura.pdf" in html
    assert "record conserva" in html
    assert "RAVENNA · RA · Sez. RA · Fg 103 · P.lla 1714 · Sub 8" in html
    assert "Dati usati nei gruppi catastali" in html
    assert "Dati catastali e titolarità dalla visura per immobile" in html
    assert "Estrazione strutturata presente" in html


def test_mortgage_extraction_summary_renders_note_data_inside_cadastral_group():
    template_root = Path(__file__).parents[1] / "sister" / "templates"
    theme_template_root = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader([FileSystemLoader(str(template_root)), FileSystemLoader(str(theme_template_root))]),
        autoescape=True,
    )
    template = env.from_string(
        '{% from "components/subject_hub.html" import inspection_extraction_details %}'
        "{{ inspection_extraction_details(documents) }}"
    )
    documents = [{
        "id": 381,
        "extraction": {
            "kind_label": "Nota di trascrizione",
            "registration": "RG 17025 · RP 12336",
            "presentation": "Presentazione 7 · 2026-02-05",
            "property_count": 1,
            "party_count": 2,
            "party_favore_count": 1,
            "party_contro_count": 1,
            "title_type": "compravendita",
            "title_description": "Atto notarile",
            "title_date": "2026-01-30",
            "lien_type": "",
            "lien_principal_euro": None,
            "lien_total_euro": None,
            "lien_rank": None,
            "schema_name": "ispezione_ipotecaria_schema",
        },
    }]

    html = template.render(documents=documents)

    assert 'data-mortgage-extraction-document="381"' in html
    assert "Nota di trascrizione" in html
    assert "RG 17025 · RP 12336" in html
    assert "Atto notarile" in html
    assert "usata nel gruppo" in html
    assert "1 a favore, 1 contro" in html


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
    assert 'aria-label="Apri la visura che completa questi dati"' in html
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


def test_subject_buildings_table_exposes_keyboard_scroll_and_hint():
    template_root = Path(__file__).parents[1] / "sister" / "templates"
    theme_template_root = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"test_base.html": "{% block page_title %}{% endblock %}"}),
                FileSystemLoader(str(template_root)),
                FileSystemLoader(str(theme_template_root)),
            ]
        ),
        autoescape=True,
    )
    env.globals["_"] = lambda value: value
    module = env.get_template("catasto/visura_soggetto_archive.html").make_module(
        vars={
            "base_template": "test_base.html",
            "doc": {"xml_parsed": {}, "query_only": True},
            "subject_route_prefix": "/persons",
        }
    )

    rendered = module.buildings_table(
        [{"address": "Via Roma 1", "category": "A/4", "income": "€ 120"}],
        eyebrow="Fabbricati",
    )

    assert "Scorri orizzontalmente per vedere tutte le colonne." in rendered
    assert 'role="region" tabindex="0"' in rendered
    assert 'aria-label="Fabbricati: tabella scorrevole orizzontalmente"' in rendered
