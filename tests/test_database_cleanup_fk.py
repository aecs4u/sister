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
    """Minimal session that enforces the response/result foreign key order."""

    def __init__(self, expired):
        self.expired_ids = expired
        self.select_count = 0
        self.events = []
        self.result_dependants = {"properties", "owners"}
        self.page_visit_dependants = {"form_elements", "errors"}
        self.document_dependants = {"xml_attributes", "xml_nodes", "metadata"}
        self.remaining = {
            "page_visits",
            "visura_documents",
            "visura_properties",
            "visura_owners",
            "visura_results",
            "visura_responses",
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, statement):
        if isinstance(statement, Delete):
            table = statement.table.name
            self.events.append(table)
            if table == "page_visit_form_elements":
                self.page_visit_dependants.discard("form_elements")
            elif table == "page_visit_errors":
                self.page_visit_dependants.discard("errors")
            elif table == "page_visits":
                assert not self.page_visit_dependants
                self.remaining.discard(table)
            elif table == "document_metadata":
                assert "xml_nodes" not in self.document_dependants
                self.document_dependants.discard("metadata")
            elif table == "document_xml_attributes":
                self.document_dependants.discard("xml_attributes")
            elif table == "document_xml_nodes":
                assert "xml_attributes" not in self.document_dependants
                self.document_dependants.discard("xml_nodes")
            elif table == "visura_documents":
                assert not self.document_dependants
                self.remaining.discard(table)
            elif table == "visura_properties":
                self.result_dependants.discard("properties")
                self.remaining.discard(table)
            elif table == "visura_owners":
                self.result_dependants.discard("owners")
                self.remaining.discard(table)
            elif table == "visura_results":
                assert not self.result_dependants, "results must be deleted after their properties and owners"
                self.remaining.discard(table)
            elif table == "visura_responses":
                assert self.remaining == {table}, "all response children must be deleted before the response"
                self.remaining.discard(table)
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
        "page_visit_form_elements",
        "page_visit_errors",
        "page_visits",
        "document_xml_attributes",
        "document_xml_nodes",
        "document_metadata",
        "visura_documents",
        "visura_properties",
        "visura_owners",
        "visura_results",
        "visura_responses",
    ]


def test_cleanup_does_not_delete_when_no_response_is_expired(monkeypatch):
    session = _Session([])
    monkeypatch.setattr(database, "is_db_writable", lambda: True)
    monkeypatch.setattr(database, "_get_session_factory", lambda: lambda: session)

    deleted = asyncio.run(database.cleanup_old_responses(ttl_seconds=60))

    assert deleted == 0
    assert session.events == []
