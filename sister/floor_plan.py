"""Optional Ocular floor-plan validation.

The SISTER package deliberately does not import :mod:`ocular` at module load
time.  Install ``sister[floor-plan-ocr]`` when the local Ocular workflow is
needed, then use :func:`validate_visura_against_floor_plan` to compare the
surface reported by a cadastral visura with the surface estimated from a
planimetry.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

OCULAR_FLOOR_PLAN_WORKFLOW = "floor-plan-ocr"


class FloorPlanFeatureUnavailable(RuntimeError):
    """Raised when the optional Ocular dependency is not installed."""


class FloorPlanValidationError(RuntimeError):
    """Raised when the Ocular workflow cannot produce a validation result."""


def _parse_area(value: Any) -> float | None:
    """Parse a positive area value, including common Italian decimal syntax."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip().replace(" ", "")
        if not text:
            return None
        # Visure normally use comma as the decimal separator.  When both
        # separators are present, dots are thousands separators.
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            result = float(text)
        except ValueError:
            return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _unwrap_visura(visura: Mapping[str, Any]) -> Mapping[str, Any]:
    data = visura.get("data")
    return data if isinstance(data, Mapping) else visura


def _surface_candidates(value: Any, path: tuple[str, ...] = ()) -> list[tuple[int, float, str]]:
    """Collect area-like values with a score based on their field name."""
    if not isinstance(value, Mapping):
        return []

    candidates: list[tuple[int, float, str]] = []
    for key, child in value.items():
        normalized = "".join(char for char in str(key).lower() if char.isalnum())
        parsed = _parse_area(child)
        if parsed is not None:
            score = {
                "superficie": 100,
                "superficief": 95,
                "totalarea": 90,
                "aream2": 85,
                "area": 80,
                "totale": 70,
            }.get(normalized, 0)
            if score:
                candidates.append((score, parsed, ".".join((*path, str(key)))))
        elif isinstance(child, Mapping):
            candidates.extend(_surface_candidates(child, (*path, str(key))))
    return candidates


def extract_visura_area_m2(visura: Mapping[str, Any], property_index: int = 0) -> float | None:
    """Extract the building surface from a SISTER visura payload.

    ``property_index`` selects an item from ``data.immobili`` when the visura
    contains more than one cadastral unit.  The explicit argument keeps the
    choice visible instead of silently comparing a floor plan with a random
    unit.
    """
    data = _unwrap_visura(visura)
    properties = data.get("immobili")
    selected: Any = data
    if isinstance(properties, list):
        if not properties or property_index < 0 or property_index >= len(properties):
            return None
        selected = properties[property_index]
    elif isinstance(data.get("immobile"), Mapping):
        selected = data["immobile"]
    candidates = _surface_candidates(selected)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _execution_output(execution: Any) -> tuple[str, dict[str, Any], str | None]:
    """Read an Ocular WorkflowExecution, while allowing simple test doubles."""
    if isinstance(execution, Mapping):
        status = str(execution.get("status", "completed"))
        output = execution.get("output_data") or execution.get("output") or {}
        error = execution.get("error_message") or execution.get("error")
    else:
        raw_status = getattr(execution, "status", "completed")
        status = getattr(raw_status, "value", str(raw_status))
        output = getattr(execution, "output_data", None) or {}
        error = getattr(execution, "error_message", None)
    if not isinstance(output, dict):
        output = {"result": output}
    return status, output, error


def _comparison(
    visura_area_m2: float | None,
    estimated_area_m2: float | None,
    *,
    tolerance_m2: float,
    tolerance_percent: float,
) -> dict[str, Any]:
    if visura_area_m2 is None or estimated_area_m2 is None:
        return {
            "status": "unavailable",
            "visura_area_m2": visura_area_m2,
            "estimated_area_m2": estimated_area_m2,
            "difference_m2": None,
            "difference_percent": None,
            "allowed_difference_m2": None,
        }

    difference = round(estimated_area_m2 - visura_area_m2, 2)
    difference_percent = round(abs(difference) / visura_area_m2, 4) if visura_area_m2 else None
    allowed = max(tolerance_m2, visura_area_m2 * tolerance_percent)
    return {
        "status": "match" if abs(difference) <= allowed else "mismatch",
        "visura_area_m2": visura_area_m2,
        "estimated_area_m2": estimated_area_m2,
        "difference_m2": difference,
        "difference_percent": difference_percent,
        "allowed_difference_m2": round(allowed, 2),
    }


async def validate_visura_against_floor_plan(
    *,
    visura: Mapping[str, Any],
    floor_plan_path: str | Path,
    calibration: Mapping[str, Any] | None = None,
    rooms: list[Mapping[str, Any]] | None = None,
    page_number: int = 1,
    output_dir: str | Path = "data/floor-plans",
    report_title: str = "Verifica superficie visura / planimetria",
    logo_path: str = "",
    property_index: int = 0,
    visura_area_m2: float | None = None,
    tolerance_m2: float = 1.0,
    tolerance_percent: float = 0.05,
    engine: Any | None = None,
    save_to_db: bool = False,
) -> dict[str, Any]:
    """Run Ocular's floor-plan workflow and compare its total with a visura.

    ``rooms`` and ``calibration`` use the input format documented by Ocular.
    A room can contain confirmed ``dimensions`` or a traced ``polygon``; a
    polygon requires a confirmed calibration.  ``engine`` is injectable for
    callers that already own an Ocular engine and for tests.
    """
    if tolerance_m2 < 0 or tolerance_percent < 0:
        raise ValueError("tolerances must be non-negative")
    if not Path(floor_plan_path).is_file():
        raise FileNotFoundError(f"Floor plan file not found: {floor_plan_path}")

    if engine is None:
        try:
            from ocular.workflow.pf_engine import PromptFlowEngine
        except ImportError as exc:
            raise FloorPlanFeatureUnavailable(
                "The floor-plan feature requires the optional dependency. "
                "Install it with `pip install 'sister[floor-plan-ocr]'`."
            ) from exc
        engine = PromptFlowEngine()

    input_data = {
        "file_path": str(Path(floor_plan_path)),
        "calibration": dict(calibration or {}),
        "rooms": [dict(room) for room in (rooms or [])],
        "page_number": page_number,
        "output_dir": str(output_dir),
        "report_title": report_title,
        "logo_path": logo_path,
    }
    execution = await engine.execute_workflow(
        workflow_id=OCULAR_FLOOR_PLAN_WORKFLOW,
        input_data=input_data,
        save_to_db=save_to_db,
    )
    status, output, error = _execution_output(execution)
    if status not in {"completed", "StepStatus.COMPLETED"}:
        raise FloorPlanValidationError(error or f"Ocular workflow failed with status: {status}")

    estimated_area = _parse_area(output.get("total_area_m2"))
    cadastral_area = _parse_area(visura_area_m2)
    if cadastral_area is None:
        cadastral_area = extract_visura_area_m2(visura, property_index=property_index)

    return {
        "workflow_id": OCULAR_FLOOR_PLAN_WORKFLOW,
        "workflow_status": status,
        "floor_plan": output,
        "comparison": _comparison(
            cadastral_area,
            estimated_area,
            tolerance_m2=tolerance_m2,
            tolerance_percent=tolerance_percent,
        ),
    }


__all__ = [
    "OCULAR_FLOOR_PLAN_WORKFLOW",
    "FloorPlanFeatureUnavailable",
    "FloorPlanValidationError",
    "extract_visura_area_m2",
    "validate_visura_against_floor_plan",
]
