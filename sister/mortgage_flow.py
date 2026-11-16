"""Durable consent and recovery primitives for paid mortgage portal actions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from .database import _get_session_factory
from .db_models import Document, MortgagePaidAction, MortgageSearchFlow, MortgageSearchFormality, MortgageSearchRequest, Request

_TOKEN_TTL_SECONDS = 300
_PAID_ACTION_RECOVERY_TIMEOUT_SECONDS = 300
_PAID_STEPS = {"accept", "notes", "title", "richieste_save"}
_OPEN_STATES = {"intended", "clicked", "uncertain"}


class MortgageFlowError(ValueError):
    """A flow, quote token, or paid-action transition is invalid."""


def _secret() -> bytes:
    value = (
        os.getenv("SISTER_MORTGAGE_CONFIRM_SECRET", "").strip()
        or os.getenv("SISTER_LINK_SECRET", "").strip()
        or os.getenv("SECRET_KEY", "").strip()
    )
    if not value or value == "change-me-in-production" or len(value) < 32:
        raise RuntimeError("A strong SISTER_MORTGAGE_CONFIRM_SECRET or SECRET_KEY is required for paid-step consent")
    return value.encode("utf-8")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def issue_confirmation_token(
    flow_id: int,
    step: str,
    quote: dict[str, Any],
    *,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    if step not in _PAID_STEPS:
        raise MortgageFlowError("unsupported_paid_step")
    created_at = now or datetime.now(timezone.utc)
    expires_at = created_at + timedelta(seconds=max(1, min(int(ttl_seconds), _TOKEN_TTL_SECONDS)))
    quote_hash = hashlib.sha256(_canonical_json(quote)).hexdigest()
    payload = {
        "flow_id": int(flow_id),
        "step": step,
        "quote": quote,
        "quote_hash": quote_hash,
        "issued_at": int(created_at.timestamp()),
        "expires_at": int(expires_at.timestamp()),
        "nonce": secrets.token_urlsafe(16),
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii").rstrip("=")
    signature = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    token = encoded + "." + signature
    return {"token": token, "expires_at": expires_at.isoformat(), "quote_hash": quote_hash}


def verify_confirmation_token(token: str, *, flow_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise MortgageFlowError("invalid_confirmation_token")
        payload_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(payload_bytes)
    except MortgageFlowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MortgageFlowError("invalid_confirmation_token") from exc
    if not isinstance(payload, dict) or payload.get("step") not in _PAID_STEPS:
        raise MortgageFlowError("invalid_confirmation_token")
    if flow_id is not None and int(payload.get("flow_id", -1)) != int(flow_id):
        raise MortgageFlowError("confirmation_token_wrong_flow")
    current = now or datetime.now(timezone.utc)
    if int(payload.get("expires_at") or 0) < int(current.timestamp()):
        raise MortgageFlowError("confirmation_token_expired")
    quote = payload.get("quote")
    if not isinstance(quote, dict) or hashlib.sha256(_canonical_json(quote)).hexdigest() != payload.get("quote_hash"):
        raise MortgageFlowError("confirmation_quote_changed")
    return payload


async def create_flow_record(
    *,
    request_id: int,
    subject_fiscal_code: str,
    office: str,
    scope: str,
    started_by: str,
    state_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        flow = MortgageSearchFlow(
            request_id=int(request_id),
            subject_fiscal_code=subject_fiscal_code,
            office=office,
            scope=scope,
            state="preview",
            current_step="preview",
            state_payload=state_payload or {},
            started_by=started_by,
            started_at=now,
            updated_at=now,
        )
        session.add(flow)
        await session.flush()
        result = {"id": int(flow.id), "request_id": flow.request_id, "state": flow.state, "current_step": flow.current_step}
        await session.commit()
        return result


async def update_flow_state(
    flow_id: int, *, state: str, current_step: str, state_payload: dict[str, Any] | None = None
) -> None:
    if state not in {"preview", "accepted", "running", "complete", "uncertain", "failed", "cancelled"}:
        raise MortgageFlowError("invalid_flow_state")
    values: dict[str, Any] = {"state": state, "current_step": current_step, "updated_at": datetime.now(timezone.utc)}
    if state_payload is not None:
        values["state_payload"] = state_payload
    async with _get_session_factory()() as session:
        await session.execute(update(MortgageSearchFlow).where(MortgageSearchFlow.id == int(flow_id)).values(**values))
        await session.commit()


async def record_paid_action_intent(flow_id: int, token: str, confirmed_by: str) -> dict[str, Any]:
    payload = verify_confirmation_token(token, flow_id=flow_id)
    step = payload["step"]
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with _get_session_factory()() as session:
        flow = (
            await session.execute(
                select(MortgageSearchFlow)
                .where(MortgageSearchFlow.id == int(flow_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if flow is None:
            raise MortgageFlowError("flow_not_found")
        existing_token = (
            await session.execute(
                select(MortgagePaidAction.id).where(MortgagePaidAction.token_digest == token_digest)
            )
        ).scalar_one_or_none()
        if existing_token is not None:
            raise MortgageFlowError("confirmation_token_already_used")
        open_action = (
            await session.execute(
                select(MortgagePaidAction.id).where(
                    MortgagePaidAction.flow_id == flow.id,
                    MortgagePaidAction.step == step,
                    MortgagePaidAction.state.in_(_OPEN_STATES),
                )
            )
        ).scalar_one_or_none()
        if open_action is not None:
            raise MortgageFlowError("paid_action_requires_reconciliation")
        if flow.state in {"uncertain", "failed", "cancelled", "complete"}:
            raise MortgageFlowError("flow_not_confirmable")
        if step == "accept" and flow.state != "preview":
            raise MortgageFlowError("flow_not_at_preview")
        if step != "accept" and flow.search_id is None:
            raise MortgageFlowError("accepted_search_required")
        quote = payload["quote"]
        if not isinstance(quote, dict) or quote.get("step") != step:
            raise MortgageFlowError("confirmation_quote_step_mismatch")
        portal_items = quote.get("portal_items") if isinstance(quote, dict) else None
        if step in {"notes", "title"}:
            if flow.state not in {"accepted", "running"} or flow.current_step != "formalita":
                raise MortgageFlowError("flow_not_at_formalita_step")
            if not isinstance(portal_items, list) or not portal_items or len(portal_items) > 100:
                raise MortgageFlowError("paid_quote_items_missing")
            ordinals = [item.get("ordinal") for item in portal_items if isinstance(item, dict)]
            values = [str(item.get("selection_value") or "") for item in portal_items if isinstance(item, dict)]
            if (
                len(ordinals) != len(portal_items)
                or any(type(ordinal) is not int or ordinal <= 0 for ordinal in ordinals)
                or len(set(ordinals)) != len(ordinals)
                or any(not value for value in values)
                or len(set(values)) != len(values)
            ):
                raise MortgageFlowError("paid_quote_items_invalid")
            formality_rows = (
                await session.execute(
                    select(MortgageSearchFormality).where(
                        MortgageSearchFormality.search_id == flow.search_id,
                        MortgageSearchFormality.ordinal.in_(ordinals),
                    )
                )
            ).scalars().all()
            formality_by_ordinal = {int(row.ordinal): row for row in formality_rows}
            if set(formality_by_ordinal) != set(ordinals) or any(
                formality_by_ordinal[ordinal].portal_selection_value != item["selection_value"]
                for ordinal, item in ((item["ordinal"], item) for item in portal_items)
            ):
                raise MortgageFlowError("paid_quote_formalities_changed")
            existing_requests = (
                await session.execute(
                    select(MortgageSearchRequest).where(
                        MortgageSearchRequest.search_id == flow.search_id,
                        MortgageSearchRequest.formality_id.in_([row.id for row in formality_rows]),
                        MortgageSearchRequest.kind == step.removesuffix("s"),
                    )
                )
            ).scalars().all()
            if any(row.status in {"intended", "requested", "available", "downloaded", "uncertain"} for row in existing_requests):
                raise MortgageFlowError("formality_request_already_owned_or_pending")
        elif step == "richieste_save":
            if flow.state not in {"accepted", "running"} or flow.current_step not in {"formalita", "richieste_list"}:
                raise MortgageFlowError("flow_not_at_richieste_step")
            request_id = str(quote.get("portal_request_id") or "").strip()
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id)
                or int(quote.get("search_id") or 0) != int(flow.search_id)
                or quote.get("kind") not in {"note", "title"}
                or quote.get("format") != "PDF"
            ):
                raise MortgageFlowError("paid_save_quote_invalid")
            try:
                amount = Decimal(str(quote.get("amount"))).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise MortgageFlowError("paid_save_quote_invalid") from exc
            if amount <= 0:
                raise MortgageFlowError("paid_save_quote_invalid")
            request_row = (
                await session.execute(
                    select(MortgageSearchRequest)
                    .where(
                        MortgageSearchRequest.search_id == flow.search_id,
                        MortgageSearchRequest.portal_request_id == request_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request_row is None or request_row.status != "available" or request_row.kind != quote["kind"]:
                raise MortgageFlowError("mortgage_request_not_available")
        now = datetime.now(timezone.utc)
        action = MortgagePaidAction(
            flow_id=flow.id,
            search_id=flow.search_id,
            step=step,
            quote=quote,
            confirmed_by=confirmed_by,
            confirmed_at=now,
            state="intended",
            token_digest=token_digest,
            token_expires_at=datetime.fromtimestamp(payload["expires_at"], timezone.utc),
        )
        session.add(action)
        flow.state = "running"
        flow.current_step = step
        flow.updated_at = now
        await session.flush()
        if step in {"notes", "title"}:
            for item in portal_items:
                formality = formality_by_ordinal[item["ordinal"]]
                session.add(
                    MortgageSearchRequest(
                        search_id=flow.search_id,
                        formality_id=formality.id,
                        kind=step.removesuffix("s"),
                        format=str(quote.get("format") or "PDF").upper(),
                        status="intended",
                        paid_action_id=action.id,
                    )
                )
        elif step == "richieste_save":
            request_row.paid_action_id = action.id
        await session.flush()
        result = {"id": int(action.id), "flow_id": int(flow.id), "search_id": flow.search_id, "step": step, "state": action.state}
        await session.commit()
        return result


async def complete_paid_request_save(action_id: int, portal_request_id: str, document_id: int) -> None:
    """Link a confirmed paid Salva action to its exact downloaded document and typed request row."""
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        action = (
            await session.execute(
                select(MortgagePaidAction)
                .where(MortgagePaidAction.id == int(action_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if action is None or action.step != "richieste_save" or action.state not in {"intended", "clicked"}:
            raise MortgageFlowError("paid_save_action_not_attachable")
        request_row = (
            await session.execute(
                select(MortgageSearchRequest)
                .where(
                    MortgageSearchRequest.search_id == action.search_id,
                    MortgageSearchRequest.portal_request_id == str(portal_request_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        document = await session.get(Document, int(document_id))
        if (
            request_row is None
            or request_row.status != "available"
            or request_row.paid_action_id != action.id
            or document is None
            or int(document.file_size or 0) <= 0
        ):
            raise MortgageFlowError("paid_save_result_link_invalid")
        request_row.status = "downloaded"
        request_row.document_id = int(document.id)
        action.portal_request_ids = [str(portal_request_id)]
        action.result = {
            **(action.result or {}),
            "portal_request_id": str(portal_request_id),
            "document_id": int(document.id),
            "filename": document.file_name,
            "completed_at": now.isoformat(),
        }
        await session.commit()


async def attach_paid_action_requests(action_id: int, portal_rows: list[dict[str, Any]]) -> None:
    """Attach portal request IDs only when the browser matched each row to a quoted formalità."""
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        action = (
            await session.execute(
                select(MortgagePaidAction)
                .where(MortgagePaidAction.id == int(action_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if action is None or action.step not in {"notes", "title"} or action.state not in {"intended", "clicked"}:
            raise MortgageFlowError("paid_action_not_attachable")
        items = action.quote.get("portal_items") if isinstance(action.quote, dict) else None
        expected = {int(item["ordinal"]) for item in items or [] if isinstance(item, dict)}
        by_ordinal = {}
        for row in portal_rows:
            if not isinstance(row, dict):
                continue
            ordinal = row.get("ordinal")
            request_id = str(row.get("portal_request_id") or "").strip()
            status = str(row.get("status") or "requested")
            if type(ordinal) is not int or not request_id or status not in {"requested", "available"}:
                raise MortgageFlowError("portal_request_mapping_invalid")
            if ordinal in by_ordinal or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
                raise MortgageFlowError("portal_request_mapping_invalid")
            by_ordinal[ordinal] = (request_id, status)
        if (
            not expected
            or set(by_ordinal) != expected
            or len({item[0] for item in by_ordinal.values()}) != len(expected)
        ):
            raise MortgageFlowError("portal_request_mapping_incomplete")
        requests = (
            await session.execute(
                select(MortgageSearchRequest).where(MortgageSearchRequest.paid_action_id == action.id)
            )
        ).scalars().all()
        request_by_ordinal = {}
        for item in items:
            formality = (
                await session.execute(
                    select(MortgageSearchFormality).where(
                        MortgageSearchFormality.search_id == action.search_id,
                        MortgageSearchFormality.ordinal == int(item["ordinal"]),
                    )
                )
            ).scalar_one_or_none()
            if formality is not None:
                request_by_ordinal[int(item["ordinal"])] = formality.id
        if len(requests) != len(expected):
            raise MortgageFlowError("portal_request_mapping_incomplete")
        for request in requests:
            ordinal = next((value for value, formality_id in request_by_ordinal.items() if formality_id == request.formality_id), None)
            if ordinal is None or ordinal not in by_ordinal:
                raise MortgageFlowError("portal_request_mapping_incomplete")
            request.portal_request_id, request.status = by_ordinal[ordinal]
            request.requested_at = now
        action.portal_request_ids = [by_ordinal[ordinal][0] for ordinal in sorted(by_ordinal)]
        action.result = {
            **(action.result or {}),
            "portal_requests": [
                {"ordinal": ordinal, "portal_request_id": by_ordinal[ordinal][0], "status": by_ordinal[ordinal][1]}
                for ordinal in sorted(by_ordinal)
            ],
        }
        await session.commit()


async def _transition_action(action_id: int, *, from_states: set[str], state: str, result: dict[str, Any] | None = None) -> None:
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        action = await session.get(MortgagePaidAction, int(action_id))
        if action is None:
            raise MortgageFlowError("paid_action_not_found")
        if action.state == state:
            return
        if action.state not in from_states:
            raise MortgageFlowError("invalid_paid_action_transition")
        action.state = state
        if state == "clicked":
            action.clicked_at = now
        if result is not None:
            action.result = {**(action.result or {}), **result}
        flow = await session.get(MortgageSearchFlow, action.flow_id)
        if flow:
            flow.updated_at = now
            if state == "uncertain":
                flow.state = "uncertain"
                flow.current_step = action.step
            elif state == "confirmed":
                flow.state = "running" if action.step != "richieste_save" else "running"
                flow.current_step = action.step
        if state == "uncertain":
            await session.execute(
                update(MortgageSearchRequest)
                .where(
                    MortgageSearchRequest.paid_action_id == action.id,
                    MortgageSearchRequest.status == "intended",
                )
                .values(status="uncertain")
            )
            if action.step == "richieste_save":
                request_id = str((action.quote or {}).get("portal_request_id") or "")
                if request_id:
                    await session.execute(
                        update(MortgageSearchRequest)
                        .where(
                            MortgageSearchRequest.search_id == action.search_id,
                            MortgageSearchRequest.portal_request_id == request_id,
                            MortgageSearchRequest.status == "available",
                        )
                        .values(status="uncertain")
                    )
        await session.commit()


async def mark_paid_action_clicked(action_id: int) -> None:
    await _transition_action(action_id, from_states={"intended"}, state="clicked")


async def mark_paid_action_confirmed(action_id: int, result: dict[str, Any] | None = None) -> None:
    await _transition_action(action_id, from_states={"clicked"}, state="confirmed", result=result or {})


async def mark_paid_action_uncertain(action_id: int, error: str | None = None) -> None:
    await _transition_action(
        action_id, from_states={"intended", "clicked"}, state="uncertain", result={"error": (error or "")[:500]}
    )


async def execute_paid_action(
    flow_id: int,
    token: str,
    confirmed_by: str,
    executor: Callable[[dict[str, Any], Callable[[], Awaitable[None]]], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Commit intent before portal work; the browser executor calls ``mark_clicked`` immediately after its click."""
    action = await record_paid_action_intent(flow_id, token, confirmed_by)

    async def clicked() -> None:
        await mark_paid_action_clicked(action["id"])

    try:
        outcome = await executor(action, clicked)
    except Exception as exc:
        await mark_paid_action_uncertain(action["id"], type(exc).__name__)
        raise
    if outcome.get("confirmed") is True:
        if outcome.get("clicked") is not True:
            await mark_paid_action_uncertain(action["id"], "executor_did_not_mark_click")
            raise MortgageFlowError("executor_did_not_mark_click")
        await mark_paid_action_confirmed(action["id"], outcome)
        return {**action, "state": "confirmed", "result": outcome}
    await mark_paid_action_uncertain(action["id"], str(outcome.get("error") or "portal_effect_not_confirmed"))
    return {**action, "state": "uncertain", "result": outcome}


