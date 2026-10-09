"""Password helpers for the file-backed local users (``local_users.txt``).

An entry's password is either plain text (development seed, logged as a warning) or a PBKDF2 hash produced by
``scripts/hash_local_password.py`` in the form ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``.
"""

from __future__ import annotations

import hashlib
import hmac
import os

PREFIX = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 390_000


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def is_hashed_password(stored: str) -> bool:
    return stored.startswith(PREFIX + "$") and stored.count("$") == 3


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
