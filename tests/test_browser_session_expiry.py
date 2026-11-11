"""Passive detection of an ended SISTER session (what `/browser/status` reports): no portal navigation involved."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import sister.browser as browser_module
from sister.browser import BrowserManager

LIVE = "https://sister3.agenziaentrate.gov.it/Visure/SceltaServizio.do"


class _Page:
    def __init__(self, url=LIVE, closed=False):
        self.url = url
        self._closed = closed

    def is_closed(self):
        return self._closed


def _manager(page=None, *, connected=True, cdp=True):
    manager = BrowserManager.__new__(BrowserManager)
    manager._auth = SimpleNamespace(
        is_cdp=cdp, _browser=SimpleNamespace(is_connected=lambda: connected), _auth_page=page or _Page()
    )
    manager.last_login_time = None
    manager._session_lost_reason = None
    return manager


@pytest.fixture
def login_record(tmp_path, monkeypatch):
    path = tmp_path / "sister_login.json"
    monkeypatch.setattr(browser_module, "LOGIN_RECORD", path)

    def write(minutes_ago: float):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        path.write_text(json.dumps({"logged_in_at": stamp.isoformat(timespec="seconds")}), encoding="utf-8")

    return write


def test_live_session_without_a_login_record_is_not_reported_lost(login_record):
    manager = _manager()
    assert manager.session_lost_reason() is None
    assert manager.session_expires_at() is None


def test_recent_login_gives_a_deadline(login_record):
    login_record(10)
    manager = _manager()
    assert manager.session_lost_reason() is None
    left = manager.session_expires_at() - datetime.now()
    assert timedelta(minutes=19) < left < timedelta(minutes=21)


def test_session_is_lost_once_the_login_is_older_than_the_session(login_record):
    login_record(31)
    assert "scaduta alle" in _manager().session_lost_reason()


def test_a_check_that_passed_after_the_deadline_overrides_a_stale_record(login_record):
    login_record(120)
    manager = _manager()
    manager.last_login_time = datetime.now() - timedelta(minutes=5)  # a later login, made without `sister app login`
    assert manager.session_expires_at() is None
    assert manager.session_lost_reason() is None


@pytest.mark.parametrize(
    "url",
    [
        "https://sister3.agenziaentrate.gov.it/Servizi/login.jsp",
        "https://sister3.agenziaentrate.gov.it/Servizi/error_sessione_scaduta.jsp",
        "about:blank",
    ],
)
def test_tab_on_a_dead_page_is_lost(login_record, url):
    assert "nuovo accesso" in _manager(_Page(url)).session_lost_reason()


def test_closed_tab_and_lost_chrome_are_lost(login_record):
    assert "chiusa" in _manager(_Page(closed=True)).session_lost_reason()
    assert "Chrome" in _manager(connected=False).session_lost_reason()


def test_a_failed_check_is_remembered(login_record):
    manager = _manager()
    manager._session_lost_reason = "Sessione SISTER scaduta o non valida"
    assert manager.session_lost_reason() == "Sessione SISTER scaduta o non valida"
