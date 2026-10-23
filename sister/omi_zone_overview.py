"""Full overview of one OMI zone: quotes over time, variations, rent and yield, plus comune context.

Sources (both local PostgreSQL databases, read with short-lived asyncpg connections):

* ``omi`` (``public`` schema): ``omi_ref_zone`` / ``omi_fact_quotazioni_valori`` for the quotes of every published
  semester, ``omi_ref_comuni`` / ``omi_fact_compravendite_*`` for the number of normalised transactions (NTN).
* ``aecs4u-stats`` (``STATS_POSTGRES_DSN``): ISTAT resident population (``istat``), MEF IRPEF income (``mef_irpef``),
  ISTAT house price index (``serving.istat_house_price_index``) and the Eurostat NUTS3 series (``eurostat``).

The builders (``build_zone_overview``, ``rebase_series``, ...) are pure so they can be tested without a database. The
context sources are optional: when one cannot be read its card reports it as unavailable instead of failing the page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from .omi_quotes import cache_ttl_seconds, omi_dsn

logger = logging.getLogger("sister")

CONNECT_TIMEOUT = 3
QUERY_TIMEOUT = 12.0
CACHE_FORMAT = 1  # bump when the raw payload layout changes: older files are then ignored
CONTEXT_RETRY_SECONDS = 300.0  # a payload without the stats context is reused only briefly, then retried
_CACHE_MAX_ENTRIES = 128
_FAILURE_BACKOFF_SECONDS = 60.0

# Typology shown first, in order of preference; the rest follow by code.
MAIN_TYPOLOGY_ORDER = (20, 21, 19, 22, 1, 5, 6, 9, 10, 13, 16)
STATE_ORDER = {"OTTIMO": 0, "NORMALE": 1, "SCADENTE": 2}
REFERENCE_AREA_SQM = 70  # the illustrative dwelling of the price-to-income indicator

_memory: dict[tuple[str, str], dict[str, Any]] = {}
_retry_at: dict[str, float] = {}

_OMI_SQL = """
WITH zz AS MATERIALIZED (
    SELECT zona_id, anno, semestre, zona, fascia, zona_descrizione, link_zona, comune, provincia, regione,
           cod_comune_amm, cod_comune_catastale, cod_comune_istat, anno * 10 + semestre AS period
    FROM public.omi_ref_zone
    WHERE cod_comune_amm = $1 OR cod_comune_catastale = $1 OR cod_comune_istat = $3::float8
),
bounds AS (
    SELECT (SELECT max(period) FROM zz) AS newest,
           (SELECT max(period) FROM zz WHERE UPPER(zona) = $2) AS zone_newest
)
SELECT zz.anno, zz.semestre, zz.zona, zz.fascia, zz.zona_descrizione, zz.link_zona, zz.comune, zz.provincia,
       zz.regione, zz.cod_comune_amm, zz.cod_comune_istat, v.cod_tipologia, v.tipologia, v.stato_conservazione,
       v.stato_prevalente, v.prezzo_min, v.prezzo_max, v.superficie_nl_compravendita, v.locazione_min,
       v.locazione_max, v.superficie_nl_locazione
FROM zz
CROSS JOIN bounds
JOIN public.omi_fact_quotazioni_valori AS v ON v.zona_id = zz.zona_id
WHERE UPPER(zz.zona) = $2 OR zz.period = bounds.newest OR zz.period = bounds.zone_newest
"""

_NTN_SQL = """
SELECT r.anno, f.ntn_totale, f.ntn_fino_50mq, f.ntn_50_85mq, f.ntn_85_115mq, f.ntn_115_145mq, f.ntn_oltre_145mq,
       c.ntn_uffici, c.ntn_negozi_lab, c.ntn_depositi_comm_autorimesse, c.ntn_depositi_comm, c.ntn_produttivo,
       p.ntn_box, p.ntn_depositi_pertinenziali
FROM public.omi_ref_comuni AS r
LEFT JOIN public.omi_fact_compravendite_residenziali AS f ON f.comune_id = r.comune_id
LEFT JOIN public.omi_fact_compravendite_commerciali AS c ON c.comune_id = r.comune_id
LEFT JOIN public.omi_fact_compravendite_pertinenze AS p ON p.comune_id = r.comune_id
WHERE r.cod_comune = $1
ORDER BY r.anno
"""


# ---------------------------------------------------------------------------
# Small parsing / arithmetic helpers
# ---------------------------------------------------------------------------


def parse_number(value: Any) -> float | None:
    """Number from a float or an Italian-formatted text (``'15,2'``, ``'1.234,5'``); ``None`` when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def clean_description(value: Any) -> str:
    """Zone description without the quotes and the trailing comma OMI leaves in it."""
    text = str(value or "").strip().strip("'\"").strip().rstrip(",").strip()
    return re.sub(r"\s+", " ", text)


def period_key(year: int, semester: int) -> int:
    return int(year) * 2 + int(semester) - 1


