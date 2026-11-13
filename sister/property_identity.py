"""Canonical cadastral identity and stable asset-key functions.

Version 1 preserves the deployed key algorithm: SHA-256 over cadastral type and
normalized municipality, section, sheet, parcel, and subunit tokens.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


IDENTITY_FIELDS = (
    "cadastre_type",
    "municipality_code",
    "municipality",
    "province",
    "section",
    "sheet",
    "parcel",
    "subunit",
)


def _clean(value: Any, *, uppercase: bool = False) -> str:
    if isinstance(value, dict):
        value = value.get("_text") or value.get("value")
    elif isinstance(value, list):
        value = value[0] if value else ""
        if isinstance(value, dict):
            value = value.get("_text") or value.get("value")
    result = re.sub(r"\s+", " ", str(value or "").strip())
    return result.upper() if uppercase else result


def canonical_property_identity(identity: dict[str, Any]) -> dict[str, str] | None:
    """Return a cleaned version 1 identity and its key, or None when incomplete."""
    value = {
        # Keep this field byte-for-byte compatible with the deployed key builder.
        "cadastre_type": str(identity.get("cadastre_type") or ""),
        "municipality_code": _clean(identity.get("municipality_code"), uppercase=True),
        "municipality": _clean(identity.get("municipality")),
        "province": _clean(identity.get("province"), uppercase=True),
        "section": _clean(identity.get("section"), uppercase=True),
        "sheet": _clean(identity.get("sheet"), uppercase=True),
        "parcel": _clean(identity.get("parcel"), uppercase=True),
        "subunit": _clean(identity.get("subunit"), uppercase=True),
    }
    if not value["cadastre_type"] or not (value["municipality_code"] or value["municipality"]):
        return None
    if not value["sheet"] or not value["parcel"]:
        return None

    def token(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.casefold())
        ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]", "", ascii_value)

    key_fields = (
        value["cadastre_type"],
        value["municipality_code"] or token(value["municipality"]),
        "" if value["municipality_code"] else token(value["province"]),
        token(value["section"]),
        token(value["sheet"]),
        token(value["parcel"]),
        token(value["subunit"]),
    )
    value["asset_key"] = hashlib.sha256("|".join(key_fields).encode("utf-8")).hexdigest()
    return value
