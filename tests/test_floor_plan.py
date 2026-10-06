"""Tests for the optional Ocular floor-plan validation feature."""

import pytest

from sister.floor_plan import (
    FloorPlanFeatureUnavailable,
    extract_visura_area_m2,
    validate_visura_against_floor_plan,
)


def test_extract_visura_area_from_response_payload():
    visura = {
        "data": {
            "immobili": [
                {"Foglio": "9", "SuperficieF": {"Totale": "84,50", "TotaleE": "3,00"}},
            ]
        }
    }

    assert extract_visura_area_m2(visura) == pytest.approx(84.5)


def test_extract_visura_area_selects_property():
    visura = {"immobili": [{"Superficie": "50,00"}, {"Superficie": "72,25"}]}

    assert extract_visura_area_m2(visura, property_index=1) == pytest.approx(72.25)


def test_extract_visura_area_from_single_immobile_payload():
    assert extract_visura_area_m2({"immobile": {"Superficie": "61,20"}}) == pytest.approx(61.2)


@pytest.mark.asyncio
async def test_validation_returns_match_with_injected_engine(tmp_path):
    plan = tmp_path / "plan.pdf"
    plan.write_bytes(b"not rendered by the fake engine")

    class FakeEngine:
        async def execute_workflow(self, **kwargs):
            assert kwargs["workflow_id"] == "floor-plan-ocr"
            assert kwargs["save_to_db"] is False
            return {"status": "completed", "output_data": {"total_area_m2": 85.0}}

    result = await validate_visura_against_floor_plan(
        visura={"immobili": [{"Superficie": "84,50"}]},
        floor_plan_path=plan,
        engine=FakeEngine(),
    )

    assert result["comparison"]["status"] == "match"
    assert result["comparison"]["difference_m2"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_validation_returns_mismatch_when_outside_tolerance(tmp_path):
    plan = tmp_path / "plan.png"
    plan.write_bytes(b"image")

    class FakeEngine:
        async def execute_workflow(self, **kwargs):
            return {"status": "completed", "output_data": {"total_area_m2": 100}}

    result = await validate_visura_against_floor_plan(
        visura={"immobili": [{"Superficie": "80"}]},
        floor_plan_path=plan,
        engine=FakeEngine(),
        tolerance_m2=1,
        tolerance_percent=0.05,
    )

    assert result["comparison"]["status"] == "mismatch"
    assert result["comparison"]["difference_percent"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_missing_optional_dependency_has_actionable_error(tmp_path, monkeypatch):
    plan = tmp_path / "plan.png"
    plan.write_bytes(b"image")
    monkeypatch.setitem(__import__("sys").modules, "ocular", None)

    with pytest.raises(FloorPlanFeatureUnavailable, match=r"sister\[floor-plan-ocr\]"):
        await validate_visura_against_floor_plan(
            visura={"immobili": [{"Superficie": "80"}]},
            floor_plan_path=plan,
        )
