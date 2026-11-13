"""Shared subject-visura rows used by the HTML page and Typst PDF export."""

from __future__ import annotations

import hashlib
import re
from typing import Any


def items(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def first(value: Any) -> dict:
    return next(iter(items(value)), {})


def _value_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("_text") or "")
    return str(value or "")


def hub_anchor_part(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    return text.lstrip("0") or ("0" if text else "") if text.isdigit() else text


def hub_parcel_anchor_id(parcel: dict) -> str:
    identity = "\x1f".join(
        hub_anchor_part(value)
        for value in (
            parcel.get("province_code") or parcel.get("province"),
            parcel.get("municipality_code") or parcel.get("municipality"),
            parcel.get("section"),
            parcel.get("sheet"),
            parcel.get("parcel"),
        )
    )
    return "hub-parcel-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def subject_parcel_anchor(
    profile: dict | None,
    province: Any,
    municipality: Any,
    section: Any,
    sheet: Any,
    parcel: Any,
    ownership_only: bool = False,
) -> str | None:
    """Find the rendered parcel-sheet anchor for a subject-group or ownership-history row."""
    if not profile:
        return None
    index = profile.get("parcel_ownership_anchor_index" if ownership_only else "parcel_anchor_index") or {}
    province_key, municipality_key = hub_anchor_part(province), hub_anchor_part(municipality)
    section_key, sheet_key, parcel_key = map(hub_anchor_part, (section, sheet, parcel))
    for province_alias in (province_key, ""):
        for municipality_alias in (municipality_key, ""):
            if not municipality_alias:
                continue
            for section_alias in (section_key, ""):
                key = (province_alias, municipality_alias, section_alias, sheet_key, parcel_key)
                if key in index:
                    return index[key]
    return None


