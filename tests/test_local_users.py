"""Local accounts live in the canonical aecs4u-auth ``users`` table (sister/local_users.py, import_local_users)."""

import pytest

from sister.command_tools import import_local_users
from sister.local_users import LocalUserDirectory, parse_users_file
from sister.passwords import hash_password

ALICE = hash_password("alice-secret", iterations=1000)
BOB = hash_password("bob-secret", iterations=1000)


def _file(tmp_path, text):
    path = tmp_path / "users.txt"
    path.write_text(text)
    return path


def test_parse_users_file_skips_comments_duplicates_and_malformed(tmp_path):
    users = parse_users_file(_file(tmp_path, f"# c\nAlice@x.it {ALICE}\nalice@x.it {BOB}\nbroken\n\nBob@x.it {BOB}\n"))
    assert set(users) == {"alice@x.it", "bob@x.it"}
    assert users["alice@x.it"] == ("Alice@x.it", ALICE)


def test_missing_file_gives_no_users(tmp_path):
    assert parse_users_file(tmp_path / "absent.txt") == {}


@pytest.mark.usefixtures("fresh_db")
class TestCanonicalUsers:
    async def _add(self, email, stored, *, superuser=False, active=True, local=True):
        from aecs4u_auth.users.models import User

        from sister import database

        user = User.from_local_credentials(email=email, hashed_password=stored, is_superuser=superuser, is_active=active)
        if not local:  # a Clerk-only account: no password
            user.hashed_password = None
        async with database._get_session_factory()() as session:
            session.add(user)
            await session.commit()
        return user

    async def test_verifies_pbkdf2_and_package_hashes_case_insensitively(self):
        from aecs4u_auth.password import get_password_hash

        await self._add("Alice@x.it", ALICE)
        await self._add("carol@x.it", get_password_hash("carol-secret"))
        directory = LocalUserDirectory(cache_seconds=0)
        assert (await directory.verify("ALICE@x.it", "alice-secret")).email == "alice@x.it"
        assert await directory.verify("carol@x.it", "carol-secret")
        assert await directory.verify("alice@x.it", "wrong") is None
        assert await directory.verify("nobody@x.it", "x") is None
        assert await directory.verify("", "x") is None

    async def test_unknown_user_costs_a_hash(self, monkeypatch):
        import sister.local_users as module

        calls = []
        real = module.verify_local_password
        monkeypatch.setattr(module, "verify_local_password", lambda p, s: calls.append(1) or real(p, s))
        assert await LocalUserDirectory(cache_seconds=0).verify("ghost@x.it", "x") is None
        assert calls == [1]

    async def test_inactive_and_passwordless_accounts_cannot_sign_in(self):
        await self._add("off@x.it", ALICE, active=False)
        await self._add("clerk@x.it", ALICE, local=False)
        directory = LocalUserDirectory(cache_seconds=0)
        assert await directory.verify("off@x.it", "alice-secret") is None
        assert await directory.verify("clerk@x.it", "alice-secret") is None
        assert await directory.get("off@x.it") is None

    async def test_admin_comes_from_the_row_or_the_allow_list(self):
        await self._add("root@x.it", ALICE, superuser=True)
        await self._add("ops@x.it", BOB)
        await self._add("plain@x.it", BOB)
        directory = LocalUserDirectory(admin_usernames={"OPS@x.it"}, cache_seconds=0)
        assert (await directory.get("root@x.it")).is_superuser
        assert (await directory.get("ops@x.it")).is_superuser
        assert not (await directory.get("plain@x.it")).is_superuser

    async def test_usernames_lists_only_active_password_accounts(self):
        await self._add("a@x.it", ALICE)
        await self._add("off@x.it", ALICE, active=False)
        await self._add("clerk@x.it", ALICE, local=False)
        assert await LocalUserDirectory().usernames() == {"a@x.it"}

    async def test_successful_login_records_the_sign_in(self):
        from aecs4u_auth.users.models import User

        from sister import database

        user = await self._add("a@x.it", ALICE)
        assert await LocalUserDirectory(cache_seconds=0).verify("a@x.it", "alice-secret")
        async with database._get_session_factory()() as session:
            assert (await session.get(User, user.id)).last_sign_in_at is not None

    async def test_import_is_dry_run_then_idempotent(self, tmp_path):
        path = _file(tmp_path, f"Alice@x.it {ALICE}\nbob@x.it {BOB}\neve@x.it plain\nnot-an-email {BOB}\n")
        preview = await import_local_users.run(path, {"alice@x.it"}, apply=False)
        assert (preview["created"], preview["skipped_plaintext"], preview["invalid_email"]) == (2, 1, 1)
        assert await LocalUserDirectory().usernames() == set()  # nothing written
        applied = await import_local_users.run(path, {"alice@x.it"}, apply=True)
        assert applied["created"] == 2
        again = await import_local_users.run(path, {"alice@x.it"}, apply=True)
        assert (again["created"], again["unchanged"]) == (0, 2)
        directory = LocalUserDirectory(cache_seconds=0)
        assert (await directory.verify("alice@x.it", "alice-secret")).is_superuser
        assert not (await directory.verify("bob@x.it", "bob-secret")).is_superuser
        assert ALICE not in str(preview) and ALICE not in str(applied)
