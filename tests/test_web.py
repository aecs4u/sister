"""Tests for web UI route handlers."""

import re
from types import SimpleNamespace

import pytest

import sister.web as web


class _FakeTheme:
    def render(self, template_name, request, **context):
        return {
            "template": template_name,
            "request": request,
            "context": context,
        }


@pytest.fixture(autouse=True)
def _stub_unrelated_detail_queries(monkeypatch):
    """Keep route tests isolated from the configured PostgreSQL database."""

    async def _empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(web, "get_db_properties_for_response", _empty)
    monkeypatch.setattr(web, "get_db_owners_for_response", _empty)


def _fake_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(theme_setup=_FakeTheme())),
        state=SimpleNamespace(user=None),
        url=SimpleNamespace(path="/results"),
    )


def test_hub_map_geocoding_retries_rural_address_without_unit_details():
    queries = web._hub_map_geocode_queries(
        "CONTRADA CALA FRANCESE n. SNC Piano T",
        "LAMPEDUSA E LINOSA",
        "AG",
    )

    assert queries == [
        "CONTRADA CALA FRANCESE, LAMPEDUSA E LINOSA, Agrigento, Italia",
        "CALA FRANCESE, LAMPEDUSA E LINOSA, Agrigento, Italia",
        "CALA FRANCESE, LAMPEDUSA, Agrigento, Italia",
    ]
    assert web._hub_map_geocode_query(
        "VIA FRANCESCO CRISPI n. 26 Piano T", "LAMPEDUSA E LINOSA", "AG"
    ) == "VIA FRANCESCO CRISPI 26, LAMPEDUSA E LINOSA, Agrigento, Italia"


def test_document_pagination_is_newest_first_and_clamps_out_of_range_pages():
    documents = [
        {"id": document_id, "created_at": f"2026-10-{document_id:02d}"}
        for document_id in range(1, 6)
    ]

    selected, page, page_count, offset = web._paginate_documents(documents, 99, page_size=2)

    assert [document["id"] for document in selected] == [1]
    assert (page, page_count, offset) == (3, 3, 4)


def test_location_page_keeps_owner_visura_links_from_other_pages():
    owner_cf = "RSSMRA85T10A562S"
    property_document = {
        "id": 1,
        "document_type": "visura_fabbricati",
        "provincia": "MI",
        "comune": "MILANO",
        "foglio": "1",
        "particella": "2",
        "intestati_json": '[{"CF":"' + owner_cf + '"}]',
    }
    owner_document = {
        "id": 99,
        "document_type": "visura_soggetto",
        "oggetto": f"Visura soggetto ({owner_cf})",
    }

    prop_map = web._build_property_map([property_document], owner_subject_docs=[owner_document])
    owner_links = prop_map["immobili"][0]["fogli"][0]["particelle"][0]["subalternos"][0]["docs"][0]["owner_docs"]

    assert [document["id"] for document in owner_links] == [99]


@pytest.mark.asyncio
async def test_documents_hub_paginates_and_filters_all_favorites_before_slicing(monkeypatch):
    from sister import database

    documents = [
        {
            "id": document_id,
            "document_type": "other",
            "filename": f"document-{document_id}.pdf",
            "file_format": "PDF",
            "file_size": 1,
            "created_at": "2026-10-01T00:00:00",
        }
        for document_id in range(1, 206)
    ]

    async def _get_all_documents(**_kwargs):
        return documents

    async def _visible_documents(_user, rows):
        return rows

    async def _favorite_ids(_user_key):
        return {1, 101, 205}

    monkeypatch.setattr(web, "get_all_documents", _get_all_documents)
    monkeypatch.setattr(web, "_filter_purchased_documents", _visible_documents)
    monkeypatch.setattr(web, "_favorite_user_key", lambda _user: "id:buyer")
    monkeypatch.setattr(database, "get_document_favorite_ids", _favorite_ids)

    paged = await web.web_documents(_fake_request(), page=2, user=SimpleNamespace(role="user"))
    paged_context = paged["context"]
    assert paged_context["page"] == 2
    assert paged_context["page_count"] == 3
    assert [document["id"] for document in paged_context["files"]] == list(range(105, 5, -1))

    favorites = await web.web_documents(
        _fake_request(), page=2, favorites=True, user=SimpleNamespace(role="user")
    )
    favorites_context = favorites["context"]
    assert favorites_context["favorites_only"] is True
    assert favorites_context["total"] == 3
    assert favorites_context["page"] == favorites_context["page_count"] == 1
    assert [document["id"] for document in favorites_context["files"]] == [205, 101, 1]


