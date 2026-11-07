"""``_wait_for_search_form``: a menu link that bounced back to SceltaServizio is pressed again at once, not waited out."""

import asyncio

import pytest

from sister import utils

FORM = "select[name='tipoCatasto']"
PROVINCES = "select[name='listacom'] option"


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    async def count(self):
        return self.page.counts.get(self.key, 0)

    async def wait_for(self, state, timeout):
        self.page.waits.append((self.key, state, timeout))
        if self.page.counts.get(self.key, 0) == 0:
            raise TimeoutError(f"Locator.wait_for: Timeout {timeout}ms exceeded.")


class FakePage:
    def __init__(self, url, counts, click_opens_form=False):
        self.url, self.counts, self.click_opens_form = url, dict(counts), click_opens_form
        self.clicks, self.waits = [], []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, role, name):
        return ("link", name)


@pytest.fixture
def fake_click(monkeypatch):
    async def click(page, locator, timeout=60000):
        page.clicks.append(locator)
        if page.click_opens_form:
            page.counts[FORM] = 1

    monkeypatch.setattr(utils, "_click_nav", click)


def run(coro):
    return asyncio.run(coro)


def test_form_already_there_needs_no_extra_click(fake_click):
    page = FakePage("https://x/Visure/SceltaLink.do", {FORM: 1})
    run(utils._wait_for_search_form(page, "Persona fisica"))
    assert page.clicks == []
    assert [w[0] for w in page.waits] == [FORM]


def test_bounce_to_scelta_servizio_presses_the_link_again(fake_click):
    page = FakePage("https://x/Visure/SceltaServizio.do?tipo=/T/TM/VCVC_", {PROVINCES: 103}, click_opens_form=True)
    run(utils._wait_for_search_form(page, "Persona fisica"))
    assert page.clicks == [("link", "Persona fisica")]  # exactly one more press, no 15 s wait before it
    assert page.waits and page.waits[0][0] == FORM


def test_second_press_that_does_not_help_still_fails_for_the_callers_retry(fake_click):
    page = FakePage("https://x/Visure/SceltaServizio.do", {PROVINCES: 103}, click_opens_form=False)
    with pytest.raises(TimeoutError):
        run(utils._wait_for_search_form(page, "Persona fisica"))
    assert len(page.clicks) == 1  # never a loop: the second attempt from a fresh SceltaServizio is the caller's


def test_other_page_without_form_is_not_treated_as_a_bounce(fake_click):
    page = FakePage("https://x/Visure/Altro.do", {})
    with pytest.raises(TimeoutError):
        run(utils._wait_for_search_form(page, "Persona fisica"))
    assert page.clicks == []
