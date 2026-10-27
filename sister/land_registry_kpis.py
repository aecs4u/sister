"""Portfolio area totals, cadastral (IMU) values and indicative OMI estimates for subject visure.

Two different figures are produced, and the page must not blur them:

- *valore catastale*: the statutory tax base from rendita / reddito dominicale (``cadastral_values``), for buildings
  and land alike;
- *stima OMI*: an indicative market range from the official OMI quotes (``omi_quotes``), buildings only, because OMI
  publishes no land quotes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from typing import Any

from . import omi_quotes
from .cadastral_values import building_cadastral_value, building_multiplier, land_cadastral_value, normalize_category
from .omi_zones import omi_zone_for_parcel
from .result_parsers import (
    birth_date_from_codice_fiscale,
    clean_quota,
    clean_right_description,
    correct_century,
    parse_right_period,
    parse_xml_nominativo,
)

logger = logging.getLogger("sister")

_SUBJECT_KEYS = (
    "VisuraSoggettoAttuale",
    "VisuraSoggettoStorica",
    "VisuraSoggettoSintetica",
    "VisuraSoggetto",
)

# Coefficients for life usufruct under the 2.5% reference-rate table in force for 2026. The bare-ownership share is
# the complement of the usufruct share. Keep the source and method visible in the rendered assessment.
_USUFRUCT_COEFFICIENTS = (
    (20, 38), (30, 36), (40, 34), (45, 32), (50, 30), (53, 28), (56, 26), (60, 24), (63, 22),
    (66, 20), (69, 18), (72, 16), (75, 14), (78, 12), (82, 10), (86, 8), (92, 6), (99, 4),
)
_USUFRUCT_SOURCE = (
    "https://www.gazzettaufficiale.it/atto/serie_generale/caricaArticoloDefault/originario?"
    "atto.codiceRedazionale=25A07054&atto.dataPubblicazioneGazzetta=2025-12-31&atto.tipoProvvedimento=DECRETO"
)


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first(value: Any) -> dict[str, Any]:
    values = _items(value)
    return values[0] if values else {}


def _subject_group_table_sort_key(group: dict[str, Any]) -> tuple[str, str]:
    """Match the municipality/sheet order used by catasto/visura_soggetto_archive.html."""
    municipality = str(group.get("Descrizione") or group.get("CodiceComune") or "").strip().lower()
    sheets: list[str] = []
    for key in ("ImmobileFabbricatiS", "ImmobileTerreniS"):
        for row in _items(group.get(key)):
            identifiers = _first(row.get("DatiIdentificativi"))
            identifier = _first(identifiers.get("IdentificativoDefinitivo"))
            sheet = str(identifier.get("Foglio") or "").strip()
            if sheet:
                sheets.append(sheet.zfill(12))
    return municipality, min(sheets, default="999999999999")


def _date_value(value: Any) -> date | None:
    text = str(value or "").strip()
    for pattern in ("%d/%m/%Y", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _subject_assessment_context(subject: dict[str, Any]) -> tuple[str, int | None, date | None]:
    request = _first(subject.get("DatiRichiesta"))
    selected = _first(request.get("SoggettoIndividuato"))
    identity = _first(selected.get("SoggettoPF")) or _first(selected.get("SoggettoPNF"))
    if not identity:
        identity = _first(subject.get("SoggettoPF")) or _first(subject.get("SoggettoPNF"))
    fiscal_code = str(identity.get("CodiceFiscale") or "").strip().upper()

    title = _first(subject.get("TitoloVisura"))
    as_of = _date_value(title.get("SituazioneAl"))
    birth_date = _date_value(identity.get("DataNascita"))
    age = None
    if birth_date and as_of and birth_date <= as_of:
        age = as_of.year - birth_date.year - ((as_of.month, as_of.day) < (birth_date.month, birth_date.day))
    return fiscal_code, age, as_of


def _share_fraction(value: Any) -> Decimal | None:
    """Parse a SISTER ownership quota without guessing when it is absent or invalid."""
    text = clean_quota(value).replace(" ", "")
    if not text:
        return None
    try:
        if text.endswith("%"):
            share = Decimal(text[:-1].replace(",", ".")) / Decimal(100)
        elif "/" in text:
            numerator, denominator = text.split("/", 1)
            if not denominator:
                return None
            share = Decimal(numerator.replace(",", ".")) / Decimal(denominator.replace(",", "."))
        else:
            share = Decimal(text.replace(",", "."))
    except (InvalidOperation, ZeroDivisionError):
        return None
    return share if share.is_finite() and Decimal(0) < share <= Decimal(1) else None


def _right_kind(description: Any, code: Any) -> str:
    text = unicodedata.normalize("NFKD", str(description or "")).encode("ascii", "ignore").decode().casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if "nuda proprieta" in text:
        return "bare"
    if "usufrutto" in text or (not text and str(code or "").strip().zfill(2) == "02"):
        return "usufruct"
    if "enfiteus" in text or "enfiteut" in text or (not text and str(code or "").strip().zfill(2) == "05"):
        return "enfiteusi"
    # "proprieta per l'area" (01T) is the landowner of a surface right: the building belongs to the superficiario
    if "proprieta" in text and not any(term in text for term in ("superficiar", "separata", "concedente", "per l area")):
        return "full"
    if not text and str(code or "").strip().zfill(2) == "01":
        return "full"
    return "other"


def _age_on(birth: date | None, as_of: date | None) -> int | None:
    if birth is None or as_of is None or birth > as_of:
        return None
    return as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))


def _owner_right(owner: dict[str, Any], as_of: date | None, as_of_text: str) -> dict[str, Any] | None:
    """One owner row as a right held on ``as_of``; ``None`` when the right had already ended or is malformed."""
    right = _first(owner.get("DirittiReali"))
    period_text = str(right.get("FineDiritto") or "")
    period = parse_right_period(period_text, as_of_text)
    if period.get("inverted"):
        return None
    end_date = _date_value(period.get("end"))
    if end_date and (not as_of or end_date < as_of):
        return None
    description = str(right.get("Descrizione") or "").strip()
    quota = clean_quota(right.get("Quota", ""))
    right_dates = re.findall(r"\d{2}/\d{2}/\d{4}", period_text)
    fixed_term = bool(
        right_dates and as_of and (right_end := _date_value(right_dates[-1])) and right_end > as_of
    ) or bool(re.search(r"\b(?:temporane[oa]|a termine)\b", description, flags=re.IGNORECASE))
    nominative = str(owner.get("Nominativo") or "")
    parsed_nominative = parse_xml_nominativo(nominative)
    code = str(right.get("CodiceDiritto") or "").strip().upper()
    owner_cf = str(owner.get("CF") or owner.get("CodiceFiscale") or "").strip().upper()
    birth_date_text = correct_century(parsed_nominative.get("birth_date", ""), owner_cf, as_of)
    if not birth_date_text:
        birth_date_text = birth_date_from_codice_fiscale(
            owner_cf, as_of, partial=parsed_nominative.get("birth_date_partial", "")
        ) or ""
    return {
        "kind": _right_kind(description, code),
        "label": description or "Diritto non specificato",
        "quota": quota,
        "share": _share_fraction(quota),
        "fixed_term": fixed_term,
        # Optional contract/authority input; the SISTER visura does not ordinarily carry this amount.
        "affrancation_value": _assessment_amount(
            right.get("ImportoAffrancazione", right.get("affrancation_value"))
        ),
        # usufrutto con diritto di accrescimento (08A): the usufruct lasts until the last of the holders dies
        "accrual": code.endswith("A") or "accrescimento" in description.casefold(),
        "name": parsed_nominative.get("name") or " ".join(nominative.split(";")[0].split()),
        "age": _age_on(_date_value(birth_date_text), as_of),
    }


def _current_group_rights(group: dict[str, Any], subject_cf: str, as_of: date | None) -> list[dict[str, Any]]:
    """Return the identified person's latest current rights for this municipality/property group.

    Every right also carries ``usufructuaries``: the usufruct holders of the same act (name, age, share, accrual),
    because the value of a bare ownership depends on *their* age, not on the owner's.
    """
    if not subject_cf:
        return []
    heading = _first(group.get("IntestazioneGruppo"))
    mutations = _items(heading.get("MutazioneSoggettiva"))
    matches: list[tuple[tuple[date, int], dict[str, Any]]] = []
    as_of_text = as_of.strftime("%d/%m/%Y") if as_of else ""
    for event_index, event in enumerate(mutations):
        intestazione = _first(event.get("Intestazione"))
        owners = _items(intestazione.get("Intestato")) or _items(event.get("Intestato"))
        raw_act = event.get("DatiDerivantiDaMutazSogg")
        act_text = raw_act.get("_text", "") if isinstance(raw_act, dict) else str(raw_act or "")
        act_date_match = re.search(r"\bdel\s+(\d{2}/\d{2}/\d{4})", act_text, flags=re.IGNORECASE)
        act_date = _date_value(act_date_match.group(1)) if act_date_match else None
        rank = (act_date or date.min, event_index)
        held = [(owner, _owner_right(owner, as_of, as_of_text)) for owner in owners]
        usufructuaries = [
            {key: right[key] for key in ("name", "age", "share", "accrual", "fixed_term")}
            for _owner, right in held
            if right and right["kind"] == "usufruct"
        ]
        for owner, right in held:
            owner_cf = str(owner.get("CF") or owner.get("CodiceFiscale") or "").strip().upper()
            if owner_cf != subject_cf or right is None:
                continue
            matches.append((rank, {**right, "usufructuaries": usufructuaries}))
    if not matches:
        return []
    latest_rank = max(rank for rank, _right in matches)
    return [right for rank, right in matches if rank == latest_rank]


def _usufruct_factor(age: int | None) -> Decimal | None:
    if age is None:
        return None
    for max_age, coefficient in _USUFRUCT_COEFFICIENTS:
        if age <= max_age:
            return Decimal(coefficient) / Decimal(40)
    return None


def _quota_label(right: dict[str, Any]) -> str:
    """'1000/1000' -> '1/1', '500/1000' -> '1/2'; the portal's own text when it is not a plain fraction."""
    share = right.get("share")
    if share is None:
        return right.get("quota") or "quota non indicata"
    fraction = Fraction(share).limit_denominator(10000)
    return f"{fraction.numerator}/{fraction.denominator}"


def _usufruct_fraction(usufructuaries: list[dict[str, Any]]) -> tuple[Decimal | None, str]:
    """Value of the whole usufruct as a fraction of the full ownership, from the *usufructuaries'* ages.

    Holders with the right of accrual (08A) keep the usufruct until the last of them dies, so their share is valued on
    the youngest one; the others on their own age. The bare ownership is the complement.
    """
    if not usufructuaries:
        return None, "Usufruttuario non indicato"
    if any(item.get("fixed_term") for item in usufructuaries):
        return None, "Durata determinata"
    total = Decimal(0)
    groups = (
        [item for item in usufructuaries if item.get("accrual")],
        *([item] for item in usufructuaries if not item.get("accrual")),
    )
    for group in groups:
        if not group:
            continue
        ages = [item.get("age") for item in group]
        if None in ages:
            return None, "Età dell'usufruttuario non disponibile"
        factor = _usufruct_factor(min(ages))
        if factor is None:
            return None, "Età fuori dal prospetto fiscale 2026"
        shares = [item.get("share") for item in group]
        if None in shares:
            return None, "Quota dell'usufruttuario non interpretabile"
        total += factor * sum(shares)
    return min(total, Decimal(1)), ""


