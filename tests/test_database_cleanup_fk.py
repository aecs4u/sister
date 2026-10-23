"""Regression tests for response cleanup with relational child rows."""

import asyncio

from sqlalchemy.sql.dml import Delete

from sister import database


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Rows(self._rows)


class _Session:
    """Minimal session that records normalized request cleanup order."""

    def __init__(self, expired):
        self.expired_ids = expired
        self.select_count = 0
        self.events = []
        self.page_visit_dependants = {"page_visit_form_element", "page_visit_error"}
        self.result_dependants = {"result_owner", "property", "request_result"}
        self.remaining = {"request_payload", "request"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, statement):
        if isinstance(statement, Delete):
            table = statement.table.name
            self.events.append(table)
            if table in self.page_visit_dependants:
                self.page_visit_dependants.discard(table)
            elif table == "page_visit":
                assert not self.page_visit_dependants
            elif table in {"result_owner", "property", "request_result"}:
                self.result_dependants.discard(table)
            elif table == "request_payload":
                self.remaining.discard(table)
            elif table == "request":
                if self.expired_ids and self.select_count > 1:
                    assert not self.page_visit_dependants
                    assert not self.result_dependants
                    assert "request_payload" not in self.remaining
            return _Result([])

        self.select_count += 1
        return _Result(self.expired_ids if self.select_count == 1 else [])

    async def commit(self):
        return None


def test_cleanup_deletes_result_dependants_before_expired_response(monkeypatch):
    session = _Session(["expired"])
    monkeypatch.setattr(database, "is_db_writable", lambda: True)
    monkeypatch.setattr(database, "_get_session_factory", lambda: lambda: session)

    deleted = asyncio.run(database.cleanup_old_responses(ttl_seconds=24 * 60 * 60))

    assert deleted == 1
    assert session.events == [
        "page_visit_form_element",
        "page_visit_error",
        "page_visit",
        "result_owner",
        "property",
        "request_result",
        "request_payload",
        "request",
        "request",
    ]


def test_cleanup_still_prunes_stale_pending_requests_without_expired_responses(monkeypatch):
    session = _Session([])
    monkeypatch.setattr(database, "is_db_writable", lambda: True)
    monkeypatch.setattr(database, "_get_session_factory", lambda: lambda: session)

    deleted = asyncio.run(database.cleanup_old_responses(ttl_seconds=60))

    assert deleted == 0
    assert session.events == ["request"]
