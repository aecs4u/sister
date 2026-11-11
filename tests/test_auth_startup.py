"""Exercise auth initialization in fresh processes so callbacks cannot leak between tests."""

import os
import subprocess
import sys
from pathlib import Path

from sister.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]


def _auth_process(tmp_path, stored_password, script):
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
        NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY="pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
        CLERK_SECRET_KEY="sk_test_isolated_startup_test",
    )
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )


def test_required_auth_refuses_partial_startup_for_plaintext_passwords(tmp_path):
    result = _auth_process(tmp_path, "test-password", """
try:
    import sister.main
except RuntimeError as exc:
    assert str(exc) == 'Authentication setup failed; refusing startup'
    assert 'not hashed' in str(exc.__cause__)
    print('EXPECTED_AUTH_STARTUP_FAILURE')
else:
    raise AssertionError('Invalid auth configuration was silently accepted')
""")
    assert result.returncode == 0, result.stderr
    assert "EXPECTED_AUTH_STARTUP_FAILURE" in result.stdout


def test_hashed_local_login_works_alongside_clerk(tmp_path):
    result = _auth_process(tmp_path, hash_password("test-password"), """
from fastapi.testclient import TestClient
from aecs4u_auth import get_password_verify_callback
import sister.main as main

assert main.app.state.auth_config.auth_mode == 'clerk_and_local'
assert get_password_verify_callback() is not None
client = TestClient(main.app)
bad = client.post('/auth/login', json={'username': 'startup-test', 'password': 'wrong'})
assert bad.status_code == 401, bad.status_code
good = client.post('/auth/login', json={'username': 'startup-test', 'password': 'test-password'})
assert good.status_code == 200, good.status_code
assert main.app.state.auth_config.session_cookie_name in good.cookies
me = client.get('/auth/me')
assert me.status_code == 200, me.status_code
print('LOCAL_LOGIN_AND_SESSION_OK')
""")
    assert result.returncode == 0, result.stderr
    assert "LOCAL_LOGIN_AND_SESSION_OK" in result.stdout
