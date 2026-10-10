"""Presentation model of a Visura XML (Fabbricati / Terreni, Attuale / Storica / Sintetica).

The document page used to walk a dict made by a generic XML-to-dict conversion, which groups repeated tags (so the
order of the history is lost), keeps the portal's raw strings and cannot tell a current owner from a former one.
This module reads the XML in document order and returns what a page needs, already interpreted:

* ``units``      the current state of each unit/parcel: identifier, classification, address, surface, related parcels,
                 current owners;
* ``history``    the property history as *periods* (identificativi, indirizzo, classamento, superficie of the same
                 period and derivation are one entry), newest first;
* ``ownership``  the ownership acts newest first, each with its owners (name, birth data, fiscal code, right, quota)
                 and the period of the right;
* ``header``     title, reference date, request, liquidation.

Semantics worth knowing (the portal does not state them):

* ``IndiceMutazione`` runs oldest → newest in Terreni and newest → oldest in Fabbricati, so acts are ordered by the
  dates of the rights they carry, not by their index;
* a right ``dal D al <data della visura>`` is still held (``ongoing``), it did not end on the visura date;
* a right listed ``dal <creazione dell'unità> al <data precedente>`` is a predecessor's right carried over by a
  variation; its start is dropped and only its end is shown.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from lxml import etree

from .result_parsers import (
    clean_quota,
    clean_right_description,
    gender_from_codice_fiscale,
    identifier_kind,
    parse_right_period,
    parse_xml_nominativo,
    squeeze,
)

_FAMILIES = {
    "VisuraFabbricatiAttuale": ("fabbricati", "attuale"),
    "VisuraFabbricatiStorica": ("fabbricati", "storica"),
    "VisuraFabbricatiSintetica": ("fabbricati", "sintetica"),
    "VisuraTerreniAttuale": ("terreni", "attuale"),
    "VisuraTerreniStorica": ("terreni", "storica"),
    "VisuraTerreniSintetica": ("terreni", "sintetica"),
    "VisuraTerrenoAttuale": ("terreni", "attuale"),
    "VisuraTerrenoStorica": ("terreni", "storica"),
    "VisuraTerrenoSintetica": ("terreni", "sintetica"),
}

_HISTORY_KINDS = {
    "DatiIdentificativi": "identificativi",
    "DatiIndirizzo": "indirizzo",
    "DatiClassamentoF": "classamento",
    "DatiClassamentoT": "classamento",
    "SuperficieF": "superficie",
    "DatiNoVariaz": "nessuna_variazione",
}
_KIND_LABELS = {
    "identificativi": "Identificativi",
    "indirizzo": "Indirizzo",
    "classamento": "Classamento",
    "superficie": "Superficie",
    "nessuna_variazione": "Nessuna variazione",
}
_KIND_ORDER = list(_KIND_LABELS)
_EXTRA_LABELS = {
    "RegimeConiugi": "Regime coniugi",
    "DirittoAggiuntivo": "Diritto aggiuntivo",
    "SoggettoComunione": "Soggetto in comunione",
}


def _humanize(name: str) -> str:
    import re

    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).capitalize()


# ---------------------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------------------


def _attrs(element) -> dict[str, str]:
    if element is None:
        return {}
    return {k: squeeze(v) for k, v in element.attrib.items() if not k.startswith("{")}


def _text(element) -> str:
    return squeeze(element.text) if element is not None else ""


def _date(value: str):
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def _ymd(value: str) -> str:
    """``20261010`` → ``10/10/2026`` (anything else is returned as is)."""
    text = squeeze(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[6:8]}/{text[4:6]}/{text[0:4]}"
    return text


def _hms(value: str) -> str:
    text = squeeze(value)
    return f"{text[0:2]}:{text[2:4]}:{text[4:6]}" if len(text) == 6 and text.isdigit() else text


def _identifier(element) -> dict[str, str]:
    """All the attributes of an ``IdentificativoDefinitivo`` / ``...Riferimento``, plus a one-line reference."""
    if element is None:
        return {}
    a = _attrs(element)
    out = {
        "province": a.get("Provincia", ""),
        "municipality": a.get("Comune", ""),
        "municipality_code": a.get("CodiceComune", ""),
        "sheet": a.get("Foglio", ""),
        "parcel": (a.get("ParticellaNum") or a.get("Particella", "")).lstrip("0") or a.get("ParticellaNum", ""),
        "parcel_denominator": a.get("ParticellaDenom", ""),
        "subunit": a.get("Subalterno", ""),
        "urban_section": a.get("SezUrbana", ""),
        "census_section": a.get("SezCensuaria", ""),
        "sequence_id": a.get("ProgrId", ""),
        "status": a.get("StatoImmobile", ""),
        "partita": _text(element.find("Partita")),
    }
    parts = []
    if out["municipality"]:
        parts.append(out["municipality"] + (f" ({out['municipality_code']})" if out["municipality_code"] else ""))
    elif out["municipality_code"]:
        parts.append(f"Comune {out['municipality_code']}")
    if out["urban_section"]:
        parts.append(f"Sez. urbana {out['urban_section']}")
    if out["census_section"]:
        parts.append(f"Sez. censuaria {out['census_section']}")
    if out["sheet"]:
        parts.append(f"FG {out['sheet']}")
    if out["parcel"]:
        parts.append(f"PT {out['parcel']}" + (f"/{out['parcel_denominator']}" if out["parcel_denominator"] else ""))
    if out["subunit"]:
        parts.append(f"Sub {out['subunit']}")
    out["reference"] = " · ".join(parts)
    return out


def _consistency(element) -> str:
    if element is None:
        return ""
    value, unit = squeeze(element.get("Valore")), squeeze(element.get("Unita"))
    return f"{value} {unit}".strip()


def _category(value: str) -> str:
    return squeeze(value).replace(":", "").strip()


def _amount(value: str) -> str:
    text = squeeze(value)
    return "" if text in ("", "-", ":") else text


def _split_category(value: str) -> tuple[str, str]:
    """``F/5 LASTRICO SOLARE`` → (``F/5``, ``LASTRICO SOLARE``); ``A07`` → (``A07``, "")."""
    import re

    match = re.match(r"^([A-Z]\s*/?\s*\d+[A-Z]?)\b\s*(.*)$", _category(value))
    return (re.sub(r"\s+", "", match.group(1)), match.group(2)) if match else (_category(value), "")


def _classification_f(element) -> dict[str, str]:
    a = _attrs(element)
    category, category_description = _split_category(a.get("Categoria", ""))
    return {
        "zone": a.get("ZonaCensuaria", ""),
        "category": category,
        "category_description": category_description,
        "class": a.get("Classe", ""),
        "consistency": _consistency(element.find("Consistenza")),
        "income_eur": _amount(a.get("RenditaEuro", "")),
        "income_lire": _amount(a.get("RenditaLire", "")),
        "partita": a.get("Partita", ""),
        "annotation": a.get("Annotazione", ""),
    }


def _classification_t(element) -> dict[str, str]:
    a = _attrs(element)
    area = a.get("SuperficieMQ", "")
    if not area and any(k in a for k in ("Ha", "Are", "Ca")):
        try:
            area = str(int(a.get("Ha") or 0) * 10000 + int(a.get("Are") or 0) * 100 + int(a.get("Ca") or 0))
        except ValueError:
            area = ""
    return {
        "quality": a.get("Qualita", ""),
        "class": a.get("Classe", ""),
        "area_m2": area,
        "dominical_income_eur": _amount(a.get("RedditoDominicaleEuro", "")),
        "dominical_income_lire": _amount(a.get("RedditoDominicaleLire", "")),
        "agricultural_income_eur": _amount(a.get("RedditoAgrarioEuro", "")),
        "agricultural_income_lire": _amount(a.get("RedditoAgrarioLire", "")),
        "deduction": a.get("SimboloDeduzione", ""),
        "portion": a.get("Porzione", ""),
    }


def _sum_it(values: list[str]) -> str:
    """Sum amounts written the Italian way (``1.234,56``); "" when none of them has a value."""
    from decimal import Decimal, InvalidOperation

    total, seen = Decimal(0), False
    for value in values:
        text = squeeze(value).replace(".", "").replace(",", ".")
        if not text:
            continue
        try:
            total += Decimal(text)
            seen = True
        except InvalidOperation:
            continue
    return f"{total:.2f}".replace(".", ",") if seen else ""


def _surface(element) -> dict[str, str]:
    a = _attrs(element)
    return {"total": a.get("Totale", ""), "excluded": a.get("TotaleE", "")}


def _derivation(element) -> dict[str, Any]:
    """``DatiDerivantiDa``: the act/practice that originated a record, its annotations and the other units it touched."""
    if element is None:
        return {"description": "", "annotations": [], "other_units": [], "other_units_label": ""}
    annotations = [
        squeeze(a.get("Descrizione") or a.text) for a in element.iter("Annotazione") if squeeze(a.get("Descrizione") or a.text)
    ]
    others = element.find("AltriImmobili")
    return {
        "description": squeeze(element.get("Descrizione")) or _text(element),
        "annotations": annotations,
        "other_units": [_identifier(i) for i in others.iter("IdentificativoDefinitivo")] if others is not None else [],
        "other_units_label": squeeze(others.get("Descrizione")) if others is not None else "",
    }


# ---------------------------------------------------------------------------------------------------------
# periods and owners
# ---------------------------------------------------------------------------------------------------------


def period_label(period: dict[str, Any]) -> str:
    start, end = period.get("start", ""), period.get("end", "")
    origin = period.get("from_origin")
    if period.get("ongoing"):
        return f"dal {start} · in corso" if start else ("dall'impianto · in corso" if origin else "in corso")
    if start and end:
        return f"dal {start} al {end}"
    if end:
        return f"dall'impianto al {end}" if origin else f"fino al {end}"
    return f"dal {start}" if start else (period.get("raw") or "")


def _owner(element, as_of: str) -> dict[str, Any]:
    name = parse_xml_nominativo(_text(element.find("Nominativo")))
    cf = _text(element.find("CF")).upper()
    rights = element.find("DirittiReali")
    r = _attrs(rights)
    period = parse_right_period(r.get("FineDiritto", ""), as_of)
    kind = identifier_kind(cf) or ("company" if name.get("registered_office") and not name.get("birth_date") else "")
    sex = name.get("sex") or (gender_from_codice_fiscale(cf) if kind == "person" else None) or ""
    known = {"Descrizione", "CodiceDiritto", "Quota", "FineDiritto"}
    return {
        "index": element.get("IndiceIntestato", ""),
        "name": name.get("name") or _text(element.find("Nominativo")),
        "note": name.get("note", ""),
        "kind": kind,
        "fiscal_code": cf,
        "sex": sex,
        "birth_date": name.get("birth_date", ""),
        "birth_date_partial": name.get("birth_date_partial", ""),
        "birth_place": name.get("birth_place", ""),
        "registered_office": name.get("registered_office", ""),
        "right": clean_right_description(r.get("Descrizione", "")),
        "right_code": r.get("CodiceDiritto", ""),
        "quota": clean_quota(r.get("Quota", "")),
        "period": {**period, "label": period_label(period)},
        "extra": {_EXTRA_LABELS.get(k, _humanize(k)): v for k, v in r.items() if k not in known and v},
    }


def _act(text: str) -> dict[str, str]:
    """``DENUNZIA (NEI PASSAGGI PER CAUSA DI MORTE) del 01/09/2012 - UU Sede ...`` → type, date, full text."""
    import re

    value = squeeze(text)
    match = re.search(r"\bdel\s+(\d{2}/\d{2}/\d{4})", value)
    kind = value[: match.start()].strip(" -") if match else ""
    return {"text": value, "type": kind, "date": match.group(1) if match else ""}


def _mutation(element, as_of: str, family: str) -> dict[str, Any]:
    owners = [_owner(o, as_of) for o in element.iter("Intestato")]
    act = _act(_text(element.find("DatiDerivantiDaMutazSogg")))
    index = squeeze(element.get("IndiceMutazione"))
    ends = [_date(o["period"]["end"]) for o in owners if o["period"]["end"]]
    starts = [_date(o["period"]["start"]) for o in owners if o["period"]["start"]]
    ongoing = any(o["period"]["ongoing"] for o in owners)
    try:
        position = int(index)
    except ValueError:
        position = 0
    return {
        "index": index,
        "act": act,
        "reference": _identifier(element.find("IdentificativoDefinitivoRiferimento")),
        "owners": owners,
        "ongoing": ongoing,
        "start": min(starts).strftime("%d/%m/%Y") if starts else "",
        "end": max(ends).strftime("%d/%m/%Y") if ends and not ongoing else "",
        # chronological key: acts whose rights are still held come last; then by the end of their rights, the act
        # date, and finally the portal index (ascending in Terreni, descending in Fabbricati)
        "_key": (
            1 if ongoing else 0,
            max(ends) if ends and not ongoing else date.min,
            _date(act["date"]) or date.min,
            position if family == "terreni" else -position,
        ),
    }


# ---------------------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------------------


def _unit(element, family: str, as_of: str) -> dict[str, Any]:
    ident = element.find("DatiIdentificativi/IdentificativoDefinitivo")
    if ident is None:
        ident = element.find("IdentificativoDefinitivo")
    unit: dict[str, Any] = {
        "identifier": _identifier(ident),
        "related": [
            {**_attrs(r), "reference": _identifier(r).get("reference", "")}
            for r in element.findall("MappaliCorrelati/IdentificativoCorrelato")
        ],
        "classification": {},
        "classifications": [],
        "address": "",
        "surface": {},
        "current_owners": [],
        "totals": {},
    }
    if family == "fabbricati":
        classification = element.find("DatiClassamentoF")
        unit["classification"] = _classification_f(classification) if classification is not None else {}
        address = element.find("DatiIndirizzo/IndirizzoImm")
        if address is None:
            address = element.find("IndirizzoImm")
        unit["address"] = _text(address)
        unit["surface"] = _surface(element.find("SuperficieF"))
    else:
        unit["classifications"] = [_classification_t(c) for c in element.findall("DatiClassamentoT/ClassamentoT")]
        rows = unit["classifications"]
        area = _sum_it([r["area_m2"] for r in rows])
        unit["totals"] = {
            "rows": len(rows),
            "area_m2": area.split(",")[0] if area else "",
            "dominical_eur": _sum_it([r["dominical_income_eur"] for r in rows]),
            "agricultural_eur": _sum_it([r["agricultural_income_eur"] for r in rows]),
        }
    current = element.find("IntestazioneAttuale")
    if current is None:
        current = element.find("Intestazione")
    if current is not None:
        unit["current_owners"] = [_owner(o, as_of) for o in current.iter("Intestato")]
    return unit


def _history(body, family: str, as_of: str) -> list[dict[str, Any]]:
    section = body.find("StoriaImmobileFabbricati" if family == "fabbricati" else "StoriaImmobileTerreni")
    if section is None:
        return []
    records: list[dict[str, Any]] = []
    for position, block in enumerate(section):
        kind = _HISTORY_KINDS.get(block.tag if isinstance(block.tag, str) else "")
        if kind is None:
            continue
        situation = squeeze(block.findtext(".//Situazione") or "")
        derived_el = block.find(".//DatiDerivantiDa")
        reference_el = block.find("IdentificativoDefinitivoRiferimento")
        if reference_el is None:
            reference_el = block.find("DatiIdentificativi/IdentificativoDefinitivo")
        if reference_el is None:
            reference_el = block.find("IdentificativoDefinitivo")
        payload: dict[str, Any] = {}
        if kind == "indirizzo":
            payload = {"address": _text(block.find("IndirizzoImm"))}
        elif kind == "classamento":
            if block.tag == "DatiClassamentoF":
                payload = _classification_f(block)
            else:
                payload = _classification_t(block.find("ClassamentoT")) if block.find("ClassamentoT") is not None else {}
        elif kind == "superficie":
            payload = _surface(block)
        elif kind == "identificativi":
            payload = _identifier(reference_el)
        period = parse_right_period(situation, as_of)
        records.append(
            {
                "kind": kind,
                "position": position,
                "period": {**period, "label": period_label(period)},
                "status": _identifier(reference_el).get("status", ""),
                "reference": _identifier(reference_el).get("reference", ""),
                "derived": _derivation(derived_el),
                "data": payload,
            }
        )

    # one entry per period: its identificativi/indirizzo/classamento/superficie are facets of it, each with the
    # derivation (act/practice) it carries; when all facets share it, it is shown once for the period
    periods: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = periods.setdefault(
            record["period"]["raw"],
            {
                "period": record["period"],
                "status": record["status"],
                "parts": {},
                "references": [],
                "_derivations": [],
                "_position": record["position"],
            },
        )
        entry["parts"].setdefault(record["kind"], []).append({"data": record["data"], "derived": record["derived"]})
        entry["_derivations"].append(record["derived"])
        if record["reference"] and record["reference"] not in entry["references"]:
            entry["references"].append(record["reference"])
        if record["status"] == "attuale":
            entry["status"] = "attuale"
    out = list(periods.values())

    def key(entry):
        period = entry["period"]
        start = _date(period["start"]) or date.min
        end = date.max if period["ongoing"] else (_date(period["end"]) or date.min)
        return (end, start, entry["_position"])

    out.sort(key=key, reverse=True)
    for entry in out:
        entry["facets"] = [
            {"kind": k, "label": _KIND_LABELS[k], "records": entry["parts"][k]} for k in _KIND_ORDER if k in entry["parts"]
        ]
        derivations = entry.pop("_derivations")
        same = all(d["description"] == derivations[0]["description"] and d["annotations"] == derivations[0]["annotations"]
                   for d in derivations)
        entry["derived"] = derivations[0] if same and derivations[0]["description"] else None
        entry["current"] = bool(entry["period"]["ongoing"])
        entry.pop("_position", None)
        entry.pop("parts", None)
    return out


def build_visura_view(content: str | bytes | None) -> dict[str, Any] | None:
    """The presentation model of a Fabbricati/Terreni visura, or None when the document is another shape."""
    if not content:
        return None
    raw = content.encode("utf-8", "replace") if isinstance(content, str) else content
    try:
        root = etree.fromstring(
            raw.replace(b"\x00", b""), etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        )
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None
    body = next((c for c in root if isinstance(c.tag, str) and c.tag in _FAMILIES), None)
    if body is None:
        return None
    family, variant = _FAMILIES[body.tag]
    title = _attrs(body.find("TitoloVisura"))
    as_of = _ymd(title.get("SituazioneAl", "")) or _ymd(title.get("Data", ""))
    request = body.find("DatiRichiesta")
    state_tags = (
        ("SituazioneAttualeFabbricati", "ImmobileFabbricati", "ImmobileFabbricatiS")
        if family == "fabbricati"
        else ("SituazioneAttualeTerreni", "ImmobileTerreni", "ImmobileTerreniS")
    )
    states = [e for tag in state_tags for e in body.findall(tag)]
    units = [_unit(s, family, as_of) for s in states]

    mutations: list[dict[str, Any]] = []
    for element in body.findall("StoriaIntestazione/MutazioneSoggettiva"):
        mutations.append(_mutation(element, as_of, family))
    if not mutations:
        for state in states:
            mutations += [_mutation(m, as_of, family) for m in state.findall("MutazioneSoggettiva")]
    mutations.sort(key=lambda m: m["_key"], reverse=True)  # newest first
    for position, mutation in enumerate(mutations):
        mutation["current"] = position == 0 and mutation["ongoing"]
        mutation.pop("_key", None)

    # a state without any act still has its current owners: show them as the one entry of the ownership
    ownership = mutations
    if not ownership:
        current_owners = [o for u in units for o in u["current_owners"]]
        if current_owners:
            ownership = [
                {"index": "", "act": {"text": "Intestazione attuale", "type": "", "date": ""}, "reference": {},
                 "owners": current_owners, "ongoing": True, "start": "", "end": "", "current": True}
            ]
    for unit in units:
        if not unit["current_owners"]:
            unit["current_owners"] = [o for m in ownership if m.get("current") for o in m["owners"]]

    return {
        "family": family,
        "variant": variant,
        "as_of": as_of,
        "header": {
            "title": title.get("Titolo", ""),
            "view_type": title.get("TipoVisura", ""),
            "service": title.get("TipoServizio", ""),
            "provenance": title.get("Provenienza", ""),
            "date": _ymd(title.get("Data", "")),
            "time": _hms(title.get("Ora", "")),
        },
        "request": {**_attrs(request), "identifier": _identifier(request.find("ImmobileIndividuato/IdentificativoDefinitivo"))
                    if request is not None and request.find("ImmobileIndividuato/IdentificativoDefinitivo") is not None
                    else {}},
        "liquidation": _attrs(body.find("DatiLiquidazione")),
        "requester": _attrs(body.find("Richiedente")).get("Descrizione", ""),
        "units": units,
        "history": _history(body, family, as_of),
        "ownership": ownership,
        "ownership_current_count": sum(1 for m in ownership if m.get("current")),
    }
