"""Search-scope metadata for subject visure documents."""

from __future__ import annotations

import re
from pathlib import Path

from .provinces import province_code, province_name


def subject_search_scope(
    filename: str | None,
    *,
    request_province: str | None = None,
    stored_scope: str | None = None,
    stored_province: str | None = None,
) -> dict[str, str]:
    """Return the subject search level and display label.

    In canonical filenames, the office/province token comes immediately after
    the fiscal code and the cadastre token follows it. Thus ``..._CE`` is a
    nationwide E-cadastre search, while ``..._MI_CE`` is a provincial Milano
    search. The office token takes precedence over stored metadata because older
    imports may have classified every filename containing ``CE`` as nationwide.
    """

    def result(scope: str, province: str = "") -> dict[str, str]:
        if scope == "nationwide":
            return {
                "search_scope": scope,
                "search_scope_label": "Nazionale",
                "search_province": "",
            }
        if scope == "provincial":
            code = province_code(province) or str(province or "").strip().upper()
            name = province_name(code) or code
            return {
                "search_scope": scope,
                "search_scope_label": f"Provinciale · {name}",
                "search_province": code,
            }
        return {
            "search_scope": "unknown",
            "search_scope_label": "Non specificato",
            "search_province": "",
        }

    stem = Path(str(filename or "")).stem.upper()
    parts = stem.split("_")

    cadastre_codes = {"CE", "CF", "CT"}
    canonical_subject_name = (
        len(parts) >= 4
        and parts[0] == "VS"
        and parts[1] in {"ATT", "STO", "SIN"}
        and re.fullmatch(r"[A-Z0-9]{11,16}", parts[2])
    )
    if canonical_subject_name:
        office = parts[3]
        tail = parts[4:]
        if office == "NAZIONALE":
            return result("nationwide")

        if office in cadastre_codes:
            next_is_cadastre = bool(tail and tail[0] in cadastre_codes)
            next_is_comune_then_cadastre = bool(
                len(tail) > 1 and re.fullmatch(r"[A-Z]\d{3}", tail[0]) and tail[1] in cadastre_codes
            )
            # CE and CT are also province codes (Caserta and Catania). A second
            # cadastre token disambiguates those provincial filenames.
            office_province = province_code(office)
            if office_province and (next_is_cadastre or next_is_comune_then_cadastre):
                return result("provincial", office_province)
            return result("nationwide")

        office_province = province_code(office)
        if office_province:
            return result("provincial", office_province)

    if stored_scope in {"nationwide", "provincial"}:
        return result(stored_scope, stored_province or "")

    request_office = str(request_province or "").strip()
    if request_office.upper() == "NAZIONALE":
        return result("nationwide")
    request_code = province_code(request_office)
    if request_code:
        return result("provincial", request_code)

    return result("unknown")
