"""One real-service SISTER extraction smoke test.

Run explicitly after restoring the authenticated SISTER browser session:

    SISTER_LIVE_TEST=1 SISTER_LIVE_TEST_FISCAL_CODE=... \
      pytest -m live tests/test_jev_extraction_workflow.py -q

The test uses the configured Jev key from .env, the real SISTER service, and
one national subject search by default. It tries the fiscal code first and
uses surname/name only if the CF search has no exact match. Set
SISTER_LIVE_TEST_PROVINCE to limit it to one office. It never uses screenshots
or a fake service.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
import pytest

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT.parents[1] / ".env", override=False)

from sister.browser import BrowserManager, _run_with_network_json  # noqa: E402
from sister.jev import jev_enabled  # noqa: E402
from sister.utils import run_visura_soggetto  # noqa: E402


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_sister_subject_extraction(monkeypatch):
    if os.getenv("SISTER_LIVE_TEST") != "1":
        pytest.skip("Set SISTER_LIVE_TEST=1 to query the live SISTER service")

    fiscal_code = os.getenv("SISTER_LIVE_TEST_FISCAL_CODE", "").strip().upper()
    if not fiscal_code:
        pytest.skip("Set SISTER_LIVE_TEST_FISCAL_CODE to the test subject's fiscal code")
    if not os.getenv("TYPESAFE_API_KEY", "").strip():
        pytest.skip("TYPESAFE_API_KEY was not loaded from .env")

    monkeypatch.setenv("SISTER_JEV_ENABLED", "true")
    assert jev_enabled(), "The live test requires Jev enabled with the .env API key"

    surname = os.getenv("SISTER_LIVE_TEST_SURNAME", "La Vardera").strip()
    given_name = os.getenv("SISTER_LIVE_TEST_GIVEN_NAME", "Ismaele").strip()
    if not surname or not given_name:
        pytest.skip("Set both SISTER_LIVE_TEST_SURNAME and SISTER_LIVE_TEST_GIVEN_NAME")

    province = os.getenv("SISTER_LIVE_TEST_PROVINCE", "").strip() or None
    manager = BrowserManager()
    print("Attaching to the existing SISTER Playwright session")
    await manager.initialize()
    try:
        # Use the already authenticated page adopted over CDP. Do not trigger a
        # second login or close the user's tab/session.
        page = manager.auth_page
        assert page is not None, "No authenticated SISTER page is available over CDP"
        scope = f"province {province}" if province else "national"
        print(f"Running the {scope} subject extraction")
        result = await _run_with_network_json(
            page,
            lambda: run_visura_soggetto(
                page,
                fiscal_code,
                provincia=province,
                cognome=surname,
                nome=given_name,
            ),
        )
    finally:
        await manager.close()

    assert isinstance(result, dict)
    assert not result.get("error"), "SISTER returned a search error"
    assert isinstance(result.get("immobili"), list)
    assert result.get("total_results") == len(result["immobili"])
    assert isinstance(result.get("network_json_responses", []), list)
    print(
        f"Extracted {len(result['immobili'])} property rows; "
        f"captured {len(result.get('network_json_responses', []))} JSON responses"
    )
