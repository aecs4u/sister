"""Tests for the workflow module (workflows.py).

Tests cover:
  - WorkflowInput validation
  - Property normalization and deduplication
  - Aggregation logic
  - Workflow API endpoint via TestClient
  - CLI workflow command via CliRunner
"""

import pytest
from pydantic import ValidationError

from sister.models import WORKFLOW_PRESETS, WorkflowInput
from sister.workflows import (
    _build_aggregate,
    _deduplicate_properties,
    _normalize_property,
    _step_key,
)

# ---------------------------------------------------------------------------
# WorkflowInput validation
# ---------------------------------------------------------------------------


class TestWorkflowInput:
    def test_valid_due_diligence(self):
        wf = WorkflowInput(
            preset="due-diligence",
            provincia="Roma",
            comune="ROMA",
            foglio="100",
            particella="50",
        )
        assert wf.preset == "due-diligence"
        assert wf.include_paid_steps is False

    def test_preset_normalized(self):
        wf = WorkflowInput(
            preset="Due-Diligence",
            provincia="Roma",
            comune="ROMA",
            foglio="100",
            particella="50",
        )
        assert wf.preset == "due-diligence"

    def test_unknown_preset_rejected(self):
        with pytest.raises(ValidationError):
            WorkflowInput(preset="nonexistent", provincia="Roma")

    def test_patrimonio_preset(self):
        wf = WorkflowInput(preset="patrimonio", codice_fiscale="RSSMRI85E28H501E")
        assert wf.preset == "patrimonio"
        assert wf.codice_fiscale == "RSSMRI85E28H501E"

    def test_include_paid_steps_flag(self):
        wf = WorkflowInput(
            preset="due-diligence",
            provincia="Roma",
            comune="ROMA",
            foglio="100",
            particella="50",
            include_paid_steps=True,
            auto_confirm=True,
        )
        assert wf.include_paid_steps is True
        assert wf.auto_confirm is True

    def test_all_presets_defined(self):
        expected = {
            "due-diligence",
            "patrimonio",
            "fondiario",
            "aziendale",
            "storico",
            "indirizzo",
            "cross-reference",
            "full-due-diligence",
            "full-patrimonio",
            "full-aziendale",
        }
        assert set(WORKFLOW_PRESETS.keys()) == expected

    def test_each_preset_has_steps_and_requires(self):
        for name, defn in WORKFLOW_PRESETS.items():
            assert "steps" in defn, f"Preset '{name}' missing 'steps'"
            assert "requires" in defn, f"Preset '{name}' missing 'requires'"
            assert isinstance(defn["steps"], list)
            assert isinstance(defn["requires"], list)
            assert len(defn["steps"]) > 0

    def test_due_diligence_includes_elaborato(self):
        steps = WORKFLOW_PRESETS["due-diligence"]["steps"]
        assert "elaborato_planimetrico" in steps

    def test_patrimonio_includes_drill_and_address(self):
        steps = WORKFLOW_PRESETS["patrimonio"]["steps"]
        assert "drill_intestati" in steps
        assert "indirizzo_reverse" in steps

    def test_fondiario_includes_export_mappa(self):
        steps = WORKFLOW_PRESETS["fondiario"]["steps"]
        assert "export_mappa" in steps
        assert "elaborato_planimetrico" in steps

    def test_storico_includes_nota(self):
        steps = WORKFLOW_PRESETS["storico"]["steps"]
        assert "nota" in steps

    def test_cross_reference_includes_cross_property(self):
        steps = WORKFLOW_PRESETS["cross-reference"]["steps"]
        assert "cross_property_intestati" in steps

    def test_depth_filtering(self):
        """Verify that paid steps are at deep or full depth."""
        from sister.models import STEP_METADATA, _DEPTH_ORDER

        for step_name, meta in STEP_METADATA.items():
            if meta["paid"]:
                assert (
                    _DEPTH_ORDER[meta["depth"]] >= _DEPTH_ORDER["deep"]
                ), f"Paid step '{step_name}' should be depth>=deep, got {meta['depth']}"

    def test_depth_validation(self):
        wf = WorkflowInput(
            preset="due-diligence",
            provincia="Roma",
            comune="ROMA",
            foglio="1",
            particella="1",
            depth="light",
        )
        assert wf.depth == "light"

    def test_invalid_depth_rejected(self):
        with pytest.raises(ValidationError):
            WorkflowInput(preset="due-diligence", provincia="X", depth="ultra")


