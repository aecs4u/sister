"""PDF of a document page, typeset with Typst.

The page data is turned into a plain JSON structure (``build_context``) and handed to the template in
``sister/typst/visura.typ``, which only reads ``data.json``: no visura text is ever pasted into Typst source, so a name or
address containing ``#``, ``$`` or ``*`` cannot change the layout or run code. No browser is involved, nothing is fetched
over HTTP and the output does not depend on the caller's session or theme.
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

from .display import merge_identifiers
from .land_registry_kpis import (
    _SUBJECT_KEYS,
    _first,
    _format_currency_it,
    _format_number_it,
    _items,
    _number,
    _quota_label,
    _share_fraction,
)
from .result_parsers import clean_quota, clean_right_description, parse_xml_nominativo

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


def _table(title: str, header: list[str], widths: list[str], align: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"kind": "table", "title": title, "header": header, "widths": widths, "align": align, "rows": rows}


def _kv(rows: list[list[str]], columns: int = 4) -> dict[str, Any]:
    return {"kind": "kv", "rows": rows, "columns": columns}


# ── ownership ───────────────────────────────────────────────────────────────────────────────────────────


def _owner_row(owner: dict[str, Any]) -> dict[str, str]:
    right = _first(owner.get("DirittiReali"))
    parsed = parse_xml_nominativo(owner.get("Nominativo"))
    sub = ""
    if parsed.get("birth_date"):
        sub = f"nato/a il {parsed['birth_date']}" + (f" · {parsed['birth_place']}" if parsed.get("birth_place") else "")
    elif parsed.get("registered_office"):
        sub = f"sede {parsed['registered_office']}"
    description = clean_right_description(right.get("Descrizione", "")) or "Diritto non specificato"
    code = _s(right.get("CodiceDiritto"))
    quota = clean_quota(right.get("Quota", ""))
    share = _share_fraction(quota)
    period = _s(right.get("FineDiritto"))
    return {
        "name": parsed.get("name") or _s(owner.get("Nominativo")) or "Intestatario non indicato",
        "sub": sub,
        "cf": _s(owner.get("CF") or owner.get("CodiceFiscale")).upper(),
        "right": f"{description} ({code})" if code else description,
        "period": f"Periodo: {period}" if period else "",
        "quota": _quota_label({"share": share, "quota": quota}) if quota else "",
    }


def _mutations(group: dict[str, Any]) -> list[dict[str, Any]]:
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
            "owners": [_owner_row(owner) for owner in owners],
        })
    return out


# ── subject visura ──────────────────────────────────────────────────────────────────────────────────────

_BUILDING_HEADER = ["Indirizzo", "Comune", "Fog.", "Part.", "Sub.", "Zona", "Cat.", "Descrizione categoria", "Classe",
                    "Consistenza", "Rendita", "Partita", "Valore catastale"]
_BUILDING_WIDTHS = ["2.1fr", "0.9fr", "auto", "auto", "auto", "auto", "auto", "1.5fr", "auto", "auto", "auto", "auto", "auto"]
_BUILDING_ALIGN = ["left"] * 10 + ["right", "left", "right"]
_LAND_HEADER = ["Comune", "Fog.", "Part.", "Qualità", "Classe", "Superficie", "Reddito dominicale", "Reddito agrario",
                "Partita", "Valore catastale"]
_LAND_WIDTHS = ["1.2fr", "auto", "auto", "1.6fr", "auto", "auto", "auto", "auto", "auto", "auto"]
_LAND_ALIGN = ["left"] * 5 + ["right", "right", "right", "left", "right"]


def _building_rows(group: dict[str, Any], estimates: list[dict[str, Any]], with_omi: bool) -> list[list[Any]]:
    rows = []
    for index, unit in enumerate(_items(group.get("ImmobileFabbricatiS"))):
        ident = _first(_first(unit.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
        classamento = _first(unit.get("DatiClassamentoF"))
        consistency = classamento.get("Consistenza") if isinstance(classamento.get("Consistenza"), dict) else {}
        surface = _first(unit.get("SuperficieF"))
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
        ]
        if with_omi:
            row.append(estimate.get("range") or _missing(estimate.get("missing", "")))
        rows.append(row)
    return rows


def _land_rows(group: dict[str, Any], estimates: list[dict[str, Any]]) -> list[list[Any]]:
    rows = []
    position = 0
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
            ])
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
    items.append(_table("", ["Diritto", "Beni e quote", "Stima OMI attribuibile"], ["1fr", "3fr", "2fr"],
                        ["left", "left", "right"], rows))
    notes = [assessment.get("coefficient_note"), assessment.get("method_note")]
    items.append({"kind": "note", "text": " ".join(n for n in notes if n)})
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
    return [
        {"kind": "tiles", "columns": 4, "tiles": [
            tile("Valore OMI fabbricati", kpis["asset_value"]),
            tile("Valore catastale (base IMU)", kpis["cadastral_value"],
                 f"Fabbricati {kpis['cadastral_value_buildings']} · Terreni {lands}"),
            tile("Superficie fabbricati", kpis["building_area"], kpis["building_area_note"]),
            tile("Superficie terreni", kpis["grounds_area"], kpis["grounds_area_note"]),
            tile("Rendita fabbricati", kpis["building_income"], kpis["building_income_note"]),
            tile("Reddito dominicale", kpis["dominical_income"], kpis["dominical_income_note"]),
            tile("Reddito agrario", kpis["agricultural_income"], kpis["agricultural_income_note"]),
        ]},
        {"kind": "note", "text": kpis["asset_value_note"]},
        {"kind": "note", "text": kpis["cadastral_value_note"]},
        {"kind": "note", "text": "Valori catastali annui sommati dalla visura, non redditi effettivi e non ponderati "
                                 "per quota di proprietà."},
    ]


def _parcel_items(lookup: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parcel list and a vector sketch per comune from the local cadastral geometries (no map tiles needed)."""
    if not lookup:
        return []
    markers = [m for m in lookup.get("parcel_markers") or [] if isinstance(m, dict)]
    features = [f for f in lookup.get("parcel_features") or [] if isinstance(f, dict)]
    unmapped = [u for u in lookup.get("unmapped_parcel_items") or [] if isinstance(u, dict)]
    if not (markers or features or unmapped):
        return []

    with_geometry = {(_s(f.get("properties", {}).get("municipality_code")), _s(f.get("properties", {}).get("sheet")),
                      _s(f.get("properties", {}).get("parcel"))) for f in features}
    rows = []
    for item in markers:
        key = (_s(item.get("municipality_code")), _s(item.get("sheet")), _s(item.get("parcel")))
        rows.append([_s(item.get("municipality")), _s(item.get("sheet")), _s(item.get("parcel")),
                     "Geometria catastale" if key in with_geometry else _missing("Posizione approssimata (centro foglio)")])
    for item in unmapped:
        rows.append([_s(item.get("municipality")), _s(item.get("sheet")), _s(item.get("parcel")),
                     _missing("Senza geometria nella cartografia locale")])
    out: list[dict[str, Any]] = [_table("Particelle elencate nella visura",
                                        ["Comune", "Foglio", "Particella", "Cartografia"],
                                        ["1.4fr", "auto", "auto", "2fr"], ["left"] * 4, rows)]
    panels = _map_panels(markers, features, unmapped, [f for f in lookup.get("sheet_features") or [] if isinstance(f, dict)])
    if panels:
        out.append({"kind": "map", "panels": panels})
        out.append({"kind": "note", "text": "Schizzo vettoriale dalle geometrie catastali locali, senza cartografia di "
                                            "sfondo: non in scala tra un riquadro e l'altro. Le particelle senza "
                                            "geometria sono indicate da un punto al centro del foglio."})
    return out


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
                sheets: list[dict[str, Any]], width_mm: float = 88.0, max_height_mm: float = 62.0) -> list[dict[str, Any]]:
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

    panels = []
    for panel in by_comune.values():
        points = [(x, y) for ring, _l in panel["rings"] for x, y in ring] + [(x, y) for x, y, _l in panel["dots"]]
        if not points:
            continue
        lon0, lon1 = min(p[0] for p in points), max(p[0] for p in points)
        lat0, lat1 = min(p[1] for p in points), max(p[1] for p in points)
        lat_mid = (lat0 + lat1) / 2
        span_x = max((lon1 - lon0) * math.cos(math.radians(lat_mid)), 1e-9)
        span_y = max(lat1 - lat0, 1e-9)
        pad = 4.0
        if len(set(points)) == 1:  # a single dot: centre it in a small panel
            scale, height = 1.0, 24.0
            lon0, lat0 = lon0 - (width_mm / 2 - pad) / math.cos(math.radians(lat_mid)), lat0 - (height / 2 - pad)
        else:
            scale = min((width_mm - 2 * pad) / span_x, (max_height_mm - 2 * pad) / span_y)
            height = min(max_height_mm, span_y * scale + 2 * pad)

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
        panels.append({
            "title": panel["name"], "h": round(height, 1), "polygons": polygons, "dots": dots,
            "note": f"{len(polygons)} con geometria · {len(dots)} senza (punto)",
        })
    return panels