def build_subject_visura_view(xml_parsed: dict, profile: dict | None, subject_code: str | None) -> dict:
    """Prepare normalized group, property, and count rows for both page renderers."""
    roots = (
        "VisuraSoggettoAttuale", "VisuraSoggettoStorica", "VisuraSoggettoSintetica", "VisuraSoggetto"
    )
    body = next((xml_parsed.get(key) for key in roots if isinstance(xml_parsed.get(key), dict)), {})
    groups = []
    subject_key = str(subject_code or "").strip().upper()
    total_buildings = total_lands = 0

    for source_index, source in enumerate(items(body.get("GruppoUnitaImmobiliari"))):
        buildings, lands, unlinked_histories = [], [], []
        sheet_values: set[str] = set()
        group_province = source.get("Provincia") or ""
        group_municipality = source.get("Descrizione") or source.get("CodiceComune") or ""
        group_section = source.get("Sezione") or ""
        for row_key, output in (("ImmobileFabbricatiS", buildings), ("ImmobileTerreniS", lands)):
            for row in items(source.get(row_key)):
                identifier = first(first(row.get("DatiIdentificativi")).get("IdentificativoDefinitivo"))
                sheet = identifier.get("Foglio") or ""
                parcel_number = identifier.get("ParticellaNum") or identifier.get("Particella") or ""
                if sheet:
                    sheet_values.add(str(sheet).strip())
                anchor = subject_parcel_anchor(
                    profile,
                    identifier.get("Provincia") or group_province,
                    identifier.get("CodiceComune") or source.get("CodiceComune")
                    or identifier.get("Comune") or group_municipality,
                    identifier.get("SezUrbana") or identifier.get("Sezione")
                    or identifier.get("SezCensuaria") or group_section,
                    sheet,
                    parcel_number,
                ) if profile else None
                if source.get("FonteElenco") and row.get("StoriaTitolarita") and not anchor:
                    unlinked_histories.append({
                        "sheet": sheet,
                        "parcel": parcel_number,
                        "subunit": identifier.get("Subalterno"),
                        "events": items(row.get("StoriaTitolarita")),
                    })

                if row_key == "ImmobileFabbricatiS":
                    classification = first(row.get("DatiClassamentoF"))
                    consistency = first(classification.get("Consistenza"))
                    surface = first(row.get("SuperficieF"))
                    address_container = first(row.get("DatiIndirizzo"))
                    address = row.get("IndirizzoImm") or address_container.get("IndirizzoImm")
                    category = str(classification.get("Categoria") or "").strip()
                    category_code, separator, category_description = category.partition(":")
                    output.append({
                        "address": _value_text(address),
                        "municipality": group_municipality or identifier.get("Comune"),
                        "municipality_label": source.get("Descrizione") or identifier.get("Comune") or source.get("CodiceComune"),
                        "municipality_code": identifier.get("CodiceComune"),
                        "province": identifier.get("Provincia") or group_province,
                        "sheet": sheet,
                        "parcel": parcel_number,
                        "subunit": identifier.get("Subalterno"),
                        "parcel_anchor": anchor,
                        "section": identifier.get("SezUrbana") or identifier.get("SezCensuaria") or group_section,
                        "census_zone": classification.get("ZonaCensuaria"),
                        "category": category.replace(":", "").strip(),
                        "category_code": category_code.strip() if separator else category,
                        "category_description": category_description.strip(),
                        "cadastral_class": classification.get("Classe"),
                        "consistency": (
                            f"{consistency.get('Valore')} {consistency.get('Unita') or ''}".strip()
                            if consistency.get("Valore") else (f"{surface.get('Totale')} MQ" if surface.get("Totale") else "")
                        ),
                        "consistency_value": consistency.get("Valore"),
                        "consistency_unit": consistency.get("Unita"),
                        "surface_total": surface.get("Totale"),
                        "surface_excluded": surface.get("EscluseAreeScoperte"),
                        "income": f"€ {classification.get('RenditaEuro')}" if classification.get("RenditaEuro") else "",
                        "income_amount": classification.get("RenditaEuro"),
                        "partita": identifier.get("Partita"),
                    })
                else:
                    classamento = first(row.get("DatiClassamentoT")).get("ClassamentoT")
                    for classification in items(classamento) or [{}]:
                        output.append({
                            "municipality": group_municipality or identifier.get("Comune"),
                            "municipality_label": source.get("Descrizione") or identifier.get("Comune") or source.get("CodiceComune"),
                            "municipality_code": identifier.get("CodiceComune"),
                            "province": identifier.get("Provincia") or group_province,
                            "sheet": sheet,
                            "parcel": parcel_number,
                            "parcel_anchor": anchor,
                            "section": identifier.get("SezCensuaria") or identifier.get("Sezione") or group_section,
                            "quality": classification.get("Qualita"),
                            "cadastral_class": classification.get("Classe"),
                            "area": f"{classification.get('SuperficieMQ')} MQ" if classification.get("SuperficieMQ") else "",
                            "area_sqm": classification.get("SuperficieMQ"),
                            "dominical_income": classification.get("RedditoDominicaleEuro"),
                            "agricultural_income": classification.get("RedditoAgrarioEuro"),
                            "partita": identifier.get("Partita"),
                        })

        intestazione = first(source.get("IntestazioneGruppo"))
        mutations = items(intestazione.get("MutazioneSoggettiva"))
        right, share = "", ""
        if mutations:
            latest = mutations[-1]
            owners = items(first(latest.get("Intestazione")).get("Intestato")) or items(latest.get("Intestato"))
            for owner in owners:
                owner_code = str(owner.get("CF") or owner.get("CodiceFiscale") or "").strip().upper()
                if subject_key and owner_code == subject_key:
                    title = first(owner.get("DirittiReali"))
                    right, share = str(title.get("Descrizione") or ""), str(title.get("Quota") or "")
                    break
        elenco_title = first(source.get("TitolaritaElenco"))
        right = right or str(elenco_title.get("Diritto") or "")
        share = share or str(elenco_title.get("Quota") or "")
        building_count = len(items(source.get("ImmobileFabbricatiS")))
        land_count = len(items(source.get("ImmobileTerreniS")))
        total_buildings += building_count
        total_lands += land_count
        groups.append({
            "source_index": source_index,
            "source": source,
            "buildings": buildings,
            "lands": lands,
            "building_count": building_count,
            "land_count": land_count,
            "sheet_list": sorted(sheet_values, key=lambda sheet: (0, int(sheet)) if sheet.isdigit() else (1, sheet)),
            "right": right,
            "share": share,
            "mutations": mutations,
            "unlinked_histories": unlinked_histories,
        })

    groups.sort(key=lambda group: (
        str(group["source"].get("Descrizione") or group["source"].get("CodiceComune") or "").strip().lower(),
        min((int(value) for value in group["sheet_list"] if value.isdigit()), default=10**12),
    ))
    return {
        "groups": groups,
        "group_count": len(groups),
        "total_buildings": total_buildings,
        "total_lands": total_lands,
    }