# ---------------------------------------------------------------------------
# Property normalization
# ---------------------------------------------------------------------------


class TestNormalizeProperty:
    def test_full_row(self):
        row = {"Provincia": "Roma", "Comune": "ROMA", "Foglio": "100", "Particella": "50", "Sub": "3"}
        result = _normalize_property(row)
        assert result == {
            "provincia": "Roma",
            "comune": "ROMA",
            "foglio": "100",
            "particella": "50",
            "subalterno": "3",
            "tipo_catasto": None,
            "sezione": None,
        }

    def test_missing_foglio_returns_none(self):
        row = {"Provincia": "Roma", "Comune": "ROMA", "Foglio": "", "Particella": "50"}
        assert _normalize_property(row) is None

    def test_empty_row_returns_none(self):
        assert _normalize_property({}) is None

    def test_strips_whitespace(self):
        row = {"Provincia": " Roma ", "Comune": " ROMA ", "Foglio": " 100 ", "Particella": " 50 "}
        result = _normalize_property(row)
        assert result["provincia"] == "Roma"
        assert result["foglio"] == "100"


class TestDeduplicateProperties:
    def test_removes_duplicates(self):
        props = [
            {"provincia": "Roma", "comune": "ROMA", "foglio": "1", "particella": "1", "subalterno": None},
            {"provincia": "Roma", "comune": "ROMA", "foglio": "1", "particella": "1", "subalterno": None},
        ]
        result = _deduplicate_properties(props)
        assert len(result) == 1

    def test_keeps_different_subalterni(self):
        props = [
            {"provincia": "Roma", "comune": "ROMA", "foglio": "1", "particella": "1", "subalterno": "1"},
            {"provincia": "Roma", "comune": "ROMA", "foglio": "1", "particella": "1", "subalterno": "2"},
        ]
        result = _deduplicate_properties(props)
        assert len(result) == 2

    def test_empty_input(self):
        assert _deduplicate_properties([]) == []


# ---------------------------------------------------------------------------
# Step key
# ---------------------------------------------------------------------------


