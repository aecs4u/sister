"""PDF of a document page, typeset with Typst.

The page data is turned into a plain JSON structure (``build_context``) and handed to the template in
``sister/typst/visura.typ``, which only reads ``data.json``: no visura text is ever pasted into Typst source, so a name or
address containing ``#``, ``$`` or ``*`` cannot change the layout or run code. No browser is involved and the output does not depend on the caller's session or theme. The only network
access is the optional OpenStreetMap basemap of the parcel sketches (``_basemap_png``, cached, ``SISTER_PDF_BASEMAP=off``).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .display import merge_identifiers, owner_details
from .land_registry_kpis import (
    is_missing_value,
    _SUBJECT_KEYS,
    _first,
    _format_currency_it,
    _format_number_it,
    _items,
    _number,
    _quota_label,
    _share_fraction,
)
from .result_parsers import clean_quota, clean_right_description

logger = logging.getLogger("sister")

_TEMPLATE = Path(__file__).with_name("typst") / "visura.typ"
_TIPO_CATASTO = {"E": "Entrambi", "F": "Fabbricati", "T": "Terreni"}
_VARIANTS = {"Attuale": "attuale", "Storica": "storica", "Sintetica": "sintetica"}


class PdfExportError(RuntimeError):
    """The document cannot be turned into a PDF (unsupported kind, Typst error)."""


# ── small formatting helpers ────────────────────────────────────────────────────────────────────────────


def _s(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("_text", "")
    return " ".join(str(value if value is not None else "").split())


def _it_date(value: Any) -> str:
    """YYYYMMDD, YYYY-MM-DD or dd/mm/yyyy -> dd/mm/yyyy; anything else unchanged."""
    text = _s(value)
    match = re.fullmatch(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text)
    return f"{match.group(3)}/{match.group(2)}/{match.group(1)}" if match else text


def _money(value: Any, decimals: int = 2) -> str:
    number = _number(value)
    return _format_currency_it(number, decimals=decimals) if number is not None else ""


def _missing(text: str) -> dict[str, Any]:
    return {"t": text or "—", "m": True}


def pdf_filename(stem: str | None, document_id: int) -> str:
    """ASCII-safe download name: the document's file stem, else ``sister_documento_<id>``."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(stem or "")).strip("._") or f"sister_documento_{document_id}"
    return f"{base}.pdf"


def _breakable(value: str, every: int = 12) -> str:
    """Let a long unbroken id wrap inside its cell (zero-width space = a line-break opportunity)."""
    value = _s(value)
    return "\u200b".join(value[i:i + every] for i in range(0, len(value), every)) if len(value) > every * 2 else value


def _table(title: str, header: list[str], widths: list[str], align: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"kind": "table", "title": title, "header": header, "widths": widths, "align": align, "rows": rows}


def _kv(rows: list[list[str]], columns: int = 4) -> dict[str, Any]:
    return {"kind": "kv", "rows": rows, "columns": columns}


# ── ownership ───────────────────────────────────────────────────────────────────────────────────────────


def _owner_row(owner: dict[str, Any], as_of: date | None = None) -> dict[str, str]:
    right = _first(owner.get("DirittiReali"))
    cf = _s(owner.get("CF") or owner.get("CodiceFiscale")).upper()
    details = owner_details(owner.get("Nominativo"), cf, as_of)
    sub = ""
    if details["birth_date"]:
        verb = {"F": "nata", "M": "nato"}.get(details["sex"], "nato/a")
        sub = f"{verb} il {details['birth_date']}" + (f" · {details['birth_place']}" if details["birth_place"] else "")
    elif details["birth_place"]:
        sub = f"nato/a a {details['birth_place']}"
    elif details["office"]:
        sub = f"sede {details['office']}"
    description = clean_right_description(right.get("Descrizione", "")) or "Diritto non specificato"
    code = _s(right.get("CodiceDiritto"))
    quota = clean_quota(right.get("Quota", ""))
    share = _share_fraction(quota)
    period = _s(right.get("FineDiritto"))
    return {
        "name": details["name"] or _s(owner.get("Nominativo")) or "Intestatario non indicato",
        "sub": sub,
        "cf": cf,
        "right": f"{description} ({code})" if code else description,
        "period": f"Periodo: {period}" if period else "",
        "quota": _quota_label({"share": share, "quota": quota}) if quota else "",
    }


def _mutations(group: dict[str, Any], as_of: date | None = None) -> list[dict[str, Any]]:
    """The group's ownership acts, newest first (the portal lists them oldest first)."""
    events = _items(_first(group.get("IntestazioneGruppo")).get("MutazioneSoggettiva"))
    out = []
    for position, event in enumerate(reversed(events)):
        act = _s(event.get("DatiDerivantiDaMutazSogg"))
        found = re.search(r"\bdel\s+(\d{2}/\d{2}/\d{4})", act)
        owners = _items(_first(event.get("Intestazione")).get("Intestato")) or _items(event.get("Intestato"))
        out.append({
            "current": position == 0,
            "index": _s(event.get("IndiceMutazione")) or str(len(events) - position),
            "act": act or "—",
            "date": found.group(1) if found else "",
            "owners": [_owner_row(owner, as_of) for owner in owners],
        })
    return out


# ── subject visura ──────────────────────────────────────────────────────────────────────────────────────

_BUILDING_HEADER = ["Indirizzo", "Comune", "Fog.", "Part.", "Sub.", "Zona", "Cat.", "Descrizione categoria", "Classe",
                    "Consistenza", "Rendita", "Partita", "Valore catastale", "Cod. Comune / Prov.", "Sez.", "Superficie catastale"]
_BUILDING_WIDTHS = ["2.1fr", "1fr", "auto", "auto", "auto", "auto", "auto", "1.5fr", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "1.2fr"]
_BUILDING_ALIGN = ["left"] * 10 + ["right", "left", "right"] + ["left"] * 3
_LAND_HEADER = ["Comune", "Fog.", "Part.", "Qualità", "Classe", "Superficie", "Reddito dominicale", "Reddito agrario",
                "Partita", "Valore catastale", "Cod. Comune / Prov.", "Sez."]
