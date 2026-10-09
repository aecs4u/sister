"""Hashed and plain entries of local_users.txt (sister/passwords.py)."""

from sister.passwords import hash_password, is_hashed_password, verify_local_password


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


def test_malformed_hash_never_matches():
    assert not verify_local_password("x", "pbkdf2_sha256$not-a-number$zz$zz")
    assert not verify_local_password("", "pbkdf2_sha256$1$00$00")