def period_label(year: int, semester: int) -> str:
    return f"{int(year)}S{int(semester)}"


def label_from_key(key: int) -> str:
    return period_label(key // 2, key % 2 + 1)


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new / old - 1.0) * 100.0


def _mid(low: float | None, high: float | None) -> float | None:
    if low is None and high is None:
        return None
    if low is None or high is None:
        return low if high is None else high
    return (low + high) / 2.0


def gross_yield(sale_mid: float | None, rent_mid: float | None) -> float | None:
    """Gross yearly yield in percent: monthly rent per m2 x 12 over the price per m2."""
    if not sale_mid or rent_mid is None or sale_mid <= 0:
        return None
    return rent_mid * 12.0 / sale_mid * 100.0


def rebase_series(series: dict[int, float], base_year: int) -> dict[int, float]:
    """Series as an index that is 100 in ``base_year`` (empty when the base year has no usable value)."""
    base = series.get(base_year)
    if not base:
        return {}
    return {year: value / base * 100.0 for year, value in series.items() if year >= base_year}


def cagr(first: float | None, last: float | None, years: float) -> float | None:
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return ((last / first) ** (1.0 / years) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# OMI quotes -> zone overview (pure)
# ---------------------------------------------------------------------------


def _normalise_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Typed, de-duplicated quote rows. The zone table repeats a zone for ``sezione`` ``''``/``' '``/NULL."""
    seen: set[tuple[Any, ...]] = set()
    quotes: list[dict[str, Any]] = []
    for row in rows:
        if row["anno"] is None or row["semestre"] is None:
            continue
        state = (row["stato_conservazione"] or "").strip().upper()
        key = (row["anno"], row["semestre"], (row["zona"] or "").strip().upper(), row["cod_tipologia"], state)
        if key in seen:
            continue
        seen.add(key)
        low, high = parse_number(row["prezzo_min"]), parse_number(row["prezzo_max"])
        rent_low, rent_high = parse_number(row["locazione_min"]), parse_number(row["locazione_max"])
        quotes.append({
            "anno": int(row["anno"]),
            "semestre": int(row["semestre"]),
            "key": period_key(row["anno"], row["semestre"]),
            "periodo": period_label(row["anno"], row["semestre"]),
            "zona": (row["zona"] or "").strip().upper(),
            "fascia": (row["fascia"] or "").strip().upper(),
            "descrizione": clean_description(row["zona_descrizione"]),
            "link_zona": (row["link_zona"] or "").strip(),
            "comune": (row["comune"] or "").strip(),
            "provincia": (row["provincia"] or "").strip(),
            "regione": (row["regione"] or "").strip(),
            "cod_comune": (row["cod_comune_amm"] or "").strip(),
            "cod_istat": row["cod_comune_istat"],
            "cod_tipologia": int(row["cod_tipologia"]) if row["cod_tipologia"] is not None else None,
            "tipologia": (row["tipologia"] or "").strip(),
            "stato": state,
            "prevalente": (row["stato_prevalente"] or "").strip().upper() == "P",
            "min": low,
            "max": high,
            "mid": _mid(low, high),
            "affitto_min": rent_low,
            "affitto_max": rent_high,
            "affitto_mid": _mid(rent_low, rent_high),
            "superficie": (row["superficie_nl_compravendita"] or "").strip().upper(),
            "superficie_affitto": (row["superficie_nl_locazione"] or "").strip().upper(),
        })
    return quotes


def _typology_sort_key(code: int | None) -> tuple[int, int]:
    code = code if code is not None else 10**6
    return (MAIN_TYPOLOGY_ORDER.index(code), code) if code in MAIN_TYPOLOGY_ORDER else (len(MAIN_TYPOLOGY_ORDER), code)


def _reference_state(quotes: list[dict[str, Any]]) -> str:
    """The conservation state a typology's trend is read on.

    The state OMI flags as prevalent can change between semesters; following the flag would splice different states
    into one line. The state that is prevalent in most periods is used instead (NORMALE on a tie).
    """
    prevalent: dict[str, int] = {}
    present: dict[str, int] = {}
    for quote in quotes:
        present[quote["stato"]] = present.get(quote["stato"], 0) + 1
        if quote["prevalente"]:
            prevalent[quote["stato"]] = prevalent.get(quote["stato"], 0) + 1
    ranking = prevalent or present
    return min(ranking, key=lambda state: (-ranking[state], STATE_ORDER.get(state, 9), state))


def _variations(points: dict[int, dict[str, Any]], field: str = "mid") -> dict[str, Any]:
    """Changes of one value over the semesters of a series (``points`` keyed by ``period_key``)."""
    if not points:
        return {}
    keys = sorted(points)
    last_key, first_key = keys[-1], keys[0]
    last, first = points[last_key][field], points[first_key][field]

    def back(semesters: int) -> float | None:
        previous = points.get(last_key - semesters)
        return pct_change(last, previous[field]) if previous else None

    peak_key = max((k for k in keys if points[k][field] is not None), key=lambda k: points[k][field], default=None)
    trough_key = min((k for k in keys if points[k][field] is not None), key=lambda k: points[k][field], default=None)
    years = (last_key - first_key) / 2.0
    return {
        "last": last,
        "periodo": points[last_key]["periodo"],
        "first_periodo": points[first_key]["periodo"],
        "vs_semester": back(1),
        "vs_year": back(2),
        "vs_5_years": back(10),
        "since_start": pct_change(last, first),
        "cagr": cagr(first, last, years),
        "years": years,
        "peak": points[peak_key][field] if peak_key is not None else None,
        "peak_periodo": points[peak_key]["periodo"] if peak_key is not None else None,
        "from_peak": pct_change(last, points[peak_key][field]) if peak_key is not None else None,
        "trough": points[trough_key][field] if trough_key is not None else None,
        "trough_periodo": points[trough_key]["periodo"] if trough_key is not None else None,
    }


def build_zone_overview(rows: list[Any], zona: str) -> dict[str, Any]:
    """Shape the quote rows of a comune (zone history + the newest periods of every zone) for the page."""
    zona = (zona or "").strip().upper()
    quotes = _normalise_rows(rows)
    if not quotes:
        return {"found": False}
    newest = max(q["key"] for q in quotes)
    newest_label = next(q["periodo"] for q in quotes if q["key"] == newest)
    sample = max(quotes, key=lambda q: q["key"])
    overview: dict[str, Any] = {
        "found": True,
        "comune": sample["comune"],
        "provincia": sample["provincia"] or next((q["provincia"] for q in quotes if q["provincia"]), ""),
        "regione": sample["regione"],
        "cod_comune": sample["cod_comune"],
        "cod_istat": sample["cod_istat"],
        "zona": zona,
        "newest_period": newest_label,
        "newest_key": newest,
    }

    zone_quotes = [q for q in quotes if q["zona"] == zona] if zona else []
    zones_at_newest = sorted({q["zona"] for q in quotes if q["key"] == newest})
    overview["zones_newest"] = zones_at_newest
    overview["zones"] = _zone_list(quotes, newest)

    if not zone_quotes:
        overview["zone_found"] = False
        return overview
    overview["zone_found"] = True

    periods = sorted({q["key"] for q in zone_quotes})
    first_key, last_key = periods[0], periods[-1]
    last_quote = next(q for q in zone_quotes if q["key"] == last_key)
    overview.update({
        "fascia": last_quote["fascia"],
        "descrizione": last_quote["descrizione"],
        "link_zona": last_quote["link_zona"],
        "first_period": next(q["periodo"] for q in zone_quotes if q["key"] == first_key),
        "last_period": last_quote["periodo"],
        "last_key": last_key,
        "period_count": len(periods),
        "gap_periods": [label_from_key(k) for k in range(first_key, last_key + 1) if k not in set(periods)],
        "discontinued": last_key < newest,
        "descriptions": _description_changes(zone_quotes),
    })

    by_typology: dict[int | None, list[dict[str, Any]]] = {}
    for quote in zone_quotes:
        by_typology.setdefault(quote["cod_tipologia"], []).append(quote)

    typologies = []
    for code in sorted(by_typology, key=_typology_sort_key):
        entries = by_typology[code]
        state = _reference_state(entries)
        points = {q["key"]: q for q in entries if q["stato"] == state}
        series = [
            {k: points[key][k] for k in (
                "periodo", "anno", "semestre", "min", "max", "mid", "affitto_min", "affitto_max", "affitto_mid",
            )} | {"yield": gross_yield(points[key]["mid"], points[key]["affitto_mid"])}
            for key in sorted(points)
        ]
        last_point = points[max(points)]
        rent_points = {k: v for k, v in points.items() if v["affitto_mid"] is not None}
        yields = {k: {"mid": gross_yield(v["mid"], v["affitto_mid"]), "periodo": v["periodo"]}
                  for k, v in points.items() if gross_yield(v["mid"], v["affitto_mid"]) is not None}
        typologies.append({
            "code": code,
            "label": entries[0]["tipologia"],
            "state": state,
            "surface": last_point["superficie"],
            "rent_surface": last_point["superficie_affitto"],
            "first_period": series[0]["periodo"],
            "last_period": series[-1]["periodo"],
            "series": series,
            "sale": _variations(points, "mid"),
            "sale_min": _variations(points, "min"),
            "sale_max": _variations(points, "max"),
            "rent": _variations(rent_points, "affitto_mid") if rent_points else {},
            "yield": _variations(yields, "mid") if yields else {},
        })
    overview["typologies"] = typologies
    overview["main_typology"] = typologies[0]["code"] if typologies else None
    overview["latest_quotes"] = _latest_quotes(zone_quotes, last_key)
    overview["history"] = _history_matrix(typologies)
    overview["comparison"] = _zone_comparison(quotes, zona, last_key, typologies[0]["code"] if typologies else None)
    overview["annual"] = _annual_means(typologies[0]) if typologies else {}
    overview["successor"] = find_successor(overview) if overview["discontinued"] and typologies else None
    return overview


def _description_tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[A-Z0-9]+", text.upper()) if len(word) >= 4}


def find_successor(overview: dict[str, Any]) -> dict[str, Any] | None:
    """The zone of the newest release that most likely replaced a zone OMI stopped publishing.

    OMI redraws and renumbers zones between releases (D4 -> D5 keeps its description and its range, for example).
    A candidate needs a similar description, or the same quote together with some description overlap; the result
    is a hint to open, never merged into the zone's own history.
    """
    series = overview["typologies"][0]["series"]
    last = series[-1] if series else None
    own_tokens = _description_tokens(overview.get("descrizione", ""))
    best: tuple[float, dict[str, Any]] | None = None
    for zone in overview.get("zones", []):
        if zone["zona"] == overview["zona"]:
            continue
        tokens = _description_tokens(zone["descrizione"])
        overlap = len(own_tokens & tokens) / len(own_tokens | tokens) if own_tokens | tokens else 0.0
        same_quote = bool(
            last and last["min"] and last["max"] and zone["min"] and zone["max"]
            and abs(zone["min"] / last["min"] - 1) <= 0.01 and abs(zone["max"] / last["max"] - 1) <= 0.01
        )
        if overlap >= 0.4 or (same_quote and overlap >= 0.2):
            score = overlap + (0.5 if same_quote else 0.0)
            if best is None or score > best[0]:
                best = (score, {"zona": zone["zona"], "descrizione": zone["descrizione"], "periodo": zone["periodo"],
                                "min": zone["min"], "max": zone["max"], "same_quote": same_quote,
                                "similarity": round(overlap * 100)})
    return best[1] if best else None


def _zone_list(quotes: list[dict[str, Any]], newest: int) -> list[dict[str, Any]]:
    """Every zone of the comune in the newest published semester, with its main residential quote."""
    zones: dict[str, dict[str, Any]] = {}
    for quote in quotes:
        if quote["key"] != newest:
            continue
        entry = zones.setdefault(quote["zona"], {
            "zona": quote["zona"], "fascia": quote["fascia"], "descrizione": quote["descrizione"],
            "periodo": quote["periodo"], "quotes": [],
        })
        entry["quotes"].append(quote)
    result = []
    for zone in sorted(zones.values(), key=lambda z: (z["fascia"], z["zona"])):
        quote = None
        for code in MAIN_TYPOLOGY_ORDER:
            candidates = [q for q in zone["quotes"] if q["cod_tipologia"] == code]
            quote = next((q for q in candidates if q["prevalente"]), candidates[0] if candidates else None)
            if quote:
                break
        result.append({
            "zona": zone["zona"], "fascia": zone["fascia"], "descrizione": zone["descrizione"],
            "periodo": zone["periodo"], "tipologia": quote["tipologia"] if quote else "",
            "min": quote["min"] if quote else None, "max": quote["max"] if quote else None,
            "affitto_min": quote["affitto_min"] if quote else None,
            "affitto_max": quote["affitto_max"] if quote else None,
        })
    return result


def _description_changes(zone_quotes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Distinct zone descriptions in chronological order: OMI redraws and renames zones between releases."""
    changes: list[dict[str, str]] = []
    for quote in sorted(zone_quotes, key=lambda q: q["key"]):
        if quote["descrizione"] and (not changes or changes[-1]["descrizione"] != quote["descrizione"]):
            changes.append({"periodo": quote["periodo"], "descrizione": quote["descrizione"]})
    return changes


def _latest_quotes(zone_quotes: list[dict[str, Any]], last_key: int) -> list[dict[str, Any]]:
    rows = [q for q in zone_quotes if q["key"] == last_key]
    previous_year = {(q["cod_tipologia"], q["stato"]): q for q in zone_quotes if q["key"] == last_key - 2}
    result = []
    for quote in sorted(rows, key=lambda q: (_typology_sort_key(q["cod_tipologia"]), STATE_ORDER.get(q["stato"], 9))):
        before = previous_year.get((quote["cod_tipologia"], quote["stato"]))
        result.append(quote | {
            "yield": gross_yield(quote["mid"], quote["affitto_mid"]),
            "spread": pct_change(quote["max"], quote["min"]),
            "vs_year": pct_change(quote["mid"], before["mid"]) if before else None,
        })
    return result


def _history_matrix(typologies: list[dict[str, Any]]) -> dict[str, Any]:
    """Period x typology table (newest first) of the reference-state sale ranges."""
    periods: dict[str, dict[str, Any]] = {}
    for typology in typologies:
        for point in typology["series"]:
            periods.setdefault(point["periodo"], {})[typology["code"]] = point
    ordered = sorted(periods, key=lambda p: (int(p[:4]), int(p[-1])), reverse=True)
    return {
        "columns": [{"code": t["code"], "label": t["label"], "state": t["state"]} for t in typologies],
        "rows": [{"periodo": p, "cells": periods[p]} for p in ordered],
    }


def _zone_comparison(
    quotes: list[dict[str, Any]], zona: str, last_key: int, typology: int | None
) -> dict[str, Any]:
    """The selected zone against the other zones of the comune in the zone's own last published semester."""
    if typology is None:
        return {}
    entries = []
    for quote in quotes:
        if quote["key"] != last_key or quote["cod_tipologia"] != typology:
            continue
        entries.append(quote)
    by_zone: dict[str, dict[str, Any]] = {}
    for quote in entries:
        current = by_zone.get(quote["zona"])
        if current is None or (quote["prevalente"] and not current["prevalente"]):
            by_zone[quote["zona"]] = quote
    zones = sorted(by_zone.values(), key=lambda q: (q["mid"] is None, -(q["mid"] or 0)))
    ranked = [{
        "zona": q["zona"], "fascia": q["fascia"], "descrizione": q["descrizione"], "min": q["min"], "max": q["max"],
        "mid": q["mid"], "affitto_mid": q["affitto_mid"], "yield": gross_yield(q["mid"], q["affitto_mid"]),
        "selected": q["zona"] == zona,
    } for q in zones]
    comune_mid = [z["mid"] for z in ranked if z["mid"] is not None]
    selected = next((z for z in ranked if z["selected"]), None)
    return {
        "typology": typology,
        "label": entries[0]["tipologia"] if entries else "",
        "periodo": label_from_key(last_key),
        "zones": ranked,
        "rank": (ranked.index(selected) + 1) if selected and selected["mid"] is not None else None,
        "of": len(ranked),
        "comune_mean": sum(comune_mid) / len(comune_mid) if comune_mid else None,
        "vs_comune": pct_change(selected["mid"], sum(comune_mid) / len(comune_mid)) if selected and comune_mid else None,
    }


def _annual_means(typology: dict[str, Any]) -> dict[str, dict[int, float]]:
    """Calendar-year mean of the main typology's sale and rent midpoints (for the cross-source comparison)."""
    sale: dict[int, list[float]] = {}
    rent: dict[int, list[float]] = {}
    for point in typology["series"]:
        if point["mid"] is not None:
            sale.setdefault(point["anno"], []).append(point["mid"])
        if point["affitto_mid"] is not None:
            rent.setdefault(point["anno"], []).append(point["affitto_mid"])
    return {
        "sale": {year: sum(values) / len(values) for year, values in sale.items()},
        "rent": {year: sum(values) / len(values) for year, values in rent.items()},
    }


# ---------------------------------------------------------------------------
# Context from aecs4u-stats (pure builders)
# ---------------------------------------------------------------------------


def build_comparison(
    annual_sale: dict[int, float], context: dict[str, Any] | None
) -> dict[str, Any]:
    """Zone price, ISTAT HPI, IRPEF income and population rebased to 100 on a common base year.

    The base is the first year in which the zone price, the HPI and the income all have a value, so the lines start
    together. Population is added only when it covers that base year (its series can be shorter), and a series that
    does not overlap the zone's years is left out. Each series stops at its own last year.
    """
    context = context or {}
    empty: dict[str, Any] = {"years": [], "series": {}, "labels": {}, "table": []}
    population = {row["year"]: float(row["population"]) for row in context.get("population", []) if row["population"]}
    income = {row["year"]: float(row["average_income"]) for row in context.get("income", []) if row["average_income"]}
    hpi = {row["year"]: float(row["index"]) for row in (context.get("hpi") or {}).get("index", [])}
    series: dict[str, dict[int, float]] = {"zone": dict(annual_sale)}
    labels = {"zone": "Prezzo di zona (OMI)"}
    for name, label, values in (
        ("hpi", "Indice prezzi abitazioni ISTAT (Italia)", hpi),
        ("income", "Reddito imponibile medio (IRPEF)", income),
    ):
        if values and set(values) & set(annual_sale):
            series[name] = values
            labels[name] = label
    if len(series) < 2 or not annual_sale:
        return empty
    base = max(min(set(values) & set(annual_sale)) for values in series.values())
    if population and base in population and base in annual_sale:
        series["population"] = population
        labels["population"] = "Popolazione residente"
    last = max(annual_sale)
    if last <= base:
        return empty
    years = list(range(base, last + 1))
    rebased = {name: rebase_series(values, base) for name, values in series.items()}
    table = []
    for name, values in series.items():
        end = max(year for year in values if base <= year <= last)
        first_value, last_value = values.get(base), values.get(end)
        table.append({
            "name": labels[name], "base": first_value, "last": last_value, "last_year": end,
            "change": pct_change(last_value, first_value), "cagr": cagr(first_value, last_value, end - base),
        })
    return {
        "base_year": base,
        "last_year": last,
        "years": years,
        "series": {name: [rebased[name].get(year) for year in years] for name in rebased},
        "labels": labels,
        "table": table,
    }


def build_affordability(
    annual_sale: dict[int, float], income: list[dict[str, Any]], area: int = REFERENCE_AREA_SQM
) -> list[dict[str, Any]]:
    """Years of the comune's average taxable income needed to buy ``area`` m2 at the zone's mean price."""
    rows = []
    for item in sorted(income, key=lambda r: r["year"]):
        price, average = annual_sale.get(item["year"]), item.get("average_income")
        if price and average:
            rows.append({
                "year": item["year"], "price": price, "income": float(average),
                "years_of_income": price * area / float(average),
            })
    return rows


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def stats_dsn() -> str | None:
    """asyncpg DSN of the ``aecs4u-stats`` database, or ``None`` when it is not configured."""
    dsn = os.getenv("STATS_POSTGRES_DSN", "").strip() or os.getenv("CADASTRAL_POSTGRES_DSN", "").strip()
    if not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    return "postgresql://" + dsn.split("://", 1)[1]


async def _connect(dsn: str):
    import asyncpg

    return await asyncio.wait_for(
        asyncpg.connect(
            dsn,
            timeout=CONNECT_TIMEOUT,
            command_timeout=QUERY_TIMEOUT,
            server_settings={"jit": "off", "statement_timeout": str(int(QUERY_TIMEOUT * 1000))},
        ),
        timeout=CONNECT_TIMEOUT + 1,
    )


def _backing_off(name: str) -> bool:
    return time.monotonic() < _retry_at.get(name, 0.0)


def _back_off(name: str, exc: Exception) -> None:
    _retry_at[name] = time.monotonic() + _FAILURE_BACKOFF_SECONDS
    logger.warning("%s lookup failed: %s", name, type(exc).__name__)


def _istat_numeric_code(cod_istat: Any) -> str | None:
    """Six-digit ISTAT municipality code (``063014``) from OMI's regional code (``15063014``)."""
    try:
        return str(int(float(cod_istat)))[-6:].zfill(6)
    except (TypeError, ValueError):
        return None


async def _fetch_omi(comune: str, zona: str) -> tuple[list[Any], list[Any]]:
    istat = float(comune) if comune.isdigit() else None
    connection = await _connect(omi_dsn() or "")
    try:
        rows = await connection.fetch(_OMI_SQL, comune, zona, istat)
        ntn = await connection.fetch(_NTN_SQL, comune) if rows else []
    finally:
        await connection.close()
    return list(rows), list(ntn)


def _ntn_summary(rows: list[Any]) -> dict[str, Any]:
    """Residential and commercial NTN by year; empty when OMI publishes none for the comune (small markets)."""
    series = []
    for row in rows:
        residential = parse_number(row["ntn_totale"])
        bands = {
            "fino_50": parse_number(row["ntn_fino_50mq"]), "da_50_a_85": parse_number(row["ntn_50_85mq"]),
            "da_85_a_115": parse_number(row["ntn_85_115mq"]), "da_115_a_145": parse_number(row["ntn_115_145mq"]),
            "oltre_145": parse_number(row["ntn_oltre_145mq"]),
        }
        other = {
            "uffici": parse_number(row["ntn_uffici"]), "negozi_laboratori": parse_number(row["ntn_negozi_lab"]),
            "depositi": parse_number(row["ntn_depositi_comm"]),
            "autorimesse": parse_number(row["ntn_depositi_comm_autorimesse"]),
            "produttivo": parse_number(row["ntn_produttivo"]), "box": parse_number(row["ntn_box"]),
            "pertinenze": parse_number(row["ntn_depositi_pertinenziali"]),
        }
        if residential is None and not any(v is not None for v in other.values()):
            continue
        series.append({"year": int(row["anno"]), "residential": residential, "bands": bands, "other": other})
    return {"series": series} if series else {}


async def _fetch_context(cod_comune: str, cod_istat: Any) -> dict[str, Any] | None:
    """Comune context from ``aecs4u-stats``; ``None`` when that database cannot be queried."""
    dsn = stats_dsn()
    if not dsn or _backing_off("aecs4u-stats"):
        return None
    istat_code = _istat_numeric_code(cod_istat)
    try:
        connection = await _connect(dsn)
    except Exception as exc:
        _back_off("aecs4u-stats", exc)
        return None
    context: dict[str, Any] = {}

    async def section(name: str, coroutine) -> None:
        try:
            context[name] = await coroutine
        except Exception as exc:  # one broken source must not hide the others
            logger.warning("aecs4u-stats %s lookup failed: %s", name, type(exc).__name__)

    async def population() -> list[dict[str, Any]]:
        if not istat_code:
            return []
        rows = await connection.fetch(
            "SELECT reference_year, population FROM istat.comune_population_by_year "
            "WHERE comune_code = $1 AND population IS NOT NULL ORDER BY reference_year", istat_code)
        return [{"year": int(r["reference_year"]), "population": int(r["population"])} for r in rows]

    async def income() -> list[dict[str, Any]]:
        rows = await connection.fetch(
            "SELECT c.anno, c.codice, c.average_income_eur AS comune, c.imponibile_freq AS taxpayers, "
            "i.average_income_eur AS italia "
            "FROM mef_irpef.income_average_comune AS c "
            "LEFT JOIN mef_irpef.income_average_italia AS i ON i.anno = c.anno "
            "WHERE c.codice = $1 ORDER BY c.anno", cod_comune)
        return [{
            "year": int(r["anno"]),
            "average_income": float(r["comune"]) if r["comune"] is not None else None,
            "taxpayers": int(r["taxpayers"]) if r["taxpayers"] is not None else None,
            "italy_average_income": float(r["italia"]) if r["italia"] is not None else None,
        } for r in rows]

    async def hpi() -> dict[str, Any]:
        rows = await connection.fetch(
            "SELECT measure, year, index FROM serving.istat_house_price_index "
            "WHERE ref_area = 'IT' AND dwelling_type = 'ALL' AND measure IN ('4', '8') ORDER BY year")
        return {
            "index": [{"year": int(r["year"]), "index": float(r["index"])}
                      for r in rows if r["measure"] == "4" and r["index"] is not None],
            "change": [{"year": int(r["year"]), "change": float(r["index"])}
                       for r in rows if r["measure"] == "8" and r["index"] is not None],
        }

    async def nuts3() -> dict[str, Any]:
        code = None
        if istat_code:
            code = await connection.fetchval(
                "SELECT nuts3 FROM istat.comune_nuts_by_year WHERE comune_code = $1 "
                "ORDER BY reference_year DESC LIMIT 1", istat_code)
        if not code:
            return {}
        population_rows = await connection.fetch(
            "SELECT year, population FROM eurostat.eurostat_nuts3_population "
            "WHERE nuts_code = $1 AND sex = 'T' AND age = 'TOTAL' AND population IS NOT NULL ORDER BY year", code)
        gdp_rows = await connection.fetch(
            "SELECT year, gdp FROM eurostat.eurostat_regional_gdp "
            "WHERE nuts_code = $1 AND unit = 'EUR_HAB' AND gdp IS NOT NULL ORDER BY year", code)
        demography_rows = await connection.fetch(
            "SELECT year, indic_de, value FROM eurostat.eurostat_migration "
            "WHERE nuts_code = $1 AND indic_de IN ('GBIRTHRT', 'GDEATHRT', 'CNMIGRATRT') AND value IS NOT NULL "
            "ORDER BY year", code)
        demography: dict[int, dict[str, float]] = {}
        for row in demography_rows:
            demography.setdefault(int(row["year"]), {})[row["indic_de"]] = float(row["value"])
        return {
            "code": code,
            "population": [{"year": int(r["year"]), "population": int(r["population"])} for r in population_rows],
            "gdp_per_capita": [{"year": int(r["year"]), "gdp": float(r["gdp"])} for r in gdp_rows],
            "demography": [{"year": y, **values} for y, values in sorted(demography.items())],
        }

    try:
        await section("population", population())
        await section("income", income())
        await section("hpi", hpi())
        await section("nuts3", nuts3())
    finally:
        await connection.close()
    return context


def _cache_dir() -> str:
    override = os.getenv("SISTER_OMI_CACHE_DIR", "").strip()
    if override:
        return override
    outputs = os.getenv("SISTER_OUTPUTS_DIR") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "outputs")
    return os.path.join(outputs, "cache", "omi_overview")


