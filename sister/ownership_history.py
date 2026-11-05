"""Ownership and property-state history: what each search saw, kept as periods and appended only when it changes.

Every search answer is an *observation* of some cadastral units: their state (classamento, consistenza, rendita, address)
and, for each, the owners it lists with their right and share. :func:`record_observations` turns one answer into
period updates:

* an observation identical to the open period moves its ``last_seen_at`` / ``last_request_id`` and adds to ``seen_count``;
* a different state or right opens a new period (the previous state period is closed);
* a period is closed (``ended_at``) only by an answer that lists *everything* it could contain and no longer lists it:
  the Intestati page of a unit (all its owners), or a complete subject search (all the properties of the subject, every
  province, Fabbricati and Terreni, every registry record of the code).

So repeating a search never adds rows, and the tables keep the full ownership history in the order the searches saw it.
An answer identical to the previous answer of the same query is not stored again at all (``request.same_as_request_id``);
:func:`extend_identical` just extends the periods it had opened or extended.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# keys that change with every run without saying anything about the cadastre
_VOLATILE = {"page_visits", "timestamp", "downloaded_pdfs", "elapsed", "elapsed_seconds", "screenshot_url", "documents_pending",
             "documents_unconfirmed", "needs_human"}
_STATE_COLUMNS = ("property_type", "category", "cadastral_class", "consistency_value", "consistency_unit", "census_zone",
                  "land_quality", "area_sqm", "income_eur", "dominical_income_eur", "agricultural_income_eur", "address",
                  "partita")


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def content_hash(data: Any) -> Optional[str]:
    """Identity of an answer's content: the same properties, owners and pages read give the same hash, whatever the run."""
    if not isinstance(data, dict):
        return None
    payload = json.dumps(_stable(data), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: _stable(state.get(k)) for k in _STATE_COLUMNS}, sort_keys=True, default=str).encode()).hexdigest()


def _has_state(state: dict[str, Any]) -> bool:
    return any(state.get(k) not in (None, "") for k in _STATE_COLUMNS if k != "property_type")


def subject_search_is_complete(data: dict, cadastre_type: Optional[str]) -> bool:
    """Does this answer list *all* the properties of the searched subject (so a missing one is no longer owned)?"""
    if not isinstance(data, dict) or data.get("error") or "province" not in data or not data.get("soggetto"):
        return False
    if (cadastre_type or "E").upper() != "E":  # only Fabbricati or only Terreni
        return False
    if data.get("incompleta") or data.get("troppi_risultati"):
        return False
    total = data.get("omonimi_totale")
    if total is not None:  # persona giuridica: every registry record of the code, from the first one
        listed = data.get("omonimi") or []
        if data.get("omonimi_elaborati") != total or not listed or listed[0].get("indice") != 1:
            return False
        if any(item.get("errore") for item in listed):
            return False
    return True


async def collect_observations(session: AsyncSession, cadastre_type: str, data: dict, request_unit: Any = None) -> list[dict]:
    """The units an answer shows, with their state and owners as ids (the same lookups as the response projection)."""
    from .database import _project_response, _property_columns, _resolve_subject
    from .reference import fiscal_code_key, get_or_create_cadastral_unit, get_or_create_right, get_or_create_subject_record

    properties, _unlinked, _results = _project_response("", cadastre_type, data)
    observations = []
    records: dict[tuple, Optional[int]] = {}
    for projected in properties:
        location = dict(projected.location)
        if request_unit is not None:
            location["province"] = location["province"] or (request_unit.province_code or "")
            location["municipality"] = location["municipality"] or (request_unit.municipality or "")
        unit_id = await get_or_create_cadastral_unit(session, **location)
        if unit_id is None:
            continue
        state = await _property_columns(session, projected.prop)
        record_id = None
        if projected.record:
            key = tuple(projected.record.items())
            if key not in records:
                records[key] = await get_or_create_subject_record(session, **projected.record)
            record_id = records[key]
        owners = set()
        for subject_fields, right_fields in projected.owners:
            subject_id = await _resolve_subject(session, subject_fields) if subject_fields else None
            if subject_id is None:
                continue
            right_id = await get_or_create_right(session, **right_fields) if right_fields else None
            same_code = projected.record and fiscal_code_key(subject_fields.get("fiscal_code")) == projected.record["fiscal_code"]
            owners.add((subject_id, right_id, record_id if same_code else None))
        observations.append({"unit_id": unit_id, "state": state, "owners": owners, "owners_complete": projected.owners_complete})
    return observations


async def _open_period(session, table: str, where: str, params: dict):
    return (await session.execute(text(f"SELECT * FROM {table} WHERE ended_at IS NULL AND {where}"), params)).mappings().first()  # noqa: S608


async def _extend(session, table: str, period_id: int, request_pk: int, at: datetime) -> None:
    await session.execute(
        text(
            f"UPDATE {table} SET last_seen_at = GREATEST(last_seen_at, :at), last_request_id = :r, seen_count = seen_count + 1 "  # noqa: S608
            "WHERE id = :id"
        ),
        {"at": at, "r": request_pk, "id": period_id},
    )


async def _close(session, table: str, period_ids: list[int], request_pk: int, at: datetime) -> int:
    if not period_ids:
        return 0
    await session.execute(
        text(f"UPDATE {table} SET ended_at = :at, ended_request_id = :r WHERE id = ANY(:ids) AND ended_at IS NULL"),  # noqa: S608
        {"at": at, "r": request_pk, "ids": period_ids},
    )
    return len(period_ids)


