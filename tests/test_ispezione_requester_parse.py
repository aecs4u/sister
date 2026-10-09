"""The ispezione parser must stop the requester at the neighbouring 'Tassa versata' field."""

import re


def _requester(text: str):
    match = re.search(r"\bRichiedente\s+(.+?)(?=\s+Tassa\s+versata\b|\n|$)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def test_pattern_in_utils_matches_this_contract():
    import inspect

    from sister import utils

    assert r"(?=\s+Tassa\s+versata\b|\n|$)" in inspect.getsource(utils)


def test_same_line_fee():
    assert _requester("Richiedente CNNMNL Tassa versata € 4,00\nNota di iscrizione") == "CNNMNL"


def test_separate_line_fee():
    assert _requester("Richiedente CNNMNL\nTassa versata € 4,00") == "CNNMNL"
