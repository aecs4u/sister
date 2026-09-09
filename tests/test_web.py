"""Tests for web UI route handlers."""

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


def _fake_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(theme_setup=_FakeTheme())),
        state=SimpleNamespace(user=None),
        url=SimpleNamespace(path="/web/results"),
    )


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
# /web/workflows routes
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

    await web.web_results(request, user=None, status="failed", source="workflow")
    assert count_kwargs["status"] == "failed"
    assert count_kwargs["source"] == "workflow"


# ---------------------------------------------------------------------------
# count_total_result_rows
# ---------------------------------------------------------------------------


class TestCountTotalResultRows:
    """SQL-based filtered counting."""

    @pytest.mark.asyncio
    async def test_returns_zero_for_missing_db(self, tmp_path, monkeypatch):
        from sister import database

        monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "nonexistent.sqlite"))
        result = await database.count_total_result_rows()
        assert result == 0

    @pytest.mark.asyncio
    async def test_invalid_status_ignored(self, tmp_path, monkeypatch):
        from sister import database

        monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.sqlite"))
        # Invalid status should be normalized to None (no filter) — both return 0 on empty DB
        total_all = await database.count_total_result_rows()
        total_bogus = await database.count_total_result_rows(status="bogus_status")
        assert total_all == total_bogus

    @pytest.mark.asyncio
    async def test_invalid_source_ignored(self, tmp_path, monkeypatch):
        from sister import database

        monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.sqlite"))
        total_all = await database.count_total_result_rows()
        total_bogus = await database.count_total_result_rows(source="bogus_source")
        assert total_all == total_bogus


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