async def record_observations(
    session: AsyncSession,
    request_pk: int,
    observed_at: datetime,
    observations: list[dict],
    subject_code: Optional[str] = None,
    subject_complete: bool = False,
) -> dict[str, int]:
    """Apply one answer to the history tables. Returns counts (opened, extended, closed) for ownership and states."""
    stats = {"owner_opened": 0, "owner_extended": 0, "owner_closed": 0, "state_opened": 0, "state_extended": 0, "state_closed": 0}
    period = {"at": observed_at, "r": request_pk}

    # property states: one open period per unit
    for obs in observations:
        state = obs["state"]
        if not _has_state(state):
            continue
        digest = state_hash(state)
        current = await _open_period(session, "property_state_history", "cadastral_unit_id = :u", {"u": obs["unit_id"]})
        if current and current["state_hash"] == digest:
            await _extend(session, "property_state_history", current["id"], request_pk, observed_at)
            stats["state_extended"] += 1
            continue
        if current:
            stats["state_closed"] += await _close(session, "property_state_history", [current["id"]], request_pk, observed_at)
        values = {k: state.get(k) for k in ("property_type", "category", "cadastral_class", "consistency_value", "consistency_unit",
                                             "census_zone", "land_quality", "area_sqm", "income_eur", "dominical_income_eur",
                                             "agricultural_income_eur", "address", "partita")}
        await session.execute(
            text(
                "INSERT INTO property_state_history (cadastral_unit_id, state_hash, " + ", ".join(values) + ", first_seen_at, "
                "first_request_id, last_seen_at, last_request_id, seen_count) VALUES (:u, :h, "
                + ", ".join(f":{k}" for k in values) + ", :at, :r, :at, :r, 1)"
            ),
            {"u": obs["unit_id"], "h": digest, **values, **period},
        )
        stats["state_opened"] += 1

    # ownership periods
    seen_by_unit: dict[int, set] = {}
    for obs in observations:
        seen_by_unit.setdefault(obs["unit_id"], set()).update(obs["owners"])
    for unit_id, owners in seen_by_unit.items():
        for subject_id, right_id, record_id in owners:
            current = await _open_period(
                session, "ownership_history",
                "cadastral_unit_id = :u AND subject_id = :s AND right_id IS NOT DISTINCT FROM :rt "
                "AND subject_record_id IS NOT DISTINCT FROM :rec",
                {"u": unit_id, "s": subject_id, "rt": right_id, "rec": record_id},
            )
            if current:
                await _extend(session, "ownership_history", current["id"], request_pk, observed_at)
                stats["owner_extended"] += 1
                continue
            await session.execute(
                text(
                    "INSERT INTO ownership_history (cadastral_unit_id, subject_id, subject_record_id, right_id, first_seen_at, "
                    "first_request_id, last_seen_at, last_request_id, seen_count) VALUES (:u, :s, :rec, :rt, :at, :r, :at, :r, 1)"
                ),
                {"u": unit_id, "s": subject_id, "rec": record_id, "rt": right_id, **period},
            )
            stats["owner_opened"] += 1

    # closures 1: the Intestati page lists every owner of the unit
    for obs in observations:
        if not obs["owners_complete"]:
            continue
        rows = (await session.execute(
            text("SELECT id, subject_id, right_id, subject_record_id FROM ownership_history WHERE ended_at IS NULL AND cadastral_unit_id = :u"),
            {"u": obs["unit_id"]},
        )).all()
        gone = [row.id for row in rows if (row.subject_id, row.right_id, row.subject_record_id) not in seen_by_unit[obs["unit_id"]]]
        stats["owner_closed"] += await _close(session, "ownership_history", gone, request_pk, observed_at)

    # closures 2: a complete subject search lists every property of the subject (and its right on each)
    if subject_complete and subject_code:
        subject_id = (await session.execute(text("SELECT id FROM subject WHERE fiscal_code = :c"), {"c": subject_code})).scalar()
        if subject_id is not None:
            held = {(unit_id, right_id, record_id) for unit_id, owners in seen_by_unit.items()
                    for sid, right_id, record_id in owners if sid == subject_id}
            rows = (await session.execute(
                text("SELECT id, cadastral_unit_id, right_id, subject_record_id FROM ownership_history WHERE ended_at IS NULL AND subject_id = :s"),
                {"s": subject_id},
            )).all()
            gone = [row.id for row in rows if (row.cadastral_unit_id, row.right_id, row.subject_record_id) not in held]
            stats["owner_closed"] += await _close(session, "ownership_history", gone, request_pk, observed_at)
    return stats


async def extend_identical(session: AsyncSession, previous_pk: int, request_pk: int, observed_at: datetime) -> int:
    """An answer identical to ``previous_pk``: extend the open periods that answer last saw. Returns how many."""
    total = 0
    for table in ("ownership_history", "property_state_history"):
        result = await session.execute(
            text(
                f"UPDATE {table} SET last_seen_at = GREATEST(last_seen_at, :at), last_request_id = :r, seen_count = seen_count + 1 "  # noqa: S608
                "WHERE last_request_id = :prev AND ended_at IS NULL"
            ),
            {"at": observed_at, "r": request_pk, "prev": previous_pk},
        )
        total += result.rowcount or 0
    return total
