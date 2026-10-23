"""Link subject-visura rows to stored per-property visure and ownership timelines."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from .visura_view import build_visura_view

logger = logging.getLogger("sister")

_SUBJECT_KEYS = (
    "VisuraSoggettoAttuale",
    "VisuraSoggettoStorica",
    "VisuraSoggettoSintetica",
    "VisuraSoggetto",
)
_SUBTYPE_RANK = {
    "storica_completa": 4,
    "storica_analitica": 3,
    "storica_sintetica": 2,
    "storica": 1,
}
_MAX_EVENTS_PER_SOURCE = 25


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first(value: Any) -> dict[str, Any]:
    values = _items(value)
    return values[0] if values else {}


def _municipality_name(value: Any) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(value or "")).strip()


def _number_key(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return str(int(raw)) if raw.isdigit() else raw


def _location_key(item: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(item.get("cadastre_type") or "").strip().upper(),
        str(item.get("province") or "").strip().casefold(),
        " ".join(str(item.get("municipality") or "").split()).casefold(),
        str(item.get("section") or "").strip().upper(),
        _number_key(item.get("sheet")),
        _number_key(item.get("parcel")),
        _number_key(item.get("subunit")),
    )


def _date_key(value: Any) -> date:
    text = str(value or "").strip()
    for pattern in ("%d/%m/%Y", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return date.min


def _source_record(document: dict[str, Any]) -> dict[str, Any] | None:
    view = build_visura_view(document.get("xml_content"))
    if not view or not view.get("ownership"):
        return None

    subtype = str(document.get("view_subtype") or view.get("variant") or "").strip().lower()
    ownership = []
    for mutation in view.get("ownership") or []:
        act = mutation.get("act") or {}
        owners = []
        for owner in mutation.get("owners") or []:
            period = owner.get("period") or {}
            owners.append({
                "name": str(owner.get("name") or "").strip(),
                "right": str(owner.get("right") or "").strip(),
                "quota": str(owner.get("quota") or "").strip(),
                "period": str(period.get("label") or "").strip(),
            })
        ownership.append({
            "act": str(act.get("text") or act.get("type") or "Mutazione").strip(),
            "date": str(act.get("date") or "").strip(),
            "start": str(mutation.get("start") or "").strip(),
            "end": str(mutation.get("end") or "").strip(),
            "ongoing": bool(mutation.get("ongoing")),
            "current": bool(mutation.get("current")),
            "owners": owners,
        })

    if not ownership:
        return None
    family_label = "Fabbricati" if view.get("family") == "fabbricati" else "Terreni"
    variant_label = subtype.replace("_", " ").strip().title()
    return {
        "document_id": document.get("document_id"),
        "label": f"Visura {family_label}" + (f" · {variant_label}" if variant_label else ""),
        "subtype": subtype,
        "reference_date": str(view.get("as_of") or document.get("reference_date") or "").strip(),
        "event_count": len(ownership),
        "omitted_events": max(0, len(ownership) - _MAX_EVENTS_PER_SOURCE),
        "ownership": ownership[:_MAX_EVENTS_PER_SOURCE],
        "created_at": str(document.get("created_at") or ""),
    }


def _select_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not sources:
        return []
    latest = max(
        sources,
        key=lambda source: (
            _date_key(source["reference_date"]), source["created_at"], source["document_id"] or 0
        ),
    )
    historical = [source for source in sources if source["subtype"].startswith("storica")]
    best_history = max(
        historical,
        key=lambda source: (
            source["event_count"], _SUBTYPE_RANK.get(source["subtype"], 0),
            _date_key(source["reference_date"]), source["created_at"],
        ),
        default=None,
    )
    selected = [latest]
    if best_history and best_history["document_id"] != latest["document_id"]:
        selected.append(best_history)
    return sorted(
        selected,
        key=lambda source: (_date_key(source["reference_date"]), source["created_at"]),
        reverse=True,
    )


async def subject_land_registry_history(xml_parsed: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Align the best stored per-unit ownership history with subject-visura table rows."""
    subject = next(
        (xml_parsed.get(key) for key in _SUBJECT_KEYS if isinstance(xml_parsed.get(key), dict)),
        None,
    ) if isinstance(xml_parsed, dict) else None
    if not subject:
        return None

    request = _first(subject.get("DatiRichiesta"))
    request_province = str(request.get("Provincia") or "").strip()
    groups = _items(subject.get("GruppoUnitaImmobiliari"))
    output = [{"buildings": [], "lands": []} for _ in groups]
    locations: list[dict[str, Any]] = []
    row_locations: list[tuple[int, str, int, tuple[str, ...]]] = []

    def add_row(
        group_index: int,
        section_name: str,
        row_index: int,
        property_row: dict[str, Any],
        group: dict[str, Any],
        cadastre_type: str,
    ) -> None:
        identifier = _first(_first(property_row.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
        location = {
            "cadastre_type": cadastre_type,
            "province": identifier.get("Provincia") or group.get("Provincia") or request_province,
            "municipality": identifier.get("Comune") or _municipality_name(group.get("Descrizione")),
            "section": (
                identifier.get("SezUrbana") or identifier.get("SezCensuaria") or group.get("Sezione") or ""
            ),
            "sheet": identifier.get("Foglio") or "",
            "parcel": identifier.get("ParticellaNum") or identifier.get("Particella") or "",
            "subunit": identifier.get("Subalterno") if cadastre_type == "F" else "",
        }
        output[group_index][section_name].append({"sources": []})
        key = _location_key(location)
        row_locations.append((group_index, section_name, row_index, key))
        if all((location["province"], location["municipality"], location["sheet"], location["parcel"])):
            locations.append(location)

    for group_index, group in enumerate(groups):
        for property_row in _items(group.get("ImmobileFabbricatiS")):
            add_row(group_index, "buildings", len(output[group_index]["buildings"]), property_row, group, "F")

        for property_row in _items(group.get("ImmobileTerreniS")):
            raw_classamento = property_row.get("DatiClassamentoT") or {}
            classamento = raw_classamento if isinstance(raw_classamento, dict) else {}
            classamenti = _items(classamento.get("ClassamentoT"))
            row_count = len(classamenti) or 1
            for _ in range(row_count):
                add_row(group_index, "lands", len(output[group_index]["lands"]), property_row, group, "T")

    if not locations:
        return output
    try:
        from .database import get_cadastral_visura_history_documents

        documents = await get_cadastral_visura_history_documents(locations)
    except Exception as exc:
        logger.debug("Related cadastral visura lookup failed: %s", type(exc).__name__)
        return output

    sources_by_location: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for document in documents:
        location = _location_key(document)
        source = _source_record(document)
        if source is not None:
            sources_by_location.setdefault(location, []).append(source)
    for group_index, section_name, row_index, key in row_locations:
        output[group_index][section_name][row_index]["sources"] = _select_sources(
            sources_by_location.get(key, [])
        )
    return output