async def reconcile_paid_action(action_id: int, *, resolution: str, reconciled_by: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if resolution not in {"confirmed", "not_executed"}:
        raise MortgageFlowError("invalid_reconciliation_resolution")
    now = datetime.now(timezone.utc)
    async with _get_session_factory()() as session:
        action = (
            await session.execute(
                select(MortgagePaidAction)
                .where(MortgagePaidAction.id == int(action_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if action is None:
            raise MortgageFlowError("paid_action_not_found")
        if action.state != "uncertain":
            raise MortgageFlowError("paid_action_not_uncertain")
        action.state = resolution
        action.reconciled_at = now
        action.result = {
            **(action.result or {}),
            "reconciliation": {"resolution": resolution, "by": reconciled_by, "at": now.isoformat(), "evidence": evidence},
        }
        flow = await session.get(MortgageSearchFlow, action.flow_id)
        if action.step == "richieste_save":
            request_id = str((action.quote or {}).get("portal_request_id") or "")
            request_row = (
                await session.execute(
                    select(MortgageSearchRequest)
                    .where(
                        MortgageSearchRequest.search_id == action.search_id,
                        MortgageSearchRequest.portal_request_id == request_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if request_row is None:
                raise MortgageFlowError("paid_save_reconciliation_request_missing")
            if resolution == "not_executed":
                if request_row.status == "downloaded":
                    raise MortgageFlowError("paid_save_document_already_linked")
                if request_row.status == "uncertain":
                    request_row.status = "available"
            else:
                try:
                    document_id = int(evidence.get("document_id") or 0)
                except (TypeError, ValueError) as exc:
                    raise MortgageFlowError("paid_save_reconciliation_requires_document") from exc
                document = await session.get(Document, document_id)
                if (
                    request_row.status not in {"uncertain", "downloaded"}
                    or request_row.paid_action_id != action.id
                    or document is None
                    or int(document.file_size or 0) <= 0
                    or (request_row.document_id is not None and int(request_row.document_id) != document_id)
                ):
                    raise MortgageFlowError("paid_save_reconciliation_requires_document")
                request_row.status = "downloaded"
                request_row.document_id = document_id
                action.portal_request_ids = [request_id]
                action.result = {
                    **(action.result or {}),
                    "portal_request_id": request_id,
                    "document_id": document_id,
                    "filename": document.file_name,
                }
        if flow:
            flow.state = "running" if resolution == "confirmed" else ("preview" if action.step == "accept" else "accepted")
            if action.step == "accept":
                flow.current_step = "accept" if resolution == "confirmed" else "preview"
            elif action.step in {"notes", "title"}:
                flow.current_step = (
                    "formalita"
                    if resolution == "not_executed" or (action.result or {}).get("page_restored") is True
                    else "richieste_list"
                )
            else:
                flow.current_step = "richieste_list"
            flow.updated_at = now
        await session.commit()
        return {"id": int(action.id), "flow_id": int(action.flow_id), "step": action.step, "state": action.state}


async def flow_status(flow_id: int) -> dict[str, Any]:
    async with _get_session_factory()() as session:
        flow = (
            await session.execute(
                select(MortgageSearchFlow)
                .where(MortgageSearchFlow.id == int(flow_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if flow is None:
            raise MortgageFlowError("flow_not_found")
        now = datetime.now(timezone.utc)
        actions = (
            await session.execute(
                select(MortgagePaidAction)
                .where(
                    MortgagePaidAction.flow_id == flow.id,
                    MortgagePaidAction.state.in_({"intended", "clicked"}),
                )
                .with_for_update()
            )
        ).scalars().all()
        cutoff = now - timedelta(seconds=_PAID_ACTION_RECOVERY_TIMEOUT_SECONDS)
        for action in actions:
            started_at = action.clicked_at or action.confirmed_at
            if started_at <= cutoff:
                action.state = "uncertain"
                action.result = {
                    **(action.result or {}),
                    "recovery": {"reason": "stale_open_action", "detected_at": now.isoformat()},
                }
                flow.state = "uncertain"
                flow.current_step = action.step
                flow.updated_at = now
        if any((action.clicked_at or action.confirmed_at) <= cutoff for action in actions):
            await session.commit()
        request_ref = (
            await session.execute(select(Request.request_ref).where(Request.id == flow.request_id))
        ).scalar_one_or_none()
        actions = (
            await session.execute(
                select(MortgagePaidAction).where(MortgagePaidAction.flow_id == flow.id).order_by(MortgagePaidAction.id)
            )
        ).scalars().all()
        pending = (
            await session.execute(
                select(MortgageSearchRequest).where(MortgageSearchRequest.search_id == flow.search_id)
            )
        ).scalars().all() if flow.search_id is not None else []
        return {
            "id": int(flow.id),
            "request_id": flow.request_id,
            "request_ref": request_ref,
            "search_id": flow.search_id,
            "subject_fiscal_code": flow.subject_fiscal_code,
            "office": flow.office,
            "scope": flow.scope,
            "state": flow.state,
            "current_step": flow.current_step,
            "state_payload": flow.state_payload,
            "actions": [
                {"id": int(action.id), "step": action.step, "state": action.state, "quote": action.quote,
                 "confirmed_by": action.confirmed_by, "confirmed_at": action.confirmed_at.isoformat(),
                 "clicked_at": action.clicked_at.isoformat() if action.clicked_at else None,
                 "result": action.result}
                for action in actions
            ],
            "pending_requests": [
                {"id": int(row.id), "kind": row.kind, "status": row.status, "portal_request_id": row.portal_request_id}
                for row in pending
            ],
        }
