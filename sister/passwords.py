"""Password helpers for file-backed local users.

Passwords are stored as PBKDF2 hashes produced by ``scripts/hash_local_password.py`` in the form
``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``. Plaintext passwords are accepted only with an explicit
development opt-in.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable

PREFIX = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 390_000


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def is_hashed_password(stored: str) -> bool:
    return stored.startswith(PREFIX + "$") and stored.count("$") == 3


def plaintext_users_allowed(value: str | None) -> bool:
    """Return whether the explicit development-only plaintext-password opt-in is enabled."""
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def require_hashed_passwords(stored_passwords: Iterable[str], *, allow_plaintext: bool = False) -> int:
    """Reject local-user files containing plaintext passwords unless development explicitly opts in.

    Returns the number of plaintext entries when allowed, so callers can emit a warning without logging usernames
    or credential values.
    """
    plaintext_count = sum(not is_hashed_password(stored) for stored in stored_passwords)
    if plaintext_count and not allow_plaintext:
        raise ValueError(
            f"{plaintext_count} local user password(s) are not hashed; hash them with "
            "scripts/hash_local_password.py or explicitly set ALLOW_PLAINTEXT_USERS=true for development only"
        )
    return plaintext_count


def verify_local_password(password: str, stored: str) -> bool:
    """Constant-time comparison against a plain or hashed stored password."""
    if is_hashed_password(stored):
        try:
            _, iterations, salt_hex, hash_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
            return hmac.compare_digest(digest.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))
