"""Exercise auth initialization in fresh processes so callbacks cannot leak between tests."""

import os
import subprocess
import sys
from pathlib import Path

from sister.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]


def _auth_process(tmp_path, stored_password, script, clerk_key_var="NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"):
    users = tmp_path / "users.txt"
    users.write_text(f"startup-test {stored_password}\n")
    environment = dict(os.environ)
    environment.update(
        ENVIRONMENT="development",
        REQUIRE_AUTHENTICATION="true",
        LOCAL_USERS_FILE=str(users),
        ALLOW_PLAINTEXT_USERS="false",
        SECRET_KEY="isolated-auth-startup-test-secret-32-characters",
        USE_CLERK_AUTH="true",
        CLERK_SECRET_KEY="sk_test_isolated_startup_test",
    )
    environment.pop("CLERK_PUBLISHABLE_KEY", None)
    environment.pop("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", None)
    environment[clerk_key_var] = "pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk"
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )


def test_legacy_users_file_is_not_read_at_startup(tmp_path):
    """Accounts live in the canonical ``users`` table; a plaintext legacy file neither blocks startup nor signs anyone in
    (``sister app import-local-users`` skips plaintext entries)."""
    result = _auth_process(tmp_path, "test-password", """
import sister.main as main
assert main.app.state.auth_config is not None
print('STARTUP_IGNORES_LEGACY_FILE')
""")
    assert result.returncode == 0, result.stderr
    assert "STARTUP_IGNORES_LEGACY_FILE" in result.stdout


def test_hashed_local_login_works_alongside_clerk(tmp_path):
    # The directory is stubbed with a canonical User row: the real lookup needs the migrated ``users`` table.
    result = _auth_process(tmp_path, hash_password("test-password"), """
from aecs4u_auth import get_password_verify_callback
from aecs4u_auth.users.models import User
from fastapi.testclient import TestClient
import sister.main as main
from sister.local_users import LocalUserDirectory
from sister.passwords import hash_password

user = User.from_local_credentials(email='startup-test@example.it', hashed_password=hash_password('test-password', iterations=1000))

async def find(self, identifier, *, local_only=False):
    return user if str(identifier).strip().casefold() in {'startup-test@example.it', user.id} else None

async def touch(user_id):
    return None

LocalUserDirectory._find = find
LocalUserDirectory._touch_last_login = staticmethod(touch)

assert main.app.state.auth_config.auth_mode == 'clerk_and_local'
assert get_password_verify_callback() is not None
client = TestClient(main.app)
bad = client.post('/auth/login', json={'username': 'startup-test@example.it', 'password': 'wrong'})
assert bad.status_code == 401, bad.status_code
good = client.post('/auth/login', json={'username': 'startup-test@example.it', 'password': 'test-password'})
assert good.status_code == 200, (good.status_code, good.text[:200])
assert main.app.state.auth_config.session_cookie_name in good.cookies
me = client.get('/auth/me')
assert me.status_code == 200, me.status_code
print('LOCAL_LOGIN_AND_SESSION_OK')
""")
    assert result.returncode == 0, result.stderr
    assert "LOCAL_LOGIN_AND_SESSION_OK" in result.stdout


def test_canonical_clerk_publishable_key_enables_clerk_alongside_local(tmp_path):
    """CLERK_PUBLISHABLE_KEY (canonical) works without the deprecated NEXT_PUBLIC_ alias."""
    result = _auth_process(tmp_path, hash_password("test-password"), """
import sister.main as main

assert main.app.state.auth_config.auth_mode == 'clerk_and_local', main.app.state.auth_config.auth_mode
print('CANONICAL_CLERK_KEY_OK')
""", clerk_key_var="CLERK_PUBLISHABLE_KEY")
    assert result.returncode == 0, result.stderr
    assert "CANONICAL_CLERK_KEY_OK" in result.stdout
