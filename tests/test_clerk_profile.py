"""The header shows the Clerk user's own name, e-mail and picture, not the bare token user."""

import asyncio
from types import SimpleNamespace

from sister import web
from sister.web import _with_clerk_profile


class _Request:
    def __init__(self, session):
        self.session = session
        self.scope = {"session": session} if session is not None else {}


def _clerk_user(**overrides):
    attrs = {"id": "user_1", "email": "", "first_name": "", "last_name": "", "image_url": None, "roles": ["admin"]}
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _session(**overrides):
    data = {
        "clerk_user_id": "user_1",
        "user_email": "ada@example.com",
        "user_full_name": "Ada Lovelace",
        "user_first_name": "Ada",
        "user_last_name": "Lovelace",
        "user_image_url": "https://img.clerk.com/ada.png",
    }
    data.update(overrides)
    return data


def _run(request, user):
    return asyncio.run(_with_clerk_profile(request, user))


def _no_fetch(monkeypatch):
    def fail(_user_id):
        raise AssertionError("Clerk must not be called")

    monkeypatch.setattr(web, "_fetch_clerk_profile", fail)


def test_profile_comes_from_the_session_and_the_rest_from_the_user(monkeypatch):
    _no_fetch(monkeypatch)
    user = _run(_Request(_session()), _clerk_user())
    assert user.full_name == "Ada Lovelace"
    assert user.email == "ada@example.com"
    assert user.image_url == "https://img.clerk.com/ada.png"
    assert user.roles == ["admin"] and user.id == "user_1"


def test_empty_session_profile_is_fetched_once_and_kept(monkeypatch):
    calls = []

    def fetch(user_id):
        calls.append(user_id)
        return {"full_name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
                "email": "ada@example.com", "image_url": "https://img.clerk.com/ada.png"}

    monkeypatch.setattr(web, "_fetch_clerk_profile", fetch)
    session = {"clerk_user_id": "user_1", "user_email": "", "user_full_name": ""}  # what the mounted sync stores
    request = _Request(session)
    user = _run(request, _clerk_user())
    assert (user.full_name, user.email, user.image_url) == (
        "Ada Lovelace", "ada@example.com", "https://img.clerk.com/ada.png")
    assert session["user_image_url"] == "https://img.clerk.com/ada.png"
    assert _run(request, _clerk_user()).full_name == "Ada Lovelace"
    assert calls == ["user_1"]


def test_clerk_failure_keeps_the_original_user_and_is_not_retried(monkeypatch):
    calls = []

    def fetch(user_id):
        calls.append(user_id)
        raise RuntimeError("down")

    monkeypatch.setattr(web, "_fetch_clerk_profile", fetch)
    request = _Request({"clerk_user_id": "user_1"})
    original = _clerk_user()
    assert _run(request, original) is original
    assert _run(request, original) is original
    assert calls == ["user_1"]


def test_other_principal_or_no_clerk_session_is_left_alone(monkeypatch):
    _no_fetch(monkeypatch)
    original = _clerk_user()
    assert _run(_Request(_session(clerk_user_id="user_2")), original) is original
    assert _run(_Request({}), original) is original
    assert _run(_Request(None), original) is original
    assert _run(_Request(_session()), None) is None