@pytest.mark.asyncio
async def test_web_results_passes_extended_filters_and_urls(monkeypatch):
    request = _fake_request()

    async def _find_responses(**kwargs):
        assert kwargs["foglio"] == "9"
        assert kwargs["particella"] == "166"
        return [
            {
                "request_id": "req_1",
                "request_type": "visura",
                "tipo_catasto": "F",
                "provincia": "Trieste",
                "comune": "TRIESTE",
                "foglio": "9",
                "particella": "166",
                "requested_at": "2026-04-12T10:30:00",
                "responded_at": None,
                "success": None,
            }
        ]

    async def _count_responses(**kwargs):
        return {"total_requests": 1, "total_responses": 0, "successful": 0, "failed": 0, "pending": 1}

    async def _count_total(**kwargs):
        return 1

    monkeypatch.setattr(web, "find_result_rows", _find_responses)
    monkeypatch.setattr(web, "count_result_rows", _count_responses)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    response = await web.web_results(
        request,
        user=None,
        provincia="Trieste",
        comune="TRIESTE",
        foglio="9",
        particella="166",
        tipo_catasto="F",
        source=None,
        status=None,
        limit=1,
        offset=1,
    )

    context = response["context"]
    assert context["results"][0]["status"] == "pending"
    assert context["results"][0]["requested_at_display"] == "2026-04-12 10:30"
    assert context["prev_url"] is not None
    assert "foglio=9" in context["prev_url"]
    assert "particella=166" in context["prev_url"]
    assert "tipo_catasto=F" in context["prev_url"]


@pytest.mark.asyncio
async def test_manual_valuation_failure_is_flagged_for_the_page_and_health(monkeypatch):
    from sister import database

    monkeypatch.setattr(web, "_OPTIONAL_DATA_DEGRADATIONS", {})
    monkeypatch.setattr(web, "_OPTIONAL_DATA_ACTIVE_FAILURES", set())

    async def _saved_values(_user_key, _asset_keys):
        return {}

    async def _fail_to_save(_user_key, _defaults):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "get_user_manual_property_valuations", _saved_values)
    monkeypatch.setattr(database, "ensure_user_property_omi_selections", _fail_to_save)
    kpis = {"table_estimates": [{"buildings": [{"asset_identity": {"asset_key": "asset-1"}}], "lands": []}]}

    result = await web._apply_user_manual_valuations(kpis, SimpleNamespace(id="demo@example.com"))

    assert result["manual_valuation_unavailable"] is True
    assert web.optional_data_degradation_summary() == {
        "since_process_start": True,
        "total": 1,
        "by_component": {"manual_property_valuations": 1},
        "active_components": ["manual_property_valuations"],
    }


@pytest.mark.asyncio
async def test_web_result_detail_renders_pending_request(monkeypatch):
    request = _fake_request()

    async def _get_result_record(_request_id):
        return {
            "request_id": "req_pending",
            "request_type": "visura",
            "tipo_catasto": "T",
            "provincia": "Roma",
            "comune": "ROMA",
            "foglio": "1",
            "particella": "2",
            "sezione": None,
            "subalterno": None,
            "requested_at": "2026-04-12T10:30:00",
            "responded_at": None,
            "success": None,
            "status": "pending",
            "data": None,
            "error": None,
            "page_visits": [],
        }

    monkeypatch.setattr(web, "get_result_record", _get_result_record)
    async def _no_docs(*_, **__):
        return []

    monkeypatch.setattr(web, "get_documents_for_response", _no_docs)

    response = await web.web_result_detail(request, "req_pending", user=None)

    context = response["context"]
    assert context["result"]["status"] == "pending"
    assert context["result"]["requested_at_display"] == "2026-04-12 10:30"
    assert context["result"]["sections"] == []


@pytest.mark.asyncio
async def test_web_result_detail_marks_not_found(monkeypatch):
    request = _fake_request()

    async def _get_result_record(_request_id):
        return None

    monkeypatch.setattr(web, "get_result_record", _get_result_record)

    response = await web.web_result_detail(request, "missing_id", user=None)

    assert response["context"]["result"] is None
    assert response["context"]["not_found"] is True


# ---------------------------------------------------------------------------
# Pagination and total_count
# ---------------------------------------------------------------------------


