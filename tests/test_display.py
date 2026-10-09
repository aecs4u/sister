"""Presentation helpers used as Jinja filters (sister/display.py)."""

from datetime import datetime

import pytest

from sister.display import clean_requester, inspection_ref, it_date, it_enum, it_money


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2025-01-30", "30/01/2025"),
        ("2025-01-30 08:57:03", "30/01/2025 08:57:03"),
        ("2025-01-30T08:50:57", "30/01/2025 08:50:57"),
        ("2026-10-08 22:06:54.772364+00:00", "08/10/2026 22:06:54 UTC"),
        ("30/01/2025", "30/01/2025"),  # already Italian: untouched
        (None, "–"),
        ("", "–"),
        (datetime(2025, 1, 30, 8, 57, 3), "30/01/2025 08:57:03"),
    ],
)
def test_it_date(raw, expected):
    assert it_date(raw) == expected


def test_it_date_without_time():
    assert it_date("2025-01-30T08:50:57", with_time=False) == "30/01/2025"


@pytest.mark.parametrize(
    "raw, expected",
    [(52000, "€ 52.000,00"), (4, "€ 4,00"), (91000.5, "€ 91.000,50"), (0, "€ 0,00"), (None, "–"), ("n.d.", "n.d.")],
)
def test_it_money(raw, expected):
    assert it_money(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("atto_notarile_pubblico", "Atto notarile pubblico"),
        ("proprieta", "Proprietà"),
        ("creditore_ipotecario", "Creditore ipotecario"),
        ("IPOTeca VOLONTARIA", "Ipoteca volontaria"),
        ("ABITAZIONE DI TIPO POPOLARE", "Abitazione di tipo popolare"),
        ("Fabbricati", "Fabbricati"),
        (None, "–"),
    ],
)
def test_it_enum(raw, expected):
    assert it_enum(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CNNMNL Tassa versata € 4,00", "CNNMNL"),
        ("CNNMNL", "CNNMNL"),
        ("tassa versata € 4,00", "–"),
        (None, "–"),
    ],
)
def test_clean_requester(raw, expected):
    assert clean_requester(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("T1 72614", "T72614"), ("T72614", "T72614"), ("t 1 72614", "T72614"), ("T12345", "T12345"), ("T1  72614", "T72614"), (None, "–"), ("X-9", "X-9")],
)
def test_inspection_ref(raw, expected):
    assert inspection_ref(raw) == expected