def subject_context(doc: dict[str, Any], kpis: dict[str, Any] | None, lookup: dict[str, Any] | None, today: date) -> dict[str, Any]:
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

    meta = [["Tipo catasto", _TIPO_CATASTO.get(tipo, tipo or "—")], ["Provincia", province or "—"],
            ["Situazione al", as_of or "—"], ["Protocollo", protocol]]
    if doc.get("id"):
        meta.append(["ID documento", str(doc["id"])])
    sections: list[dict[str, Any]] = [{
        "title": "Soggetto individuato", "badges": [], "items": [_kv([
            ["Nominativo", name, _s(subject.get("CodiceFiscale"))],
            ["Nascita", _s(subject.get("DataNascita")) or "—",
             " ".join(x for x in (_s(subject.get("ComuneNascita")), f"({_s(subject.get('Provincia'))})" if subject.get("Provincia") else "") if x)],
            ["Sesso", _s(subject.get("Sesso")) or "—"],
            ["Visura del", f"{_it_date(title_block.get('Data'))} {_s(title_block.get('Ora'))[:2]}:{_s(title_block.get('Ora'))[2:4]}".strip(" :")],
        ])],
    }]
    if kpis:
        assessment = kpis.get("owner_assessment") or {}
        sections.append({"title": "Valutazione patrimoniale per quota e diritto", "badges": [], "items": _assessment_items(assessment)})
        sections.append({"title": "Indicatori del patrimonio catastale", "badges": [], "items": _kpi_items(kpis)})

    groups = [g for g in _items(root.get("GruppoUnitaImmobiliari"))]
    estimates = (kpis or {}).get("table_estimates") or []
    with_omi = any(e.get("range") for group in estimates for e in group.get("buildings", []))
    for index, raw in enumerate(groups):
        group = merge_identifiers(raw)
        buildings = _building_rows(group, (estimates[index] if index < len(estimates) else {}).get("buildings", []), with_omi)
        lands = _land_rows(group, (estimates[index] if index < len(estimates) else {}).get("lands", []))
        badges = [f"Catasto {_TIPO_CATASTO.get(_s(group.get('TipoCatasto')), _s(group.get('TipoCatasto')))}".strip(),
                  f"{len(buildings) + len(lands)} immobili"]
        if group.get("Sezione"):
            badges.append(f"Sez. {_s(group['Sezione'])}")
        items: list[dict[str, Any]] = []
        if buildings:
            items.append(_table("Fabbricati (Catasto Fabbricati)", _BUILDING_HEADER + (["Stima OMI indicativa"] if with_omi else []),
                                _BUILDING_WIDTHS + (["1.5fr"] if with_omi else []), _BUILDING_ALIGN + (["right"] if with_omi else []),
                                buildings))
        if lands:
            items.append(_table("Terreni (Catasto Terreni)", _LAND_HEADER, _LAND_WIDTHS, _LAND_ALIGN, lands))
        mutations = _mutations(group)
        if mutations:
            items.append({"kind": "mutations", "title": f"Titolarità · {len(mutations)} {'mutazione' if len(mutations) == 1 else 'mutazioni'}", "items": mutations})
        sections.append({"title": _s(group.get("Descrizione")) or _s(group.get("CodiceComune")) or f"Gruppo {index + 1}",
                         "badges": badges, "items": items})

    parcel_items = _parcel_items(lookup)
    if parcel_items:
        sections.append({"title": "Mappa catastale", "badges": [], "items": parcel_items})
    sections.append({"title": "Avvertenze", "badges": [], "items": [{"kind": "note", "text": (
        "Documento generato da SISTER a partire dalla visura catastale. Le stime OMI e i valori catastali sono indicativi "
        "e non costituiscono perizia; i terreni non hanno quotazioni OMI. I dati personali qui riportati sono soggetti "
        "alla normativa sulla protezione dei dati.")}]})
    return {
        "title": name,
        "subtitle": f"Visura soggetto {variant} — {_TIPO_CATASTO.get(tipo, tipo)} — {province}".strip(" —"),
        "footer": f"SISTER · {name} · generato il {today.strftime('%d/%m/%Y')}",
        "meta": meta,
        "sections": sections,
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


def build_context(doc: dict[str, Any], kpis: dict[str, Any] | None, lookup: dict[str, Any] | None,
                  today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    xml = doc.get("xml_parsed") or {}
    if any(isinstance(xml.get(k), dict) for k in _SUBJECT_KEYS):
        return subject_context(doc, kpis, lookup, today)
    if doc.get("view"):
        return property_context(doc, today)
    raise PdfExportError("PDF export is available for visure only")


# ── typesetting ─────────────────────────────────────────────────────────────────────────────────────────


def render_pdf(context: dict[str, Any]) -> bytes:
    """Typeset ``context`` (see the header of ``visura.typ``) and return the PDF bytes."""
    try:
        import typst
    except ImportError as exc:  # pragma: no cover - declared in pyproject
        raise PdfExportError("the typst package is not installed") from exc
    workdir = Path(tempfile.mkdtemp(prefix="sister-pdf-"))
    try:
        shutil.copyfile(_TEMPLATE, workdir / "visura.typ")
        (workdir / "data.json").write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
        return typst.compile(str(workdir / "visura.typ"), root=str(workdir), format="pdf")
    except Exception as exc:  # typst raises a plain RuntimeError with the diagnostics
        logger.warning("Typst compilation failed: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        raise PdfExportError("Typst compilation failed") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