def _make_result_rows(n, source="single"):
    """Generate n fake result rows."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "request_id": f"req_{i}",
                "request_type": "visura" if source == "single" else "workflow:test",
                "source": source,
                "tipo_catasto": "F",
                "provincia": "Roma",
                "comune": "ROMA",
                "foglio": "1",
                "particella": str(i),
                "sezione": None,
                "subalterno": None,
                "requested_at": f"2026-04-12T10:{i:02d}:00",
                "responded_at": f"2026-04-12T10:{i:02d}:30",
                "success": True,
                "status": "completed",
                "data": None,
                "error": None,
            }
        )
    return rows


@pytest.mark.asyncio
async def test_web_results_pagination_uses_total_count(monkeypatch):
    """total_count drives next/prev URLs, not len(results)."""
    request = _fake_request()
    all_rows = _make_result_rows(5)

    async def _find(**kwargs):
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 50)
        return all_rows[offset : offset + limit]

    async def _count(**kwargs):
        return {"total_requests": 5, "total_responses": 5, "successful": 5, "failed": 0, "partial": 0, "pending": 0}

    async def _count_total(**kwargs):
        return 5

    monkeypatch.setattr(web, "find_result_rows", _find)
    monkeypatch.setattr(web, "count_result_rows", _count)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    # Page 1: offset=0, limit=2 — should have next but no prev
    resp = await web.web_results(request, user=None, limit=2, offset=0)
    ctx = resp["context"]
    assert ctx["current_count"] == 2
    assert ctx["total_count"] == 5
    assert ctx["prev_url"] is None
    assert ctx["next_url"] is not None

    # Page 3: offset=4, limit=2 — should have prev but no next
    resp = await web.web_results(request, user=None, limit=2, offset=4)
    ctx = resp["context"]
    assert ctx["current_count"] == 1
    assert ctx["prev_url"] is not None
    assert ctx["next_url"] is None


@pytest.mark.asyncio
async def test_web_results_mixed_single_and_workflow(monkeypatch):
    """Results page handles both single and workflow rows."""
    request = _fake_request()
    single = _make_result_rows(2, source="single")
    wf = _make_result_rows(1, source="workflow")
    wf[0]["request_id"] = "wf_test_001"
    wf[0]["status"] = "partial"
    wf[0]["success"] = False
    all_rows = single + wf

    async def _find(**kwargs):
        src = kwargs.get("source")
        rows = [r for r in all_rows if src is None or r["source"] == src]
        return rows[: kwargs.get("limit", 50)]

    async def _count(**kwargs):
        return {"total_requests": 3, "total_responses": 3, "successful": 2, "failed": 0, "partial": 1, "pending": 0}

    async def _count_total(**kwargs):
        src = kwargs.get("source")
        return len([r for r in all_rows if src is None or r["source"] == src])

    monkeypatch.setattr(web, "find_result_rows", _find)
    monkeypatch.setattr(web, "count_result_rows", _count)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    resp = await web.web_results(request, user=None, source=None)
    ctx = resp["context"]
    assert ctx["total_count"] == 3
    assert len(ctx["results"]) == 3

    # Filter to workflow only
    resp = await web.web_results(request, user=None, source="workflow")
    ctx = resp["context"]
    assert ctx["total_count"] == 1


@pytest.mark.asyncio
async def test_web_results_preserves_single_no_match_status(monkeypatch):
    request = _fake_request()

    async def _find(**_kwargs):
        return [
            {
                "request_id": "req_no_match",
                "request_type": "visura",
                "source": "single",
                "success": False,
                "status": "no_match",
                "error": "Nessuna corrispondenza trovata",
            }
        ]

    async def _count(**_kwargs):
        return {"total_requests": 1, "total_responses": 1, "successful": 0, "failed": 0, "no_match": 1, "pending": 0}

    async def _count_total(**_kwargs):
        return 1

    monkeypatch.setattr(web, "find_result_rows", _find)
    monkeypatch.setattr(web, "count_result_rows", _count)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    response = await web.web_results(request, user=None, source="single")

    assert response["context"]["results"][0]["status"] == "no_match"


# ---------------------------------------------------------------------------
# _build_result_sections
# ---------------------------------------------------------------------------


class TestBuildResultSections:
    """Test section classification logic."""

    def test_flat_table_with_data(self):
        data = {
            "immobili": [
                {"Foglio": "101", "Particella": "2", "Rendita": "500"},
                {"Foglio": "101", "Particella": "3", "Rendita": "600"},
            ]
        }
        sections = web._build_result_sections(data)
        assert len(sections) == 1
        s = sections[0]
        assert s["kind"] == "flat_table"
        assert s["count"] == 2
        assert "Foglio" in s["columns"]
        assert s["rows"][0]["Foglio"] == "101"

    def test_empty_rows_skipped(self):
        data = {"immobili": [{}, {}, {}]}
        sections = web._build_result_sections(data)
        assert len(sections) == 0

    def test_empty_string_keys_removed(self):
        data = {"immobili": [{"": "", "Foglio": "1", "Particella": "2"}]}
        sections = web._build_result_sections(data)
        assert len(sections) == 1
        assert "" not in sections[0]["columns"]

    def test_scalar_value_section(self):
        data = {"total_results": 42}
        sections = web._build_result_sections(data)
        assert len(sections) == 1
        assert sections[0]["kind"] == "value"
        assert sections[0]["value"] == 42

    def test_page_visits_excluded(self):
        data = {"page_visits": [{"step": "a"}], "total": 1}
        sections = web._build_result_sections(data)
        assert len(sections) == 1
        assert sections[0]["name"] == "total"

    def test_downloaded_pdfs_normalized(self):
        data = {
            "downloaded_pdfs": [
                {
                    "filename": "DOC_1.p7m",
                    "file_format": "P7M",
                    "file_size": 1024,
                    "oggetto": "VISURA TEST",
                    "richiesta_del": "01/01/2026",
                    "parsed_data": {
                        "intestati": [{"Nominativo": "TEST", "CF": "ABC123"}],
                        "xml_content": "",
                    },
                }
            ]
        }
        sections = web._build_result_sections(data)
        # Intestati aggregation emits an extra flat_table section before downloaded_docs
        assert len(sections) == 2
        agg = sections[0]
        assert agg["kind"] == "flat_table"
        assert agg["name"] == "intestati_da_documenti"
        assert agg["count"] == 1
        assert agg["rows"][0]["Nominativo"] == "TEST"
        s = sections[1]
        assert s["kind"] == "downloaded_docs"
        assert s["count"] == 1
        doc = s["docs"][0]
        assert doc["filename"] == "DOC_1.p7m"
        assert len(doc["intestati_rows"]) == 1
        assert doc["intestati_rows"][0]["Nominativo"] == "TEST"

    def test_workflow_steps_section(self):
        data = {"steps": [{"step": "visura", "status": "completed"}]}
        sections = web._build_result_sections(data)
        assert len(sections) == 1
        assert sections[0]["kind"] == "workflow_steps"

    def test_mixed_rows_flattened_to_flat_table(self):
        # Mixed rows (dict subfield + list-of-dict) produce a main flat_table
        # plus a child flat_table per list-of-dict field.
        data = {
            "results": [
                {
                    "result_index": 1,
                    "immobile": {"Foglio": "1", "Categoria": "A/2"},
                    "intestati": [{"Nominativo": "Rossi", "Quota": "1/2"}, {"Nominativo": "Bianchi", "Quota": "1/2"}],
                },
                {
                    "result_index": 2,
                    "immobile": {"Foglio": "2", "Categoria": "C/6"},
                    "intestati": [{"Nominativo": "Verdi", "Quota": "1/1"}],
                },
            ]
        }
        sections = web._build_result_sections(data)
        # Expect two sections: main + child "intestati"
        assert len(sections) == 2
        main = sections[0]
        assert main["kind"] == "flat_table"
        assert main["name"] == "results"
        assert "Foglio" in main["columns"]
        assert "Categoria" in main["columns"]
        assert "intestati_n" in main["columns"]
        assert main["rows"][0]["intestati_n"] == 2
        assert main["rows"][1]["intestati_n"] == 1

        child = sections[1]
        assert child["kind"] == "flat_table"
        assert child["name"] == "results_intestati"
        assert child["join_key"] == "result_index"
        assert "result_index" in child["columns"]
        assert "Nominativo" in child["columns"]
        # 2 intestati for result 1 + 1 for result 2 = 3 rows total
        assert child["count"] == 3
        assert child["rows"][0]["result_index"] == 1
        assert child["rows"][0]["Nominativo"] == "Rossi"
        assert child["rows"][2]["result_index"] == 2


# ---------------------------------------------------------------------------
# Result detail — single-query payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_result_detail_single_query(monkeypatch):
    """Detail page renders sections and page_visit_rows for single queries."""
    request = _fake_request()

    async def _get_result_record(_id):
        return {
            "request_id": "req_F_abc123",
            "request_type": "visura",
            "tipo_catasto": "F",
            "provincia": "Trieste",
            "comune": "TRIESTE",
            "foglio": "9",
            "particella": "166",
            "sezione": None,
            "subalterno": "3",
            "cost_text": None,
            "cost_value": None,
            "requested_at": "2026-04-12T10:30:00",
            "responded_at": "2026-04-12T10:30:05",
            "success": True,
            "status": "completed",
            "data": {
                "immobili": [{"Foglio": "9", "Particella": "166", "Sub": "3"}],
                "intestati": [{"Nominativo o denominazione": "TEST USER", "Codice fiscale": "XYZ"}],
                "page_visits": [
                    {"step": "fill_form", "url": "https://example.com", "timestamp": "2026-04-12T10:30:01"}
                ],
            },
            "error": None,
            "page_visits": [
                {"step": "fill_form", "url": "https://example.com", "timestamp": "2026-04-12T10:30:01"},
            ],
        }

    async def _get_docs(_id, **kwargs):
        return []

    monkeypatch.setattr(web, "get_result_record", _get_result_record)
    monkeypatch.setattr(web, "get_documents_for_response", _get_docs)

    resp = await web.web_result_detail(request, "req_F_abc123", user=None)
    ctx = resp["context"]
    result = ctx["result"]

    assert result["status"] == "completed"
    assert len(result["sections"]) >= 2  # immobili + intestati
    table_sections = [s for s in result["sections"] if s["kind"] == "flat_table"]
    assert any(s["name"] == "immobili" for s in table_sections)
    assert any(s["name"] == "intestati" for s in table_sections)
    assert len(result["page_visit_rows"]) == 1
    assert result["page_visit_rows"][0]["Step"] == "fill_form"


# ---------------------------------------------------------------------------
# Result detail — workflow payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_result_detail_workflow(monkeypatch):
    """Detail page renders workflow steps and downloaded docs."""
    request = _fake_request()

    async def _get_result_record(_id):
        return None

    async def _get_workflow_record(_id):
        return {
            "request_id": "wf_test_001",
            "request_type": "workflow:due-diligence",
            "tipo_catasto": "F",
            "provincia": "Ravenna",
            "comune": "RAVENNA",
            "foglio": "101",
            "particella": "2",
            "sezione": None,
            "subalterno": None,
            "cost_text": None,
            "cost_value": None,
            "requested_at": "2026-04-12T12:00:00",
            "responded_at": "2026-04-12T12:05:00",
            "success": True,
            "status": "completed",
            "data": {
                "steps": [
                    {"step": "visura", "status": "completed", "data": {"immobili": []}},
                    {"step": "intestati", "status": "completed", "data": {"intestati": []}},
                ],
                "downloaded_pdfs": [
                    {
                        "filename": "DOC_1.p7m",
                        "file_format": "P7M",
                        "file_size": 5000,
                        "oggetto": "VISURA TEST",
                        "richiesta_del": "12/04/2026",
                        "parsed_data": {"intestati": [], "xml_content": ""},
                    },
                ],
                "page_visits": [],
            },
            "error": None,
            "page_visits": [],
        }

    async def _get_docs(_id, **kwargs):
        return []

    monkeypatch.setattr(web, "get_result_record", _get_result_record)
    monkeypatch.setattr(web, "get_workflow_result_record", _get_workflow_record)
    monkeypatch.setattr(web, "get_documents_for_response", _get_docs)

    resp = await web.web_result_detail(request, "wf_test_001", user=None)
    ctx = resp["context"]
    result = ctx["result"]

    assert result["status"] == "completed"
    step_sections = [s for s in result["sections"] if s["kind"] == "workflow_steps"]
    assert len(step_sections) == 1
    assert step_sections[0]["count"] == 2
    doc_sections = [s for s in result["sections"] if s["kind"] == "downloaded_docs"]
    assert len(doc_sections) == 1
    assert doc_sections[0]["count"] == 1


# ---------------------------------------------------------------------------
# /workflows routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_workflows_list(monkeypatch):
    """Workflows list page renders runs with step counts."""
    request = _fake_request()

    async def _find_runs(**kwargs):
        return [
            {
                "workflow_id": "wf_test_001",
                "preset": "due-diligence",
                "status": "completed",
                "provincia": "Ravenna",
                "comune": "RAVENNA",
                "foglio": "101",
                "particella": "2",
                "tipo_catasto": "F",
                "total_steps": 5,
                "completed_steps": 5,
                "created_at": "2026-04-12T12:00:00",
                "updated_at": "2026-04-12T12:05:00",
            },
            {
                "workflow_id": "wf_test_002",
                "preset": "intestati",
                "status": "partial",
                "provincia": "Roma",
                "comune": "ROMA",
                "foglio": "1",
                "particella": "50",
                "tipo_catasto": "T",
                "total_steps": 3,
                "completed_steps": 1,
                "created_at": "2026-04-12T13:00:00",
                "updated_at": "2026-04-12T13:01:00",
            },
        ]

    monkeypatch.setattr(web, "find_workflow_runs", _find_runs)
    monkeypatch.setattr(web, "_get_auth_status", lambda: {"state": "ready", "mode": "local", "message": "ok"})

    resp = await web.web_workflows(request, user=None, status=None, limit=50, offset=0)
    ctx = resp["context"]
    assert len(ctx["runs"]) == 2
    assert ctx["runs"][0]["workflow_id"] == "wf_test_001"
    assert ctx["runs"][0]["created_at_display"] == "2026-04-12 12:00"
    assert ctx["runs"][1]["status"] == "partial"


@pytest.mark.asyncio
async def test_web_workflows_list_status_filter(monkeypatch):
    """Workflows list page passes status filter."""
    request = _fake_request()
    captured_kwargs = {}

    async def _find_runs(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(web, "find_workflow_runs", _find_runs)
    monkeypatch.setattr(web, "_get_auth_status", lambda: {"state": "unavailable", "mode": "local", "message": "n/a"})

    await web.web_workflows(request, user=None, status="completed", limit=10, offset=0)
    assert captured_kwargs["status"] == "completed"
    assert captured_kwargs["limit"] == 10


@pytest.mark.asyncio
async def test_web_workflow_detail_renders(monkeypatch):
    """Dedicated workflow detail page renders step sections."""
    request = _fake_request()

    async def _get_workflow_record(_id):
        return {
            "request_id": "wf_detail_001",
            "request_type": "workflow:due-diligence",
            "tipo_catasto": "F",
            "provincia": "Ravenna",
            "comune": "RAVENNA",
            "foglio": "101",
            "particella": "2",
            "sezione": None,
            "subalterno": None,
            "cost_text": None,
            "cost_value": None,
            "requested_at": "2026-04-12T12:00:00",
            "responded_at": "2026-04-12T12:05:00",
            "success": True,
            "status": "completed",
            "data": {
                "steps": [
                    {"step": "visura", "status": "completed", "data": {"immobili": [{"Foglio": "101"}]}},
                ],
                "total_results": 1,
            },
            "error": None,
            "page_visits": [],
        }

    async def _get_docs(_id, **kwargs):
        return []

    monkeypatch.setattr(web, "get_workflow_result_record", _get_workflow_record)
    monkeypatch.setattr(web, "get_documents_for_response", _get_docs)

    resp = await web.web_workflow_detail(request, "wf_detail_001", user=None)
    ctx = resp["context"]
    assert resp["template"] == "workflow_detail.html"
    result = ctx["result"]
    assert result["status"] == "completed"
    assert result["requested_at_display"] == "2026-04-12 12:00"
    step_sections = [s for s in result["sections"] if s["kind"] == "workflow_steps"]
    assert len(step_sections) == 1


@pytest.mark.asyncio
async def test_web_workflow_detail_not_found(monkeypatch):
    """Workflow detail returns 404 for unknown workflow."""
    request = _fake_request()

    async def _get_workflow_record(_id):
        return None

    monkeypatch.setattr(web, "get_workflow_result_record", _get_workflow_record)

    resp = await web.web_workflow_detail(request, "wf_missing", user=None)
    assert resp["context"]["result"] is None


# ---------------------------------------------------------------------------
# Status filter edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_results_status_filter_passed_to_count(monkeypatch):
    """Status filter is passed to both find and count functions."""
    request = _fake_request()
    count_kwargs = {}

    async def _find(**kwargs):
        return []

    async def _count(**kwargs):
        return {"total_requests": 0, "total_responses": 0, "successful": 0, "failed": 0, "partial": 0, "pending": 0}

    async def _count_total(**kwargs):
        count_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr(web, "find_result_rows", _find)
    monkeypatch.setattr(web, "count_result_rows", _count)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    response = await web.web_results(request, user=None, status="failed", source="workflow")
    assert count_kwargs["status"] == "failed"
    assert count_kwargs["source"] == "workflow"
    assert "no_match" in response["context"]["stats_urls"]["no_match"]

    await web.web_results(request, user=None, status="no_match", source="single")
    assert count_kwargs["status"] == "no_match"


def test_results_page_renders_mobile_cards_and_optional_columns(main_module, monkeypatch):
    """The server-rendered page exposes cards on mobile and keeps optional data out of the default table."""
    from fastapi.testclient import TestClient

    row = _make_result_rows(1)[0]
    row.update(
        {
            "status": "no_match",
            "success": False,
            "request_type_label": "Visura per soggetto",
            "request_type_description": "Descrizione breve della query",
            "request_type_tags": ["Catasto"],
            "person_details": "Mario Rossi",
            "fiscal_code": "RSSMRA85T10A562S",
            "property_count": 2,
            "owner_count": 1,
        }
    )

    async def _find(**_kwargs):
        return [row]

    async def _count(**_kwargs):
        return {
            "total_requests": 1,
            "total_responses": 1,
            "successful": 0,
            "failed": 0,
            "no_match": 1,
            "partial": 0,
            "pending": 0,
        }

    async def _count_total(**_kwargs):
        return 1

    monkeypatch.setattr(web, "find_result_rows", _find)
    monkeypatch.setattr(web, "count_result_rows", _count)
    monkeypatch.setattr(web, "count_total_result_rows", _count_total)

    client = TestClient(main_module.app)
    response = client.get("/results")

    assert response.status_code == 200
    html = response.text
    assert "results-mobile-cards" in html
    assert "Richieste eseguite" in html
    assert 'data-copy-request-id="req_0"' in html
    assert "Scegli colonne" in html
    assert 'data-result-column="request_type_tags"' in html
    assert 'data-result-column="extracted_results"' in html
    assert "Nessuna corrispondenza" in html
    assert "/static/css/pages/results.css?v=20261030a" in html
    assert html.count("visible: false") == 10
    columns = html.split("columns: [", 1)[1].split("\n        ],", 1)[0]
    assert re.findall(r'\{title: "([^"]+)"', columns)[:7] == [
        "Data",
        "Stato",
        "Tipo richiesta",
        "Soggetto / immobile",
        "Comune / provincia",
        "Rif. richiesta",
        "Origine",
    ]
    for title in ("Data", "Stato"):
        descriptor = re.search(r'\{title: "' + title + r'"[^}]*\}', columns)
        assert descriptor and "frozen: true" in descriptor.group(0)
    css = client.get("/static/css/pages/results.css")
    assert css.status_code == 200
    assert "@media (max-width: 767.98px)" in css.text
    assert ".results-table-card" in css.text


@pytest.mark.asyncio
async def test_document_favorite_state_is_loaded_for_the_signed_in_user(monkeypatch):
    from sister import database

    user = SimpleNamespace(id="demo-id", username="demo", email="demo@example.com")
    doc = {"id": 1886}
    called = {}

    async def _favorite_ids(user_key):
        called["user_key"] = user_key
        return {1886, 1885}

    monkeypatch.setattr(database, "get_document_favorite_ids", _favorite_ids)

    enabled = await web._load_document_favorite_state(doc, user)

    assert enabled is True
    assert called["user_key"] == "id:demo-id"
    assert doc["favorites_enabled"] is True
    assert doc["is_favorite"] is True


@pytest.mark.asyncio
async def test_result_detail_filter_clear_controls_are_labeled(main_module, monkeypatch):
    from starlette.requests import Request

    async def _get_result(_request_id):
        return {
            "request_id": "req_accessibility",
            "request_type": "visura",
            "tipo_catasto": "F",
            "provincia": "Trieste",
            "comune": "TRIESTE",
            "foglio": "9",
            "particella": "166",
            "requested_at": "2026-10-30T10:00:00",
            "responded_at": "2026-10-30T10:00:01",
            "success": True,
            "status": "completed",
            "data": {},
            "page_visits": [],
        }

    async def _get_docs(_request_id, **_kwargs):
        return []

    async def _get_properties(_request_id):
        return [{"subject_fiscal_code": "RSSMRA85T10A562S"}]

    async def _get_owners(_request_id):
        return [{"fiscal_code": "RSSMRA85T10A562S"}]

    monkeypatch.setattr(web, "get_result_record", _get_result)
    monkeypatch.setattr(web, "get_documents_for_response", _get_docs)
    monkeypatch.setattr(web, "get_db_properties_for_response", _get_properties)
    monkeypatch.setattr(web, "get_db_owners_for_response", _get_owners)

    rendered_context = {}

    class _CaptureTheme:
        def render(self, template_name, _request, **context):
            rendered_context.update(context)
            return {"template": template_name}

    template_env = main_module.app.state.theme_setup.templates.env
    # Patching the proxy attribute makes pytest teardown remove the backing key.
    monkeypatch.setitem(main_module.app.state._state, "theme_setup", _CaptureTheme())

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/results/req_accessibility",
            "raw_path": b"/results/req_accessibility",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": main_module.app,
        }
    )
    response = await web.web_result_detail(request, "req_accessibility", user=None)

    assert response["template"] == "result_detail.html"
    assert rendered_context["result"]["db_properties"]
    assert rendered_context["result"]["db_owners"]
    template_source, _, _ = template_env.loader.get_source(template_env, "result_detail.html")
    template_env.get_template("result_detail.html")  # compile the real template
    for control_id, label in (
        ("prop-filter-clear", "Rimuovi il filtro sui titolari"),
        ("owners-filter-clear", "Rimuovi il filtro sugli immobili"),
    ):
        match = re.search(r'<button\b(?:(?!>).)*\bid="' + control_id + r'"(?:(?!>).)*>', template_source, re.DOTALL)
        assert match and f'aria-label="{label}"' in match.group(0)


# ---------------------------------------------------------------------------
# count_total_result_rows
# ---------------------------------------------------------------------------


class TestCountTotalResultRows:
    """SQL-based filtered counting with an isolated session fake."""

    class _QueryResult:
        def scalar_one(self):
            return 7

    class _Session:
        def __init__(self):
            self.statements = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, statement):
            self.statements.append(statement)
            return TestCountTotalResultRows._QueryResult()

    @pytest.mark.asyncio
    async def test_workflow_source_returns_zero_without_querying_sister_db(self, monkeypatch):
        from sister import database

        def _unexpected_factory():
            raise AssertionError("workflow results are owned by opendata")

        monkeypatch.setattr(database, "_get_session_factory", _unexpected_factory)
        result = await database.count_total_result_rows(source="workflow")
        assert result == 0

    @pytest.mark.asyncio
    async def test_invalid_status_is_ignored(self, monkeypatch):
        from sister import database

        session = self._Session()
        monkeypatch.setattr(database, "_get_session_factory", lambda: lambda: session)
        total_all = await database.count_total_result_rows()
        total_bogus = await database.count_total_result_rows(status="bogus_status")
        assert total_all == total_bogus == 7
        assert len(session.statements) == 2
        assert str(session.statements[0]) == str(session.statements[1])

    @pytest.mark.asyncio
    async def test_invalid_source_is_normalized_to_all_sources(self, monkeypatch):
        from sister import database

        session = self._Session()
        monkeypatch.setattr(database, "_get_session_factory", lambda: lambda: session)
        total_all = await database.count_total_result_rows()
        total_bogus = await database.count_total_result_rows(source="bogus_source")
        assert total_all == total_bogus == 7
        assert len(session.statements) == 2
        assert str(session.statements[0]) == str(session.statements[1])


# ---------------------------------------------------------------------------
# is_db_writable
# ---------------------------------------------------------------------------


class TestIsDbWritable:
    """Database write-mode detection."""

    def test_writable_path(self, tmp_path):
        from sister import database

        db_file = tmp_path / "test.sqlite"
        db_file.touch()
        old = database._db_writable
        database._db_writable = None  # reset cache
        database.DB_PATH = str(db_file)
        try:
            assert database.is_db_writable() is True
        finally:
            database._db_writable = old

    def test_nonexistent_writable_parent(self, tmp_path):
        from sister import database

        old = database._db_writable
        database._db_writable = None
        database.DB_PATH = str(tmp_path / "new.sqlite")
        try:
            assert database.is_db_writable() is True
        finally:
            database._db_writable = old


@pytest.mark.asyncio
async def test_building_3d_geocodes_the_address_and_builds_a_google_earth_link(monkeypatch):
    async def _geocode(query):
        assert query == "Via El Alamein, 48124 Ravenna"
        return {"lat": 44.406153, "lng": 12.196336, "label": "Via El Alamein, Ravenna", "kind": "residential"}

    monkeypatch.setattr(web, "_geocode_address", _geocode)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")

    out = await web.web_building_3d(_fake_request(), q="Via El Alamein, 48124 Ravenna", user=None)

    assert out["template"] == "building.html"
    assert out["context"]["place"]["lat"] == 44.406153
    assert out["context"]["maps_api_key"] == ""
    assert out["context"]["earth_url"].startswith("https://earth.google.com/web/@44.4061530,12.1963360,")


@pytest.mark.asyncio
async def test_building_3d_explicit_coordinates_skip_geocoding(monkeypatch):
    async def _boom(_query):
        raise AssertionError("geocoder must not be called")

    monkeypatch.setattr(web, "_geocode_address", _boom)
    out = await web.web_building_3d(_fake_request(), q="Palazzo", lat=44.4, lng=12.2, user=None)
    assert out["context"]["place"]["kind"] == "coordinate"

    out = await web.web_building_3d(_fake_request(), q="", lat=999.0, lng=12.2, user=None)
    assert out["context"]["place"] is None and out["context"]["earth_url"] == ""


@pytest.mark.asyncio
async def test_building_3d_kmz_route_returns_a_kmz_and_maps_errors(monkeypatch):
    import io
    import zipfile

    from fastapi import HTTPException

    import sister.building_kmz as building_kmz

    way = {
        "type": "way",
        "id": 1,
        "tags": {"building": "yes", "height": "12"},
        "geometry": [
            {"lat": 44.4060, "lon": 12.1960},
            {"lat": 44.4060, "lon": 12.1962},
            {"lat": 44.4062, "lon": 12.1962},
            {"lat": 44.4060, "lon": 12.1960},
        ],
    }

    async def _fetch(lat, lng, radius=80, tries=3):
        return {"elements": [way]}

    monkeypatch.setattr(building_kmz, "fetch_buildings", _fetch)
    response = await web.web_building_3d_kmz(lat=44.4061, lng=12.1961, name="Via El Alamein, Ravenna", user=None)
    assert response.media_type == "application/vnd.google-earth.kmz"
    assert 'filename="via_el_alamein_ravenna_3d.kmz"' in response.headers["content-disposition"]
    kml = zipfile.ZipFile(io.BytesIO(response.body)).read("doc.kml").decode()
    assert "<extrude>1</extrude>" in kml and ",12.0" in kml

    async def _down(*_a, **_k):
        raise building_kmz.OverpassUnavailable("Timeout")

    monkeypatch.setattr(building_kmz, "fetch_buildings", _down)
    with pytest.raises(HTTPException) as err:
        await web.web_building_3d_kmz(lat=44.4, lng=12.2, user=None)
    assert err.value.status_code == 503
    with pytest.raises(HTTPException) as err:
        await web.web_building_3d_kmz(lat=99.0, lng=12.2, user=None)
    assert err.value.status_code == 422
