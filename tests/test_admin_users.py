"""The SISTER account directory consumes the shared theme without enabling CRUD."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sister import web


@pytest.mark.parametrize("query,expected", [("", ["admin@example.test", "reader"]), ("ADMIN", ["admin@example.test"]), ("missing", [])])
def test_users_directory_uses_shared_template_and_server_search(main_module, monkeypatch, query, expected):
    admin = SimpleNamespace(id="admin", role="admin", is_superuser=True)
    monkeypatch.setitem(main_module.app.dependency_overrides, web._require_admin, lambda: admin)
    monkeypatch.setattr(main_module.app.state, "local_user_keys", ["id:reader", "email:reader", "id:admin@example.test"], raising=False)
    monkeypatch.setenv("SISTER_ADMIN_USERS", "admin@example.test")

    response = TestClient(main_module.app).get("/admin/users", params={"q": query})

    assert response.status_code == 200
    assert "Gestione utenti" in response.text
    assert "SISTER_ADMIN_USERS" in response.text
    assert "/static/css/sister.css" in response.text
    assert f'value="{query}"' in response.text
    for username in ("admin@example.test", "reader"):
        assert (f'<td class="fw-semibold">{username}</td>' in response.text) == (username in expected)
    assert "createUserModal" not in response.text
    assert "AdminUsersConfig" not in response.text
    assert "admin-users.js" not in response.text
