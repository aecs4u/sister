"""Check digit of the Italian codice fiscale (persona fisica), so a wrong code is never sent to SISTER.

SISTER answers a code with a wrong check character with "Codice Fiscale non valido!! si vuole continuare ugualmente?" and
would find nobody anyway. The 16th character is computed from the first 15 (homocode letters replacing digits are part of
those 15, so they need no special case).
"""

from __future__ import annotations

import re
from datetime import date

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


_MONTHS = "ABCDEHLMPRST"
_OMOCODE = {letter: str(digit) for digit, letter in enumerate("LMNPQRSTUV")}


def decode_codice_fiscale(code: str, today: date | None = None) -> dict[str, str] | None:
    """Birth date (``dd/mm/yyyy``), sex (``M``/``F``) and birthplace code of a persona fisica's codice fiscale.

    Homocode letters (omocodia) are mapped back to digits. The two-digit year takes the century that does not put the
    birth in the future, so someone born before 1926 reads as 20xx: the result is *derived* and must be shown as such.
    ``None`` when the code is not a well-formed persona fisica code.
    """
    value = (code or "").strip().upper()
    if not valid_codice_fiscale(value):
        return None
    chars = list(value)
    for position in (6, 7, 9, 10, 12, 13, 14):
        chars[position] = _OMOCODE.get(chars[position], chars[position])
    year_text, month_letter, day_text = "".join(chars[6:8]), chars[8], "".join(chars[9:11])
    if not (year_text.isdigit() and day_text.isdigit() and month_letter in _MONTHS):
        return None
    day = int(day_text)
    sex = "F" if day > 40 else "M"
    day = day - 40 if day > 40 else day
    today = today or date.today()
    year = 2000 + int(year_text)
    if year > today.year:
        year -= 100
    try:
        born = date(year, _MONTHS.index(month_letter) + 1, day)
    except ValueError:
        return None
    return {"birth_date": born.strftime("%d/%m/%Y"), "sex": sex, "birth_place_code": "".join(chars[11:15])}