def _cache_path(key: tuple[str, str]) -> str:
    name = "_".join(re.sub(r"[^A-Z0-9-]", "-", part) or "x" for part in key)
    return os.path.join(_cache_dir(), f"{name}.json")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:  # NaN is not valid JSON
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return float(value) if hasattr(value, "as_tuple") else str(value)  # Decimal / anything else


def _read_cached(key: tuple[str, str]) -> dict[str, Any] | None:
    """Raw payload of a previous lookup, from memory or the cache file (whatever its age)."""
    payload = _memory.get(key)
    if payload is not None:
        return payload
    try:
        with open(_cache_path(key), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != CACHE_FORMAT or not payload.get("rows"):
        return None
    _remember(key, payload)
    return payload


def _remember(key: tuple[str, str], payload: dict[str, Any]) -> None:
    if len(_memory) >= _CACHE_MAX_ENTRIES:
        _memory.pop(next(iter(_memory)))
    _memory[key] = payload


def _write_cached(key: tuple[str, str], payload: dict[str, Any]) -> None:
    _remember(key, payload)
    path = _cache_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError as exc:  # the cache is an optimisation: never fail the page over it
        logger.warning("OMI overview cache not written (%s): %s", path, type(exc).__name__)


def _is_fresh(payload: dict[str, Any]) -> bool:
    age = time.time() - float(payload.get("saved_at", 0))
    return age < (cache_ttl_seconds() if payload.get("context") is not None else CONTEXT_RETRY_SECONDS)


async def _fetch_payload(code: str, zone: str) -> dict[str, Any]:
    """Raw OMI rows, NTN and stats context, as plain JSON-able data (what the cache stores)."""
    rows, ntn_rows = await _fetch_omi(code, zone)
    payload: dict[str, Any] = {
        "format": CACHE_FORMAT, "saved_at": time.time(),
        "rows": [_jsonable(dict(row)) for row in rows], "ntn": [_jsonable(dict(row)) for row in ntn_rows],
        "context": None,
    }
    if rows:
        first = rows[0]
        payload["context"] = await _fetch_context(first["cod_comune_amm"] or code, first["cod_comune_istat"])
    return payload


def _assemble(payload: dict[str, Any], zone: str, code: str, stale: bool) -> dict[str, Any]:
    """The page's overview from a raw payload (all derived figures are computed here, never cached)."""
    overview = build_zone_overview(payload["rows"], zone)
    if not overview.get("found"):
        return overview
    saved_at = float(payload.get("saved_at", 0))
    overview["cache"] = {
        "saved_at": saved_at, "age_days": max(time.time() - saved_at, 0.0) / 86400.0,
        "ttl_days": cache_ttl_seconds() / 86400.0, "stale": stale,
    }
    overview["ntn"] = _ntn_summary(payload.get("ntn", []))
    context = payload.get("context")
    overview["context"] = context
    if overview.get("zone_found"):
        annual = (overview.get("annual") or {}).get("sale", {})
        overview["comparison_chart"] = build_comparison(annual, context)
        overview["affordability"] = build_affordability(annual, (context or {}).get("income", []))
        overview["reference_area_sqm"] = REFERENCE_AREA_SQM
    return overview


async def load_zone_overview(comune: str, zona: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Overview of one OMI zone (or, without ``zona``, the zone list of the comune).

    OMI publishes twice a year, so the raw data of a lookup is kept for ``OMI_CACHE_DAYS`` (30) in memory and in a
    file under ``outputs/cache/omi_overview`` (it survives the service's frequent reloads). A database that cannot be
    reached falls back to the stored copy, marked ``cache.stale``; ``refresh=True`` skips the stored copy.

    ``None`` means no data at all (database unreachable and nothing stored); ``{"found": False}`` means OMI knows no
    such comune. The comune context is ``None`` when ``aecs4u-stats`` is unavailable.
    """
    code, zone = str(comune or "").strip().upper(), str(zona or "").strip().upper()
    if not code:
        return None
    key = (code, zone)
    stored = None if refresh else _read_cached(key)
    if stored is not None and _is_fresh(stored):
        return _assemble(stored, zone, code, stale=False)
    if omi_dsn() and not _backing_off("omi"):
        try:
            payload = await _fetch_payload(code, zone)
        except Exception as exc:
            _back_off("omi", exc)
        else:
            if payload["rows"]:  # an unknown comune is not stored: it may be a typo
                _write_cached(key, payload)
            return _assemble(payload, zone, code, stale=False)
    stored = stored or _read_cached(key)  # database down: an expired copy is better than an error page
    return _assemble(stored, zone, code, stale=True) if stored is not None else None


def overview_chart_data(overview: dict[str, Any]) -> dict[str, Any]:
    """The part of an overview the page's charts need, as plain JSON-serialisable data."""
    context = overview.get("context") or {}
    comparison = overview.get("comparison") or {}
    return {
        "typologies": [{
            "code": t["code"], "label": t["label"], "state": t["state"],
            "labels": [p["periodo"] for p in t["series"]],
            "min": [p["min"] for p in t["series"]], "max": [p["max"] for p in t["series"]],
            "mid": [p["mid"] for p in t["series"]],
            "rent_min": [p["affitto_min"] for p in t["series"]], "rent_max": [p["affitto_max"] for p in t["series"]],
            "rent_mid": [p["affitto_mid"] for p in t["series"]],
            "yield": [p["yield"] for p in t["series"]],
        } for t in overview.get("typologies", [])],
        "main_typology": overview.get("main_typology"),
        "comparison": overview.get("comparison_chart") or {},
        "zones": [
            {"zona": z["zona"], "min": z["min"], "max": z["max"], "selected": z["selected"]}
            for z in comparison.get("zones", [])
        ],
        "zones_label": comparison.get("label", ""),
        "zones_period": comparison.get("periodo", ""),
        "population": context.get("population", []),
        "nuts3_demography": (context.get("nuts3") or {}).get("demography", []),
    }
