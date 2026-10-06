"""Offline state detection for important SISTER form transitions."""

import pytest

from sister.models import SelectorDrift
from sister.page_state import detect_sister_page_state, require_sister_page_state


class FakeLocator:
    def __init__(self, count=0, text=""):
        self._count = count
        self._text = text

    async def count(self):
        return self._count

    async def inner_text(self):
        return self._text


class FakePage:
    def __init__(self, *, url="https://sister.example/Visure/SceltaServizio.do", states=None, body=""):
        self.url = url
        self.states = states or {}
        self.body = body

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(text=self.body)
        return FakeLocator(self.states.get(selector, 0))

    def get_by_role(self, role, *, name):
        return FakeLocator(self.states.get(f"role:{role}:{name}", 0))


@pytest.mark.asyncio
async def test_detects_office_selection_and_service_menu():
    office = FakePage(states={"select[name='listacom']": 1})
    menu = FakePage(states={"role:link:Immobile": 1})
    assert (await detect_sister_page_state(office)).name == "office_selection"
    assert (await detect_sister_page_state(menu)).name == "service_menu"


@pytest.mark.asyncio
async def test_detects_result_no_match_captcha_and_locked_states():
    result = FakePage(states={"input[visImmSel], input[name='visImmSel'], table.listaIsp4": 1})
    no_match = FakePage(body="NESSUNA CORRISPONDENZA TROVATA")
    captcha = FakePage(states={".g-recaptcha": 1})
    locked = FakePage(url="https://sister.example/error_locked.jsp")
    assert (await detect_sister_page_state(result)).name == "property_results"
    assert (await detect_sister_page_state(no_match)).name == "no_match"
    assert (await detect_sister_page_state(captcha)).name == "captcha"
    assert (await detect_sister_page_state(locked)).name == "locked_session"


@pytest.mark.asyncio
async def test_unexpected_page_is_captured_then_raises_selector_drift():
    page = FakePage(url="https://sister.example/unrecognized")
    captured = []

    class Logger:
        async def log(self, page, step):
            captured.append((page.url, step))

    with pytest.raises(SelectorDrift, match="expected one of: office_selection"):
        await require_sister_page_state(page, {"office_selection"}, page_logger=Logger(), step="start")
    assert captured == [(page.url, "selector_drift_start_unknown")]