_LAND_WIDTHS = ["1.2fr", "auto", "auto", "1.6fr", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto"]
_LAND_ALIGN = ["left"] * 5 + ["right", "right", "right", "left", "right"] + ["left", "left"]


def _preferred_source_value(estimate: dict[str, Any]) -> str:
    selected = estimate.get("preferred_valuation") or {}
    if not selected.get("range"):
        return "—"
    return f"{selected.get('provider_label') or 'Fonte scelta'} · {selected['range']}"


def _building_rows(
    group: dict[str, Any], estimates: list[dict[str, Any]], with_omi: bool, with_preferred_source: bool = False
) -> list[list[Any]]:
    rows = []
    for index, unit in enumerate(_items(group.get("ImmobileFabbricatiS"))):
        ident = _first(_first(unit.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
        classamento = _first(unit.get("DatiClassamentoF"))
        consistency = classamento.get("Consistenza") if isinstance(classamento.get("Consistenza"), dict) else {}
        surface = _first(unit.get("SuperficieF"))
        surface_parts = []
        if surface.get("Totale"):
            surface_parts.append(f"Totale {_s(surface.get('Totale'))} m²")
        if surface.get("EscluseAreeScoperte"):
            surface_parts.append(f"escluse aree scoperte {_s(surface.get('EscluseAreeScoperte'))} m²")
        category = _s(classamento.get("Categoria"))
        code, _, description = category.partition(":")
        estimate = estimates[index] if index < len(estimates) else {}
        row: list[Any] = [
            _s(_first(unit.get("DatiIndirizzo")).get("IndirizzoImm") or unit.get("IndirizzoImm")) or "—",
            _s(group.get("Descrizione") or ident.get("Comune")),
            _s(ident.get("Foglio")) or "—", _s(ident.get("ParticellaNum")) or "—", _s(ident.get("Subalterno")) or "—",
            _s(classamento.get("ZonaCensuaria")) or "—",
            code.strip() or "—", description.strip() or "—", _s(classamento.get("Classe")) or "—",
            (f"{_s(consistency.get('Valore'))} {_s(consistency.get('Unita'))}".strip()
             or (f"{_s(surface.get('Totale'))} MQ" if surface.get("Totale") else "—")),
            _money(classamento.get("RenditaEuro")) or "—",
            _s(ident.get("Partita")) or "—",
            estimate.get("cadastral_value") or _missing(estimate.get("cadastral_missing", "")),
            _joined([ident.get("CodiceComune"), ident.get("Provincia") or group.get("Provincia")]) or "—",
            _s(ident.get("SezUrbana") or ident.get("SezCensuaria") or group.get("Sezione")) or "—",
            _joined(surface_parts) or "—",
        ]
        if with_omi:
            row.append(estimate.get("range") or _missing(estimate.get("missing", "")))
        if with_preferred_source:
            row.append(_preferred_source_value(estimate))
        rows.append(row)
    return rows


def _land_rows(
    group: dict[str, Any], estimates: list[dict[str, Any]], with_preferred_source: bool = False
) -> list[list[Any]]:
    rows = []
    position = 0
    seen_preferred_assets: set[str] = set()
    for unit in _items(group.get("ImmobileTerreniS")):
        ident = _first(_first(unit.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
        for classamento in _items(_first(unit.get("DatiClassamentoT")).get("ClassamentoT")) or [{}]:
            estimate = estimates[position] if position < len(estimates) else {}
            position += 1
            area = _number(classamento.get("SuperficieMQ"))
            rows.append([
                _s(group.get("Descrizione") or ident.get("Comune")),
                _s(ident.get("Foglio")) or "—", _s(ident.get("ParticellaNum")) or "—",
                _s(classamento.get("Qualita")) or "—", _s(classamento.get("Classe")) or "—",
                f"{_format_number_it(area)} m²" if area is not None else "—",
                _money(classamento.get("RedditoDominicaleEuro")) or "—",
                _money(classamento.get("RedditoAgrarioEuro")) or "—",
                _s(ident.get("Partita")) or "—",
                estimate.get("cadastral_value") or _missing(estimate.get("cadastral_missing", "")),
                _joined([ident.get("CodiceComune"), ident.get("Provincia") or group.get("Provincia")]) or "—",
                _s(ident.get("SezCensuaria") or ident.get("Sezione") or group.get("Sezione")) or "—",
            ])
            if with_preferred_source:
                asset_key = (estimate.get("asset_identity") or {}).get("asset_key")
                if asset_key and asset_key in seen_preferred_assets:
                    rows[-1].append("—")
                else:
                    rows[-1].append(_preferred_source_value(estimate))
                    if asset_key:
                        seen_preferred_assets.add(asset_key)
    return rows


def _assessment_items(assessment: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not assessment.get("has_rights"):
        items.append({"kind": "text", "text": "Non è stato possibile associare al soggetto una titolarità corrente. "
                                              "Non è stata presunta la piena proprietà."})
        return items
    items.append({"kind": "tiles", "columns": 3, "tiles": [
        {"label": t["label"], "value": t["value"], "missing": t["missing"], "note": ""} for t in assessment["tiles"]
    ]})
    rows = []
    for right in assessment["rights"]:
        detail = f"{right['assets']} {'bene' if right['assets'] == 1 else 'beni'} · {right.get('shares') or 'quota non indicata'}"
        if right.get("unvalued_assets") and right.get("valued_assets"):
            detail += f" · stima su {right['valued_assets']} di {right['assets']}"
            if right.get("unvalued_reasons"):
                detail += f" · non stimati: {right['unvalued_reasons']}"
        value = right.get("value") or "Non disponibile"
        rows.append([right.get("label") or "Altro diritto", detail,
                     _missing(value) if _is_missing(value) else value])
    valuation_label = (
        "Stima attribuibile · fonte preferita"
        if assessment.get("preferred_source_used")
        else "Stima OMI attribuibile"
    )
    items.append(
        _table(
            "",
            ["Diritto", "Beni e quote", valuation_label],
            ["1fr", "3fr", "2fr"],
            ["left", "left", "right"],
            rows,
        )
    )
    # the same estimate grouped by province, right type, quota and ownership percentage (as the page's tabs)
    views = [v for v in assessment.get("omi_breakdown_views") or [] if isinstance(v, dict) and v.get("rows")]
    if assessment.get("show_omi_breakdown") and any(len(v["rows"]) > 1 for v in views):
        breakdown_rows = []
        for view in views:
            for index, row in enumerate(view["rows"]):
                value = row.get("value") or "Non disponibile"
                if row.get("unvalued_assets"):
                    value += f" · {row['unvalued_assets']} senza stima"
                breakdown_rows.append([
                    view.get("label", "") if index == 0 else "", row.get("label", ""),
                    str(row.get("valued_assets", 0)), _missing(value) if _is_missing(value) else value,
                ])
        items.append(_table(
            "Ripartizione della stima attribuibile",
            ["Raggruppamento", "Voce", "Beni stimati", valuation_label],
            ["1.2fr", "2.4fr", "auto", "2.2fr"], ["left", "left", "right", "right"], breakdown_rows,
        ))
    notes = [assessment.get("coefficient_note"), assessment.get("method_note")]
    items.append({"kind": "note", "text": " ".join(n for n in notes if n)})
    if assessment.get("preferred_source_used"):
        items.append({
            "kind": "note",
            "text": (
                "La fonte preferita selezionata dall'utente sostituisce OMI solo per i beni a cui è associata; "
                "OMI resta riportato separatamente. Le stime esterne sono inserite manualmente e non verificate da SISTER."
            ),
        })
    exclusions = []
    if assessment.get("unmatched_assets"):
        exclusions.append(f"{assessment['unmatched_assets']} elementi senza titolarità associabile al soggetto")
    if assessment.get("unknown_quota_assets"):
        exclusions.append(f"{assessment['unknown_quota_assets']} elementi con quota assente o non interpretabile, "
                          "esclusi dal calcolo pro-quota")
    if exclusions:
        items.append({"kind": "note", "text": "; ".join(exclusions) + "."})
    return items


def _is_missing(text: str) -> bool:
    from .land_registry_kpis import is_missing_value

    return is_missing_value(text)


def _kpi_items(kpis: dict[str, Any]) -> list[dict[str, Any]]:
    def tile(label: str, value: str, note: str = "") -> dict[str, Any]:
        return {"label": label, "value": value, "note": note, "missing": _is_missing(value)}

    lands = kpis["cadastral_value_lands"] if kpis.get("land_rows") else "nessuno"
    tiles = [
        tile("Valore OMI fabbricati", kpis["asset_value"]),
        tile("Valore catastale (base IMU)", kpis["cadastral_value"],
             f"Fabbricati {kpis['cadastral_value_buildings']} · Terreni {lands}"),
        tile("Superficie fabbricati", kpis["building_area"], kpis["building_area_note"]),
        tile("Superficie terreni", kpis["grounds_area"], kpis["grounds_area_note"]),
        tile("Rendita fabbricati", kpis["building_income"], kpis["building_income_note"]),
        tile("Reddito dominicale", kpis["dominical_income"], kpis["dominical_income_note"]),
        tile("Reddito agrario", kpis["agricultural_income"], kpis["agricultural_income_note"]),
    ]
    if kpis.get("asset_value_adjusted"):
        tiles.append(tile(
            "Valore fabbricati personalizzato",
            kpis["asset_value_adjusted"],
            kpis.get("asset_value_adjusted_note", ""),
        ))
    if kpis.get("asset_value_central"):
        tiles.insert(1, tile(
            "Stima centrale OMI per zona",
            kpis["asset_value_central"],
            kpis.get("asset_value_central_note", ""),
        ))
    return [
        {"kind": "tiles", "columns": 4, "tiles": tiles},
        {"kind": "note", "text": kpis["asset_value_note"]},
        {"kind": "note", "text": kpis["cadastral_value_note"]},
        {"kind": "note", "text": "Valori catastali annui sommati dalla visura, non redditi effettivi e non ponderati "
                                 "per quota di proprietà."},
    ]


def _used_omi_zones(groups: list[Any], estimates: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The OMI zones the valuation actually used, per (cadastral comune code, zone), with the categories and unit count.

    A building counts when it has an OMI estimate; the zone is the one whose quotation was applied
    (``omi_zona_used`` when a neighbouring zone stood in, else ``omi_zona``).
    """
    from .omi_quotes import OMI_TYPOLOGY_LABELS

    used: dict[tuple[str, str], dict[str, Any]] = {}
    for group, group_estimate in zip(groups, estimates):
        if not isinstance(group, dict):
            continue
        rows = group_estimate.get("buildings") or []
        for index, unit in enumerate(_items(group.get("ImmobileFabbricatiS"))):
            estimate = rows[index] if index < len(rows) else {}
            zone = _s(estimate.get("omi_zona_used") or estimate.get("omi_zona")).upper()
            if not (zone and estimate.get("range")):
                continue
            ident = _first(_first(unit.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
            code = _s(ident.get("CodiceComune")).upper()
            category = _s(_first(unit.get("DatiClassamentoF")).get("Categoria")).partition(":")[0].strip() or "—"
            entry = used.setdefault((code, zone), {
                "comune": _s(group.get("Descrizione") or ident.get("Comune")),
                "province": _s(ident.get("Provincia") or group.get("Provincia")),
                "categories": {},
            })
            typology = _s(estimate.get("omi_tipologia"))
            label = OMI_TYPOLOGY_LABELS.get(int(typology)) if typology.isdigit() else ""
            item = entry["categories"].setdefault(category, {"units": 0, "typologies": set(), "low": None, "high": None})
            item["units"] += 1
            for field, pick in (("omi_price_min", min), ("omi_price_max", max)):
                value = _number(estimate.get(field))
                if value is not None:
                    key = "low" if field == "omi_price_min" else "high"
                    item[key] = value if item[key] is None else pick(item[key], value)
            if label:
                item["typologies"].add(label)
    return used


def _summary_items(kpis: dict[str, Any]) -> list[dict[str, Any]]:
    """Four headline figures and the limits that matter, from figures the report shows further on."""
    assessment = kpis.get("owner_assessment") or {}
    total = next((t for t in assessment.get("tiles") or [] if "Totale" in _s(t.get("label"))), None)
    buildings, lands = kpis.get("building_units") or 0, kpis.get("land_rows") or 0
    if not (total or buildings or lands):
        return []
    valued = (kpis.get("asset_value_coverage") or {}).get("valued_units")
    tiles = [
        {"label": "Stima OMI attribuibile", "value": (total or {}).get("value") or "Non disponibile",
         "missing": bool(total is None or total.get("missing")), "note": ""},
        {"label": "Valore catastale (base IMU)", "value": _s(kpis.get("cadastral_value")) or "Non disponibile",
         "missing": is_missing_value(kpis.get("cadastral_value")), "note": ""},
        {"label": "Patrimonio in visura", "value": f"{buildings} fabbricati · {lands} classamenti terreni", "missing": False,
         "note": f"in {kpis.get('distinct_comuni') or 0} comuni"},
        {"label": "Copertura della stima OMI", "value": f"{valued} di {buildings} fabbricati" if valued is not None and buildings else "—",
         "missing": False, "note": ""},
    ]
    items: list[dict[str, Any]] = [{"kind": "tiles", "columns": 4, "tiles": tiles}]
    limits = _s(kpis.get("asset_value_coverage_note"))
    if limits:
        items.append({"kind": "note", "text": f"Limiti della stima: {limits}"})
    return items


def _parcel_items(
    lookup: dict[str, Any] | None, used_zones: dict[tuple[str, str], dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Parcel list and a vector sketch per comune from the local cadastral geometries (the basemap is added at render time)."""
    if not lookup:
        return []
    markers = [m for m in lookup.get("parcel_markers") or [] if isinstance(m, dict)]
    features = [f for f in lookup.get("parcel_features") or [] if isinstance(f, dict)]
    unmapped = [u for u in lookup.get("unmapped_parcel_items") or [] if isinstance(u, dict)]
    if not (markers or features or unmapped):
        return []

    def identity(item: dict[str, Any]) -> tuple[str, ...]:
        properties = item.get("properties") or item
        return tuple(_s(properties.get(key)).upper() for key in (
            "province", "municipality_code", "section", "sheet", "parcel"
        ))

    with_geometry = {identity(feature) for feature in features}
    rows = []
    seen = set()
    for item in markers:
        key = identity(item)
        seen.add(key)
        if key in with_geometry:
            position = "Geometria catastale locale"
        elif item.get("position_source") == "osm_address":
            position = _joined([
                "Posizione indicativa geocodificata con OpenStreetMap",
                item.get("geocode_label"),
            ])
        else:
            position = "Posizione indicativa: centro del foglio catastale"
        rows.append([
            _s(item.get("province")) or "—", _s(item.get("municipality")) or "—",
            _s(item.get("municipality_code")) or "—", _s(item.get("section")) or "—",
            _s(item.get("sheet")) or "—", _s(item.get("parcel")) or "—",
            _joined(item.get("subunits") or []) or "—", position,
        ])
    for feature in features:
        key = identity(feature)
        if key in seen:
            continue
        seen.add(key)
        properties = feature.get("properties") or {}
        rows.append([
            _s(properties.get("province")) or "—", _s(properties.get("municipality")) or "—",
            _s(properties.get("municipality_code")) or "—", _s(properties.get("section")) or "—",
            _s(properties.get("sheet")) or "—", _s(properties.get("parcel")) or "—",
            _joined(properties.get("subunits") or []) or "—", "Geometria catastale locale",
        ])
    for item in unmapped:
        key = identity(item)
        if key in seen:
            continue
        seen.add(key)
        rows.append([
            _s(item.get("province")) or "—", _s(item.get("municipality")) or "—",
            _s(item.get("municipality_code")) or "—", _s(item.get("section")) or "—",
            _s(item.get("sheet")) or "—", _s(item.get("parcel")) or "—",
            _joined(item.get("subunits") or []) or "—", "Senza geometria nella cartografia locale",
        ])
    out: list[dict[str, Any]] = [_table("Particelle elencate nella visura",
                                        ["Provincia", "Comune", "Cod. comune", "Sez.", "Foglio", "Particella", "Subalterni", "Cartografia / posizione"],
                                        ["0.7fr", "1.1fr", "auto", "auto", "auto", "auto", "auto", "2.3fr"], ["left"] * 8, rows)]
    omi_features = [f for f in (lookup.get("omi_zone_features") or {}).get("features") or [] if isinstance(f, dict)]
    used_zones = used_zones or {}
    vintages: dict[tuple[str, str], str] = {}
    zone_features: list[dict[str, Any]] = []
    seen_features = set()
    for feature in omi_features:
        properties = feature.get("properties") or {}
        zone_key = (_s(properties.get("municipality_code")).upper(), _s(properties.get("omi_zone")).upper())
        if zone_key not in used_zones:
            continue
        vintages.setdefault(zone_key, _s(properties.get("boundary_vintage")))
        feature_key = _s(properties.get("omi_feature_key") or properties.get("reference"))
        if feature_key not in seen_features:
            seen_features.add(feature_key)
            zone_features.append(feature)
    omi_rows = []
    for (code, zone), entry in sorted(used_zones.items(), key=lambda kv: (kv[1]["comune"], kv[0][1])):
        for category, detail in sorted(entry["categories"].items()):
            omi_rows.append([
                entry["province"] or "—",
                _joined([entry["comune"], f"({code})" if code else ""]) or "—",
                zone,
                category,
                _joined(sorted(detail["typologies"])) or "—",
                (f"€ {_format_number_it(detail['low'])} – {_format_number_it(detail['high'])}/m²"
                 if detail.get("low") is not None and detail.get("high") is not None else "—"),
                str(detail["units"]),
                vintages.get((code, zone)) or "—",
            ])
    if omi_rows:
        out.append(_table(
            "Zone OMI usate per la stima",
            ["Provincia", "Comune", "Zona OMI", "Categoria catastale", "Tipologia OMI", "Vendita OMI €/m²",
             "Immobili", "Edizione confini"],
            ["0.7fr", "1.5fr", "auto", "auto", "2fr", "1.4fr", "auto", "auto"],
            ["left"] * 5 + ["right", "right", "left"], omi_rows,
        ))
    panels = _map_panels(markers, features, unmapped, [f for f in lookup.get("sheet_features") or [] if isinstance(f, dict)],
                         zone_features=zone_features)
    if panels:
        out.append({"kind": "map", "panels": panels, "note": (
            "Schizzo vettoriale dalle geometrie catastali locali, con sfondo © OpenStreetMap contributors (se disponibile); "
            "le aree grigie sono le zone OMI usate per la stima. Non in scala tra un riquadro e l'altro. I punti indicano una "
            "posizione approssimata e non il confine o la posizione certa della particella.")})
    return out


def _joined(values: list[Any], separator: str = " · ") -> str:
    return separator.join(text for value in values if (text := _s(value)))


def _parcel_reference(parcel: dict[str, Any], subunit: Any = "") -> str:
    parts = []
    if parcel.get("section"):
        parts.append(f"Sez. {_s(parcel['section'])}")
    if parcel.get("sheet"):
        parts.append(f"Fg. {_s(parcel['sheet'])}")
    if parcel.get("parcel"):
        parts.append(f"P.lla {_s(parcel['parcel'])}")
    if subunit:
        parts.append(f"Sub. {_s(subunit)}")
    municipality = _joined([
        parcel.get("municipality"),
        f"({parcel.get('municipality_code')})" if parcel.get("municipality_code") else "",
    ])
    province = f"Provincia {_s(parcel.get('province'))}" if parcel.get("province") else ""
    return _joined([municipality, province, *parts]) or "Riferimento catastale non indicato"


def _archive_report_items(archive: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not archive:
        return []
    documents = [
        (category.get("label") or "Altri documenti", document)
        for category in archive.get("categories") or []
        for document in category.get("documents") or []
    ]
    summary_rows = [
        ["Documenti logici", str(archive.get("document_count", len(documents)))],
        ["Record/file nel database", str(archive.get("file_count", 0))],
        ["File originali disponibili", str(archive.get("available_file_count", 0))],
        ["File originali assenti", str(archive.get("missing_file_count", 0))],
    ]
    rows = []
    for category, document in documents:
        files = []
        for file in document.get("files") or []:
            facts = [file.get("filename"), f"ID {file.get('id')}" if file.get("id") is not None else "",
                     file.get("file_format"), file.get("size_human"),
                     "disponibile" if file.get("available") else "file assente"]
            files.append(_joined(facts))
        extraction = "Estrazione strutturata presente" if document.get("has_extraction") else "Nessuna estrazione strutturata"
        if document.get("used_in_cadastral_groups"):
            extraction += "; dati usati: " + (_joined(document.get("cadastral_group_usage_labels") or []) or "sì")
        else:
            extraction += "; non usato nei gruppi catastali"
        provenance = _joined(document.get("source_labels") or []) or "Collegamento non classificato"
        properties = _joined(document.get("property_labels") or [])
        if properties:
            provenance += " · Immobili: " + properties
        rows.append([
            f"{document.get('title') or 'Documento'} · ID logico {document.get('id', '—')}",
            _joined([category, document.get("date"), document.get("type") or "tipo non classificato"]),
            provenance,
            extraction,
            _joined(files, "; ") or "Nessun file fisico associato",
        ])
    items: list[dict[str, Any]] = [_kv(summary_rows, columns=4)]
    if archive.get("missing_file_count"):
        items.append({"kind": "note", "text": (
            "I record con file assente conservano i metadati e i collegamenti nel database; il file originale non è "
            "presente nell'archivio su disco."
        )})
    if rows:
        items.append(_table(
            "Inventario completo dei documenti collegati",
            ["Documento e ID", "Categoria, data e tipo", "Provenienza e immobili", "Estrazione e utilizzo", "File originali"],
            ["1.6fr", "1.1fr", "1.8fr", "1.6fr", "2.1fr"], ["left"] * 5, rows,
        ))
    else:
        items.append({"kind": "note", "text": "Nessun documento collegato trovato nell'archivio."})
    return items


def _inspection_details(extraction: dict[str, Any] | None) -> list[str]:
    if not extraction:
        return ["Nessuna estrazione strutturata disponibile"]
    rows = []
    inspection = _joined([
        extraction.get("kind_label"), extraction.get("registration"), extraction.get("presentation"),
        f"ispezione {extraction.get('inspection_date')}" if extraction.get("inspection_date") else "",
        extraction.get("office"),
        f"estrazione ID {extraction.get('id')}" if extraction.get("id") else "",
    ])
    if inspection:
        rows.append(inspection)
    title = _joined([
        extraction.get("title_type"), extraction.get("title_description"),
        f"data {extraction.get('title_date')}" if extraction.get("title_date") else "",
        f"Rep. {extraction.get('title_repertory_number')}" if extraction.get("title_repertory_number") else "",
        f"notaio {extraction.get('title_notary')}" if extraction.get("title_notary") else "",
    ])
    if title:
        rows.append("Titolo: " + title)
    lien = _joined([
        extraction.get("lien_type"),
        f"capitale € {extraction.get('lien_principal_euro')}" if extraction.get("lien_principal_euro") else "",
        f"interessi € {extraction.get('lien_interest_euro')}" if extraction.get("lien_interest_euro") else "",
        f"spese € {extraction.get('lien_expenses_euro')}" if extraction.get("lien_expenses_euro") else "",
        f"totale € {extraction.get('lien_total_euro')}" if extraction.get("lien_total_euro") else "",
        f"grado {extraction.get('lien_rank')}" if extraction.get("lien_rank") else "",
        f"tasso annuo {extraction.get('lien_annual_interest_rate')}" if extraction.get("lien_annual_interest_rate") else "",
        f"durata {extraction.get('lien_duration')}" if extraction.get("lien_duration") else "",
    ])
    if lien:
        rows.append("Formalità / ipoteca: " + lien)
    for property in extraction.get("properties") or []:
        rows.append("Immobile: " + _joined([
            property.get("reference"), property.get("address"), property.get("nature"),
            f"{property['room_count']} vani" if property.get("room_count") is not None else "",
            f"{property['area_sqm']} m²" if property.get("area_sqm") is not None else "",
            f"piano {property.get('floor')}" if property.get("floor") else "",
        ]))
    for party in extraction.get("parties") or []:
        rows.append("Parte: " + _joined([
            party.get("role"), party.get("name"),
            f"CF {party.get('fiscal_code')}" if party.get("fiscal_code") else "",
            party.get("quality"), party.get("right"), party.get("share"),
        ]))
    rows.append(
        f"{extraction.get('property_count', 0)} immobili estratti · {extraction.get('party_count', 0)} parti "
        f"({extraction.get('party_favore_count', 0)} a favore, {extraction.get('party_contro_count', 0)} contro)"
    )
    return rows


def _subject_visura_items(
    subject_documents: list[dict[str, Any]] | None,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    documents = subject_documents or (profile or {}).get("subject_visure") or []
    rows = []
    for document in documents:
        rows.append([
            f"{_s(document.get('title')) or 'Visura'} · ID {_s(document.get('id')) or '—'}",
            _joined([document.get("tipo_catasto"), document.get("subtype") or document.get("tipo"), document.get("scope")]),
            _s(document.get("situazione_al") or document.get("date")) or "—",
            _joined([document.get("protocollo"), document.get("protocollo_anno")]) or "—",
            _joined(document.get("formats") or []) or "—",
        ])
    if not rows:
        return []
    return [_table(
        "Visure per soggetto disponibili",
        ["Documento", "Tipo e ambito", "Situazione/data", "Protocollo", "Formati"],
        ["2fr", "1.6fr", "1.1fr", "1fr", "0.7fr"], ["left"] * 5, rows,
    )]


def _subject_formalities_items(inspections: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Render the archived mortgage-inspection summaries shown on the subject page."""
    if not inspections:
        return []

    items: list[dict[str, Any]] = [{"kind": "note", "text": (
        "Dati estratti dall'elenco SISTER archiviato; non sono il contenuto delle singole note PDF."
    )}]
    for inspection in inspections:
        if not isinstance(inspection, dict):
            continue
        meta = inspection.get("inspection") or {}
        if not isinstance(meta, dict):
            meta = {}
        note_request = inspection.get("note_request") or {}
        if not isinstance(note_request, dict):
            note_request = {}

        if note_request.get("accepted") and not note_request.get("downloaded"):
            items.append({"kind": "note", "text": (
                "Il portale ha accettato la richiesta PDF per "
                f"{_s(note_request.get('selected_count'))} note. "
                "Le note sono disponibili per il download nella pagina Richieste; "
                "non sono ancora archiviate tra i documenti SISTER."
            )})

        title_parts = ["Ispezione " + (_s(meta.get("number")) or "—")]
        if meta.get("date"):
            title_parts.append("del " + _it_date(meta.get("date")))
        if meta.get("office"):
            title_parts.append(_s(meta.get("office")))
        if meta.get("request_code"):
            title_parts.append("Richiesta " + _s(meta.get("request_code")))

        rows = []
        formalities = inspection.get("formalities") or []
        for formality in formalities:
            if not isinstance(formality, dict):
                continue
            kind = _joined([formality.get("tipo") or "Formalità", formality.get("verso")])
            registers = _joined([
                "RP " + (_s(formality.get("registro_particolare")) or "—"),
                "RG " + (_s(formality.get("registro_generale")) or "—"),
            ])
            official = _s(formality.get("pubblico_ufficiale")) or "—"
            repertoire = _s(formality.get("repertorio"))
            if repertoire:
                repertoire_text = "Rep. " + repertoire
                repertoire_date = _s(formality.get("data_repertorio_display"))
                if repertoire_date:
                    repertoire_text += " del " + repertoire_date
                official = _joined([official, repertoire_text])

            availability = []
            if formality.get("nota_disponibile_elettronica"):
                availability.append("Nota elettronica")
            if formality.get("presenza_titolo_telematico"):
                availability.append("Titolo telematico presente")
            place = _s(formality.get("immobili")) or "—"
            rows.append([
                _s(formality.get("data_formalita_display") or formality.get("data_formalita")) or "—",
                _joined([kind, registers]),
                official,
                _s(formality.get("causale")) or "—",
                _joined([place, *availability]),
            ])

        items.append(_table(
            _joined(title_parts),
            ["Data", "Formalità e registri", "Pubblico ufficiale e repertorio", "Atto", "Luogo e disponibilità"],
            ["0.8fr", "1.6fr", "1.6fr", "1.2fr", "1.5fr"],
            ["left"] * 5,
            rows,
        ))
    return items if len(items) > 1 else []


def _subject_hub_report_items(
    profile: dict[str, Any] | None,
    portfolio: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not profile:
        return []
    items: list[dict[str, Any]] = []
    totals = profile.get("totals") or {}
    query_rows = [
        ["Ricerca principale ID", _breakable(profile.get("request_id")) or "—"],
        ["Tipo catasto", _TIPO_CATASTO.get(_s(profile.get("tipo_catasto")), _s(profile.get("tipo_catasto")) or "—")],
        ["Richiesta", _s(profile.get("requested_at_display")) or "—"],
        ["Risposta", _s(profile.get("responded_at_display")) or "—"],
        ["Particelle", str(totals.get("parcels", 0))],
        ["Unità", str(totals.get("units", 0))],
        ["Fabbricati", str(totals.get("buildings", 0))],
        ["Terreni", str(totals.get("lands", 0))],
        ["Particelle con visure archiviate", str(totals.get("with_visure", 0))],
        ["Particelle con planimetrie", str(totals.get("with_planimetrie", 0))],
        ["Particelle con ispezioni ipotecarie", str(totals.get("with_ispezioni", 0))],
        ["Particelle con geometria/mappa", str(totals.get("with_map", 0))],
    ]
    if portfolio:
        query_rows.extend([
            ["Ambito del portafoglio", _s(portfolio.get("scope_label")) or "—"],
            ["Visure provinciali aggregate", "Sì" if portfolio.get("aggregated") else "No"],
            ["Province aggregate", _joined(portfolio.get("provinces") or []) or "—"],
        ])
    items.append(_kv(query_rows, columns=4))

    provinces = []
    for province in profile.get("provinces") or []:
        provinces.append([
            _s(province.get("provincia")) or "—", _s(province.get("codice")) or "—",
            str(province.get("fabbricati") if province.get("fabbricati") is not None else "—"),
            str(province.get("terreni") if province.get("terreni") is not None else "—"),
        ])
    if provinces:
        items.append(_table(
            "Distribuzione territoriale del risultato salvato",
            ["Provincia", "Codice", "Fabbricati", "Terreni"],
            ["1.5fr", "auto", "auto", "auto"], ["left", "left", "right", "right"], provinces,
        ))

    queries = [{
        "request_id": profile.get("request_id"),
        "type": "Ricerca principale · " + _s(profile.get("tipo_catasto")),
        "date": profile.get("responded_at_display") or profile.get("requested_at_display"),
        "success": True,
        "total_results": totals.get("units"),
    }, *(profile.get("other_queries") or [])]
    query_history = []
    for query in queries:
        success = query.get("success")
        query_history.append([
            _breakable(query.get("request_id")) or "—",
            _s(query.get("label") or query.get("type") or query.get("request_type") or query.get("tipo_catasto")) or "—",
            _s(query.get("responded_at_display") or query.get("date") or query.get("responded_at") or query.get("requested_at")) or "—",
            "Sì" if success is True else ("No" if success is False else "—"),
            str(query.get("total_results") if query.get("total_results") is not None else "—"),
        ])
    if query_history:
        items.append(_table(
            "Ricerche per soggetto archiviate",
            ["ID richiesta", "Tipo", "Data", "Esito", "Risultati"],
            ["1.1fr", "2fr", "1.4fr", "auto", "auto"], ["left"] * 5, query_history,
        ))

    # one row per unit; the parcel's archive coverage sits on its first unit (the unit's own cadastral data is in the
    # per-comune tables above, so it is not repeated here)
    unit_rows = []
    for parcel in profile.get("map_parcels") or []:
        documents, requests = parcel.get("documents") or {}, parcel.get("requests") or {}
        coverage_parts = [
            f"{label}: {len(documents.get(kind) or [])}"
            for kind, label in (
                ("visure", "Visure"), ("planimetrie", "Planimetrie/elaborati"), ("mappe", "Mappe"),
                ("ispezioni", "Ispezioni ipotecarie"), ("altri", "Altri documenti"),
            ) if documents.get(kind)
        ]
        for label, count in (
            ("Richieste catastali", len(requests.get("ispezioni_catastali") or [])),
            ("Richieste mappe", len(requests.get("mappe") or [])),
            ("Visure con titolarità completa", len(parcel.get("ownership") or [])),
            ("Documenti di altre unità della particella", parcel.get("other_units_documents", 0)),
        ):
            if count:
                coverage_parts.append(f"{label}: {count}")
        if parcel.get("has_map"):
            coverage_parts.append("geometria locale disponibile")
        coverage_text = _joined(coverage_parts) or "Nessun documento archiviato"
        for position, unit in enumerate(parcel.get("units") or [{}]):
            is_building = unit.get("property_type") == "building"
            enriched = (f"Dati completati dalla visura #{unit.get('enriched_from')} ({_joined(unit.get('enriched_fields') or [])})"
                        if unit.get("enriched_from") else "")
            unit_rows.append([
                _joined([parcel.get("province"), parcel.get("municipality")]) or "—",
                _parcel_reference(parcel, unit.get("subunit")),
                "Fabbricato" if is_building else "Terreno",
                _joined([unit.get("right_type"), f"Quota {unit.get('ownership_share')}" if unit.get("ownership_share") else ""]) or "—",
                _joined([coverage_text if position == 0 else "", enriched], " · ") or "come sopra",
            ])
    if unit_rows:
        items.append(_table(
            "Unità del risultato salvato e copertura documentale",
            ["Provincia e comune", "Identificativo catastale", "Tipo", "Diritto e quota", "Documenti e cartografia archiviati"],
            ["1fr", "1.5fr", "0.8fr", "1.1fr", "2.6fr"], ["left"] * 5, unit_rows,
        ))

    coverage_rows = []
    linked_rows = []
    inspection_documents: dict[str, dict[str, Any]] = {}
    ownership_rows = []
    property_history_rows = []
    document_labels = {
        "visure": "Visure",
        "planimetrie": "Planimetrie/elaborati",
        "mappe": "Mappe",
        "ispezioni": "Ispezioni ipotecarie",
        "altri": "Altri documenti",
    }
    for parcel in profile.get("map_parcels") or []:
        reference = _parcel_reference(parcel)
        documents = parcel.get("documents") or {}
        requests = parcel.get("requests") or {}
        coverage = [
            f"{label}: {len(documents.get(kind) or [])}"
            for kind, label in document_labels.items()
        ]
        coverage.extend([
            f"Richieste catastali: {len(requests.get('ispezioni_catastali') or [])}",
            f"Richieste mappe: {len(requests.get('mappe') or [])}",
            "Geometria locale disponibile" if parcel.get("has_map") else "Geometria locale non disponibile",
            f"Visure con titolarità completa: {len(parcel.get('ownership') or [])}",
            f"Documenti di altre unità della particella: {parcel.get('other_units_documents', 0)}",
        ])
        coverage_rows.append([reference, "Fabbricato" if parcel.get("is_building") else "Terreno", str(len(parcel.get("units") or [])), _joined(coverage)])
        for kind, label in document_labels.items():
            for document in documents.get(kind) or []:
                extraction = document.get("extraction")
                linked_rows.append([
                    reference,
                    label,
                    f"{_s(document.get('title')) or 'Documento'} · ID {_s(document.get('id')) or '—'}",
                    _joined([document.get("type"), document.get("date"), f"Sub. {document.get('subunit')}" if document.get("subunit") else "", _joined(document.get("formats") or [])]),
                    "Estrazione strutturata disponibile" if extraction else "Nessuna estrazione strutturata",
                ])
                if kind == "ispezioni":
                    key = str(document.get("id") or document.get("title") or id(document))
                    existing = inspection_documents.setdefault(key, {**document, "references": []})
                    if reference not in existing["references"]:
                        existing["references"].append(reference)
        for kind in ("ispezioni_catastali", "mappe"):
            for query in requests.get(kind) or []:
                linked_rows.append([
                    reference,
                    f"Richiesta: {_s(query.get('label') or kind)}",
                    f"ID richiesta {_s(query.get('request_id')) or '—'}",
                    _joined([query.get("date"), "esito positivo" if query.get("success") is True else ("esito negativo" if query.get("success") is False else "esito non indicato"), f"{query.get('total_results')} risultati" if query.get("total_results") is not None else ""]),
                    "—",
                ])
        if parcel.get("has_map") and not documents.get("mappe"):
            linked_rows.append([reference, "Geometria", "Cartografia catastale locale", "Geometria in cache", "Nessun file documento"])

        for ownership in parcel.get("ownership") or []:
            source = _joined([
                f"Visura per immobile #{ownership.get('document_id')}" if ownership.get("document_id") else "Visura per immobile",
                f"situazione al {ownership.get('as_of')}" if ownership.get("as_of") else "",
                f"Sub. {ownership.get('subunit')}" if ownership.get("subunit") else "",
            ])
            current_owners = ownership.get("current_owners") or []
            for owner in current_owners:
                ownership_rows.append([
                    reference, source, "Intestatario attuale",
                    _joined([owner.get("name"), f"CF {owner.get('fiscal_code')}" if owner.get("fiscal_code") else ""]),
                    _joined([owner.get("right"), f"Quota {owner.get('quota')}" if owner.get("quota") else "", owner.get("period")]) or "—",
                ])
            for act in ownership.get("acts") or []:
                owners = _joined([
                    _joined([owner.get("name"), f"CF {owner.get('fiscal_code')}" if owner.get("fiscal_code") else "", owner.get("right"), owner.get("quota"), owner.get("period")])
                    for owner in act.get("owners") or []
                ], "; ")
                ownership_rows.append([
                    reference,
                    source,
                    _joined(["Atto attuale" if act.get("current") else "Atto", act.get("type"), act.get("date"), act.get("start"), act.get("end")]),
                    owners or "Titolari non indicati",
                    "Fonte: visura per immobile",
                ])
            view = ownership.get("view") or {}
            unit_view = ownership.get("unit_view") or {}
            identifier = unit_view.get("identifier") or {}
            view_reference = _joined([
                identifier.get("municipality"), identifier.get("sheet"), identifier.get("parcel"), identifier.get("subunit")
            ]) or reference
            for period in view.get("history") or []:
                period_label = (period.get("period") or {}).get("label") or "—"
                for facet in period.get("facets") or []:
                    for record in facet.get("records") or []:
                        data = record.get("data") or {}
                        values = _joined([f"{_HISTORY_LABELS.get(key, key)}: {_s(value)}" for key, value in data.items() if value])
                        derived = (record.get("derived") or {}).get("description") or ""
                        property_history_rows.append([
                            view_reference,
                            source,
                            period_label,
                            _s(facet.get("label")) or "—",
                            values or "—",
                            _s(derived) or "—",
                        ])

    if linked_rows:
        items.append(_table(
            "Documenti e richieste collegati a ciascuna particella",
            ["Particella", "Categoria", "Documento / richiesta", "Tipo, data, formato ed esito", "Estrazione"],
            ["1.6fr", "1fr", "1.7fr", "1.8fr", "1.2fr"], ["left"] * 5, linked_rows,
        ))

    for document in profile.get("subject_inspections") or []:
        key = str(document.get("id") or document.get("title") or id(document))
        existing = inspection_documents.setdefault(key, {**document, "references": []})
        if "Soggetto" not in existing["references"]:
            existing["references"].append("Soggetto")
    inspection_rows = []
    for document in inspection_documents.values():
        extraction = document.get("extraction")
        inspection_rows.append([
            _joined([f"ID {document.get('id')}" if document.get("id") else "", document.get("title"), document.get("date")]) or "Ispezione ipotecaria",
            _joined(document.get("references") or []) or "—",
            _joined(_inspection_details(extraction)),
        ])
    if inspection_rows:
        items.append(_table(
            "Ispezioni ipotecarie ed estrazioni strutturate complete",
            ["Documento", "Collegamento", "Dati estratti: formalità, immobili, parti e provenienza"],
            ["1.4fr", "1.8fr", "4.2fr"], ["left"] * 3, inspection_rows,
        ))

    def grouped(rows: list[list[Any]]) -> list[list[Any]]:
        """Particella and source (the first two columns) are said once per run of rows, not on every row."""
        out, previous = [], None
        for row in rows:
            key = (row[0], row[1])
            head = f"{row[0]} · {row[1]}" if key != previous else ""
            out.append([head, *row[2:]])
            previous = key
        return out

    if ownership_rows:
        items.append(_table(
            "Titolarità e atti dalle visure per immobile archiviate",
            ["Particella e fonte", "Atto / intestazione", "Titolari e identificativi", "Diritto e periodo"],
            ["2fr", "1.7fr", "2.2fr", "1.4fr"], ["left"] * 4, grouped(ownership_rows),
        ))
    if property_history_rows:
        items.append(_table(
            "Variazioni catastali dalle visure per immobile archiviate",
            ["Bene e fonte", "Periodo", "Dato", "Valori", "Derivazione"],
            ["2fr", "1fr", "0.9fr", "2.4fr", "1.5fr"], ["left"] * 5, grouped(property_history_rows),
        ))
    return items


def _rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    kind, coordinates = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coordinates[0]] if coordinates else []
    if kind == "MultiPolygon":
        return [polygon[0] for polygon in coordinates if polygon]
    return []


def _centre(feature: dict[str, Any]) -> tuple[float, float] | None:
    points = [(x, y) for ring in _rings(feature.get("geometry") or {}) for x, y in ring]
    if not points:
        return None
    return ((min(p[0] for p in points) + max(p[0] for p in points)) / 2, (min(p[1] for p in points) + max(p[1] for p in points)) / 2)


def _map_panels(markers: list[dict[str, Any]], features: list[dict[str, Any]], unmapped: list[dict[str, Any]],
                sheets: list[dict[str, Any]], width_mm: float | None = None,
                max_height_mm: float | None = None,
                zone_features: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    by_comune: dict[str, dict[str, Any]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        panel = by_comune.setdefault(_s(props.get("municipality_code")) or _s(props.get("municipality")),
                                     {"name": _s(props.get("municipality")), "rings": [], "dots": []})
        for ring in _rings(feature.get("geometry") or {}):
            panel["rings"].append((ring, f"{_s(props.get('sheet'))}/{_s(props.get('parcel'))}"))
    drawn = {(code, label) for code, panel in by_comune.items() for _ring, label in panel["rings"]}
    for marker in markers:
        label = f"{_s(marker.get('sheet'))}/{_s(marker.get('parcel'))}"
        code = _s(marker.get("municipality_code")) or _s(marker.get("municipality"))
        if (code, label) in drawn or marker.get("latitude") is None or marker.get("longitude") is None:
            continue
        panel = by_comune.setdefault(code, {"name": _s(marker.get("municipality")), "rings": [], "dots": []})
        panel["dots"].append((float(marker["longitude"]), float(marker["latitude"]), label))

    # parcels without geometry: a dot at the centre of their sheet, as the web map does
    for item in unmapped:
        for sheet in sheets:
            sp = sheet.get("properties") or {}
            if (_s(sp.get("municipality_code")), _s(sp.get("sheet")), _s(sp.get("section"))) != (
                    _s(item.get("municipality_code")), _s(item.get("sheet")), _s(item.get("section"))):
                continue
            centre = _centre(sheet)
            if centre:
                code = _s(item.get("municipality_code")) or _s(item.get("municipality"))
                panel = by_comune.setdefault(code, {"name": _s(item.get("municipality")), "rings": [], "dots": []})
                panel["dots"].append((centre[0], centre[1], f"{_s(item.get('sheet'))}/{_s(item.get('parcel'))}"))
                break

    # the OMI zones used by the valuation are drawn behind the parcels of their comune
    for feature in zone_features or []:
        props = feature.get("properties") or {}
        panel = by_comune.get(_s(props.get("municipality_code")))
        if panel is None:
            continue
        for ring in _rings(feature.get("geometry") or {}):
            panel.setdefault("zones", []).append((ring, _s(props.get("omi_zone"))))

    # The Typst page is A4 landscape with 10 mm margins; its map grid uses a
    # 6 pt gutter and up to three columns. Scale each sketch to that actual
    # column width. A single municipality should use the full map area instead
    # of retaining the old three-column width (88 mm).
    columns = min(max(len(by_comune), 1), 3)
    if width_mm is None:
        width_mm = (277.0 - (columns - 1) * (6 * 25.4 / 72)) / columns
    if max_height_mm is None:
        max_height_mm = {1: 86.0, 2: 72.0, 3: 62.0}[columns]

    panels = []
    for panel in by_comune.values():
        zone_rings = panel.get("zones", [])
        points = ([(x, y) for ring, _l in panel["rings"] for x, y in ring] + [(x, y) for x, y, _l in panel["dots"]]
                  + [(x, y) for ring, _l in zone_rings for x, y in ring])
        if not points:
            continue
        lon0, lon1 = min(p[0] for p in points), max(p[0] for p in points)
        lat0, lat1 = min(p[1] for p in points), max(p[1] for p in points)
        lat_mid = (lat0 + lat1) / 2
        span_x = max((lon1 - lon0) * math.cos(math.radians(lat_mid)), 1e-9)
        span_y = max(lat1 - lat0, 1e-9)
        pad = 4.0
        if len(set(points)) == 1:  # a single dot: centre it in a small panel
            scale, height = 12500.0, min(max_height_mm, 60.0)  # ~ 0.00008 degrees per mm: a few hundred metres across
            lon0 = lon0 - (width_mm / 2 - pad) / (math.cos(math.radians(lat_mid)) * scale)
            lat0 = lat0 - (height / 2 - pad) / scale
        else:
            scale = min((width_mm - 2 * pad) / span_x, (max_height_mm - 2 * pad) / span_y)
            height = min(max_height_mm, span_y * scale + 2 * pad)
            spare = width_mm - (span_x * scale + 2 * pad)  # height-limited sketch: centre it in the frame
            if spare > 0:
                lon0 -= (spare / 2) / (math.cos(math.radians(lat_mid)) * scale)

        def project(x: float, y: float) -> tuple[float, float]:
            return (round(pad + (x - lon0) * math.cos(math.radians(lat_mid)) * scale, 2),
                    round(height - pad - (y - lat0) * scale, 2))

        polygons = []
        for ring, label in panel["rings"]:
            step = max(1, len(ring) // 200)
            pts = [project(x, y) for x, y in ring[::step]]
            polygons.append({
                "points": pts, "label": label, "selected": True,
                "lx": round(sum(p[0] for p in pts) / len(pts) - 3, 2), "ly": round(sum(p[1] for p in pts) / len(pts) - 2, 2),
            })
        dots = [{"x": project(x, y)[0], "y": project(x, y)[1], "label": label} for x, y, label in panel["dots"]]
        zones = []
        def ring_area(ring: list[list[float]]) -> float:
            return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2

        biggest: dict[str, float] = {}
        for ring, label in zone_rings:
            biggest[label] = max(biggest.get(label, 0.0), ring_area(ring))
        labelled: set[str] = set()
        for ring, label in zone_rings:
            step = max(1, len(ring) // 300)
            pts = [project(x, y) for x, y in ring[::step]]
            show_label = label not in labelled and ring_area(ring) >= biggest[label]
            labelled.add(label) if show_label else None
            zones.append({
                "points": pts, "label": label if show_label else "",
                "lx": round(sum(p[0] for p in pts) / len(pts) - 3, 2), "ly": round(sum(p[1] for p in pts) / len(pts) - 2, 2),
            })
        cos_mid = math.cos(math.radians(lat_mid))
        view = {  # the geographic window of the sketch, for the basemap
            "west": lon0 - pad / (cos_mid * scale), "east": lon0 + (width_mm - pad) / (cos_mid * scale),
            "south": lat0 - pad / scale, "north": lat0 + (height - pad) / scale,
            "w": width_mm, "h": height,
        }
        panels.append({
            "title": panel["name"], "h": round(height, 1), "polygons": polygons, "dots": dots, "zones": zones,
            "view": view,
            "note": f"{len(polygons)} con geometria · {len(dots)} senza (punto)",
        })
    return panels


def subject_context(
    doc: dict[str, Any],
    kpis: dict[str, Any] | None,
    lookup: dict[str, Any] | None,
    today: date,
    subject_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    subject_report = subject_report or {}
    xml = doc.get("xml_parsed") or {}
    key = next((k for k in _SUBJECT_KEYS if isinstance(xml.get(k), dict)), None)
    if key is None:
        raise PdfExportError("not a subject visura")
    root = xml[key]
    variant = next((label for suffix, label in _VARIANTS.items() if key.endswith(suffix)), "")
    title_block, request = _first(root.get("TitoloVisura")), _first(root.get("DatiRichiesta"))
    selected = _first(request.get("SoggettoIndividuato"))
    subject = _first(selected.get("SoggettoPF")) or _first(selected.get("SoggettoPNF"))
    name = (" ".join(x for x in (_s(subject.get("Cognome")), _s(subject.get("Nome"))) if x)
            or _s(subject.get("Denominazione")) or _s(doc.get("oggetto")) or f"Documento {doc.get('id')}")
    tipo = _s(title_block.get("TipoCatasto") or request.get("TipoCatasto"))
    province = _s(request.get("Provincia") or doc.get("provincia"))
    protocol = f"{_s(request.get('Protocollo')) or '-'} / {_s(request.get('Anno')) or '-'}"
    as_of = _it_date(title_block.get("SituazioneAl"))
    as_of_date = None
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", as_of):
        as_of_date = date(int(as_of[6:]), int(as_of[3:5]), int(as_of[:2]))

    # fields the source does not have (a search without a stored visura) are left out, not printed as "—"
    meta = [["Tipo catasto", _TIPO_CATASTO.get(tipo, tipo or "—")]]
    if province:
        meta.append(["Provincia", province])
    if as_of:
        meta.append(["Situazione al", as_of])
    if _s(request.get("Protocollo")) or _s(request.get("Anno")):
        meta.append(["Protocollo", protocol])
    if doc.get("id"):
        meta.append(["ID documento", str(doc["id"])])
    if subject.get("Denominazione") and not subject.get("Cognome"):  # persona giuridica
        subject_rows = [["Denominazione", name, _s(subject.get("CodiceFiscale"))],
                        ["Sede", _s(subject.get("Sede")) or "—", _s(subject.get("CodiceSede"))]]
    else:
        subject_rows = [["Nominativo", name, _s(subject.get("CodiceFiscale"))]]
        from .fiscal_code import decode_codice_fiscale

        derived = decode_codice_fiscale(_s(subject.get("CodiceFiscale"))) or {}
        birth_place = " ".join(x for x in (
            _s(subject.get("ComuneNascita")), f"({_s(subject.get('Provincia'))})" if subject.get("Provincia") else "") if x)
        birth_date = _s(subject.get("DataNascita"))
        sex = _s(subject.get("Sesso"))
        # what the visura does not carry is read from the codice fiscale, and said so
        subject_rows.append(["Nascita", birth_date or derived.get("birth_date") or "—",
                             birth_place or ("dedotta dal codice fiscale" if not birth_date and derived else "")
                             + (f" · codice luogo {derived['birth_place_code']}" if not birth_place and derived else "")])
        subject_rows.append(["Sesso", sex or derived.get("sex") or "—",
                             "dedotto dal codice fiscale" if not sex and derived else ""])
    visura_date = f"{_it_date(title_block.get('Data'))} {_s(title_block.get('Ora'))[:2]}:{_s(title_block.get('Ora'))[2:4]}".strip(" :")
    if visura_date:
        subject_rows.append(["Visura del", visura_date])
    sections: list[dict[str, Any]] = [{"title": "Soggetto individuato", "badges": [], "items": [_kv(subject_rows)]}]
    summary = _summary_items(kpis) if kpis else []
    if summary:
        sections.append({"title": "In sintesi", "badges": [], "items": summary, "toc": False})
    if kpis:
        assessment = kpis.get("owner_assessment") or {}
        sections.append({"title": "Valutazione patrimoniale per quota e diritto", "badges": [], "items": _assessment_items(assessment)})
        kpi_items = _kpi_items(kpis)
        market = [m for m in kpis.get("market_valuation") or [] if isinstance(m, dict)]
        if market:
            kpi_items.append(_table(
                "Riferimento di mercato · prezzi al m² da annunci",
                ["Fonte", "Ambito e data", "€/m²", "Beni", "Valore", "Centrale"],
                ["auto", "3fr", "auto", "auto", "auto", "auto"], ["left", "left", "left", "right", "right", "right"],
                [[m.get("label", ""), f"{m.get('scope', '')} · al {m.get('as_of', '')}", m.get("per_sqm", ""),
                  f"{m.get('units')}/{m.get('of_units')}", m.get("range", ""), m.get("central", "")] for m in market],
            ))
            kpi_items.append({"kind": "note", "text": (
                "Prezzi richiesti negli annunci di Immobiliare.it e idealista (non di compravendita) per la superficie "
                "stimata delle abitazioni; solo i comuni presenti nei database raccolti. Riferimento di confronto con OMI, "
                "non una stima sostitutiva.")})
        sections.append({"title": "Indicatori del patrimonio catastale", "badges": [], "items": kpi_items})
        manual_lines = []
        for item in kpis.get("manual_valuation_assets") or []:
            identity = item.get("identity") or {}
            property_label = " · ".join(part for part in (
                "Terreno" if item.get("kind") == "lands" else "Fabbricato",
                identity.get("municipality"),
                ("Fg. " + identity["sheet"]) if identity.get("sheet") else "",
                ("Part. " + identity["parcel"]) if identity.get("parcel") else "",
                ("Sub. " + identity["subunit"]) if identity.get("subunit") else "",
            ) if part)
            for valuation in item.get("valuations") or []:
                detail = (
                    f"{property_label}: {valuation.get('provider_label') or valuation.get('provider')} · "
                    f"{valuation.get('range')}"
                )
                if valuation.get("source_date"):
                    detail += f" · rilevata il {_it_date(valuation['source_date'])}"
                if valuation.get("source_url"):
                    detail += f" · {valuation['source_url']}"
                if valuation.get("notes"):
                    detail += f" · {valuation['notes']}"
                manual_lines.append(detail)
            omi_override = item.get("omi_override")
            if omi_override:
                manual_lines.append(
                    f"{property_label}: fascia OMI personalizzata · {omi_override.get('range')}"
                )
        if manual_lines:
            sections.append({
                "title": "Valori personalizzati e provenienza delle valutazioni esterne",
                "badges": [],
                "items": [{"kind": "note", "text": line} for line in manual_lines],
            })

    groups = [g for g in _items(root.get("GruppoUnitaImmobiliari"))]
    estimates = (kpis or {}).get("table_estimates") or []
    with_omi = any(e.get("range") for group in estimates for e in group.get("buildings", []))
    with_preferred_building = any(
        e.get("manual_valuations") for group in estimates for e in group.get("buildings", [])
    )
    with_preferred_land = any(
        e.get("manual_valuations") for group in estimates for e in group.get("lands", [])
    )
    for index, raw in enumerate(groups):
        group = merge_identifiers(raw)
        building_estimates = (estimates[index] if index < len(estimates) else {}).get("buildings", [])
        land_estimates = (estimates[index] if index < len(estimates) else {}).get("lands", [])
        buildings = _building_rows(group, building_estimates, with_omi, with_preferred_building)
        lands = _land_rows(group, land_estimates, with_preferred_land)
        badges = [f"Catasto {_TIPO_CATASTO.get(_s(group.get('TipoCatasto')), _s(group.get('TipoCatasto')))}".strip(),
                  f"{len(buildings) + len(lands)} immobili"]
        if group.get("Sezione"):
            badges.append(f"Sez. {_s(group['Sezione'])}")
        items: list[dict[str, Any]] = []
        if buildings:
            building_header = (
                _BUILDING_HEADER
                + (["Stima OMI indicativa"] if with_omi else [])
                + (["Fonte preferita"] if with_preferred_building else [])
            )
            building_widths = (
                _BUILDING_WIDTHS
                + (["1.5fr"] if with_omi else [])
                + (["1.8fr"] if with_preferred_building else [])
            )
            building_align = (
                _BUILDING_ALIGN
                + (["right"] if with_omi else [])
                + (["right"] if with_preferred_building else [])
            )
            items.append(_table("Fabbricati (Catasto Fabbricati)", building_header,
                                building_widths, building_align,
                                buildings))
        if lands:
            land_header = _LAND_HEADER + (["Fonte preferita"] if with_preferred_land else [])
            land_widths = _LAND_WIDTHS + (["1.8fr"] if with_preferred_land else [])
            land_align = _LAND_ALIGN + (["right"] if with_preferred_land else [])
            items.append(_table("Terreni (Catasto Terreni)", land_header, land_widths, land_align, lands))
        mutations = _mutations(group, as_of_date)
        if mutations:
            items.append({"kind": "mutations", "title": f"Titolarità · {len(mutations)} {'mutazione' if len(mutations) == 1 else 'mutazioni'}", "items": mutations})
        sections.append({"title": _s(group.get("Descrizione")) or _s(group.get("CodiceComune")) or f"Gruppo {index + 1}",
                         "badges": badges, "items": items, "toc": False})

    appendix_section = None
    history = subject_report.get("land_registry_history") or []
    history_rows = []
    omitted_history_events = 0
    for group_index, group in enumerate(groups):
        group_history = history[group_index] if group_index < len(history) else {}
        for kind, row_name in (("buildings", "ImmobileFabbricatiS"), ("lands", "ImmobileTerreniS")):
            source_rows = _items(group.get(row_name))
            history_rows_for_kind = group_history.get(kind) or []
            for row_index, property_row in enumerate(source_rows):
                unit_history = history_rows_for_kind[row_index] if row_index < len(history_rows_for_kind) else {}
                identifier = _first(_first(property_row.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
                reference = _joined([
                    group.get("Descrizione") or identifier.get("Comune"),
                    f"Sez. {identifier.get('SezUrbana') or identifier.get('SezCensuaria')}"
                    if identifier.get("SezUrbana") or identifier.get("SezCensuaria") else "",
                    f"Fg. {identifier.get('Foglio')}" if identifier.get("Foglio") else "",
                    f"P.lla {identifier.get('ParticellaNum') or identifier.get('Particella')}"
                    if identifier.get("ParticellaNum") or identifier.get("Particella") else "",
                    f"Sub. {identifier.get('Subalterno')}" if identifier.get("Subalterno") else "",
                ]) or "Riferimento catastale non indicato"
                for source in unit_history.get("sources") or []:
                    omitted_history_events += int(source.get("omitted_events") or 0)
                    source_label = _joined([
                        source.get("label"),
                        f"Documento #{source.get('document_id')}" if source.get("document_id") else "",
                        f"situazione al {source.get('reference_date')}" if source.get("reference_date") else "",
                    ])
                    for event_index, event in enumerate(source.get("ownership") or [], start=1):
                        owners = _joined([
                            _joined([
                                owner.get("name"), owner.get("right"), owner.get("quota"), owner.get("period")
                            ])
                            for owner in event.get("owners") or []
                        ], "; ")
                        act = _joined([
                            event.get("act"), event.get("date"),
                            f"dal {event.get('start')}" if event.get("start") else "",
                            f"al {event.get('end')}" if event.get("end") else "",
                            "in corso" if event.get("ongoing") else "",
                        ])
                        history_rows.append([reference, source_label, str(event_index), act or "Atto non descritto", owners or "Titolari non indicati"])
    if history_rows:
        history_items: list[dict[str, Any]] = [_table(
            "Eventi storici dalle visure per immobile collegate",
            ["Bene", "Documento sorgente", "N.", "Atto e periodo", "Titolari, diritti e quote"],
            ["1.5fr", "1.7fr", "auto", "2.2fr", "2.4fr"], ["left"] * 5, history_rows,
        )]
        if omitted_history_events:
            history_items.append({"kind": "note", "text": f"Eventi storici non riportati: {omitted_history_events}."})
        else:
            history_items.append({"kind": "note", "text": "Sono inclusi tutti gli eventi contenuti nei documenti sorgente selezionati per ciascuna unità."})
        # the long ownership/mutation tables go to an appendix after the archive, so the report's body stays short
        appendix_section = {"title": "Appendice · Storia catastale e titolarità documentale", "badges": [], "items": history_items}

    parcel_items = _parcel_items(lookup, _used_omi_zones(groups, estimates))
    if parcel_items:
        sections.append({"title": "Mappa catastale", "badges": [], "items": parcel_items})
    visura_items = _subject_visura_items(subject_report.get("subject_documents"), subject_report.get("subject_hub"))
    if visura_items:
        sections.append({"title": "Altre visure per soggetto", "badges": [], "items": visura_items})
    hub_items = _subject_hub_report_items(subject_report.get("subject_hub"), portfolio=subject_report.get("subject_portfolio"))
    # the per-property ownership and variation tables are the bulk of a large report: they go to the appendix
    appendix_titles = ("Titolarità e atti dalle visure per immobile archiviate", "Variazioni catastali dalle visure per immobile archiviate")
    moved = [item for item in hub_items if item.get("title") in appendix_titles]
    hub_items = [item for item in hub_items if item.get("title") not in appendix_titles]
    if moved:
        if appendix_section is None:
            appendix_section = {"title": "Appendice · Storia catastale e titolarità documentale", "badges": [], "items": []}
        appendix_section["items"].extend(moved)
    if hub_items:
        sections.append({"title": "Risultato completo, copertura e dati per particella", "badges": [], "items": hub_items})
    archive_items = _archive_report_items(subject_report.get("archive"))
    archive = subject_report.get("archive") or {}
    if archive_items and (archive.get("document_count") or archive.get("file_count")):
        sections.append({"title": "Archivio storico completo dei documenti", "badges": [], "items": archive_items})
    formalities_items = _subject_formalities_items(subject_report.get("subject_formalities"))
    if formalities_items:
        sections.append({"title": "Ispezioni ipotecarie", "badges": [], "items": formalities_items})
    if appendix_section:
        sections.append(appendix_section)
    sections.append({"title": "Avvertenze", "badges": [], "items": [{"kind": "note", "text": (
        "Documento generato da SISTER a partire dalla visura catastale. Le stime OMI e i valori catastali sono indicativi "
        "e non costituiscono perizia; i terreni non hanno quotazioni OMI. I dati personali qui riportati sono soggetti "
        "alla normativa sulla protezione dei dati.")}]})
    subtitle = f"Visura soggetto {variant} — {_TIPO_CATASTO.get(tipo, tipo)} — {province}".strip(" —")
    if subject_report:
        subtitle = f"Report completo del soggetto · {subtitle}"
    return {
        "title": name,
        "subtitle": subtitle,
        "footer": f"SISTER · {name} · generato il {today.strftime('%d/%m/%Y')}",
        "meta": meta,
        "sections": sections,
        "toc": len([sec for sec in sections if sec.get("toc", True)]) >= 5,  # contents on page 1 of a full report
    }


# ── property visura (fabbricati / terreni) ──────────────────────────────────────────────────────────────


_HISTORY_LABELS = {
    "sheet": "Foglio", "parcel": "Particella", "subunit": "Sub.", "urban_section": "Sez. urbana", "partita": "Partita",
    "zone": "Zona", "category": "Categoria", "category_description": "Descrizione", "class": "Classe",
    "consistency": "Consistenza", "income_eur": "Rendita €", "address": "Indirizzo", "total": "Superficie totale",
    "excluded": "Superficie escluse aree scoperte", "quality": "Qualità", "area_m2": "Superficie m²",
    "dominical_income_eur": "Reddito dominicale €", "agricultural_income_eur": "Reddito agrario €",
}


def property_context(doc: dict[str, Any], today: date) -> dict[str, Any]:
    view = doc.get("view")
    if not view:
        raise PdfExportError("document has no structured view")
    header, request = view.get("header") or {}, view.get("request") or {}
    ident = (request.get("identifier") or {}).get("reference") or ""
    title = header.get("title") or "Visura catastale"
    meta = [["Situazione al", view.get("as_of") or "—"], ["Data visura", f"{header.get('date', '')} {header.get('time', '')}".strip() or "—"],
            ["Provenienza", header.get("provenance") or "—"],
            ["Protocollo", f"{request.get('Protocollo', '-')} / {request.get('Anno', '-')}"]]
    if doc.get("id"):
        meta.append(["ID documento", str(doc["id"])])
    sections: list[dict[str, Any]] = []
    units = view.get("units") or []
    if view.get("family") == "fabbricati":
        rows = []
        for unit in units:
            u, c, sf = unit.get("identifier") or {}, unit.get("classification") or {}, unit.get("surface") or {}
            rows.append([unit.get("address") or "—", u.get("municipality") or "—", u.get("sheet") or "—",
                         u.get("parcel") or "—", u.get("subunit") or "—", c.get("zone") or "—", c.get("category") or "—",
                         c.get("category_description") or "—", c.get("class") or "—", c.get("consistency") or "—",
                         f"{sf.get('total')} m²" if sf.get("total") else "—",
                         f"€ {c['income_eur']}" if c.get("income_eur") else "—"])
        header_row = ["Indirizzo", "Comune", "Fog.", "Part.", "Sub.", "Zona", "Cat.", "Descrizione categoria", "Classe",
                      "Consistenza", "Superficie", "Rendita"]
        table = _table("Unità immobiliari", header_row, ["2fr", "1fr"] + ["auto"] * 5 + ["1.5fr", "auto", "auto", "auto", "auto"],
                       ["left"] * 10 + ["right", "right"], rows)
    else:
        rows = []
        for unit in units:
            u = unit.get("identifier") or {}
            for c in unit.get("classifications") or [{}]:
                rows.append([u.get("municipality") or "—", u.get("sheet") or "—", u.get("parcel") or "—",
                             c.get("quality") or "—", c.get("class") or "—",
                             f"{c['area_m2']} m²" if c.get("area_m2") else "—",
                             f"€ {c['dominical_income_eur']}" if c.get("dominical_income_eur") else "—",
                             f"€ {c['agricultural_income_eur']}" if c.get("agricultural_income_eur") else "—"])
        table = _table("Particelle", ["Comune", "Fog.", "Part.", "Qualità", "Classe", "Superficie", "Reddito dominicale", "Reddito agrario"],
                       ["1.2fr", "auto", "auto", "2fr", "auto", "auto", "auto", "auto"], ["left"] * 5 + ["right"] * 3, rows)
    sections.append({"title": ident or "Immobile", "badges": [str(view.get("variant") or "").capitalize()], "items": [table]})

    owner_rows = []
    for unit in units:
        for owner in unit.get("current_owners") or []:
            owner_rows.append([owner.get("name") or "—", owner.get("fiscal_code") or "—",
                               owner.get("right") or "—", owner.get("quota") or "—",
                               (owner.get("period") or {}).get("label") or "—"])
    if owner_rows:
        sections.append({"title": "Intestati attuali", "badges": [], "items": [
            _table("", ["Nominativo", "Codice fiscale", "Diritto", "Quota", "Periodo"], ["2fr", "1.3fr", "1.6fr", "auto", "1.4fr"],
                   ["left"] * 5, owner_rows)]})

    history = []
    for period in view.get("history") or []:
        label = (period.get("period") or {}).get("label") or ""
        for facet in period.get("facets") or []:
            for record in facet.get("records") or []:
                data = record.get("data") or {}
                bits = [f"{_HISTORY_LABELS[k]}: {v}" for k, v in data.items() if v and k in _HISTORY_LABELS]
                derived = (record.get("derived") or {}).get("description") or ""
                history.append([label, facet.get("label") or "", " · ".join(bits), derived or ""])
    if history:
        sections.append({"title": "Storia dell'immobile", "badges": [], "items": [
            _table("", ["Periodo", "Dato", "Valori", "Derivante da"], ["1.1fr", "0.9fr", "2.6fr", "2.4fr"], ["left"] * 4, history)]})

    acts = []
    for position, mutation in enumerate(view.get("ownership") or []):
        act = mutation.get("act") or {}
        acts.append({
            "current": position == 0 and bool(mutation.get("ongoing")),
            "index": str(mutation.get("index") or position + 1),
            "act": act.get("text") or "—",
            "date": act.get("date") or "",
            "owners": [{
                "name": owner.get("name") or "—",
                "sub": f"nato/a il {owner['birth_date']}" + (f" · {owner['birth_place']}" if owner.get("birth_place") else "")
                if owner.get("birth_date") else (f"sede {owner['registered_office']}" if owner.get("registered_office") else ""),
                "cf": owner.get("fiscal_code") or "",
                "right": owner.get("right") or "—",
                "period": (owner.get("period") or {}).get("label") or "",
                "quota": owner.get("quota") or "",
            } for owner in mutation.get("owners") or []],
        })
    if acts:
        sections.append({"title": "Titolarità", "badges": [f"{len(acts)} atti"], "items": [
            {"kind": "mutations", "title": "Atti, dal più recente", "items": acts}]})
    return {
        "title": title,
        "subtitle": ident,
        "footer": f"SISTER · {title} · generato il {today.strftime('%d/%m/%Y')}",
        "meta": meta,
        "sections": sections,
    }


def build_context(
    doc: dict[str, Any],
    kpis: dict[str, Any] | None,
    lookup: dict[str, Any] | None,
    today: date | None = None,
    subject_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    xml = doc.get("xml_parsed") or {}
    if any(isinstance(xml.get(k), dict) for k in _SUBJECT_KEYS):
        return subject_context(doc, kpis, lookup, today, subject_report)
    if doc.get("view"):
        return property_context(doc, today)
    raise PdfExportError("PDF export is available for visure only")


# ── typesetting ─────────────────────────────────────────────────────────────────────────────────────────


_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_TILE_USER_AGENT = "sister/1 (info@aecs4u.com)"
_MAX_TILES = 20


def _basemap_png(view: dict[str, float]) -> bytes | None:
    """OpenStreetMap tiles covering ``view`` stitched into one faded PNG, or None (switched off, offline, no PIL).

    Tiles are cached on disk (``SISTER_TILE_CACHE``) so a repeated report does not hit the tile server again.
    """
    import os

    if os.getenv("SISTER_PDF_BASEMAP", "on").strip().lower() in {"0", "off", "false", "no"}:
        return None
    try:
        import io

        import httpx
        from PIL import Image
    except ImportError:
        return None

    def merc(lon: float, lat: float, zoom: int) -> tuple[float, float]:
        lat = max(min(lat, 85.0511), -85.0511)
        n = 256 * 2 ** zoom
        rad = math.radians(lat)
        return ((lon + 180.0) / 360.0 * n, (1 - math.asinh(math.tan(rad)) / math.pi) / 2 * n)

    target_px = view["w"] / 25.4 * 150  # 150 dpi
    zoom = 1
    for candidate in range(1, 19):
        x0, y0 = merc(view["west"], view["north"], candidate)
        x1, y1 = merc(view["east"], view["south"], candidate)
        count = (int(x1 // 256) - int(x0 // 256) + 1) * (int(y1 // 256) - int(y0 // 256) + 1)
        if count > _MAX_TILES:
            break
        zoom = candidate
        if x1 - x0 >= target_px:
            break
    x0, y0 = merc(view["west"], view["north"], zoom)
    x1, y1 = merc(view["east"], view["south"], zoom)
    tx0, tx1, ty0, ty1 = int(x0 // 256), int(x1 // 256), int(y0 // 256), int(y1 // 256)
    cache_dir = Path(os.getenv("SISTER_TILE_CACHE", "data/tile_cache"))
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (240, 240, 240))
    try:
        with httpx.Client(timeout=6.0, headers={"User-Agent": _TILE_USER_AGENT}) as client:
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    if not 0 <= tx < 2 ** zoom or not 0 <= ty < 2 ** zoom:
                        continue
                    path = cache_dir / str(zoom) / str(tx) / f"{ty}.png"
                    if path.is_file():
                        data = path.read_bytes()
                    else:
                        response = client.get(_TILE_URL.format(z=zoom, x=tx, y=ty))
                        response.raise_for_status()
                        data = response.content
                        try:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(data)
                        except OSError:
                            pass
                    canvas.paste(Image.open(io.BytesIO(data)).convert("RGB"), ((tx - tx0) * 256, (ty - ty0) * 256))
        box = (x0 - tx0 * 256, y0 - ty0 * 256, x1 - tx0 * 256, y1 - ty0 * 256)
        out_w = max(int(target_px), 64)
        image = canvas.crop(tuple(int(round(v)) for v in box)).resize((out_w, max(int(out_w * view["h"] / view["w"]), 32)))
        image = Image.blend(image, Image.new("RGB", image.size, (255, 255, 255)), 0.35)  # fade: the overlays stay legible
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - the report is still useful without the background
        logger.warning("PDF basemap unavailable: %s", type(exc).__name__)
        return None


def _attach_basemaps(context: dict[str, Any], workdir: Path) -> None:
    number = 0
    for section in context.get("sections") or []:
        for item in section.get("items") or []:
            if item.get("kind") != "map":
                continue
            for panel in item.get("panels") or []:
                view = panel.pop("view", None)
                png = _basemap_png(view) if view else None
                if png:
                    name = f"basemap{number}.png"
                    number += 1
                    (workdir / name).write_bytes(png)
                    panel["bg"] = name


def render_pdf(context: dict[str, Any]) -> bytes:
    """Typeset ``context`` (see the header of ``visura.typ``) and return the PDF bytes."""
    try:
        import typst
    except ImportError as exc:  # pragma: no cover - declared in pyproject
        raise PdfExportError("the typst package is not installed") from exc
    workdir = Path(tempfile.mkdtemp(prefix="sister-pdf-"))
    try:
        shutil.copyfile(_TEMPLATE, workdir / "visura.typ")
        _attach_basemaps(context, workdir)
        (workdir / "data.json").write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
        return typst.compile(str(workdir / "visura.typ"), root=str(workdir), format="pdf")
    except Exception as exc:  # typst raises a plain RuntimeError with the diagnostics
        logger.warning("Typst compilation failed: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        raise PdfExportError("Typst compilation failed") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