class TestStepKey:
    def test_simple(self):
        assert _step_key("search") == "search"

    def test_with_context(self):
        key = _step_key("intestati", tipo_catasto="F", subalterno="3")
        assert "intestati" in key
        assert "subalterno=3" in key
        assert "tipo_catasto=F" in key

    def test_none_values_excluded(self):
        key = _step_key("search", foglio=None, particella="50")
        assert "foglio" not in key
        assert "particella=50" in key


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestBuildAggregate:
    def test_empty_steps(self):
        result = _build_aggregate([])
        assert result["properties"] == []
        assert result["owners"] == []
        assert result["links"] == []
        assert result["addresses"] == []
        assert result["risk_flags"] == []

    def test_collects_immobili(self):
        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": "1", "Particella": "10", "Sub": ""},
                        {"Foglio": "1", "Particella": "20", "Sub": "3"},
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["properties"]) == 2

    def test_deduplicates_immobili(self):
        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": "1", "Particella": "10", "Sub": ""},
                        {"Foglio": "1", "Particella": "10", "Sub": ""},
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["properties"]) == 1

    def test_collects_intestati(self):
        steps = [
            {
                "step": "intestati",
                "status": "completed",
                "data": {
                    "intestati": [
                        {"Codice fiscale": "ABC123", "Nominativo": "Mario Rossi"},
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["owners"]) == 1
        assert len(result["links"]) == 1

    def test_skips_error_steps(self):
        steps = [
            {"step": "search", "status": "error", "data": None, "error": "fail"},
        ]
        result = _build_aggregate(steps)
        assert result["properties"] == []
        assert result["owners"] == []
        assert result["links"] == []

    def test_risk_flag_missing_cf(self):
        steps = [
            {
                "step": "intestati",
                "status": "completed",
                "data": {
                    "intestati": [
                        {"Nominativo": "Mario Rossi"},  # no CF
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert any(f["type"] == "missing_cf" for f in result["risk_flags"])

    def test_risk_flag_multiple_owners(self):
        steps = [
            {
                "step": "drill_intestati",
                "status": "completed",
                "data": {
                    "drill_results": [
                        {
                            "property": {"foglio": "1", "particella": "10", "subalterno": ""},
                            "intestati": [
                                {"Codice fiscale": "AAA", "Nominativo": "A"},
                                {"Codice fiscale": "BBB", "Nominativo": "B"},
                            ],
                            "status": "completed",
                        },
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert any(f["type"] == "multiple_owners" for f in result["risk_flags"])

    def test_aggregate_includes_addresses(self):
        steps = [
            {
                "step": "indirizzo_reverse",
                "status": "completed",
                "data": {
                    "addresses": [
                        {"property": {"foglio": "1", "particella": "10"}, "addresses": ["VIA ROMA 1"]},
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["addresses"]) == 1

    def test_aggregate_collects_owner_portfolios(self):
        steps = [
            {
                "step": "cross_property_intestati",
                "status": "completed",
                "data": {
                    "owner_portfolios": [
                        {
                            "codice_fiscale": "ABC123",
                            "immobili": [
                                {"Foglio": "5", "Particella": "20", "Sub": ""},
                            ],
                            "status": "completed",
                        },
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["properties"]) == 1
        assert any(l["owner_key"] == "ABC123" for l in result["links"])

    def test_drill_down_results(self):
        steps = [
            {
                "step": "drill_intestati",
                "status": "completed",
                "data": {
                    "drill_results": [
                        {
                            "property": {"foglio": "1", "particella": "10", "subalterno": "3"},
                            "intestati": [{"Codice fiscale": "XYZ789", "Nominativo": "Luigi Bianchi"}],
                            "status": "completed",
                        },
                    ],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["properties"]) == 1
        assert len(result["owners"]) == 1
        assert any("drill:" in link["source_step"] for link in result["links"])


# ---------------------------------------------------------------------------
# Workflow preset validation (without server)
# ---------------------------------------------------------------------------


class TestStepExecutorRegistry:
    """Verify all steps referenced in presets have executors."""

    def test_all_preset_steps_have_executors(self):
        from sister.workflows import _STEP_EXECUTORS

        for name, defn in WORKFLOW_PRESETS.items():
            for step in defn["steps"]:
                assert step in _STEP_EXECUTORS, f"Step '{step}' in preset '{name}' has no executor"

    def test_executor_count(self):
        from sister.workflows import _STEP_EXECUTORS

        # 18 browser + 3 analytical + 4 multi-hop = 25
        assert len(_STEP_EXECUTORS) >= 25


class TestWorkflowPresetValidation:
    """Verify preset validation without a running server."""

    def test_unknown_preset_rejected_by_model(self):
        with pytest.raises(ValidationError, match="Unknown preset"):
            WorkflowInput(preset="nonexistent")

    def test_due_diligence_includes_paid_in_steps(self):
        """Paid steps are in the steps list but filtered by depth/paid flags at runtime."""
        defn = WORKFLOW_PRESETS["due-diligence"]
        assert "ispezione_ipotecaria" in defn["steps"]

    def test_storico_includes_paid_in_steps(self):
        defn = WORKFLOW_PRESETS["storico"]
        assert "ispezione_ipotecaria" in defn["steps"]

    def test_risk_score_in_all_presets(self):
        """risk_score should be the last step in every preset."""
        for name, defn in WORKFLOW_PRESETS.items():
            assert "risk_score" in defn["steps"], f"Preset '{name}' missing risk_score"

    def test_timeline_build_in_document_presets(self):
        for name in ("due-diligence", "storico"):
            assert "timeline_build" in WORKFLOW_PRESETS[name]["steps"]

    def test_owner_expand_in_intestati_presets(self):
        for name in ("due-diligence", "patrimonio", "aziendale", "storico", "indirizzo"):
            assert "owner_expand" in WORKFLOW_PRESETS[name]["steps"], f"Preset '{name}' missing owner_expand"


# ---------------------------------------------------------------------------
# CLI workflow command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Analytical step executors (no browser)
# ---------------------------------------------------------------------------


class TestTimelineBuild:
    """Test _exec_timeline_build post-processing step."""

    def test_empty_results(self):
        import asyncio
        from sister.workflows import _exec_timeline_build

        result = asyncio.run(_exec_timeline_build(None, {}, []))
        assert result["total_events"] == 0
        assert result["timeline"] == []
        assert result["gaps"] == []

    def test_extracts_dated_events(self):
        import asyncio
        from sister.workflows import _exec_timeline_build

        steps = [
            {
                "step": "nota",
                "status": "completed",
                "data": {
                    "risultati": [
                        {"Data": "15/03/2020", "Tipo": "Compravendita", "Nota": "123"},
                        {"Data": "20/06/2018", "Tipo": "Donazione", "Nota": "456"},
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_timeline_build(None, {}, steps))
        assert result["total_events"] == 2
        assert result["dated_events"] == 2
        # Should be sorted chronologically (2018 before 2020)
        assert result["timeline"][0]["date"] == "20/06/2018"
        assert result["timeline"][1]["date"] == "15/03/2020"

    def test_detects_gaps(self):
        import asyncio
        from sister.workflows import _exec_timeline_build

        steps = [
            {
                "step": "originali",
                "status": "completed",
                "data": {
                    "risultati": [
                        {"Data": "01/01/2010", "Tipo": "Registrazione"},
                        {"Data": "01/01/2020", "Tipo": "Aggiornamento"},
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_timeline_build(None, {}, steps))
        assert result["total_gaps"] >= 1
        assert result["gaps"][0]["gap_years"] == 10

    def test_skips_non_completed(self):
        import asyncio
        from sister.workflows import _exec_timeline_build

        steps = [
            {"step": "nota", "status": "error", "data": None, "error": "fail"},
        ]
        result = asyncio.run(_exec_timeline_build(None, {}, steps))
        assert result["total_events"] == 0


class TestRiskScore:
    """Test _exec_risk_score post-processing step."""

    def test_empty_results(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        result = asyncio.run(_exec_risk_score(None, {}, []))
        assert result["total_flags"] == 0
        assert result["total_properties"] == 0
        assert result["total_owners"] == 0

    def test_flags_missing_cf(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {
                "step": "intestati",
                "status": "completed",
                "data": {
                    "intestati": [
                        {"Nominativo": "Mario Rossi"},  # no CF
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_risk_score(None, {}, steps))
        assert any(f["type"] == "missing_cf" for f in result["risk_flags"])

    def test_flags_multiple_owners(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {
                "step": "drill_intestati",
                "status": "completed",
                "data": {
                    "drill_results": [
                        {
                            "property": {"foglio": "1", "particella": "10", "subalterno": ""},
                            "intestati": [
                                {"Codice fiscale": "AAABBB80C01H501A"},
                                {"Codice fiscale": "CCCDDD90E02H501B"},
                            ],
                            "status": "completed",
                        },
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_risk_score(None, {}, steps))
        assert any(f["type"] == "multiple_owners" for f in result["risk_flags"])

    def test_flags_ownership_mismatch(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {"step": "soggetto", "status": "completed", "data": {"immobili": []}},
            {
                "step": "intestati",
                "status": "completed",
                "data": {
                    "intestati": [
                        {"Codice fiscale": "AAABBB80C01H501A"},  # not in soggetto source
                    ],
                },
            },
        ]
        params = {"codice_fiscale": "XXXYYYZZZ00A00A"}
        result = asyncio.run(_exec_risk_score(None, params, steps))
        assert any(f["type"] == "ownership_mismatch" for f in result["risk_flags"])

    def test_geographic_concentration(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": str(i), "Particella": str(i), "Sub": "", "Provincia": "Roma", "Comune": "ROMA"}
                        for i in range(60)
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_risk_score(None, {}, steps))
        assert any(f["type"] == "high_property_count" for f in result["risk_flags"])
        assert len(result["geographic_concentration"]) > 0

    def test_severity_counts(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {
                "step": "intestati",
                "status": "completed",
                "data": {
                    "intestati": [
                        {"Nominativo": "No CF Person"},
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_risk_score(None, {}, steps))
        assert "severity_counts" in result
        assert result["severity_counts"]["medium"] > 0

    def test_timeline_gaps_propagated(self):
        import asyncio
        from sister.workflows import _exec_risk_score

        steps = [
            {
                "step": "timeline_build",
                "status": "completed",
                "data": {
                    "gaps": [{"from_date": "01/01/2010", "to_date": "01/01/2020", "gap_years": 10}],
                    "timeline": [],
                    "total_events": 0,
                    "dated_events": 0,
                    "undated_events": 0,
                    "total_gaps": 1,
                },
            },
        ]
        result = asyncio.run(_exec_risk_score(None, {}, steps))
        assert any(f["type"] == "timeline_gap" for f in result["risk_flags"])


class TestWhenClauses:
    """Test that STEP_METADATA when-clauses work correctly."""

    def test_mappa_skipped_without_foglio(self):
        from sister.models import STEP_METADATA

        meta = STEP_METADATA["mappa"]
        assert not meta["when"]([], {"foglio": None})
        assert meta["when"]([], {"foglio": "100"})

    def test_nota_skipped_without_numero_nota(self):
        from sister.models import STEP_METADATA

        meta = STEP_METADATA["nota"]
        assert not meta["when"]([], {"numero_nota": None})
        assert meta["when"]([], {"numero_nota": "12345"})

    def test_timeline_build_skipped_without_document_steps(self):
        from sister.models import STEP_METADATA

        meta = STEP_METADATA["timeline_build"]
        # No completed document steps → skip
        empty_results = [{"step": "search", "status": "completed", "data": {}}]
        assert not meta["when"](empty_results, {})
        # With nota → run
        with_nota = [{"step": "nota", "status": "completed", "data": {"risultati": []}}]
        assert meta["when"](with_nota, {})

    def test_owner_expand_skipped_without_intestati(self):
        from sister.models import STEP_METADATA

        meta = STEP_METADATA["owner_expand"]
        empty = [{"step": "search", "status": "completed", "data": {"immobili": []}}]
        assert not meta["when"](empty, {})
        with_intestati = [{"step": "intestati", "status": "completed", "data": {"intestati": [{"CF": "X"}]}}]
        assert meta["when"](with_intestati, {})

    def test_risk_score_has_no_when(self):
        from sister.models import STEP_METADATA

        assert "when" not in STEP_METADATA["risk_score"]


class TestAggregateWithNewSteps:
    """Test aggregation with timeline_build and risk_score outputs."""

    def test_timeline_propagated_to_aggregate(self):
        events = [{"source_step": "nota", "date": "01/01/2020", "details": {}}]
        steps = [
            {
                "step": "timeline_build",
                "status": "completed",
                "data": {
                    "timeline": events,
                    "total_events": 1,
                    "dated_events": 1,
                    "undated_events": 0,
                    "gaps": [],
                    "total_gaps": 0,
                },
            },
        ]
        result = _build_aggregate(steps)
        assert result["timeline"] == events

    def test_risk_scores_merged_to_aggregate(self):
        flags = [{"type": "missing_cf", "severity": "medium", "owner": "Test"}]
        steps = [
            {
                "step": "risk_score",
                "status": "completed",
                "data": {
                    "risk_flags": flags,
                    "total_flags": 1,
                    "severity_counts": {"medium": 1},
                    "total_properties": 0,
                    "total_owners": 0,
                    "geographic_concentration": [],
                },
            },
        ]
        result = _build_aggregate(steps)
        assert any(f["type"] == "missing_cf" for f in result["risk_flags"])
        assert result["risk_scores"]["total_flags"] == 1

    def test_owner_expand_properties_in_aggregate(self):
        steps = [
            {
                "step": "owner_expand",
                "status": "completed",
                "data": {
                    "owner_entities": [{"codice_fiscale": "ABC", "status": "completed"}],
                    "discovered_properties": [
                        {"provincia": "Roma", "comune": "ROMA", "foglio": "99", "particella": "1", "subalterno": None},
                    ],
                    "total_discovered": 1,
                    "total_owners": 1,
                    "truncated": False,
                },
            },
        ]
        result = _build_aggregate(steps)
        assert len(result["properties"]) == 1


# ---------------------------------------------------------------------------
# Multi-hop step executors
# ---------------------------------------------------------------------------


class TestPropertyRank:
    """Test _exec_property_rank scoring."""

    def test_empty_results(self):
        import asyncio
        from sister.workflows import _exec_property_rank

        result = asyncio.run(_exec_property_rank(None, {}, []))
        assert result["total"] == 0

    def test_scores_same_location_higher(self):
        import asyncio
        from sister.workflows import _exec_property_rank

        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": "1", "Particella": "10", "Sub": "", "Provincia": "Roma", "Comune": "ROMA"},
                        {"Foglio": "2", "Particella": "20", "Sub": "", "Provincia": "Milano", "Comune": "MILANO"},
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_property_rank(None, {"provincia": "Roma", "comune": "ROMA"}, steps))
        ranked = result["ranked_properties"]
        assert len(ranked) == 2
        # Roma property should score higher (same location as seed)
        assert ranked[0]["provincia"] == "Roma"
        assert ranked[0]["_score"] > ranked[1]["_score"]

    def test_multi_path_discovery_bonus(self):
        import asyncio
        from sister.workflows import _exec_property_rank

        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [{"Foglio": "1", "Particella": "10", "Sub": "", "Provincia": "X", "Comune": "Y"}],
                },
            },
            {
                "step": "owner_expand",
                "status": "completed",
                "data": {
                    "discovered_properties": [
                        {"provincia": "X", "comune": "Y", "foglio": "1", "particella": "10", "subalterno": None}
                    ],
                    "owner_entities": [],
                    "total_owners": 0,
                    "total_discovered": 1,
                    "truncated": False,
                },
            },
        ]
        result = asyncio.run(_exec_property_rank(None, {}, steps))
        # The property found via two paths should get a bonus
        ranked = result["ranked_properties"]
        assert len(ranked) == 1
        assert ranked[0]["_score"] > 10  # base is 10, multi-path adds 10

    def test_above_threshold_count(self):
        import asyncio
        from sister.workflows import _exec_property_rank

        steps = [
            {
                "step": "search",
                "status": "completed",
                "data": {
                    "immobili": [
                        {"Foglio": str(i), "Particella": str(i), "Sub": "", "Provincia": "X", "Comune": "Y"}
                        for i in range(5)
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_property_rank(None, {"min_property_score": 50}, steps))
        assert result["above_threshold"] == 0  # base score 10 < threshold 50


class TestPortfolioDrillIntestati:
    """Test that portfolio_drill_intestati requires property_rank output."""

    def test_skips_without_rank(self):
        import asyncio
        from sister.workflows import _exec_portfolio_drill_intestati

        result = asyncio.run(_exec_portfolio_drill_intestati(None, {}, []))
        assert result["skipped"]

    def test_skips_below_threshold(self):
        import asyncio
        from sister.workflows import _exec_portfolio_drill_intestati

        steps = [
            {
                "step": "property_rank",
                "status": "completed",
                "data": {
                    "ranked_properties": [
                        {
                            "provincia": "X",
                            "comune": "Y",
                            "foglio": "1",
                            "particella": "1",
                            "subalterno": None,
                            "_score": 5,
                            "_key": "X:Y:1:1:",
                        },
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_portfolio_drill_intestati(None, {"min_property_score": 30}, steps))
        assert result["skipped"]


class TestPortfolioIpotecaria:
    """Test paid step budget controls."""

    def test_skips_without_auto_confirm(self):
        import asyncio
        from sister.workflows import _exec_portfolio_ipotecaria

        steps = [
            {
                "step": "property_rank",
                "status": "completed",
                "data": {
                    "ranked_properties": [
                        {
                            "_score": 50,
                            "_key": "X:Y:1:1:",
                            "provincia": "X",
                            "comune": "Y",
                            "foglio": "1",
                            "particella": "1",
                        }
                    ],
                },
            },
        ]
        result = asyncio.run(_exec_portfolio_ipotecaria(None, {"auto_confirm": False}, steps))
        assert result["skipped"]

    def test_respects_max_paid_steps(self):
        import asyncio
        from sister.workflows import _exec_portfolio_ipotecaria

        steps = [
            {
                "step": "property_rank",
                "status": "completed",
                "data": {
                    "ranked_properties": [
                        {
                            "_score": 50,
                            "_key": f"X:Y:{i}:{i}:",
                            "provincia": "X",
                            "comune": "Y",
                            "foglio": str(i),
                            "particella": str(i),
                        }
                        for i in range(10)
                    ],
                },
            },
        ]
        # Already used 3 of 3 paid steps
        params = {"auto_confirm": True, "max_paid_steps": 3, "_paid_step_count": 3, "min_property_score": 30}
        result = asyncio.run(_exec_portfolio_ipotecaria(None, params, steps))
        assert result["total"] == 0  # no budget left


class TestMultiHopPresets:
    """Test full-* preset definitions."""

    def test_full_due_diligence_exists(self):
        assert "full-due-diligence" in WORKFLOW_PRESETS

    def test_full_patrimonio_exists(self):
        assert "full-patrimonio" in WORKFLOW_PRESETS

    def test_full_aziendale_exists(self):
        assert "full-aziendale" in WORKFLOW_PRESETS

    def test_full_presets_have_property_rank(self):
        for name in ("full-due-diligence", "full-patrimonio", "full-aziendale"):
            assert "property_rank" in WORKFLOW_PRESETS[name]["steps"]
            assert "portfolio_drill_intestati" in WORKFLOW_PRESETS[name]["steps"]

    def test_full_presets_end_with_risk_score(self):
        for name in ("full-due-diligence", "full-patrimonio", "full-aziendale"):
            steps = WORKFLOW_PRESETS[name]["steps"]
            assert steps[-1] == "risk_score"

    def test_full_presets_valid_input(self):
        wf = WorkflowInput(
            preset="full-due-diligence",
            provincia="Roma",
            comune="ROMA",
            foglio="1",
            particella="1",
            depth="full",
            max_paid_steps=5,
            max_owners=15,
        )
        assert wf.depth == "full"
        assert wf.max_paid_steps == 5


class TestBudgetControls:
    """Test that budget fields are validated."""

    def test_max_fanout_range(self):
        with pytest.raises(ValidationError):
            WorkflowInput(preset="due-diligence", provincia="X", max_fanout=0)
        with pytest.raises(ValidationError):
            WorkflowInput(preset="due-diligence", provincia="X", max_fanout=101)

    def test_max_paid_steps_range(self):
        wf = WorkflowInput(
            preset="due-diligence", provincia="X", comune="X", foglio="1", particella="1", max_paid_steps=0
        )
        assert wf.max_paid_steps == 0

    def test_full_depth_accepted(self):
        wf = WorkflowInput(
            preset="full-due-diligence", provincia="X", comune="X", foglio="1", particella="1", depth="full"
        )
        assert wf.depth == "full"

    def test_executor_count_with_multi_hop(self):
        from sister.workflows import _STEP_EXECUTORS

        assert len(_STEP_EXECUTORS) >= 25


class TestWorkflowCLI:
    """Test CLI workflow command via CliRunner (mocked client)."""

    def test_dry_run_with_preset(self):
        from typer.testing import CliRunner
        from sister.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "query",
                "workflow",
                "--preset",
                "due-diligence",
                "-P",
                "Roma",
                "-C",
                "ROMA",
                "-F",
                "100",
                "-p",
                "50",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert "due-diligence" in result.output

    def test_unknown_preset_exits_1(self):
        from typer.testing import CliRunner
        from sister.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "query",
                "workflow",
                "--preset",
                "nonexistent",
            ],
        )
        assert result.exit_code == 1
        assert "Unknown preset" in result.output

    def test_preset_calls_server(self, monkeypatch):
        """Verify preset workflow calls client.workflow()."""
        from typer.testing import CliRunner
        from sister.cli import app
        from sister.client import VisuraClient

        called_with = {}

        async def mock_workflow(self, **kwargs):
            called_with.update(kwargs)
            return {
                "preset": kwargs.get("preset"),
                "steps": [{"step": "search", "status": "completed", "data": {"immobili": [], "total": 0}}],
                "summary": {"completed": 1, "failed": 0, "skipped": 0, "total_steps": 1},
            }

        monkeypatch.setattr(VisuraClient, "workflow", mock_workflow)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "query",
                "workflow",
                "--preset",
                "fondiario",
                "-P",
                "Roma",
                "-C",
                "ROMA",
            ],
        )
        assert result.exit_code == 0
        assert called_with.get("preset") == "fondiario"

    def test_preset_forwards_budget_controls(self, monkeypatch):
        """Verify budget flags are forwarded to client.workflow()."""
        from typer.testing import CliRunner
        from sister.cli import app
        from sister.client import VisuraClient

        called_with = {}

        async def mock_workflow(self, **kwargs):
            called_with.update(kwargs)
            return {
                "preset": kwargs.get("preset"),
                "steps": [],
                "summary": {"completed": 0, "failed": 0, "skipped": 0, "total_steps": 0},
            }

        monkeypatch.setattr(VisuraClient, "workflow", mock_workflow)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "query",
                "workflow",
                "--preset",
                "full-due-diligence",
                "-P",
                "Roma",
                "-C",
                "ROMA",
                "-F",
                "1",
                "-p",
                "1",
                "--depth",
                "full",
                "--max-owners",
                "15",
                "--max-properties-per-owner",
                "25",
                "--max-history",
                "8",
                "--max-paid",
                "5",
                "--max-steps",
                "200",
                "--include-paid",
                "--yes",
            ],
        )
        assert result.exit_code == 0
        assert called_with.get("preset") == "full-due-diligence"
        assert called_with.get("depth") == "full"
        assert called_with.get("max_owners") == 15
        assert called_with.get("max_properties_per_owner") == 25
        assert called_with.get("max_historical_properties") == 8
        assert called_with.get("max_paid_steps") == 5
        assert called_with.get("max_total_steps") == 200
        assert called_with.get("auto_confirm") is True
        assert called_with.get("include_paid_steps") is True
