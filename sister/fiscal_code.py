"""Check digit of the Italian codice fiscale (persona fisica), so a wrong code is never sent to SISTER.

SISTER answers a code with a wrong check character with "Codice Fiscale non valido!! si vuole continuare ugualmente?" and
would find nobody anyway. The 16th character is computed from the first 15 (homocode letters replacing digits are part of
those 15, so they need no special case).
"""

from __future__ import annotations

import re

_SHAPE = re.compile(r"^[A-Z0-9]{16}$")

_ODD = {
    **dict(zip("0123456789", (1, 0, 5, 7, 9, 13, 15, 17, 19, 21))),
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23))),
}


def _even(char: str) -> int:
    return int(char) if char.isdigit() else ord(char) - ord("A")


def check_character(first_fifteen: str) -> str:
    """The expected 16th character for the first fifteen (upper case letters and digits)."""
    total = sum(_ODD[c] if position % 2 == 0 else _even(c) for position, c in enumerate(first_fifteen))
    return chr(ord("A") + total % 26)


def valid_codice_fiscale(code: str) -> bool:
    """True when ``code`` has the shape of a persona fisica codice fiscale and a correct check character."""
    code = (code or "").strip().upper()
    return bool(_SHAPE.match(code)) and check_character(code[:15]) == code[15]


def valid_partita_iva(code: str) -> bool:
    """True for 11 digits with a correct check digit (the partita IVA, also the codice fiscale of a company)."""
    code = (code or "").strip()
    if not re.fullmatch(r"\d{11}", code):
        return False
    total = 0
    for position, char in enumerate(code[:10]):
        digit = int(char)
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return (10 - total % 10) % 10 == int(code[10])
