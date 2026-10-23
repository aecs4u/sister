"""Portfolio area totals, cadastral (IMU) values and indicative OMI estimates for subject visure.

Two different figures are produced, and the page must not blur them:

- *valore catastale*: the statutory tax base from rendita / reddito dominicale (``cadastral_values``), for buildings
  and land alike;
- *stima OMI*: an indicative market range from the official OMI quotes (``omi_quotes``), buildings only, because OMI
  publishes no land quotes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from . import omi_quotes
from .cadastral_values import building_cadastral_value, building_multiplier, land_cadastral_value, normalize_category
from .result_parsers import clean_quota, clean_right_description, parse_right_period

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
    # "proprieta per l'area" (01T) is the landowner of a surface right: the building belongs to the superficiario
    if "proprieta" in text and not any(term in text for term in ("superficiar", "separata", "concedente", "per l area")):
        return "full"
    if not text and str(code or "").strip().zfill(2) == "01":
        return "full"
    return "other"


_BIRTH_DATE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


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
    birth = _BIRTH_DATE.search(nominative)
    code = str(right.get("CodiceDiritto") or "").strip().upper()
    return {
        "kind": _right_kind(description, code),
        "label": description or "Diritto non specificato",
        "quota": quota,
        "share": _share_fraction(quota),
        "fixed_term": fixed_term,
        # usufrutto con diritto di accrescimento (08A): the usufruct lasts until the last of the holders dies
        "accrual": code.endswith("A") or "accrescimento" in description.casefold(),
        "name": " ".join((nominative[: birth.start()] if birth else nominative.split(";")[0]).split()),
        "age": _age_on(_date_value(birth.group(1)) if birth else None, as_of),
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


def _format_value_range(low: Decimal, high: Decimal) -> str:
    return f"{_format_currency_it(float(low))} – {_format_currency_it(float(high))}"


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
        ("Totale diritti stimati OMI", assessment["omi_total"], "Somma delle stime OMI attribuibili ai diritti del soggetto."),
        ("Piena proprietà · quota", (by_kind.get("full") or {}).get("value") or "Nessuna intestata al soggetto",
         "Il soggetto non ha piena proprietà su alcun bene della visura." if "full" not in by_kind else
         "Stima OMI della piena proprietà del soggetto."),
        ("Usufrutto · quota", (by_kind.get("usufruct") or {}).get("value") or "Nessuno intestato al soggetto",
         "Il soggetto non è usufruttuario di alcun bene della visura (l'usufrutto può essere di altre persone)."
         if "usufruct" not in by_kind else "Stima OMI dell'usufrutto del soggetto, sull'età di chi lo detiene."),
        ("Nuda proprietà · quota", (by_kind.get("bare") or {}).get("value") or "Nessuna intestata al soggetto",
         "Il soggetto non è nudo proprietario di alcun bene della visura." if "bare" not in by_kind else
         "Valore pieno meno il valore dell'usufrutto, calcolato sull'età degli usufruttuari."),
        ("Valore catastale · quota", assessment["cadastral_share"], "Base imponibile IMU × quota dichiarata. " + quota_note),
        ("Superficie catastale · quota", assessment["area_share"], "Superficie catastale × quota dichiarata. " + quota_note),
        ("Rendita fabbricati · quota", assessment["building_income_share"], "Rendita catastale dei fabbricati × quota. " + quota_note),
        ("Reddito dominicale · quota", assessment["dominical_income_share"], "Reddito dominicale dei terreni × quota. " + quota_note),
        ("Reddito agrario · quota", assessment["agricultural_income_share"], "Reddito agrario dei terreni × quota. " + quota_note),
    ]
    return [{"label": label, "value": value, "hint": hint, "missing": is_missing_value(value)} for label, value, hint in tiles]


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

    for asset in assets:
        asset_key = (asset["group_index"], asset["table_kind"], asset["row_index"])
        rights = asset.get("rights") or []
        if not rights:
            unmatched_assets.add(asset_key)
            continue
        estimate = table_estimates[asset["group_index"]][asset["table_kind"]][asset["row_index"]]
        estimate_low = _assessment_decimal(estimate.get("value_min"))
        estimate_high = _assessment_decimal(estimate.get("value_max"))
        cadastral_value = _assessment_decimal(asset.get("cadastral_value"))
        area = _assessment_decimal(asset.get("area"))

        for right in rights:
            kind = right["kind"]
            bucket_key = kind if kind != "other" else f"other:{right['label'].casefold()}"
            bucket = accumulators.setdefault(bucket_key, {
                "kind": kind,
                "label": {"full": "Piena proprietà", "usufruct": "Usufrutto", "bare": "Nuda proprietà"}.get(
                    kind, clean_right_description(right["label"])),
                "assets": set(),
                "shares": set(),
                "value_low": Decimal(0),
                "value_high": Decimal(0),
                "valued_assets": set(),
                "missing_value_assets": set(),
                "missing_reasons": {},
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
            else:
                factor = None
            if factor is None or estimate_low is None or estimate_high is None:
                bucket["missing_value_assets"].add(asset_key)
                if estimate_low is None or estimate_high is None:
                    reason = estimate.get("missing") or "Stima OMI non disponibile"
                    omi_missing_reasons[asset_key] = reason
                else:
                    reason = factor_reason or "Coefficiente non disponibile"
                bucket["missing_reasons"][asset_key] = reason
                continue
            attributed_low = estimate_low * share * factor
            attributed_high = estimate_high * share * factor
            bucket["value_low"] += attributed_low
            bucket["value_high"] += attributed_high
            bucket["valued_assets"].add(asset_key)
            omi_total_low += attributed_low
            omi_total_high += attributed_high
            omi_total_seen = True

    rights_summary = []
    order = {"full": 0, "usufruct": 1, "bare": 2, "other": 3}
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
            "ponderati per quota e non rappresentano redditi effettivi. Valori indicativi, non perizia; i terreni "
            "non hanno quotazioni OMI."
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


def _format_number_it(value: float, *, decimals: int = 2) -> str:
    rendered = f"{value:,.{decimals}f}"
    whole, _, fraction = rendered.partition(".")
    whole = whole.replace(",", ".")
    fraction = fraction.rstrip("0")
    return f"{whole},{fraction}" if fraction else whole


def _format_currency_it(value: float, *, decimals: int = 0) -> str:
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


async def subject_land_registry_kpis(
    xml_parsed: dict[str, Any], map_lookup: dict[str, Any] | None
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

    groups = _items(subject.get("GruppoUnitaImmobiliari"))
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
            classamento = _first(property_row.get("DatiClassamentoF"))
            surface = _first(property_row.get("SuperficieF"))
            area = _number(surface.get("Totale"))
            income = _number(classamento.get("RenditaEuro"))
            if area is not None:
                building_area += area
                building_area_units += 1
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
            table_estimates[group_index]["buildings"].append({
                "range": "",
                "value_min": None,
                "value_max": None,
                "note": _initial_omi_note(classamento.get("Categoria"), area),
                "cadastral_value": cadastral_value,
                "cadastral_note": cadastral_note,
                "category": normalize_category(classamento.get("Categoria")),
                # A category link can still show all OMI zones for this municipality when
                # no parcel geometry is available to resolve the property's exact zone.
                "omi_comune": code,
                "omi_zona": "",
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
                "group_index": group_index,
                "table_row_index": table_row_index,
            })

        for property_row in _items(group.get("ImmobileTerreniS")):
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

    land_rows = sum(len(group["lands"]) for group in table_estimates)

    def building_figure(value: str | None) -> str:
        return value if value is not None else (NOT_AVAILABLE if building_units else NO_BUILDINGS)

    def land_figure(value: str | None) -> str:
        return value if value is not None else (NOT_AVAILABLE if land_rows else NO_LANDS)

    def land_note(count: int, what: str) -> str:
        return f"{count} classamenti con {what}" if land_rows else "La visura non elenca terreni"

    kpis: dict[str, Any] = {
        "distinct_comuni": len(distinct_municipalities),
        "building_rows": building_units,
        "land_rows": land_rows,
        "building_area": building_figure(f"{_format_number_it(building_area)} m²" if building_area_units else None),
        "building_area_note": (
            f"{building_area_units} di {building_units} unità con superficie in visura" if building_units else "La visura non elenca fabbricati"
        ),
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
    if buildings and not isinstance(map_lookup, dict):
        kpis["asset_value_note"] = (
            "Stima OMI non eseguita: senza la mappa catastale locale non si può individuare la zona OMI delle "
            "particelle. Il valore catastale non ne dipende."
        )
    if not buildings or not isinstance(map_lookup, dict):
        return kpis

    # Markers are existing cadastral-map centroids, so OMI zone matching uses
    # the same parcel geometry and shared land-registry zone lookup as its dossier.
    markers_by_parcel: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for marker in map_lookup.get("parcel_markers") or []:
        if not isinstance(marker, dict):
            continue
        key = (
            str(marker.get("municipality_code") or "").strip().upper(),
            str(marker.get("sheet") or "").strip().upper(),
            str(marker.get("parcel") or "").strip().upper(),
        )
        if all(key):
            markers_by_parcel.setdefault(key, []).append(marker)

    mapped_buildings = []
    for building in buildings:
        if not building.get("municipality_code"):
            continue
        key = (building["municipality_code"], building["sheet"], building["parcel"])
        candidates = markers_by_parcel.get(key, [])
        section = building["section"]
        if section:
            section_matches = [
                marker for marker in candidates
                if str(marker.get("section") or "").strip().upper() == section
            ]
            marker = section_matches[0] if len(section_matches) == 1 else (
                candidates[0] if not section_matches and len(candidates) == 1 else None
            )
        else:
            marker = candidates[0] if len(candidates) == 1 else None
        if marker and marker.get("latitude") is not None and marker.get("longitude") is not None:
            mapped_buildings.append((building, marker))
    try:
        from land_registry import stats_service
    except ImportError:
        logger.debug("Shared land_registry package is unavailable for subject OMI KPIs")
        return kpis

    zone_keys = {}
    for building, marker in mapped_buildings:
        try:
            latitude = float(marker["latitude"])
            longitude = float(marker["longitude"])
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        province = building["province"] or str(marker.get("province") or "").strip()
        key = (building["municipality_code"], province, latitude, longitude)
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        zone_keys[parcel_key] = key
    unique_zones = set(zone_keys.values())
    zone_requests = {
        key: (lambda key=key: stats_service.aget_omi_zone_at_point(
            key[1], key[2], key[3], cadastral_code=key[0]
        ))
        for key in unique_zones
        if key[1]
    }
    zone_results = await _run_bounded(
        zone_requests,
        timeout=8,
    )

    quote_requests = {}
    parcel_zones = {}
    for building, _marker in mapped_buildings:
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        zone = (zone_results.get(zone_keys.get(parcel_key)) or {}).get("zone")
        if not zone:
            continue
        zone = str(zone).strip().upper()
        parcel_zones[parcel_key] = zone
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        row_estimate["omi_comune"] = building["municipality_code"]
        row_estimate["omi_zona"] = zone
        if building.get("area") and building.get("category"):
            quote_key = (building["municipality_code"], zone)
            if quote_key not in quote_requests:
                quote_requests[quote_key] = lambda quote_key=quote_key: omi_quotes.omi_zone_quotes(*quote_key)

    # If some buildings have no parcel centroid (or no OMI-zone match), still give them a useful, honest range:
    # select the category quote in each OMI zone of their municipality and span the lowest to highest values.
    fallback_buildings = []
    fallback_comuni = set()
    for building in buildings:
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        if parcel_key in parcel_zones:
            continue
        if not (building.get("municipality_code") and building.get("area") and building.get("category")):
            continue
        fallback_buildings.append(building)
        fallback_comuni.add(building["municipality_code"])

    municipality_quote_requests = {
        code: (lambda code=code: _comune_quotes_payload(code))
        for code in fallback_comuni
    }
    exact_quotes_task = asyncio.create_task(_run_bounded(quote_requests, timeout=8))
    municipality_quotes_task = asyncio.create_task(
        _run_bounded(municipality_quote_requests, timeout=8)
    )
    quotes_by_zone, quotes_by_comune = await asyncio.gather(exact_quotes_task, municipality_quotes_task)

    value_min = 0.0
    value_max = 0.0
    valued_units = 0
    exact_zone_units = 0
    municipality_range_units = 0
    for building, _marker in mapped_buildings:
        row_estimate = table_estimates[building["group_index"]]["buildings"][building["table_row_index"]]
        if not building.get("category"):
            row_estimate["note"] = "Categoria catastale assente."
            continue
        if not building.get("area"):
            row_estimate["note"] = "Superficie catastale assente."
            continue
        parcel_key = (
            building["municipality_code"], building["section"], building["sheet"], building["parcel"]
        )
        zone = parcel_zones.get(parcel_key)
        if not zone:
            continue
        quotes = quotes_by_zone.get((building["municipality_code"], zone))
        if quotes is None:
            row_estimate["note"] = "Database OMI non disponibile."
            continue
        quote, reason = omi_quotes.select_quote(quotes, building["category"])
        if quote is None:
            row_estimate["note"] = reason
            continue
        try:
            low = building["area"] * float(quote["prezzo_min"])
            high = building["area"] * float(quote["prezzo_max"])
        except (KeyError, TypeError, ValueError):
            continue
        if low < 0 or high < low:
            continue
        value_min += low
        value_max += high
        row_estimate["value_min"] = low
        row_estimate["value_max"] = high
        valued_units += 1
        exact_zone_units += 1
        surface = {"L": "lorda", "N": "netta"}.get(quote.get("superficie"), "")
        row_estimate["range"] = f"{_format_currency_it(low)} – {_format_currency_it(high)}"
        row_estimate["note"] = " · ".join(part for part in (
            "Stima OMI indicativa",
            str(quote.get("tipologia") or ""),
            str(quote.get("stato_conservazione") or "").capitalize(),
            str(quote.get("periodo") or ""),
            f"zona {zone}",
            f"€ {_format_number_it(float(quote['prezzo_min']))}–{_format_number_it(float(quote['prezzo_max']))} al m²"
            + (f" di superficie {surface}" if surface else ""),
            "calcolata sulla superficie catastale; non è una perizia",
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
        quote_range, reason = omi_quotes.select_quote_range_across_zones(all_quotes, building["category"])
        if quote_range is None:
            row_estimate["note"] = reason
            continue
        low = building["area"] * quote_range["prezzo_min"]
        high = building["area"] * quote_range["prezzo_max"]
        value_min += low
        value_max += high
        row_estimate["value_min"] = low
        row_estimate["value_max"] = high
        valued_units += 1
        municipality_range_units += 1
        row_estimate["range"] = f"{_format_currency_it(low)} – {_format_currency_it(high)}"
        row_estimate["note"] = (
            "Intervallo OMI comunale: zona della particella non determinata; "
            f"minimo e massimo della tipologia catastale tra {quote_range['zone_count']} zone OMI disponibili. "
            "Stima sulla superficie catastale, non è una perizia."
        )

    if valued_units:
        kpis["asset_value"] = f"{_format_currency_it(value_min)} – {_format_currency_it(value_max)}"
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
        "Superficie catastale usata come proxy: non equivale necessariamente alla superficie commerciale."
    )
    kpis["owner_assessment"] = _owner_assessment(owner_assessment_assets, table_estimates, subject_age)
    return kpis
