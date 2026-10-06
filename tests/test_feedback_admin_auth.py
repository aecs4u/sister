"""Regression tests for the 2026-09-21 audit: the feedback admin API must require X-API-Key and fail closed.

Before the fix the handlers took `_: None = None` instead of Depends(_require_admin), so GET/PUT /config and
POST /send-invitations (which mails caller-supplied recipients/text from the org mailbox) were public.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sister import feedback_admin as fa

BASE = "/api/v1/admin/feedback"
BODY = {"recipients": [{"name": "x", "email": "a@example.com"}], "custom_message": "m"}
CASES = [("GET", "/config", {}), ("PUT", "/config", {"json": {}}), ("POST", "/send-invitations", {"json": BODY})]


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(fa.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("method", "path", "kwargs"), CASES)
def test_fails_closed_when_api_key_not_configured(client, monkeypatch, method, path, kwargs):
    monkeypatch.setattr(fa, "_api_key", None)
    assert client.request(method, BASE + path, **kwargs).status_code == 503


@pytest.mark.parametrize(("method", "path", "kwargs"), CASES)
def test_requires_valid_api_key(client, monkeypatch, method, path, kwargs):
    monkeypatch.setattr(fa, "_api_key", "k123")
    assert client.request(method, BASE + path, **kwargs).status_code == 401
    assert client.request(method, BASE + path, headers={"X-API-Key": "wrong"}, **kwargs).status_code == 401
