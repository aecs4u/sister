"""Local accounts: the ``local_user`` table with the users file as fallback (sister/local_users.py)."""

import pytest

from sister.command_tools import import_local_users
from sister.local_users import LocalUserDirectory, normalize_source, parse_users_file
from sister.passwords import hash_password

ALICE = hash_password("alice-secret", iterations=1000)
BOB = hash_password("bob-secret", iterations=1000)


def _file(tmp_path, text):
    path = tmp_path / "users.txt"
    path.write_text(text)
    return path


def test_parse_users_file_skips_comments_duplicates_and_malformed(tmp_path):
    path = _file(tmp_path, f"# c\nAlice {ALICE}\nalice {BOB}\nbroken\n\nBob {BOB}\n")
    users = parse_users_file(path)
    assert set(users) == {"alice", "bob"}
    assert users["alice"] == ("Alice", ALICE)


def test_unknown_source_falls_back_to_auto():
    assert normalize_source("nonsense") == "auto"
    assert normalize_source(" DB ") == "db"


async def test_file_mode_verifies_and_assigns_admin(tmp_path):
    users = parse_users_file(_file(tmp_path, f"Alice {ALICE}\nBob {BOB}\n"))
    directory = LocalUserDirectory(users, admin_usernames={"alice"}, source="file")
    alice = await directory.verify("ALICE", "alice-secret")
    assert alice and alice.is_superuser and alice.role == "admin" and alice.id == "alice"
    bob = await directory.verify("bob", "bob-secret")
    assert bob and not bob.is_superuser
    assert await directory.verify("alice", "wrong") is None
    assert await directory.verify("nobody", "x") is None
    assert await directory.get("") is None


async def test_unknown_user_still_costs_a_hash(tmp_path, monkeypatch):
    calls = []
    import sister.local_users as module

    real = module.verify_local_password
    monkeypatch.setattr(module, "verify_local_password", lambda p, s: calls.append(1) or real(p, s))
    directory = LocalUserDirectory({}, source="file")
    assert await directory.verify("ghost", "x") is None
    assert calls == [1]


@pytest.mark.usefixtures("fresh_db")
class TestTable:
    async def _add(self, username, stored, role="user", active=True):
        from sister import database
        from sister.db_models import LocalUser

        async with database._get_session_factory()() as session:
            session.add(
                LocalUser(username_key=username.casefold(), username=username, password_hash=stored, role=role,
                          is_active=active)
            )
            await session.commit()

    async def test_db_user_verifies_and_records_login(self):
        from sister import database
        from sister.db_models import LocalUser

        await self._add("Alice", ALICE, role="admin")
        directory = LocalUserDirectory({}, source="db", cache_seconds=0)
        account = await directory.verify("alice", "alice-secret")
        assert account and account.is_superuser
        async with database._get_session_factory()() as session:
            assert (await session.get(LocalUser, "alice")).last_login_at is not None
        assert await directory.verify("alice", "nope") is None

    async def test_inactive_user_cannot_sign_in_and_file_cannot_override(self, tmp_path):
        await self._add("Alice", ALICE, active=False)
        users = parse_users_file(_file(tmp_path, f"Alice {BOB}\n"))
        directory = LocalUserDirectory(users, source="auto", cache_seconds=0)
        assert await directory.verify("alice", "alice-secret") is None
        assert await directory.verify("alice", "bob-secret") is None  # the file's password is not a way around it
        assert await directory.get("alice") is None

    async def test_auto_falls_back_to_file_for_users_not_in_table(self, tmp_path):
        await self._add("Alice", ALICE)
        users = parse_users_file(_file(tmp_path, f"Bob {BOB}\n"))
        directory = LocalUserDirectory(users, source="auto", cache_seconds=0)
        assert await directory.verify("alice", "alice-secret")
        assert await directory.verify("bob", "bob-secret")
        assert await directory.usernames() == {"Alice", "Bob"}
        db_only = LocalUserDirectory(users, source="db", cache_seconds=0)
        assert await db_only.verify("bob", "bob-secret") is None

    async def test_table_rejects_plaintext(self):
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await self._add("Eve", "plain-text")

    async def test_import_is_dry_run_then_idempotent(self, tmp_path):
        path = _file(tmp_path, f"Alice {ALICE}\nBob {BOB}\nEve plain\n")
        preview = await import_local_users.run(path, {"alice"}, apply=False)
        assert preview["create"] == ["Alice", "Bob"] and preview["skipped_plaintext"] == ["Eve"]
        assert await LocalUserDirectory({}, source="db").usernames() == set()
        applied = await import_local_users.run(path, {"alice"}, apply=True)
        assert applied["create"] == ["Alice", "Bob"]
        again = await import_local_users.run(path, {"alice"}, apply=True)
        assert again["create"] == [] and again["unchanged"] == ["Alice", "Bob"]
        directory = LocalUserDirectory({}, source="db", cache_seconds=0)
        assert (await directory.verify("alice", "alice-secret")).is_superuser
        assert not (await directory.verify("bob", "bob-secret")).is_superuser
        assert "hash" not in str(preview).lower() and ALICE not in str(applied)

    async def test_unreachable_table_falls_back_to_file_in_auto(self, tmp_path, monkeypatch):
        users = parse_users_file(_file(tmp_path, f"Bob {BOB}\n"))
        directory = LocalUserDirectory(users, source="auto", cache_seconds=0)

        async def boom(key):
            raise RuntimeError("relation does not exist")

        monkeypatch.setattr(directory, "_db_row", boom)
        assert await directory.verify("bob", "bob-secret")