def _assessment_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _assessment_amount(value: Any) -> Decimal | None:
    """Parse an explicitly supplied redemption amount, including Italian decimal notation."""
    if isinstance(value, dict):
        value = value.get("_text")
    if value is None:
        return None
    text = str(value).strip().replace("€", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        if "." in text and text.rfind(",") < text.rfind("."):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or ("." in text and len(text.rsplit(".", 1)[1]) == 3):
        text = text.replace(".", "")
    return _assessment_decimal(text)


def _infer_affrancation_amount(asset: dict[str, Any]) -> Decimal | None:
    """Estimate 15 annual cadastral-income units as a visible proxy for the missing canon."""
    income_key = "building_income" if asset.get("table_kind") == "buildings" else "dominical_income"
    annual_income = _assessment_decimal(asset.get(income_key))
    if annual_income is None or annual_income <= 0:
        return None
    return (annual_income * Decimal(15)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _affrancation_amount(right: dict[str, Any], asset: dict[str, Any]) -> Decimal | None:
    """Resolve an account override, a supplied amount, or the cadastral-income proxy."""
    override = _assessment_amount(asset.get("affrancation_amount_override"))
    if override is not None:
        return override
    supplied = _assessment_amount(right.get("affrancation_value"))
    return supplied if supplied is not None else _infer_affrancation_amount(asset)


def _asset_identity(
    *,
    cadastre_type: str,
    municipality_code: Any,
    municipality: Any,
    province: Any,
    section: Any,
    sheet: Any,
    parcel: Any,
    subunit: Any = "",
) -> dict[str, str] | None:
    """Build a stable cadastral identity for one property row and its user-entered valuations."""
    def clean(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("_text") or value.get("value")
        elif isinstance(value, list):
            value = value[0] if value else ""
            if isinstance(value, dict):
                value = value.get("_text") or value.get("value")
        text = str(value or "").strip()
        return re.sub(r"\s+", " ", text)

    identity = {
        "cadastre_type": cadastre_type,
        "municipality_code": clean(municipality_code).upper(),
        "municipality": clean(municipality),
        "province": clean(province).upper(),
        "section": clean(section).upper(),
        "sheet": clean(sheet).upper(),
        "parcel": clean(parcel).upper(),
        "subunit": clean(subunit).upper(),
    }
    if (
        not (identity["municipality_code"] or identity["municipality"])
        or not identity["sheet"]
        or not identity["parcel"]
    ):
        return None

    def token(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold())
        ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]", "", ascii_value)

    key_fields = (
        identity["cadastre_type"], identity["municipality_code"] or token(identity["municipality"]),
        "" if identity["municipality_code"] else token(identity["province"]),
        token(identity["section"]), token(identity["sheet"]),
        token(identity["parcel"]), token(identity["subunit"]),
    )
    identity["asset_key"] = hashlib.sha256("|".join(key_fields).encode("utf-8")).hexdigest()
    return identity


def _preferred_valuation_estimates(
    table_estimates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return row estimates selected by each user's source preference for the owner-right calculation."""
    preferred = []
    for group in table_estimates:
        preferred_group = {"buildings": [], "lands": []}
        for kind in ("buildings", "lands"):
            for estimate in group.get(kind, []):
                selected = dict(estimate)
                source = estimate.get("preferred_source")
                if source == "omi":
                    pass  # value_min/value_max already carry the official OMI range for building rows.
                elif source:
                    provider_value = next(
                        (value for value in estimate.get("manual_valuations", []) if value.get("provider") == source),
                        None,
                    )
                    if provider_value and kind == "buildings":
                        selected["value_min"] = provider_value.get("value_min")
                        selected["value_max"] = provider_value.get("value_max") or provider_value.get("value_min")
                        selected["range"] = provider_value.get("range", "")
                        selected["note"] = provider_value.get("note", "")
                    else:
                        selected["value_min"] = selected["value_max"] = None
                else:
                    selected["value_min"] = selected["value_max"] = None
                preferred_group[kind].append(selected)
        preferred.append(preferred_group)
    return preferred


_MANUAL_PROVIDER_LABELS = {
    "immobiliare": "Immobiliare.it",
    "idealista": "idealista",
    "other": "Altro",
}


def apply_user_manual_valuations(
    kpis: dict[str, Any],
    saved_values: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Add a user's provider valuations and preferred-source display to a subject KPI result."""
    table_estimates = kpis.get("table_estimates") or []
    kpis.pop("asset_value_adjusted", None)
    kpis.pop("asset_value_adjusted_note", None)
    adjusted_value_min = Decimal(0)
    adjusted_value_max = Decimal(0)
    adjusted_units = 0
    overridden_units = 0
    for group in table_estimates:
        for kind in ("buildings", "lands"):
            for estimate in group.get(kind, []):
                identity = estimate.get("asset_identity") or {}
                asset_key = identity.get("asset_key")
                saved = saved_values.get(asset_key, {}) if asset_key else {}
                base_min = _assessment_decimal(estimate.get("value_min"))
                base_max = _assessment_decimal(estimate.get("value_max"))
                if kind == "buildings" and base_min is not None and base_max is not None and base_max >= base_min:
                    override = saved.get("omi_override") or {}
                    override_min = _assessment_decimal(override.get("value_min"))
                    override_max = _assessment_decimal(override.get("value_max")) or override_min
                    has_override = (
                        override_min is not None
                        and override_min > 0
                        and override_max is not None
                        and override_max >= override_min
                    )
                    estimate["omi_override"] = ({
                        "value_min": format(override_min, ".2f"),
                        "value_max": (
                            format(override_max, ".2f") if override.get("value_max") is not None else ""
                        ),
                        "range": (
                            _format_value_range(override_min, override_max)
                            if override_min != override_max else _format_currency_it(override_min)
                        ),
                        "is_override": True,
                    } if has_override else None)
                    adjusted_value_min += override_min if has_override else base_min
                    adjusted_value_max += override_max if has_override else base_max
                    adjusted_units += 1
                    if has_override:
                        overridden_units += 1
                else:
                    estimate["omi_override"] = None
                manual_valuations = []
                for row in saved.get("valuations", []):
                    provider = str(row.get("provider") or "")
                    if provider not in _MANUAL_PROVIDER_LABELS:
                        continue
                    low = _assessment_decimal(row.get("value_min"))
                    high = _assessment_decimal(row.get("value_max"))
                    if low is None:
                        continue
                    high = high if high is not None else low
                    label = _MANUAL_PROVIDER_LABELS[provider]
                    manual_valuations.append({
                        "provider": provider,
                        "provider_label": label,
                        "value_min": str(low),
                        "value_max": str(high),
                        "range": _format_value_range(low, high) if low != high else _format_currency_it(low),
                        "source_date": str(row.get("source_date") or ""),
                        "source_url": str(row.get("source_url") or ""),
                        "notes": str(row.get("notes") or ""),
                        "note": (
                            f"Valore inserito manualmente da {label}"
                            + (f" il {row['source_date']}" if row.get("source_date") else "")
                            + ". Non verificato da SISTER."
                        ),
                    })
                estimate["manual_valuations"] = manual_valuations
                omi_available = (
                    kind == "buildings"
                    and _assessment_decimal(estimate.get("value_min")) is not None
                    and _assessment_decimal(estimate.get("value_max")) is not None
                )
                available_sources = {item["provider"] for item in manual_valuations}
                saved_preference = saved.get("preferred_source")
                if saved_preference == "omi" and omi_available:
                    preferred_source = "omi"
                elif saved_preference in available_sources:
                    preferred_source = saved_preference
                elif omi_available:
                    preferred_source = "omi"
                else:
                    preferred_source = next(
                        (
                            provider
                            for provider in ("immobiliare", "idealista", "other")
                            if provider in available_sources
                        ),
                        "",
                    )
                estimate["preferred_source"] = preferred_source
                selected = next((item for item in manual_valuations if item["provider"] == preferred_source), None)
                if preferred_source == "omi" and omi_available:
                    selected = {
                        "provider": "omi",
                        "provider_label": "OMI",
                        "value_min": float(estimate["value_min"]),
                        "value_max": float(estimate["value_max"]),
                        "range": estimate.get("range") or "",
                    }
                estimate["preferred_valuation"] = selected

    if overridden_units:
        kpis["asset_value_adjusted"] = (
            _format_value_range(adjusted_value_min, adjusted_value_max)
            if adjusted_value_min != adjusted_value_max else _format_currency_it(adjusted_value_min)
        )
        kpis["asset_value_adjusted_note"] = (
            f"Totale indicativo su {adjusted_units} righe di fabbricato stimabili: intervallo personalizzato applicato "
            f"a {overridden_units}; per le altre resta la fascia OMI originale. "
            "La fascia OMI originale rimane invariata nel riquadro principale."
        )

    assessment_assets = kpis.pop("_assessment_assets", [])
    age = kpis.pop("_subject_age", None)
    for asset in assessment_assets:
        estimate = table_estimates[asset["group_index"]][asset["table_kind"]][asset["row_index"]]
        asset_key = (estimate.get("asset_identity") or {}).get("asset_key")
        saved_amount = saved_values.get(asset_key, {}).get("affrancation_amount") if asset_key else None
        if saved_amount is not None:
            asset["affrancation_amount_override"] = saved_amount
    has_manual_preference = any(
        estimate.get("preferred_source") not in ("", "omi")
        for group in table_estimates
        for estimate in group.get("buildings", [])
    )
    if assessment_assets:
        kpis["owner_assessment"] = _owner_assessment(
            assessment_assets,
            _preferred_valuation_estimates(table_estimates),
            age,
        )
    if has_manual_preference:
        assessment = kpis.get("owner_assessment") or {}
        assessment["preferred_source_used"] = True
        for tile in assessment.get("tiles", []):
            if tile.get("label") == "Totale diritti stimati OMI":
                tile["label"] = "Totale diritti stimati · fonte preferita"
                tile["hint"] = (
                    "Somma delle stime attribuibili calcolate usando per ogni fabbricato la fonte selezionata."
                )
            elif tile.get("label") == "Piena proprietà · quota" and "Stima OMI" in tile.get("hint", ""):
                tile["hint"] = "Stima con la fonte preferita per il fabbricato attribuito al soggetto."
            elif tile.get("label") == "Usufrutto · quota" and "Stima OMI" in tile.get("hint", ""):
                tile["hint"] = "Stima con la fonte preferita, ripartita in base all'età del titolare."
            elif tile.get("label") == "Nuda proprietà · quota" and "Valore pieno" in tile.get("hint", ""):
                tile["hint"] = (
                    "Valore con la fonte preferita meno quello dell'usufrutto, in base all'età degli usufruttuari."
                )
            elif tile.get("label") == "Enfiteusi · quota" and "Stima OMI attribuibile" in tile.get("hint", ""):
                tile["hint"] = (
                    "Stima attribuibile con la fonte preferita: valore pieno del bene meno l'importo di affrancazione "
                    "indicato o stimato per l'intero bene, poi × quota. La stima automatica è modificabile."
                )
        assessment["method_note"] = (
            "La stima per quota e diritto usa, per ciascun fabbricato, la fonte selezionata dall'utente: "
            "quotazione OMI "
            "oppure valore inserito manualmente da un portale esterno. I valori esterni non sono verificati da SISTER "
            "e possono essere prezzi richiesti, non prezzi di compravendita. Per l'enfiteusi l'importo di affrancazione "
            "può essere inserito o stimato come 15 × reddito catastale annuo, usato solo come proxy modificabile del "
            "canone effettivo. I terreni restano esclusi dalla stima di mercato per diritto. "
            "Valori indicativi, non perizia."
        )
    kpis["manual_valuation_assets"] = _manual_valuation_assets(table_estimates, assessment_assets, saved_values)
    return kpis


def _manual_affrancation_amounts(
    table_estimates: list[dict[str, Any]],
    assessment_assets: list[dict[str, Any]],
    saved_values: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Prepare one editable whole-property affrancazione amount per enfiteusi asset."""
    by_key: dict[str, dict[str, Any]] = {}
    for asset in assessment_assets:
        right = next((item for item in (asset.get("rights") or []) if item.get("kind") == "enfiteusi"), None)
        if right is None:
            continue
        estimate = table_estimates[asset["group_index"]][asset["table_kind"]][asset["row_index"]]
        asset_key = (estimate.get("asset_identity") or {}).get("asset_key")
        if not asset_key:
            continue
        details = by_key.setdefault(
            asset_key,
            {"kind": asset["table_kind"], "annual_income": Decimal(0), "supplied_amount": None},
        )
        income_field = "building_income" if asset["table_kind"] == "buildings" else "dominical_income"
        annual_income = _assessment_decimal(asset.get(income_field))
        if annual_income is not None:
            details["annual_income"] += annual_income
        supplied_amount = _assessment_amount(right.get("affrancation_value"))
        if details["supplied_amount"] is None and supplied_amount is not None:
            details["supplied_amount"] = supplied_amount

    result: dict[str, dict[str, Any]] = {}
    for asset_key, details in by_key.items():
        annual_income = details["annual_income"]
        inferred = (
            (annual_income * Decimal(15)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if annual_income > 0 else None
        )
        automatic = details["supplied_amount"] if details["supplied_amount"] is not None else inferred
        saved = _assessment_amount(saved_values.get(asset_key, {}).get("affrancation_amount"))
        amount = saved if saved is not None else automatic
        income_name = "rendita catastale" if details["kind"] == "buildings" else "reddito dominicale"
        if details["supplied_amount"] is not None:
            basis_note = "Importo già presente nei dati acquisiti; puoi modificarlo e salvarlo."
        elif inferred is not None:
            basis_note = (
                f"Stima automatica: 15 × {income_name} annuo ({_format_currency_it(annual_income, decimals=2)}) "
                f"= {_format_currency_it(inferred, decimals=2)}. Il reddito catastale è usato solo come proxy del "
                "canone effettivo."
            )
        else:
            basis_note = (
                f"Nessun {income_name} disponibile per la stima automatica; inserisci un importo ricavato dal titolo "
                "o da una fonte attendibile."
            )
        source_note = "Importo personalizzato salvato nel tuo account." if saved is not None else (
            "Importo presente nei dati acquisiti." if details["supplied_amount"] is not None else
            "Stima automatica indicativa." if inferred is not None else "Importo non disponibile."
        )
        result[asset_key] = {
            "amount": format(amount, ".2f") if amount is not None else "",
            "automatic_amount": format(automatic, ".2f") if automatic is not None else "",
            "is_override": saved is not None,
            "source_note": source_note,
            "basis_note": basis_note,
        }
    return result


def _manual_valuation_assets(
    table_estimates: list[dict[str, Any]],
    assessment_assets: list[dict[str, Any]] | None = None,
    saved_values: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One compact, deduplicated UI item per cadastral property, merging multiple land classamenti."""
    by_key: dict[str, dict[str, Any]] = {}
    for group_index, group in enumerate(table_estimates):
        for kind in ("buildings", "lands"):
            for row_index, estimate in enumerate(group.get(kind, [])):
                identity = estimate.get("asset_identity") or {}
                asset_key = identity.get("asset_key")
                if not asset_key:
                    continue
                item = by_key.get(asset_key)
                if item is None:
                    item = {
                        "asset_key": asset_key,
                        "identity": {key: value for key, value in identity.items() if key != "asset_key"},
                        "kind": kind,
                        "omi_available": kind == "buildings" and bool(estimate.get("range")),
                        "omi_range": estimate.get("range") or "",
                        "omi_central": (
                            _format_currency_it(estimate["value_estimate"])
                            if kind == "buildings" and estimate.get("value_estimate") is not None else ""
                        ),
                        "omi_zone": estimate.get("omi_zona") or "",
                        "omi_base_min": str(estimate.get("value_min") or "") if kind == "buildings" else "",
                        "omi_base_max": str(estimate.get("value_max") or "") if kind == "buildings" else "",
                        "omi_override": estimate.get("omi_override"),
                        "valuations": estimate.get("manual_valuations", []),
                        "preferred_source": estimate.get("preferred_source") or "",
                        "preferred_range": (estimate.get("preferred_valuation") or {}).get("range", ""),
                        "preferred_label": (estimate.get("preferred_valuation") or {}).get("provider_label", ""),
                        "table_rows": [],
                    }
                    by_key[asset_key] = item
                else:
                    item["omi_available"] = item["omi_available"] or (
                        kind == "buildings" and bool(estimate.get("range"))
                    )
                    if not item["omi_range"] and estimate.get("range"):
                        item["omi_range"] = estimate["range"]
                    if not item["omi_central"] and estimate.get("value_estimate") is not None:
                        item["omi_central"] = _format_currency_it(estimate["value_estimate"])
                    if not item["omi_zone"] and estimate.get("omi_zona"):
                        item["omi_zone"] = estimate["omi_zona"]
                    if not item["omi_base_min"] and kind == "buildings" and estimate.get("value_min") is not None:
                        item["omi_base_min"] = str(estimate["value_min"])
                    if not item["omi_base_max"] and kind == "buildings" and estimate.get("value_max") is not None:
                        item["omi_base_max"] = str(estimate["value_max"])
                    if not item["omi_override"] and estimate.get("omi_override"):
                        item["omi_override"] = estimate["omi_override"]
                    if not item["valuations"] and estimate.get("manual_valuations"):
                        item["valuations"] = estimate["manual_valuations"]
                    if not item["preferred_source"] and estimate.get("preferred_source"):
                        item["preferred_source"] = estimate["preferred_source"]
                    if not item["preferred_range"] and (estimate.get("preferred_valuation") or {}).get("range"):
                        item["preferred_range"] = estimate["preferred_valuation"]["range"]
                    if (
                        not item["preferred_label"]
                        and (estimate.get("preferred_valuation") or {}).get("provider_label")
                    ):
                        item["preferred_label"] = estimate["preferred_valuation"]["provider_label"]
                item["table_rows"].append({"group_index": group_index, "kind": kind, "row_index": row_index})
    affrancation = _manual_affrancation_amounts(table_estimates, assessment_assets or [], saved_values or {})
    for asset_key, item in by_key.items():
        if asset_key in affrancation:
            item["affrancation"] = affrancation[asset_key]
    return list(by_key.values())


def _enfiteusi_residual_range(
    full_value_low: Decimal, full_value_high: Decimal, redemption_amount: Decimal, share: Decimal
) -> tuple[Decimal, Decimal]:
    """Estimate the enfiteuta's share as max(0, full value - effective affrancazione amount)."""
    return (
        max(Decimal(0), full_value_low - redemption_amount) * share,
        max(Decimal(0), full_value_high - redemption_amount) * share,
    )


def _format_value_range(low: Decimal, high: Decimal) -> str:
    return f"{_format_currency_it(low)} – {_format_currency_it(high)}"


NO_LANDS = "Nessun terreno in visura"
NO_BUILDINGS = "Nessun fabbricato in visura"
NOT_AVAILABLE = "Non disponibile"

# Why a building row has no OMI range, as a label short enough for a table cell; the full sentence stays in `note`.
_SHORT_REASONS = (
    ("Superficie catastale assente", "Superficie assente"),
    ("Categoria catastale assente", "Categoria assente"),
    ("senza una tipologia OMI", "Categoria senza tipologia OMI"),
    ("Nessuna quotazione OMI", "Comune senza quotazioni OMI"),
    ("non quotata", "Non quotata da OMI"),
    ("Database OMI non disponibile", "Database OMI non raggiungibile"),
    ("Quotazioni OMI comunali non disponibili", "Quotazioni OMI non raggiungibili"),
    ("zona o categoria", "Zona OMI non individuata"),
)


def _short_reason(note: Any) -> str:
    text = str(note or "")
    for fragment, label in _SHORT_REASONS:
        if fragment in text:
            return label
    return "Stima OMI non disponibile"


def _label_missing_estimates(table_estimates: list[dict[str, Any]]) -> None:
    """Give every cell without a value a short, visible reason (`missing` / `cadastral_missing`)."""
    for group in table_estimates:
        for kind in ("buildings", "lands"):
            for estimate in group.get(kind, []):
                # Recomputed on every call: the OMI step fills ranges after the first (owner assessment) pass.
                if kind == "buildings":
                    if estimate.get("range"):
                        estimate.pop("missing", None)
                    else:
                        estimate["missing"] = _short_reason(estimate.get("note"))
                if estimate.get("cadastral_value"):
                    estimate.pop("cadastral_missing", None)
                else:
                    estimate["cadastral_missing"] = str(estimate.get("cadastral_note") or "").rstrip(".") or NOT_AVAILABLE


def _reason_summary(reasons: dict[Any, str]) -> str:
    """'Non quotata da OMI (3); Zona OMI non individuata (1)' from {asset: reason}."""
    counts: dict[str, int] = {}
    for reason in reasons.values():
        counts[reason] = counts.get(reason, 0) + 1
    return "; ".join(f"{reason} ({count})" for reason, count in sorted(counts.items(), key=lambda item: -item[1]))


_MISSING_VALUE = re.compile(r"^(Non |Nessun|Quota non|Stima OMI non|Età|Durata|Metodo non)")


def is_missing_value(text: Any) -> bool:
    """True for a figure that is a reason ("Non stimabile: …", "Nessun terreno in visura") rather than a number."""
    return bool(_MISSING_VALUE.match(str(text or "")))


def _assessment_tiles(assessment: dict[str, Any]) -> list[dict[str, Any]]:
    """The metric tiles of the owner assessment, one definition for the web card and the PDF."""
    by_kind = {right["kind"]: right for right in assessment["rights"] if right["kind"] != "other"}
    quota_note = (
        "Quota di titolarità dichiarata; non è ponderata per usufrutto o nuda proprietà, quindi non è il valore "
        "economico del diritto."
    )
    tiles = [
        ("Totale diritti stimati OMI", assessment["omi_total"],
         "Somma delle stime OMI attribuibili calcolate; gli elementi non stimabili restano indicati nella tabella."),
        ("Piena proprietà · quota", (by_kind.get("full") or {}).get("value") or "Nessuna intestata al soggetto",
         "Il soggetto non ha piena proprietà su alcun bene della visura." if "full" not in by_kind else
         "Stima OMI della piena proprietà del soggetto."),
        ("Usufrutto · quota", (by_kind.get("usufruct") or {}).get("value") or "Nessuno intestato al soggetto",
         "Il soggetto non è usufruttuario di alcun bene della visura (l'usufrutto può essere di altre persone)."
         if "usufruct" not in by_kind else "Stima OMI dell'usufrutto del soggetto, sull'età di chi lo detiene."),
        ("Nuda proprietà · quota", (by_kind.get("bare") or {}).get("value") or "Nessuna intestata al soggetto",
         "Il soggetto non è nudo proprietario di alcun bene della visura." if "bare" not in by_kind else
         "Valore pieno meno il valore dell'usufrutto, calcolato sull'età degli usufruttuari."),
        ("Enfiteusi · quota", (by_kind.get("enfiteusi") or {}).get("value") or "Nessuna intestata al soggetto",
         "Stima attribuibile: valore pieno del bene meno importo di affrancazione indicato o stimato per l'intero bene, "
         "poi × quota." if "enfiteusi" in by_kind else "Il soggetto non risulta enfiteuta di alcun bene della visura."),
        ("Valore catastale · quota", assessment["cadastral_share"], "Base imponibile IMU × quota dichiarata. " + quota_note),
        ("Superficie catastale · quota", assessment["area_share"], "Superficie catastale × quota dichiarata. " + quota_note),
        ("Rendita fabbricati · quota", assessment["building_income_share"], "Rendita catastale dei fabbricati × quota. " + quota_note),
        ("Reddito dominicale · quota", assessment["dominical_income_share"], "Reddito dominicale dei terreni × quota. " + quota_note),
        ("Reddito agrario · quota", assessment["agricultural_income_share"], "Reddito agrario dei terreni × quota. " + quota_note),
    ]
    affrancation_links = {
        "Totale diritti stimati OMI": assessment.get("omi_missing_affrancation_asset_keys", []),
        "Enfiteusi · quota": (by_kind.get("enfiteusi") or {}).get("missing_affrancation_asset_keys", []),
    }
    return [
        {
            "label": label,
            "value": value,
            "hint": hint,
            "missing": is_missing_value(value),
            "affrancation_asset_keys": affrancation_links.get(label, []),
        }
        for label, value, hint in tiles
    ]


def _owner_assessment(
    assets: list[dict[str, Any]], table_estimates: list[dict[str, Any]], age: int | None
) -> dict[str, Any]:
    """Estimate the subject's share and right in the listed assets; never assume an absent quota is 100%."""
    accumulators: dict[str, dict[str, Any]] = {}
    unmatched_assets: set[tuple[int, str, int]] = set()
    unknown_quota_assets: set[tuple[int, str, int]] = set()
    cadastral_share = Decimal(0)
    cadastral_share_seen = False
    area_share = Decimal(0)
    area_share_seen = False
    building_income_share = Decimal(0)
    building_income_share_seen = False
    dominical_income_share = Decimal(0)
    dominical_income_share_seen = False
    agricultural_income_share = Decimal(0)
    agricultural_income_share_seen = False
    omi_total_low = Decimal(0)
    omi_total_high = Decimal(0)
    omi_total_seen = False
    usufruct_factor = _usufruct_factor(age)
    holders: dict[str, tuple[int | None, Decimal | None]] = {}  # usufructuary -> (age, coefficient), for the note
    accrual_used = False
    _label_missing_estimates(table_estimates)
    has_buildings = any(asset["table_kind"] == "buildings" for asset in assets)
    has_lands = any(asset["table_kind"] == "lands" for asset in assets)
    omi_missing_reasons: dict[tuple[int, str, int], str] = {}
    omi_missing_affrancation_asset_keys: set[str] = set()

    for asset in assets:
        asset_key = (asset["group_index"], asset["table_kind"], asset["row_index"])
        rights = asset.get("rights") or []
        if not rights:
            unmatched_assets.add(asset_key)
            continue
        estimate = table_estimates[asset["group_index"]][asset["table_kind"]][asset["row_index"]]
        asset_anchor_key = (estimate.get("asset_identity") or {}).get("asset_key")
        estimate_low = _assessment_decimal(estimate.get("value_min"))
        estimate_high = _assessment_decimal(estimate.get("value_max"))
        cadastral_value = _assessment_decimal(asset.get("cadastral_value"))
        area = _assessment_decimal(asset.get("area"))

        for right in rights:
            kind = right["kind"]
            bucket_key = kind if kind != "other" else f"other:{right['label'].casefold()}"
            bucket = accumulators.setdefault(bucket_key, {
                "kind": kind,
                "label": {"full": "Piena proprietà", "usufruct": "Usufrutto", "bare": "Nuda proprietà",
                          "enfiteusi": "Enfiteusi"}.get(
                    kind, clean_right_description(right["label"])),
                "assets": set(),
                "shares": set(),
                "value_low": Decimal(0),
                "value_high": Decimal(0),
                "valued_assets": set(),
                "missing_value_assets": set(),
                "missing_reasons": {},
                "missing_affrancation_asset_keys": set(),
            })
            bucket["assets"].add(asset_key)
            bucket["shares"].add(_quota_label(right))
            share = right.get("share")
            if share is None:
                unknown_quota_assets.add(asset_key)
                continue
            if cadastral_value is not None:
                cadastral_share += cadastral_value * share
                cadastral_share_seen = True
            if area is not None:
                area_share += area * share
                area_share_seen = True
            building_income = _assessment_decimal(asset.get("building_income"))
            dominical_income = _assessment_decimal(asset.get("dominical_income"))
            agricultural_income = _assessment_decimal(asset.get("agricultural_income"))
            if building_income is not None:
                building_income_share += building_income * share
                building_income_share_seen = True
            if dominical_income is not None:
                dominical_income_share += dominical_income * share
                dominical_income_share_seen = True
            if agricultural_income is not None:
                agricultural_income_share += agricultural_income * share
                agricultural_income_share_seen = True

            affrancation_value = _affrancation_amount(right, asset) if kind == "enfiteusi" else None
            factor_reason = ""
            if kind == "full":
                factor = Decimal(1)
            elif kind == "usufruct":
                # own age, or the youngest co-holder's when the usufruct accrues to the survivor
                own_holders = [item for item in right.get("usufructuaries") or [] if item.get("accrual")]
                if right.get("fixed_term"):
                    factor, factor_reason = None, "Durata determinata"
                elif right.get("accrual") and own_holders and None not in [item.get("age") for item in own_holders]:
                    factor = _usufruct_factor(min(item["age"] for item in own_holders))
                    accrual_used = True
                else:
                    factor = usufruct_factor
                if factor is None and not factor_reason:
                    factor_reason = "Età del titolare non disponibile" if age is None else "Età fuori dal prospetto fiscale 2026"
            elif kind == "bare":
                fraction, factor_reason = (
                    (None, "Durata determinata") if right.get("fixed_term")
                    else _usufruct_fraction(right.get("usufructuaries") or [])
                )
                factor = None if fraction is None else Decimal(1) - fraction
                for holder in right.get("usufructuaries") or []:
                    holders[holder.get("name") or "Usufruttuario"] = (holder.get("age"), _usufruct_factor(holder.get("age")))
                    accrual_used = accrual_used or bool(holder.get("accrual"))
            elif kind == "enfiteusi":
                factor = Decimal(1)
                if affrancation_value is None:
                    factor, factor_reason = None, "Importo di affrancazione non disponibile"
            else:
                factor = None
            if factor is None or estimate_low is None or estimate_high is None:
                bucket["missing_value_assets"].add(asset_key)
                reasons = []
                if kind == "enfiteusi" and affrancation_value is None:
                    reasons.append("Importo di affrancazione non disponibile")
                    if asset_anchor_key:
                        bucket["missing_affrancation_asset_keys"].add(asset_anchor_key)
                if estimate_low is None or estimate_high is None:
                    omi_reason = (
                        "Valore di mercato del terreno non disponibile: OMI non pubblica quotazioni per i terreni"
                        if asset["table_kind"] == "lands"
                        else estimate.get("missing") or "Stima OMI non disponibile"
                    )
                    reasons.append(omi_reason)
                    if asset["table_kind"] == "buildings":
                        omi_missing_reasons[asset_key] = omi_reason
                if not reasons:
                    reasons.append(factor_reason or "Coefficiente non disponibile")
                reason = "; ".join(reasons)
                if kind == "enfiteusi" and asset["table_kind"] == "buildings":
                    omi_missing_reasons[asset_key] = reason
                    if "Importo di affrancazione non disponibile" in reason and asset_anchor_key:
                        omi_missing_affrancation_asset_keys.add(asset_anchor_key)
                bucket["missing_reasons"][asset_key] = reason
                continue
            if kind == "enfiteusi":
                attributed_low, attributed_high = _enfiteusi_residual_range(
                    estimate_low,
                    estimate_high,
                    affrancation_value,
                    share,
                )
            else:
                attributed_low = estimate_low * share * factor
                attributed_high = estimate_high * share * factor
            bucket["value_low"] += attributed_low
            bucket["value_high"] += attributed_high
            bucket["valued_assets"].add(asset_key)
            omi_total_low += attributed_low
            omi_total_high += attributed_high
            omi_total_seen = True

    rights_summary = []
    order = {"full": 0, "usufruct": 1, "bare": 2, "enfiteusi": 3, "other": 4}
    for bucket in sorted(accumulators.values(), key=lambda item: (order.get(item["kind"], 3), item["label"].casefold())):
        low, high = bucket["value_low"], bucket["value_high"]
        if bucket["valued_assets"]:
            value = _format_value_range(low, high)
        elif bucket["kind"] == "other":
            value = "Metodo non disponibile per questo diritto"
        elif bucket["missing_reasons"]:
            value = "Non stimabile: " + _reason_summary(bucket["missing_reasons"])
        else:
            value = "Stima OMI non disponibile"
        rights_summary.append({
            "unvalued_reasons": _reason_summary(bucket["missing_reasons"]),
            "kind": bucket["kind"],
            "label": bucket["label"],
            "assets": len(bucket["assets"]),
            "shares": ", ".join(sorted(bucket["shares"])),
            "value": value,
            "valued_assets": len(bucket["valued_assets"]),
            "unvalued_assets": len(bucket["missing_value_assets"]),
            "missing_affrancation_asset_keys": sorted(bucket["missing_affrancation_asset_keys"]),
        })

    limited_rights_present = any(bucket["kind"] in ("usufruct", "bare") for bucket in accumulators.values())
    if not limited_rights_present:
        coefficient_note = "Nella visura non risultano quote di usufrutto o nuda proprietà."
    else:
        parts = []
        if "usufruct" in accumulators:
            parts.append(
                f"usufrutto del soggetto: età {age} anni, coefficiente {usufruct_factor * 100:.0f}%"
                if usufruct_factor is not None and age is not None
                else "usufrutto del soggetto: età non utilizzabile nel prospetto fiscale 2026"
            )
        if holders:
            parts.append("usufruttuari dei beni in nuda proprietà: " + ", ".join(
                f"{name} {age} anni → {factor * 100:.0f}%" if age is not None and factor is not None
                else f"{name} (età non utilizzabile)"
                for name, (age, factor) in sorted(holders.items())
            ))
        coefficient_note = (
            "Il valore dell'usufrutto dipende dall'età di chi lo detiene (prospetto fiscale 2026, 2,5%): "
            + "; ".join(parts) + ". La nuda proprietà vale il valore pieno meno quello dell'usufrutto."
            + (" Con diritto di accrescimento si considera l'età del più giovane." if accrual_used else "")
            + " Per i diritti senza termine espresso si assume usufrutto vitalizio."
        )
    # A figure is "not applicable" when the visura lists no asset of that kind, "not computable" when it lists
    # some but the data is missing; the two must not read the same.
    quota_problem = "Quota non interpretabile" if unknown_quota_assets else NOT_AVAILABLE
    if omi_total_seen:
        omi_total = _format_value_range(omi_total_low, omi_total_high)
    elif omi_missing_reasons:
        omi_total = "Non stimabile: " + _reason_summary(omi_missing_reasons)
    elif not has_buildings:
        omi_total = NO_BUILDINGS
    else:
        omi_total = quota_problem
    result = {
        "has_rights": bool(rights_summary),
        "rights": rights_summary,
        "omi_total": omi_total,
        "omi_missing_affrancation_asset_keys": sorted(omi_missing_affrancation_asset_keys),
        "cadastral_share": (
            _format_currency_it(float(cadastral_share)) if cadastral_share_seen
            else quota_problem if (has_buildings or has_lands) else NOT_AVAILABLE
        ),
        "area_share": (
            f"{_format_number_it(float(area_share))} m²" if area_share_seen
            else quota_problem if (has_buildings or has_lands) else NOT_AVAILABLE
        ),
        "building_income_share": (
            _format_currency_it(float(building_income_share), decimals=2) if building_income_share_seen
            else quota_problem if has_buildings else NO_BUILDINGS
        ),
        "dominical_income_share": (
            _format_currency_it(float(dominical_income_share), decimals=2) if dominical_income_share_seen
            else quota_problem if has_lands else NO_LANDS
        ),
        "agricultural_income_share": (
            _format_currency_it(float(agricultural_income_share), decimals=2) if agricultural_income_share_seen
            else quota_problem if has_lands else NO_LANDS
        ),
        "unmatched_assets": len(unmatched_assets),
        "unknown_quota_assets": len(unknown_quota_assets),
        "age": age,
        "usufruct_factor": float(usufruct_factor) if usufruct_factor is not None else None,
        "coefficient_note": coefficient_note,
        "method_note": (
            "La stima OMI del fabbricato è ripartita per quota e diritto. Per usufrutto vitalizio e nuda proprietà "
            "si applica il prospetto fiscale 2026 al 2,5% in base all'età; se la visura indica una durata specifica "
            "il valore del diritto non è stimato. Il valore catastale in quota è la base imponibile moltiplicata per "
            "la quota dichiarata, non il valore economico del diritto. Rendite e redditi catastali sono inoltre "
            "ponderati per quota e non rappresentano redditi effettivi. Per l'enfiteusi si stima max(0, valore OMI "
            "della piena proprietà − importo di affrancazione indicato o stimato per l'intero bene) × quota. Se manca "
            "un importo esplicito, la stima automatica usa 15 × reddito catastale annuo come proxy modificabile del "
            "canone effettivo; non è un calcolo ufficiale e può non corrispondere al regime applicabile al titolo. "
            "La formula è indicativa e non sostituisce il calcolo fiscale dell'affrancazione. Valori non "
            "peritali; i terreni non hanno quotazioni OMI."
        ),
        "source_url": _USUFRUCT_SOURCE,
    }
    result["tiles"] = _assessment_tiles(result)
    return result


def _number(value: Any) -> float | None:
    """Parse XML numeric attributes, including Italian thousands/decimal marks."""
    if isinstance(value, dict):
        value = value.get("_text")
    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    if "," in text:
        if "." in text and text.rfind(",") < text.rfind("."):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or ("." in text and len(text.rsplit(".", 1)[1]) == 3):
        text = text.replace(".", "")
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _consistency_area_m2(value: Any) -> float | None:
    """Use an explicitly measured m² consistency only as an OMI area proxy.

    Subject-search results can provide a consistency such as ``19 m2`` without the cadastral surface. Other
    consistencies are handled separately by the disclosed statistical fallback below.
    """
    measurement = _first(value)
    if measurement:
        raw_value = measurement.get("Valore") or measurement.get("_text")
        unit = measurement.get("Unita") or measurement.get("unit") or ""
    else:
        raw_value = value
        match = re.fullmatch(r"\s*([\d.,]+)\s*(m(?:2|²)|mq)\s*", str(value or ""), re.IGNORECASE)
        if not match:
            return None
        raw_value, unit = match.groups()
    normalized_unit = str(unit).strip().casefold().replace("²", "2").replace(" ", "")
    if normalized_unit not in {"m2", "mq"}:
        return None
    area = _number(raw_value)
    return area if area is not None and area > 0 else None


# National means from Agenzia delle Entrate, Statistiche Catastali 2012, tables 12 and 13 (A/10: table 22).
# Each pair is (mean cadastral surface in m² per unit, mean cadastral rooms per unit). Their ratio is
# a deliberately coarse m²-per-vano fallback; it is used for indicative OMI only, never as cadastral surface.
_NATIONAL_CATEGORY_MEANS_2012: dict[str, tuple[float, float]] = {
    "A1": (292.0, 11.1),
    "A2": (126.0, 5.9),
    "A3": (110.0, 5.3),
    "A4": (95.0, 4.4),
    "A5": (58.0, 2.4),
    "A6": (70.0, 2.6),
    "A7": (185.0, 7.8),
    "A8": (479.0, 15.8),
    "A9": (635.0, 21.2),
    "A10": (134.1, 5.6),
    "A11": (63.0, 3.0),
}


def _consistency_area_vani(value: Any, category: Any) -> tuple[float, float, float] | None:
    """Estimate OMI area from explicit cadastral rooms and a national category mean.

    Returns (estimated m², rooms, m²/vano). This is not a cadastral surface and is intentionally limited to
    residential/office categories for which the official statistical publication reports both measures.
    """
    measurement = _first(value)
    if measurement:
        raw_value = measurement.get("Valore") or measurement.get("_text")
        unit = measurement.get("Unita") or measurement.get("unit") or ""
    else:
        match = re.fullmatch(r"\s*([\d.,]+)\s*vani?(?:\s+catastali)?\s*", str(value or ""), re.IGNORECASE)
        if not match:
            return None
        raw_value, unit = match.groups()
    if not str(unit).strip().casefold().startswith("van"):
        return None
    rooms = _number(raw_value)
    means = _NATIONAL_CATEGORY_MEANS_2012.get(normalize_category(category))
    if rooms is None or rooms <= 0 or means is None:
        return None
    mean_area, mean_rooms = means
    square_metres_per_room = mean_area / mean_rooms
    estimated_area = rooms * square_metres_per_room
    return estimated_area, rooms, square_metres_per_room


def _format_number_it(value: float, *, decimals: int = 2) -> str:
    rendered = f"{value:,.{decimals}f}"
    whole, _, fraction = rendered.partition(".")
    whole = whole.replace(",", ".")
    fraction = fraction.rstrip("0")
    return f"{whole},{fraction}" if fraction else whole


def _format_currency_it(value: float | Decimal, *, decimals: int = 0) -> str:
    rendered = f"{value:,.{decimals}f}"
    whole, _, fraction = rendered.partition(".")
    whole = whole.replace(",", ".")
    return f"€ {whole},{fraction}" if decimals else f"€ {whole}"


def _initial_omi_note(category: Any, area: float | None) -> str:
    """Why a building has no OMI range before any lookup: the category is checked first, it is the real obstacle
    for F/D units, which usually also lack a surface."""
    normalized = normalize_category(category)
    if not normalized:
        return "Categoria catastale assente."
    if not omi_quotes.OMI_TYPOLOGY_BY_CATEGORY.get(normalized):
        return "Categoria catastale senza una tipologia OMI corrispondente."
    if area is None:
        return "Superficie catastale assente."
    return "Stima OMI non disponibile per zona o categoria catastale."


def _note_amount(value: Any) -> str:
    """'217.710,2' -> '217.710,20': an amount in a note keeps both decimals (whole amounts stay '1.000')."""
    text = _format_number_it(float(value), decimals=2)
    return text + "0" if "," in text and len(text.rsplit(",", 1)[1]) == 1 else text


def _building_value_note(value: Any, category: Any) -> str:
    base = _note_amount(value.base)
    return (
        f"Valore catastale ai fini IMU: rendita € {base} × {_format_number_it(float(value.revaluation))} × "
        f"{int(value.multiplier)} (categoria {normalize_category(category)}). Base imponibile fiscale, "
        "non è un valore di mercato; quota di proprietà non applicata."
    )


def _land_value_note(value: Any) -> str:
    base = _note_amount(value.base)
    return (
        f"Valore catastale ai fini IMU: reddito dominicale € {base} × {_format_number_it(float(value.revaluation))} × "
        f"{int(value.multiplier)}. Base imponibile fiscale, non è un valore di mercato; moltiplicatore 135 "
        "(75 per coltivatori diretti e IAP, non applicato); quota di proprietà non applicata."
    )


async def _comune_quotes_payload(code: str) -> dict[str, Any] | None:
    """All zones' quotes of a comune from the omi database, in the shape select_quote_range_across_zones reads."""
    quotes = await omi_quotes.omi_comune_quotes(code)
    return None if quotes is None else {"quotes": quotes}


async def _run_bounded(requests: dict[Any, Any], *, limit: int = 10, timeout: float = 8) -> dict[Any, Any]:
    """Run optional land-registry lookups concurrently and retain completed results."""
    semaphore = asyncio.Semaphore(limit)

    async def run(key: Any, factory: Any) -> tuple[Any, Any]:
        async with semaphore:
            try:
                return key, await factory()
            except Exception as exc:  # The document remains useful without enrichment data.
                logger.debug("Subject OMI lookup failed (%s): %s", key, type(exc).__name__)
                return key, None

    tasks = [asyncio.create_task(run(key, factory)) for key, factory in requests.items()]
    if not tasks:
        return {}
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = {}
    for task in done:
        try:
            key, value = task.result()
        except Exception:
            continue
        results[key] = value
    return results


def _parcel_zone_point(feature: dict[str, Any] | None, marker: dict[str, Any]) -> tuple[float, float, str] | None:
    """Prefer a point inside the cadastral parcel; a centroid can fall outside concave parcels."""
    if feature and isinstance(feature.get("geometry"), dict):
        try:
            from shapely.geometry import shape

            point = shape(feature["geometry"]).representative_point()
            latitude, longitude = float(point.y), float(point.x)
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return latitude, longitude, "punto interno alla particella"
        except Exception:  # An unavailable or invalid polygon falls back to the stored parcel centroid.
            pass
    try:
        latitude = float(marker["latitude"])
        longitude = float(marker["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude, "centroide catastale"


async def subject_land_registry_kpis(
    xml_parsed: dict[str, Any],
    map_lookup: dict[str, Any] | None,
    *,
    include_assessment_context: bool = False,
    selection_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Sum subject-visura areas and OMI ranges for mapped and unmapped building units."""
    subject = next(
        (xml_parsed.get(key) for key in _SUBJECT_KEYS if isinstance(xml_parsed.get(key), dict)),
        None,
    ) if isinstance(xml_parsed, dict) else None
    if not subject:
        return None

    buildings: list[dict[str, Any]] = []
    building_area = 0.0
    building_area_units = 0
    building_area_proxy = 0.0
    building_area_proxy_units = 0
    building_units = 0
    building_income = 0.0
    building_income_units = 0
    grounds_area = 0.0
    grounds_area_rows = 0
    dominical_income = 0.0
    dominical_income_rows = 0
    agricultural_income = 0.0
    agricultural_income_rows = 0
    cadastral_buildings = Decimal(0)
    cadastral_building_units = 0
    cadastral_lands = Decimal(0)
    cadastral_land_rows = 0

    # The archive template sorts municipality groups by name, then by the first cadastral sheet.
    # The client-side table enrichers pair these rows with table_estimates by position, so the
    # server-side arrays must follow the same display order.
    groups = sorted(_items(subject.get("GruppoUnitaImmobiliari")), key=_subject_group_table_sort_key)
    # One entry per table row, in the order the page renders them: `buildings` per fabbricato, `lands` per
    # classamento (the theme also draws one empty row for a terreno without any).
    table_estimates = [{"buildings": [], "lands": []} for _ in groups]
    subject_cf, subject_age, _as_of = _subject_assessment_context(subject)
    owner_assessment_assets: list[dict[str, Any]] = []
    distinct_municipalities = set()
    for index, group in enumerate(groups):
        code = str(group.get("CodiceComune") or "").strip().upper()
        name = re.sub(r"\s*\([^)]*\)\s*$", "", str(group.get("Descrizione") or "")).strip()
        if code:
            distinct_key = ("code", code)
        elif name:
            distinct_key = ("name", " ".join(name.split()).casefold())
        else:
            distinct_key = ("row", index)
        distinct_municipalities.add(distinct_key)

    for group_index, group in enumerate(groups):
        municipality_code = str(group.get("CodiceComune") or "").strip().upper()
        province = str(group.get("Provincia") or "").strip()
        group_name = re.sub(r"\s*\([^)]*\)\s*$", "", str(group.get("Descrizione") or "")).strip()
        group_rights = _current_group_rights(group, subject_cf, _as_of)

        for property_row in _items(group.get("ImmobileFabbricatiS")):
            building_units += 1
            identifier = _first(
                _first(property_row.get("DatiIdentificativi")).get("IdentificativoDefinitivo")
            )
            asset_identity = _asset_identity(
                cadastre_type="F",
                municipality_code=identifier.get("CodiceComune") or municipality_code,
                municipality=identifier.get("Comune") or group_name,
                province=identifier.get("Provincia") or province,
                section=identifier.get("SezUrbana") or group.get("Sezione"),
                sheet=identifier.get("Foglio"),
                parcel=identifier.get("ParticellaNum") or identifier.get("Particella"),
                subunit=identifier.get("Subalterno"),
            )
            classamento = _first(property_row.get("DatiClassamentoF"))
            surface = _first(property_row.get("SuperficieF"))
            area = _number(surface.get("Totale"))
            consistency_area = _consistency_area_m2(classamento.get("Consistenza")) if area is None else None
            vani_area_details = (
                _consistency_area_vani(classamento.get("Consistenza"), classamento.get("Categoria"))
                if area is None and consistency_area is None else None
            )
            vani_area = vani_area_details[0] if vani_area_details else None
            omi_area = area if area is not None else (
                consistency_area if consistency_area is not None else vani_area
            )
            omi_area_source = (
                "superficie catastale" if area is not None else
                "consistenza catastale in m² usata come proxy" if consistency_area is not None else
                "superficie stimata dalla consistenza in vani con media nazionale per categoria (2012)"
                if vani_area_details else ""
            )
            omi_area_formula = (
                f"{_format_number_it(vani_area_details[1])} vani × "
                f"{_format_number_it(vani_area_details[2])} m²/vano ≈ {_format_number_it(vani_area)} m²"
                if vani_area_details else
                f"{_format_number_it(consistency_area)} m² di consistenza (proxy)"
                if consistency_area is not None else
                f"{_format_number_it(area)} m² di superficie catastale" if area is not None else ""
            )
            income = _number(classamento.get("RenditaEuro"))
            if area is not None:
                building_area += area
                building_area_units += 1
            elif omi_area is not None:
                # Fill missing visura surfaces with the cadastral-consistency proxy already used for OMI.
                building_area_proxy += omi_area
                building_area_proxy_units += 1
            if income is not None:
                building_income += income
                building_income_units += 1
            code = str(identifier.get("CodiceComune") or municipality_code).strip().upper()
            table_row_index = len(table_estimates[group_index]["buildings"])
            cadastral = building_cadastral_value(income, classamento.get("Categoria"))
            if cadastral is not None:
                cadastral_buildings += cadastral.value
                cadastral_building_units += 1
                cadastral_value, cadastral_note = (
                    _format_currency_it(float(cadastral.value)),
                    _building_value_note(cadastral, classamento.get("Categoria")),
                )
            elif income is None:
                cadastral_value, cadastral_note = "", "Rendita catastale assente."
            elif building_multiplier(classamento.get("Categoria")) is None:
                cadastral_value, cadastral_note = "", "Categoria catastale senza moltiplicatore IMU (gruppo E/F o assente)."
            else:
                cadastral_value, cadastral_note = "", "Rendita catastale nulla."
            omi_note = _initial_omi_note(classamento.get("Categoria"), area)
            normalized_category = normalize_category(classamento.get("Categoria"))
            if (consistency_area is not None or vani_area is not None) and omi_quotes.OMI_TYPOLOGY_BY_CATEGORY.get(normalized_category):
                omi_note = (
                    "Stima OMI non disponibile senza zona o quotazioni; disponibile una superficie proxy dalla consistenza catastale."
                )
            table_estimates[group_index]["buildings"].append({
                "range": "",
                "value_min": None,
                "value_max": None,
                "value_estimate": None,
                "note": omi_note,
                "omi_area_formula": omi_area_formula,
                "actual_size_sqm": omi_area,
                "actual_size_default_sqm": omi_area,
                "actual_size_is_override": False,
                "actual_size_source": omi_area_source,
                "actual_size_default_source": omi_area_source,
                "cadastral_value": cadastral_value,
                "cadastral_note": cadastral_note,
                "category": normalize_category(classamento.get("Categoria")),
                # A category link can still show all OMI zones for this municipality when
                # no parcel geometry is available to resolve the property's exact zone.
                "omi_comune": code,
                "omi_zona": "",
                "omi_tipologia": "",
                "asset_identity": asset_identity,
            })
            owner_assessment_assets.append({
                "group_index": group_index,
                "table_kind": "buildings",
                "row_index": table_row_index,
                "rights": group_rights,
                "area": area,
                "cadastral_value": cadastral.value if cadastral is not None else None,
                "building_income": income,
            })
            buildings.append({
                "municipality_code": code,
                "province": str(identifier.get("Provincia") or province).strip(),
                "municipality": str(identifier.get("Comune") or group_name).strip(),
                "section": str(identifier.get("SezUrbana") or group.get("Sezione") or "").strip().upper(),
                "sheet": str(identifier.get("Foglio") or "").strip().upper(),
                "parcel": str(identifier.get("ParticellaNum") or identifier.get("Particella") or "").strip().upper(),
                "category": classamento.get("Categoria") or "",
                "area": area,
                "omi_area": omi_area,
                "omi_area_source": omi_area_source,
                "omi_area_formula": omi_area_formula,
                "group_index": group_index,
                "table_row_index": table_row_index,
                "asset_key": (asset_identity or {}).get("asset_key"),
            })

        for property_row in _items(group.get("ImmobileTerreniS")):
            identifier = _first(
                _first(property_row.get("DatiIdentificativi")).get("IdentificativoDefinitivo")
            )
            asset_identity = _asset_identity(
                cadastre_type="T",
                municipality_code=identifier.get("CodiceComune") or municipality_code,
                municipality=identifier.get("Comune") or group_name,
                province=identifier.get("Provincia") or province,
                section=identifier.get("Sezione") or identifier.get("SezUrbana") or group.get("Sezione"),
                sheet=identifier.get("Foglio"),
                parcel=identifier.get("ParticellaNum") or identifier.get("Particella"),
            )
            classamento = _first(property_row.get("DatiClassamentoT"))
            for row in _items(classamento.get("ClassamentoT")) or [{}]:
                area = _number(row.get("SuperficieMQ"))
                dominical = _number(row.get("RedditoDominicaleEuro"))
                agricultural = _number(row.get("RedditoAgrarioEuro"))
                land_value = land_cadastral_value(dominical)
                if land_value is not None:
                    cadastral_lands += land_value.value
                    cadastral_land_rows += 1
                table_row_index = len(table_estimates[group_index]["lands"])
                table_estimates[group_index]["lands"].append({
                    "cadastral_value": _format_currency_it(float(land_value.value)) if land_value else "",
                    "cadastral_note": (
                        _land_value_note(land_value) if land_value else "Reddito dominicale assente."
                    ),
                    "asset_identity": asset_identity,
                })
                owner_assessment_assets.append({
                    "group_index": group_index,
                    "table_kind": "lands",
                    "row_index": table_row_index,
                    "rights": group_rights,
                    "area": area,
                    "cadastral_value": land_value.value if land_value is not None else None,
                    "dominical_income": dominical,
                    "agricultural_income": agricultural,
                })
                if area is not None:
                    grounds_area += area
                    grounds_area_rows += 1
                if dominical is not None:
                    dominical_income += dominical
                    dominical_income_rows += 1
                if agricultural is not None:
                    agricultural_income += agricultural
                    agricultural_income_rows += 1

    saved_actual_sizes: dict[str, Decimal] = {}
    if selection_user_id and buildings:
        try:
            from .database import get_user_property_actual_sizes

            saved_actual_sizes = await get_user_property_actual_sizes(
                selection_user_id, [building.get("asset_key") for building in buildings]
            )
        except Exception as exc:  # Editable sizes are optional; the cadastral default still works.
            logger.warning("Saved property sizes unavailable: %s", type(exc).__name__)

    for building in buildings:
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        default_size = building.get("omi_area")
        default_source = building.get("omi_area_source") or ""
        saved_size = saved_actual_sizes.get(building.get("asset_key") or "")
        if saved_size is not None:
            effective_size = float(saved_size)
            building["omi_area"] = effective_size
            building["omi_area_source"] = "dimensione inserita dall'utente"
            building["omi_area_formula"] = f"{_format_number_it(effective_size)} m² inseriti dall'utente"
            row_estimate["actual_size_source"] = "dimensione inserita dall'utente"
            row_estimate["actual_size_is_override"] = True
            row_estimate["omi_area_formula"] = building["omi_area_formula"]
        else:
            effective_size = default_size
        row_estimate["actual_size_sqm"] = effective_size
        row_estimate["actual_size_default_sqm"] = default_size
        row_estimate["actual_size_default_source"] = default_source

    land_rows = sum(len(group["lands"]) for group in table_estimates)

    def building_figure(value: str | None) -> str:
        return value if value is not None else (NOT_AVAILABLE if building_units else NO_BUILDINGS)

    def land_figure(value: str | None) -> str:
        return value if value is not None else (NOT_AVAILABLE if land_rows else NO_LANDS)

    def land_note(count: int, what: str) -> str:
        return f"{count} classamenti con {what}" if land_rows else "La visura non elenca terreni"

    building_area_total = building_area + building_area_proxy
    building_area_available_units = building_area_units + building_area_proxy_units
    if building_area_proxy_units:
        building_area_figure = f"≈ {_format_number_it(building_area_total)} m²"
    elif building_area_units:
        building_area_figure = f"{_format_number_it(building_area)} m²"
    else:
        building_area_figure = None

    if building_units:
        building_area_note = f"{building_area_units} di {building_units} unità con superficie in visura"
        if building_area_proxy_units:
            missing_without_estimate = building_units - building_area_available_units
            building_area_note += (
                f" · Stima indicativa per {building_area_proxy_units} unità senza superficie: "
                f"{_format_number_it(building_area_proxy)} m² stimati dai dati di consistenza catastale."
            )
            if missing_without_estimate:
                building_area_note += f" Dati insufficienti per stimare altre {missing_without_estimate} unità."
            building_area_note += " Il valore stimato deve essere aggiornato con i valori effettivi riportati in visura."
        elif building_area_units < building_units:
            building_area_note += f" · Dati insufficienti per stimare le altre {building_units - building_area_units} unità."
    else:
        building_area_note = "La visura non elenca fabbricati"

    kpis: dict[str, Any] = {
        "distinct_comuni": len(distinct_municipalities),
        "building_rows": building_units,
        "land_rows": land_rows,
        "building_area": building_figure(building_area_figure),
        "building_area_note": building_area_note,
        "grounds_area": land_figure(f"{_format_number_it(grounds_area)} m²" if grounds_area_rows else None),
        "grounds_area_note": land_note(grounds_area_rows, "superficie in visura"),
        "building_income": building_figure(
            _format_currency_it(building_income, decimals=2) if building_income_units else None
        ),
        "building_income_note": (
            f"{building_income_units} di {building_units} fabbricati con rendita in visura" if building_units
            else "La visura non elenca fabbricati"
        ),
        "dominical_income": land_figure(
            _format_currency_it(dominical_income, decimals=2) if dominical_income_rows else None
        ),
        "dominical_income_note": land_note(dominical_income_rows, "reddito dominicale"),
        "agricultural_income": land_figure(
            _format_currency_it(agricultural_income, decimals=2) if agricultural_income_rows else None
        ),
        "agricultural_income_note": land_note(agricultural_income_rows, "reddito agrario"),
        "cadastral_value": (
            _format_currency_it(float(cadastral_buildings + cadastral_lands))
            if cadastral_building_units + cadastral_land_rows
            else NOT_AVAILABLE if (building_units or land_rows) else "Nessun immobile in visura"
        ),
        "cadastral_value_buildings": building_figure(
            _format_currency_it(float(cadastral_buildings)) if cadastral_building_units else None
        ),
        "cadastral_value_lands": land_figure(
            _format_currency_it(float(cadastral_lands)) if cadastral_land_rows else None
        ),
        "cadastral_value_note": (
            f"Valore catastale (base imponibile IMU) di {cadastral_building_units} "
            f"{'fabbricato' if cadastral_building_units == 1 else 'fabbricati'} con rendita e "
            f"{cadastral_land_rows} {'classamento' if cadastral_land_rows == 1 else 'classamenti'} con reddito dominicale: rendita × 1,05 × moltiplicatore di categoria, "
            "reddito dominicale × 1,25 × 135. È un valore fiscale, non di mercato; quote di proprietà non applicate."
        ),
        "asset_value": NOT_AVAILABLE if building_units else NO_BUILDINGS,
        "asset_value_note": (
            "Stima OMI indicativa dei fabbricati; terreni esclusi e quote di proprietà non applicate."
            if building_units else "La visura non elenca fabbricati: non c'è nulla da stimare con le quotazioni OMI."
        ),
        "table_estimates": table_estimates,
        "owner_assessment": _owner_assessment(owner_assessment_assets, table_estimates, subject_age),
    }
    if include_assessment_context:
        kpis["_assessment_assets"] = owner_assessment_assets
        kpis["_subject_age"] = subject_age
    if buildings and not isinstance(map_lookup, dict):
        kpis["asset_value_note"] = (
            "Stima OMI non eseguita: senza la mappa catastale locale non si può individuare la zona OMI delle "
            "particelle. Il valore catastale non ne dipende."
        )
    if not buildings:
        return kpis

    saved_selections: dict[str, dict[str, Any]] = {}
    if selection_user_id:
        try:
            from .database import get_user_property_omi_selections

            saved_selections = await get_user_property_omi_selections(
                selection_user_id, [building.get("asset_key") for building in buildings]
            )
        except Exception as exc:  # Saved choices are optional; the automatic estimate remains available.
            logger.warning("Saved OMI selections unavailable: %s", type(exc).__name__)

    # Resolve OMI zones against the parcel itself when its cadastral polygon is available.
    markers_by_parcel: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    features_by_parcel: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for marker in (map_lookup or {}).get("parcel_markers") or []:
        if not isinstance(marker, dict):
            continue
        key = (
            str(marker.get("municipality_code") or "").strip().upper(),
            str(marker.get("sheet") or "").strip().upper(),
            str(marker.get("parcel") or "").strip().upper(),
        )
        if all(key):
            markers_by_parcel.setdefault(key, []).append(marker)
    for feature in (map_lookup or {}).get("parcel_features") or []:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        key = (
            str(properties.get("municipality_code") or "").strip().upper(),
            str(properties.get("sheet") or "").strip().upper(),
            str(properties.get("parcel") or "").strip().upper(),
        )
        if all(key):
            features_by_parcel.setdefault(key, []).append(feature)

    mapped_buildings = []
    for building in buildings:
        if not building.get("municipality_code"):
            continue
        key = (building["municipality_code"], building["sheet"], building["parcel"])
        candidates = markers_by_parcel.get(key, [])
        feature_candidates = features_by_parcel.get(key, [])
        section = building["section"]
        if section:
            marker_matches = [
                marker for marker in candidates
                if str(marker.get("section") or "").strip().upper() == section
            ]
            feature_matches = [
                feature for feature in feature_candidates
                if str((feature.get("properties") or {}).get("section") or "").strip().upper() == section
            ]
            marker = marker_matches[0] if len(marker_matches) == 1 else (
                candidates[0] if not marker_matches and len(candidates) == 1 else None
            )
            feature = feature_matches[0] if len(feature_matches) == 1 else (
                feature_candidates[0] if not feature_matches and len(feature_candidates) == 1 else None
            )
        else:
            marker = candidates[0] if len(candidates) == 1 else None
            feature = feature_candidates[0] if len(feature_candidates) == 1 else None
        if marker or feature:
            mapped_buildings.append((building, marker or {}, feature))
    stats_service = None
    try:
        from land_registry import stats_service
    except ImportError:
        logger.debug("Shared land_registry package is unavailable for subject OMI KPIs")

    zone_keys = {}
    zone_point_methods = {}
    geometry_requests: dict[tuple[str, str, str, str], tuple[str, str, dict[str, Any]]] = {}
    for building, marker, feature in mapped_buildings:
        province = building["province"] or str(marker.get("province") or "").strip()
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        if feature and province:
            geometry_requests.setdefault(
                parcel_key,
                (province, building["municipality_code"], feature),
            )
        point = _parcel_zone_point(feature, marker)
        if point is None:
            continue
        latitude, longitude, point_method = point
        key = (building["municipality_code"], province, latitude, longitude)
        zone_keys[parcel_key] = key
        zone_point_methods[parcel_key] = point_method

    geometry_items = list(geometry_requests.items())
    geometry_results = await asyncio.gather(*(
        asyncio.to_thread(omi_zone_for_parcel, province, cadastral_code, feature)
        for _parcel_key, (province, cadastral_code, feature) in geometry_items
    ))
    geometry_parcel_zones = {
        parcel_key: zone
        for (parcel_key, _request), zone in zip(geometry_items, geometry_results)
        if zone
    }
    for parcel_key in geometry_parcel_zones:
        zone_point_methods[parcel_key] = "zona OMI con maggiore sovrapposizione alla particella catastale"

    unique_zones = set(zone_keys.values())
    zone_requests = {
        key: (lambda key=key: stats_service.aget_omi_zone_at_point(
            key[1], key[2], key[3], cadastral_code=key[0]
        ))
        for key in unique_zones
        if key[1] and stats_service is not None
    }
    zone_results = await _run_bounded(
        zone_requests,
        timeout=8,
    )

    parcel_zones = dict(geometry_parcel_zones)
    point_parcel_zones = {}
    for building, _marker, _feature in mapped_buildings:
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        zone = (zone_results.get(zone_keys.get(parcel_key)) or {}).get("zone")
        if not zone:
            continue
        zone = str(zone).strip().upper()
        point_parcel_zones[parcel_key] = zone
        parcel_zones.setdefault(parcel_key, zone)
    quote_requests = {}
    selected_zones: dict[str, str] = {}
    for building in buildings:
        asset_key = building.get("asset_key")
        saved = saved_selections.get(asset_key, {})
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        automatic_zone = str(parcel_zones.get(parcel_key) or "").strip().upper()
        saved_zone = str(saved.get("zone_code") or "").strip().upper()
        legacy_automatic_zone = bool(
            saved.get("is_manual") is None
            and saved_zone
            and automatic_zone
            and saved_zone != automatic_zone
            and saved_zone == point_parcel_zones.get(parcel_key)
            and not saved.get("typology_code")
        )
        if saved.get("is_manual") is True:
            zone = saved_zone or automatic_zone
        elif saved.get("is_manual") is False or legacy_automatic_zone:
            zone = automatic_zone or saved_zone
        else:
            zone = saved_zone or automatic_zone
        selected_zones[asset_key or ""] = zone
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        row_estimate["omi_comune"] = building["municipality_code"]
        row_estimate["omi_zona"] = zone
        row_estimate["omi_zone_point_method"] = zone_point_methods.get(parcel_key, "")
        row_estimate["omi_zone_selected"] = bool(
            saved_zone
            and saved_zone != automatic_zone
            and not legacy_automatic_zone
            and saved.get("is_manual") is not False
        )
        if legacy_automatic_zone:
            row_estimate["_omi_legacy_zone_code"] = saved_zone
        row_estimate["omi_typology_selected"] = False
        row_estimate["omi_tipologia"] = str(saved.get("typology_code") or "")
        if zone and building.get("municipality_code"):
            quote_key = (building["municipality_code"], zone)
            if quote_key not in quote_requests:
                quote_requests[quote_key] = lambda quote_key=quote_key: omi_quotes.omi_zone_quotes(*quote_key)

    # Municipality data supplies the no-zone range and the selectable OMI zones and typologies.
    fallback_buildings = []
    comune_codes = {building["municipality_code"] for building in buildings if building.get("municipality_code")}
    for building in buildings:
        if selected_zones.get(building.get("asset_key") or ""):
            continue
        if not (building.get("municipality_code") and building.get("omi_area") and building.get("category")):
            continue
        fallback_buildings.append(building)

    municipality_quote_requests = {
        code: (lambda code=code: _comune_quotes_payload(code))
        for code in comune_codes
    }
    exact_quotes_task = asyncio.create_task(_run_bounded(quote_requests, timeout=8))
    municipality_quotes_task = asyncio.create_task(
        _run_bounded(municipality_quote_requests, timeout=8)
    )
    quotes_by_zone, quotes_by_comune = await asyncio.gather(exact_quotes_task, municipality_quotes_task)

    options_by_comune: dict[str, dict[str, Any]] = {}
    for code in comune_codes:
        payload = quotes_by_comune.get(code)
        all_quotes = payload.get("quotes", []) if isinstance(payload, dict) else []
        zones_by_code: dict[str, list[dict[str, Any]]] = {}
        for quote in all_quotes:
            zone_code = str(quote.get("zona") or "").strip().upper()
            if zone_code:
                zones_by_code.setdefault(zone_code, []).append(quote)
        for (quote_comune, zone_code), zone_quotes in quotes_by_zone.items():
            if quote_comune == code and zone_quotes is not None:
                zones_by_code.setdefault(zone_code, zone_quotes)
        typologies_by_zone: dict[str, list[dict[str, Any]]] = {}
        for zone_code, zone_quotes in zones_by_code.items():
            labels_by_code: dict[int, str] = {}
            for quote in zone_quotes:
                try:
                    typology = int(quote.get("cod_tipologia"))
                except (TypeError, ValueError):
                    continue
                labels_by_code.setdefault(
                    typology,
                    str(quote.get("tipologia") or omi_quotes.OMI_TYPOLOGY_LABELS.get(typology) or f"Tipologia {typology}"),
                )
            typologies_by_zone[zone_code] = [
                {"code": typology, "label": label}
                for typology, label in sorted(labels_by_code.items(), key=lambda item: (item[1].casefold(), item[0]))
            ]

        defaults_by_category: dict[str, dict[str, int]] = {}
        categories = {building.get("category") for building in buildings if building["municipality_code"] == code}
        for category in categories:
            if not category:
                continue
            normalized = normalize_category(category)
            defaults: dict[str, int] = {}
            for zone_code, zone_quotes in zones_by_code.items():
                selected, _reason = omi_quotes.select_quote(zone_quotes, category)
                if selected is not None:
                    try:
                        defaults[zone_code] = int(selected["cod_tipologia"])
                        continue
                    except (KeyError, TypeError, ValueError):
                        pass
                for candidate in omi_quotes.OMI_TYPOLOGY_BY_CATEGORY.get(normalized, ()):
                    if any(str(row.get("cod_tipologia")) == str(candidate) for row in zone_quotes):
                        defaults[zone_code] = candidate
                        break
            if all_quotes:
                quote_range, _reason = omi_quotes.select_quote_range_across_zones(all_quotes, category)
                if quote_range and quote_range.get("cod_tipologia"):
                    defaults[""] = int(quote_range["cod_tipologia"])
                else:
                    for candidate in omi_quotes.OMI_TYPOLOGY_BY_CATEGORY.get(normalized, ()):
                        if any(str(row.get("cod_tipologia")) == str(candidate) for row in all_quotes):
                            defaults[""] = candidate
                            break
            defaults_by_category[normalized] = defaults

        # Retain an automatically mapped or previously selected zone even if this OMI release has no rows for it.
        known_zones = set(zones_by_code)
        for building in buildings:
            if building["municipality_code"] != code:
                continue
            zone = selected_zones.get(building.get("asset_key") or "")
            if zone and zone not in known_zones:
                known_zones.add(zone)
                zones_by_code.setdefault(zone, [])
                typologies_by_zone.setdefault(zone, [])
        options_by_comune[code] = {
            "zones": [{"code": zone, "label": zone} for zone in sorted(known_zones)],
            "typologies_by_zone": typologies_by_zone,
            "defaults_by_category": defaults_by_category,
        }
    kpis["omi_options_by_comune"] = options_by_comune

    # Show the category-based best guess even when surface data or a usable quote is absent.
    for building in buildings:
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        zone = selected_zones.get(building.get("asset_key") or "") or ""
        category_defaults = (
            options_by_comune.get(building["municipality_code"], {})
            .get("defaults_by_category", {})
            .get(normalize_category(building.get("category")), {})
        )
        saved_typology = saved_selections.get(building.get("asset_key"), {}).get("typology_code")
        default_typology = category_defaults.get(zone, category_defaults.get(""))
        row_estimate["omi_tipologia_default"] = str(default_typology or "")
        row_estimate["omi_tipologia"] = str(saved_typology or default_typology or "")
        # A saved code can equal the current category guess. Still show that persisted value in the selector,
        # instead of making it look like the user's choice was discarded after the page reloads.
        row_estimate["omi_typology_selected"] = saved_typology is not None

    value_min = 0.0
    value_max = 0.0
    value_estimate = 0.0
    valued_units = 0
    exact_zone_units = 0
    municipality_range_units = 0
    for building in buildings:
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        if not building.get("category"):
            row_estimate["note"] = "Categoria catastale assente."
            continue
        if not building.get("omi_area"):
            row_estimate["note"] = "Superficie catastale assente."
            continue
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        zone = selected_zones.get(building.get("asset_key") or "")
        if not zone:
            continue
        quotes = quotes_by_zone.get((building["municipality_code"], zone))
        if quotes is None:
            row_estimate["note"] = "Database OMI non disponibile."
            continue
        raw_typology = saved_selections.get(building.get("asset_key"), {}).get("typology_code")
        try:
            typology_code = int(raw_typology) if raw_typology is not None else None
        except (TypeError, ValueError):
            typology_code = None
        quote, reason = omi_quotes.select_quote(quotes, building["category"], typology_code=typology_code)
        if quote is not None:
            row_estimate["omi_tipologia"] = str(quote.get("cod_tipologia") or row_estimate.get("omi_tipologia") or "")
        if quote is None:
            row_estimate["note"] = reason
            continue
        try:
            price_min = float(quote["prezzo_min"])
            price_max = float(quote["prezzo_max"])
            price_mid = (price_min + price_max) / 2
            low = building["omi_area"] * price_min
            high = building["omi_area"] * price_max
            estimate = building["omi_area"] * price_mid
        except (KeyError, TypeError, ValueError):
            continue
        if low < 0 or high < low or estimate < low or estimate > high:
            continue
        value_min += low
        value_max += high
        value_estimate += estimate
        row_estimate["value_min"] = low
        row_estimate["value_max"] = high
        row_estimate["value_estimate"] = estimate
        valued_units += 1
        exact_zone_units += 1
        surface = {"L": "lorda", "N": "netta"}.get(quote.get("superficie"), "")
        area_formula = building.get("omi_area_formula") or f"{_format_number_it(building['omi_area'])} m²"
        value_formula = (
            f"{area_formula} × € {_format_number_it(price_min)}–"
            f"€ {_format_number_it(price_max)}/m² = "
            f"{_format_currency_it(low)}–{_format_currency_it(high)}"
        )
        central_formula = f"punto medio della quotazione: {area_formula} × € {_format_number_it(price_mid)}/m² = {_format_currency_it(estimate)}"
        row_estimate["range"] = f"{_format_currency_it(low)} – {_format_currency_it(high)}"
        row_estimate["note"] = " · ".join(part for part in (
            "Stima OMI indicativa",
            str(quote.get("tipologia") or ""),
            str(quote.get("stato_conservazione") or "").capitalize(),
            str(quote.get("periodo") or ""),
            f"zona {zone}",
            f"€ {_format_number_it(price_min)}–{_format_number_it(price_max)} al m²"
            + (f" di superficie {surface}" if surface else ""),
            f"Formula: {value_formula}",
            central_formula,
            row_estimate.get("omi_zone_point_method"),
            f"calcolata su {building['omi_area_source']}; non è una perizia",
        ) if part)

    for building in fallback_buildings:
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        payload = quotes_by_comune.get(building["municipality_code"])
        if not isinstance(payload, dict):
            row_estimate["note"] = "Quotazioni OMI comunali non disponibili per stimare l'immobile senza zona puntuale."
            continue
        all_quotes = payload.get("quotes")
        if not isinstance(all_quotes, list):
            all_quotes = []
        raw_typology = saved_selections.get(building.get("asset_key"), {}).get("typology_code")
        try:
            typology_code = int(raw_typology) if raw_typology is not None else None
        except (TypeError, ValueError):
            typology_code = None
        quote_range, reason = omi_quotes.select_quote_range_across_zones(
            all_quotes, building["category"], typology_code=typology_code
        )
        if quote_range is None:
            row_estimate["note"] = reason
            continue
        if quote_range.get("cod_tipologia"):
            row_estimate["omi_tipologia"] = str(quote_range["cod_tipologia"])
        low = building["omi_area"] * quote_range["prezzo_min"]
        high = building["omi_area"] * quote_range["prezzo_max"]
        estimate = building["omi_area"] * quote_range["prezzo_mid"]
        value_min += low
        value_max += high
        value_estimate += estimate
        row_estimate["value_min"] = low
        row_estimate["value_max"] = high
        row_estimate["value_estimate"] = estimate
        valued_units += 1
        municipality_range_units += 1
        row_estimate["range"] = f"{_format_currency_it(low)} – {_format_currency_it(high)}"
        area_formula = building.get("omi_area_formula") or f"{_format_number_it(building['omi_area'])} m²"
        row_estimate["note"] = (
            "Intervallo OMI comunale: zona della particella non determinata; "
            f"minimo e massimo della tipologia catastale tra {quote_range['zone_count']} zone OMI disponibili. "
            f"Formula: {area_formula} × "
            f"€ {_format_number_it(quote_range['prezzo_min'])}–€ {_format_number_it(quote_range['prezzo_max'])}/m² = "
            f"{_format_currency_it(low)}–{_format_currency_it(high)}. Stima centrale basata sulla mediana dei "
            f"punti medi delle quotazioni effettive delle zone: {area_formula} × "
            f"€ {_format_number_it(quote_range['prezzo_mid'])}/m² = {_format_currency_it(estimate)}. "
            f"Stima su {building['omi_area_source']}; non è una perizia."
        )

    if valued_units:
        kpis["asset_value"] = f"{_format_currency_it(value_min)} – {_format_currency_it(value_max)}"
        kpis["asset_value_central"] = _format_currency_it(value_estimate)
        kpis["asset_value_central_note"] = (
            "Valore centrale indicativo, non pubblicato da OMI come importo singolo: somma dei punti medi delle fasce "
            "delle zone individuate; per gli immobili senza zona puntuale, mediana dei punti medi delle quotazioni "
            "effettive delle zone comunali. "
            "La fascia OMI complessiva resta nel riquadro Valore OMI fabbricati."
        )
    _label_missing_estimates(table_estimates)
    unvalued_reasons = _reason_summary({
        (group_index, row_index): estimate["missing"]
        for group_index, group in enumerate(table_estimates)
        for row_index, estimate in enumerate(group["buildings"])
        if estimate.get("missing")
    })
    kpis["asset_value_note"] = (
        f"Stima OMI indicativa su {valued_units} di {building_units} fabbricati "
        f"({exact_zone_units} con zona puntuale, {municipality_range_units} con intervallo comunale su tutte le zone OMI); "
        + (f"non stimati: {unvalued_reasons}; " if unvalued_reasons else "")
        + "terreni esclusi perché OMI non pubblica quotazioni per i terreni, quote di proprietà non applicate. "
        "La stima usa la superficie catastale quando disponibile; se manca, usa la consistenza in m² come proxy o, per i vani "
        "delle categorie A, stima la superficie moltiplicando i vani per il fattore medio nazionale della categoria (Statistiche "
        "Catastali 2012). La superficie stimata è solo un proxy OMI: non sostituisce la superficie catastale e non viene usata "
        "per il calcolo della superficie catastale in quota. La stima centrale somma il punto medio della fascia OMI "
        "della zona individuata; quando la zona non è determinata, usa la mediana dei punti medi delle fasce OMI "
        "effettive del comune. La fascia min–max resta visibile per rappresentare l'incertezza. La stima OMI non "
        "equivale necessariamente alla superficie commerciale."
    )
    kpis["owner_assessment"] = _owner_assessment(owner_assessment_assets, table_estimates, subject_age)
    if include_assessment_context:
        kpis["_assessment_assets"] = owner_assessment_assets
        kpis["_subject_age"] = subject_age
    return kpis
