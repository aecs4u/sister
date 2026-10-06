"""Optional smoke test against the local SISTER export under ``/data``.

This test is opt-in because the data directory is developer-local. It checks
the package orchestration with a real cadastral visura and plan document while
keeping the Ocular engine injectable, as in the unit tests.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from sister.floor_plan import validate_visura_against_floor_plan


pytestmark = pytest.mark.integration


def _existing_data_root() -> Path:
    return Path(os.environ.get("SISTER_DATA_ROOT", "/data/aecs4u.it/sister"))


def _read_visura_area(pdf_path: Path) -> float:
    text = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"Dati di superficie:\s+Totale:\s+(\d+(?:[.,]\d+)?)\s+m2", text)
    if not match:
        raise AssertionError(f"No cadastral surface found in {pdf_path}")
    return float(match.group(1).replace(",", "."))


@pytest.mark.asyncio
async def test_existing_sister_visura_and_plan_document_can_be_validated():
    """Validate the existing 128 m² Ravenna visura against its plan document."""
    root = _existing_data_root()
    visura_pdf = root / "documents" / "DOC_2007543581.pdf"
    floor_plan_pdf = root / "documents" / "DOC_2007532041.pdf"
    if os.environ.get("SISTER_EXISTING_DATA_TEST") != "1":
        pytest.skip("set SISTER_EXISTING_DATA_TEST=1 to use developer-local SISTER data")
    if not visura_pdf.is_file() or not floor_plan_pdf.is_file():
        pytest.skip(f"existing SISTER fixture files not found under {root}")

    visura_area = _read_visura_area(visura_pdf)

    class SmokeEngine:
        async def execute_workflow(self, **kwargs):
            assert kwargs["workflow_id"] == "floor-plan-ocr"
            assert kwargs["input_data"]["file_path"] == str(floor_plan_pdf)
            # This is the confirmed room total supplied to Ocular for this
            # smoke run; production calls should use traced room polygons.
            return {"status": "completed", "output_data": {"total_area_m2": 128.0}}

    result = await validate_visura_against_floor_plan(
        visura={"immobili": [{"Superficie": visura_area}]},
        floor_plan_path=floor_plan_pdf,
        calibration={"mode": "printed_scale", "denominator": 100, "dpi": 300},
        rooms=[{"name": "Alloggio sub. 68", "dimensions": {"width_m": 8, "length_m": 16}}],
        engine=SmokeEngine(),
    )

    assert result["comparison"]["status"] == "match"
    assert result["comparison"]["visura_area_m2"] == pytest.approx(128)
    assert result["comparison"]["estimated_area_m2"] == pytest.approx(128)
