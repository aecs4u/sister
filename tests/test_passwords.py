"""Hashed and plain entries of local_users.txt (sister/passwords.py)."""

import pytest

from sister.passwords import (
    hash_password,
    is_hashed_password,
    plaintext_users_allowed,
    require_hashed_passwords,
    verify_local_password,
)


def test_hash_round_trip():
    stored = hash_password("s3cret", iterations=1000)
    assert is_hashed_password(stored)
    assert verify_local_password("s3cret", stored)
    assert not verify_local_password("wrong", stored)


def test_hashes_are_salted():
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


def test_plain_entries_still_work():
    assert not is_hashed_password("demo123")
    assert verify_local_password("demo123", "demo123")
    assert not verify_local_password("demo124", "demo123")


def test_plaintext_passwords_are_rejected_without_explicit_startup_opt_in():
    with pytest.raises(ValueError, match="ALLOW_PLAINTEXT_USERS=true"):
        require_hashed_passwords([hash_password("safe", iterations=1000), "demo123"])


def test_plaintext_passwords_are_allowed_only_when_explicitly_requested():
    assert require_hashed_passwords([hash_password("safe", iterations=1000), "demo123"], allow_plaintext=True) == 1


def test_hashed_passwords_pass_startup_check():
    assert require_hashed_passwords([hash_password("one", iterations=1000), hash_password("two", iterations=1000)]) == 0


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_plaintext_opt_in_accepts_explicit_truthy_values(value):
    assert plaintext_users_allowed(value)


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "enabled"])
def test_plaintext_opt_in_is_off_by_default(value):
    assert not plaintext_users_allowed(value)


def test_malformed_hash_never_matches():
    assert not verify_local_password("x", "pbkdf2_sha256$not-a-number$zz$zz")
    assert not verify_local_password("", "pbkdf2_sha256$1$00$00")
