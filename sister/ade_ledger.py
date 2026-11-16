"""Separate, evidence-backed AdE account ledger.

This module never writes to SISTER's billing or document-credit wallets. The statement is the source of posted movements;
receipts and pagoPA imports are evidence linked to those movements.
"""

from __future__ import annotations

import hashlib
import subprocess
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select

from .ade_receipt import parse_receipt_text
from .ade_statement import StatementSnapshot, parse_statement_detail_html, parse_statement_html


class AdeLedgerError(ValueError):
    """An AdE import or reconciliation violates the evidence rules."""


def _receipt_charge_kind(element: str) -> str | None:
    """Map known receipt labels to typed charge kinds; unknown labels stay unclassified."""
    normalized = unicodedata.normalize("NFKD", str(element or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.replace("/", " ").replace("(", " ").replace(")", " ").split())
    normalized = normalized.removesuffix(" sicilia").strip()
    known = {
        "ispezione nominativo": "inspection",
        "ispezione nominativi": "inspection",
        "ispezione nominativo e immobile": "inspection",
        "ispezione nominativi e immobili": "inspection",
        "per ogni nominativo e o immobile richiesto": "inspection",
        "note visionate": "note",
        "per ogni nota visionata": "note",
        "titoli visionati": "title",
        "per ogni titolo visionato": "title",
    }
    return known.get(normalized)


def _row_amounts(row) -> tuple[str, str, str, str, str]:
    return (
        row.posted_on.isoformat(),
        row.office or "",
        row.description,
        str(row.credit_amount) if row.credit_amount is not None else "",
        str(row.debit_amount) if row.debit_amount is not None else "",
    )


async def import_statement_snapshot(
    snapshot: StatementSnapshot,
    *,
    source_filename: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Idempotently persist a parsed statement and rebuild the dedicated ledger."""
    from .database import _get_session_factory
    from .db_models import AdeStatementImport, AdeStatementRow

    if len(source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_sha256.lower()):
        raise AdeLedgerError("source_sha256_invalid")
    source_sha256 = source_sha256.lower()
    async with _get_session_factory()() as session:
        duplicate = (
            await session.execute(select(AdeStatementImport).where(AdeStatementImport.source_sha256 == source_sha256))
        ).scalar_one_or_none()
        if duplicate:
            import_id = duplicate.id
            already_imported = True
            rows_added = 0
        else:
            imported = AdeStatementImport(
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
                total_credit=snapshot.total_credit,
                total_debit=snapshot.total_debit,
                closing_balance=snapshot.closing_balance,
                source_filename=source_filename,
                source_sha256=source_sha256,
                summary={"row_count": len(snapshot.rows)},
            )
            session.add(imported)
            await session.flush()
            rows_added = 0
            for row in snapshot.rows:
                existing = await session.get(AdeStatementRow, row.statement_key)
                expected = (
                    row.posted_on.isoformat(),
                    row.office or "",
                    row.description,
                    str(row.credit_amount) if row.credit_amount is not None else "",
                    str(row.debit_amount) if row.debit_amount is not None else "",
                )
                if existing is not None:
                    if _row_amounts(existing) != expected:
                        raise AdeLedgerError(f"statement_row_conflict:{row.statement_key}")
                    continue
                session.add(
                    AdeStatementRow(
                        statement_key=row.statement_key,
                        import_id=imported.id,
                        posted_on=row.posted_on,
                        office=row.office,
                        description=row.description,
                        credit_amount=row.credit_amount,
                        debit_amount=row.debit_amount,
                        row_data=row.row_data,
                    )
                )
                rows_added += 1
            await session.commit()
            import_id = imported.id
            already_imported = False
    balance = await rebuild_ledger()
    return {
        "import_id": import_id,
        "duplicate": already_imported,
        "rows_added": rows_added,
        "row_count": len(snapshot.rows),
        "balance": balance,
    }


async def import_statement_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    raw = source.read_bytes()
    snapshot = parse_statement_html(raw)
    return await import_statement_snapshot(
        snapshot,
        source_filename=source.name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


async def import_statement_detail_file(statement_key: str, path: str | Path) -> dict[str, Any]:
    from .database import _get_session_factory
    from .db_models import AdeStatementRow

    source = Path(path)
    raw = source.read_bytes()
    detail = parse_statement_detail_html(raw)
    detail.update({"source_filename": source.name, "source_sha256": hashlib.sha256(raw).hexdigest()})
    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                select(AdeStatementRow).where(AdeStatementRow.statement_key == statement_key).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise AdeLedgerError("statement_row_not_found")
        row.detail_data = detail
        row.detail_checked_at = datetime.now(timezone.utc)
        await session.commit()
    return {"statement_key": statement_key, "identified": detail["identified"], "identifiers": {
        "inspections": detail["inspection_numbers"],
        "requests": detail["portal_request_ids"],
        "registers": detail["registers"],
    }}


async def import_receipt_text(
    text: str,
    *,
    source_filename: str,
    source_sha256: str,
) -> dict[str, Any]:
    from .database import _get_session_factory
    from .db_models import AdeReceipt, AdeReceiptLine

    receipt = parse_receipt_text(text)
    if len(source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_sha256.lower()):
        raise AdeLedgerError("source_sha256_invalid")
    async with _get_session_factory()() as session:
        duplicate = (
            await session.execute(select(AdeReceipt).where(AdeReceipt.source_sha256 == source_sha256))
        ).scalar_one_or_none()
        if duplicate:
            return {"receipt_id": duplicate.id, "duplicate": True, "amount": str(duplicate.amount)}
        existing_key = (
            await session.execute(select(AdeReceipt).where(AdeReceipt.receipt_key == source_sha256.lower()))
        ).scalar_one_or_none()
        if existing_key:
            raise AdeLedgerError("receipt_key_conflict")
        row = AdeReceipt(
            receipt_key=source_sha256.lower(),
            receipt_date=receipt.receipt_date,
            amount=receipt.amount,
            payment_method=receipt.payment_method,
            source_filename=source_filename,
            source_sha256=source_sha256.lower(),
            receipt_data=receipt.receipt_data,
        )
        session.add(row)
        await session.flush()
        for line in receipt.lines:
            session.add(
                AdeReceiptLine(
                    receipt_id=row.id,
                    ordinal=line.ordinal,
                    element=line.element,
                    quantity=line.quantity,
                    unit_amount=line.unit_amount,
                    amount=line.amount,
                )
            )
        await session.commit()
        return {"receipt_id": row.id, "duplicate": False, "amount": str(row.amount), "line_count": len(receipt.lines)}


def extract_pdf_text(path: str | Path) -> str:
    source = Path(path)
    if source.suffix.casefold() in {".txt", ".text"}:
        return source.read_text(encoding="utf-8-sig")
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(source), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdeLedgerError("pdftotext_unavailable_or_timed_out") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise AdeLedgerError("receipt_pdf_text_extraction_failed")
    return result.stdout


async def import_receipt_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    raw = source.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    return await import_receipt_text(
        extract_pdf_text(source),
        source_filename=source.name,
        source_sha256=source_sha256,
    )


async def _ledger_balance(session) -> Decimal:
    from .db_models import AdeLedgerEntry

    last = (
        await session.execute(
            select(AdeLedgerEntry.balance_after)
            .order_by(AdeLedgerEntry.posted_on.desc(), AdeLedgerEntry.sequence_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return Decimal(str(last if last is not None else "0.00"))


async def rebuild_ledger() -> dict[str, Any]:
    """Recompute one dedicated movement per statement row; daily credits precede charges."""
    from .database import _get_session_factory
    from .db_models import AdeLedgerEntry, AdeStatementRow

    async with _get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AdeStatementRow).order_by(
                        AdeStatementRow.posted_on,
                        AdeStatementRow.credit_amount.is_(None),
                        AdeStatementRow.statement_key,
                    )
                )
            ).scalars()
        )
        entries = {
            entry.statement_key: entry
            for entry in (await session.execute(select(AdeLedgerEntry))).scalars()
        }
        balance = Decimal("0.00")
        total_credit = Decimal("0.00")
        total_debit = Decimal("0.00")
        topup_count = 0
        usage_count = 0
        for sequence_no, row in enumerate(rows, start=1):
            if row.credit_amount is not None:
                amount = Decimal(row.credit_amount)
                entry_type = "topup"
                balance += amount
                total_credit += amount
                topup_count += 1
            else:
                amount = Decimal(row.debit_amount)
                entry_type = "usage"
                balance -= amount
                total_debit += amount
                usage_count += 1
            entry = entries.get(row.statement_key)
            if entry is None:
                entry = AdeLedgerEntry(
                    statement_key=row.statement_key,
                    posted_on=row.posted_on,
                    sequence_no=sequence_no,
                    entry_type=entry_type,
                    amount=amount,
                    balance_after=balance,
                )
                session.add(entry)
            else:
                desired = (row.posted_on, sequence_no, entry_type, amount, balance)
                current = (entry.posted_on, entry.sequence_no, entry.entry_type, entry.amount, entry.balance_after)
                if current != desired:
                    entry.posted_on, entry.sequence_no, entry.entry_type, entry.amount, entry.balance_after = desired
                    entry.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return {
            "balance": str(balance),
            "total_credit": str(total_credit),
            "total_debit": str(total_debit),
            "topup_count": topup_count,
            "usage_count": usage_count,
            "movement_count": len(rows),
        }


async def link_validated_pagopa(statement_key: str, payment_id: int, *, linked_by: str) -> None:
    """Link a top-up only to one validated pagoPA row with the same amount and a matching payment date."""
    from .database import _get_session_factory
    from .db_models import AdeLedgerEntry, PagoPAPaymentHistory

    actor = str(linked_by or "").strip()
    if not actor:
        raise AdeLedgerError("link_actor_required")
    async with _get_session_factory()() as session:
        entry = (
            await session.execute(
                select(AdeLedgerEntry).where(AdeLedgerEntry.statement_key == statement_key).with_for_update()
            )
        ).scalar_one_or_none()
        payment = await session.get(PagoPAPaymentHistory, int(payment_id))
        if entry is None or entry.entry_type != "topup":
            raise AdeLedgerError("topup_not_found")
        if payment is None or payment.payment_status != "validated" or payment.currency != "EUR":
            raise AdeLedgerError("pagopa_payment_not_validated")
        if Decimal(payment.amount) != Decimal(entry.amount):
            raise AdeLedgerError("pagopa_amount_mismatch")
        payment_dates = {payment.payment_date, payment.operation_date, payment.validation_date}
        if entry.posted_on not in payment_dates:
            raise AdeLedgerError("pagopa_date_mismatch")
        other_link = (
            await session.execute(
                select(AdeLedgerEntry).where(
                    AdeLedgerEntry.pagopa_payment_id == payment.id,
                    AdeLedgerEntry.statement_key != statement_key,
                )
            )
        ).scalar_one_or_none()
        if other_link:
            raise AdeLedgerError("pagopa_payment_already_linked")
        entry.pagopa_payment_id = payment.id
        entry.linked_by = actor
        entry.linked_at = datetime.now(timezone.utc)
        entry.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def allocate_statement_charge(
    statement_key: str,
    amount: Decimal,
    *,
    evidence: str,
    evidence_ref: str,
    created_by: str,
    search_id: int | None = None,
    receipt_line_id: int | None = None,
) -> dict[str, Any]:
    """Allocate a posted debit only with explicit evidence; otherwise keep it unattributed."""
    from .database import _get_session_factory
    from .db_models import (
        AdeChargeAllocation,
        AdeReceipt,
        AdeReceiptLine,
        AdeStatementRow,
        MortgageSearch,
        MortgageSearchCharge,
        MortgageSearchFormality,
        MortgageSearchRequest,
    )

    try:
        value = Decimal(amount).quantize(Decimal("0.01"))
    except Exception as exc:  # noqa: BLE001 - normalize bad interactive input to a domain error
        raise AdeLedgerError("allocation_amount_invalid") from exc
    evidence = str(evidence or "").strip()
    evidence_ref = str(evidence_ref or "").strip()
    actor = str(created_by or "").strip()
    if value <= 0 or not evidence_ref or len(evidence_ref) > 256 or not actor:
        raise AdeLedgerError("allocation_evidence_required")
    if evidence not in {"statement_detail", "receipt", "single_inspection_amount", "unattributed"}:
        raise AdeLedgerError("allocation_evidence_invalid")
    if (evidence == "unattributed") != (search_id is None):
        raise AdeLedgerError("allocation_search_mismatch")
    if (evidence == "receipt") != (receipt_line_id is not None):
        raise AdeLedgerError("allocation_receipt_line_required")

    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                select(AdeStatementRow).where(AdeStatementRow.statement_key == statement_key).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.debit_amount is None:
            raise AdeLedgerError("statement_debit_not_found")
        same_ref = (
            await session.execute(
                select(AdeChargeAllocation).where(
                    AdeChargeAllocation.statement_key == statement_key,
                    AdeChargeAllocation.evidence_ref == evidence_ref,
                )
            )
        ).scalar_one_or_none()
        if same_ref:
            if (
                Decimal(same_ref.amount) == value
                and same_ref.search_id == search_id
                and same_ref.receipt_line_id == receipt_line_id
                and same_ref.evidence == evidence
            ):
                charge_id = (
                    await session.execute(
                        select(MortgageSearchCharge.id).where(MortgageSearchCharge.allocation_id == same_ref.id)
                    )
                ).scalar_one_or_none()
                return {
                    "allocation_id": same_ref.id,
                    "charge_id": charge_id,
                    "charge_projected": charge_id is not None,
                    "duplicate": True,
                    "allocated": str(value),
                }
            raise AdeLedgerError("allocation_evidence_conflict")

        already_allocated = (
            await session.execute(
                select(func.coalesce(func.sum(AdeChargeAllocation.amount), Decimal("0.00"))).where(
                    AdeChargeAllocation.statement_key == statement_key
                )
            )
        ).scalar_one()
        if Decimal(already_allocated) + value > Decimal(row.debit_amount):
            raise AdeLedgerError("allocation_exceeds_statement_debit")

        if evidence == "receipt":
            receipt_line = (
                await session.execute(
                    select(AdeReceiptLine).where(AdeReceiptLine.id == receipt_line_id).with_for_update()
                )
            ).scalar_one_or_none()
            if receipt_line is None or Decimal(receipt_line.amount) != value:
                raise AdeLedgerError("receipt_amount_mismatch")
            existing_line_allocation = (
                await session.execute(
                    select(AdeChargeAllocation.id).where(AdeChargeAllocation.receipt_line_id == receipt_line_id)
                )
            ).scalar_one_or_none()
            if existing_line_allocation is not None:
                raise AdeLedgerError("receipt_line_already_allocated")
            receipt = await session.get(AdeReceipt, receipt_line.receipt_id)
            if receipt is None or receipt.receipt_date != row.posted_on:
                raise AdeLedgerError("receipt_date_mismatch")
            identifiers = receipt.receipt_data or {}
            search = await session.get(MortgageSearch, search_id)
            if search is None:
                raise AdeLedgerError("mortgage_search_not_found")
            matched = search.inspection_number in (identifiers.get("inspection_numbers") or [])
            request_ids = identifiers.get("portal_request_ids") or []
            if not matched and request_ids:
                matched_request = (
                    await session.execute(
                        select(MortgageSearchRequest.id).where(
                            MortgageSearchRequest.search_id == search_id,
                            MortgageSearchRequest.portal_request_id.in_(request_ids),
                        )
                    )
                ).scalar_one_or_none()
                matched = matched_request is not None
            if not matched:
                raise AdeLedgerError("receipt_search_mismatch")
        elif evidence == "single_inspection_amount":
            searches = list(
                (
                    await session.execute(
                        select(MortgageSearch).where(MortgageSearch.inspection_date == row.posted_on)
                    )
                ).scalars()
            )
            if len(searches) != 1 or searches[0].id != search_id:
                raise AdeLedgerError("not_a_single_inspection_day")
            charge = (
                await session.execute(
                    select(MortgageSearchCharge).where(
                        MortgageSearchCharge.search_id == search_id,
                        MortgageSearchCharge.amount == value,
                        MortgageSearchCharge.basis.in_(("cost_page", "receipt", "statement_detail")),
                    )
                )
            ).scalar_one_or_none()
            if charge is None:
                raise AdeLedgerError("inspection_charge_evidence_missing")
        elif evidence == "statement_detail":
            search = await session.get(MortgageSearch, search_id)
            if search is None:
                raise AdeLedgerError("mortgage_search_not_found")
            detail = row.detail_data or {}
            matched = search.inspection_number in (detail.get("inspection_numbers") or [])
            if not matched:
                request_ids = detail.get("portal_request_ids") or []
                if request_ids:
                    matched_request = (
                        await session.execute(
                            select(MortgageSearchRequest.id).where(
                                MortgageSearchRequest.search_id == search_id,
                                MortgageSearchRequest.portal_request_id.in_(request_ids),
                            )
                        )
                    ).scalar_one_or_none()
                    matched = matched_request is not None
            if not matched:
                for register in detail.get("registers") or []:
                    formality = (
                        await session.execute(
                            select(MortgageSearchFormality.id).where(
                                MortgageSearchFormality.search_id == search_id,
                                MortgageSearchFormality.registro_particolare == str(register.get("registro_particolare") or ""),
                                MortgageSearchFormality.registro_generale == str(register.get("registro_generale") or ""),
                            )
                        )
                    ).scalar_one_or_none()
                    if formality is not None:
                        matched = True
                        break
            if not matched:
                raise AdeLedgerError("statement_detail_does_not_identify_search")
        elif search_id is not None:
            search = await session.get(MortgageSearch, search_id)
            if search is None:
                raise AdeLedgerError("mortgage_search_not_found")

        allocation = AdeChargeAllocation(
            statement_key=statement_key,
            search_id=search_id,
            receipt_line_id=receipt_line_id,
            amount=value,
            evidence=evidence,
            evidence_ref=evidence_ref,
            created_by=actor,
        )
        session.add(allocation)
        await session.flush()
        charge_id = None
        projection_reason = None
        if evidence == "receipt":
            charge_kind = _receipt_charge_kind(receipt_line.element)
            if charge_kind is None:
                projection_reason = "receipt_line_kind_unrecognized"
            else:
                charge = MortgageSearchCharge(
                    search_id=int(search_id),
                    kind=charge_kind,
                    quantity=int(receipt_line.quantity),
                    unit_amount=Decimal(receipt_line.unit_amount),
                    amount=value,
                    currency="EUR",
                    charged_at=None,
                    basis="receipt",
                    receipt_id=receipt_line.receipt_id,
                    allocation_id=int(allocation.id),
                )
                session.add(charge)
                await session.flush()
                charge_id = charge.id
        await session.commit()
        return {
            "allocation_id": allocation.id,
            "charge_id": charge_id,
            "charge_projected": charge_id is not None,
            "projection_reason": projection_reason,
            "duplicate": False,
            "allocated": str(value),
        }


async def get_ade_ledger_admin_data(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    from .database import _get_session_factory
    from .db_models import AdeChargeAllocation, AdeLedgerEntry, AdeReceipt, AdeReceiptLine, AdeStatementImport, AdeStatementRow

    page_size = max(1, min(int(limit), 500))
    start = max(0, int(offset))
    async with _get_session_factory()() as session:
        entries = list(
            (
                await session.execute(
                    select(AdeLedgerEntry)
                    .order_by(AdeLedgerEntry.posted_on.desc(), AdeLedgerEntry.sequence_no.desc())
                    .offset(start)
                    .limit(page_size)
                )
            ).scalars()
        )
        imports = list(
            (
                await session.execute(select(AdeStatementImport).order_by(AdeStatementImport.imported_at.desc()).limit(10))
            ).scalars()
        )
        receipts = list(
            (
                await session.execute(select(AdeReceipt).order_by(AdeReceipt.receipt_date.desc(), AdeReceipt.id.desc()).limit(20))
            ).scalars()
        )
        receipt_lines = (
            await session.execute(
                select(AdeReceiptLine, AdeReceipt)
                .join(AdeReceipt, AdeReceipt.id == AdeReceiptLine.receipt_id)
                .order_by(AdeReceipt.receipt_date.desc(), AdeReceiptLine.ordinal)
                .limit(200)
            )
        ).all()
        statement_rows = {
            row.statement_key: row
            for row in (
                await session.execute(
                    select(AdeStatementRow).where(AdeStatementRow.statement_key.in_([entry.statement_key for entry in entries]))
                )
            ).scalars()
        } if entries else {}
        allocations_by_key: dict[str, list[AdeChargeAllocation]] = {}
        for allocation in (
            await session.execute(
                select(AdeChargeAllocation).where(
                    AdeChargeAllocation.statement_key.in_([entry.statement_key for entry in entries])
                )
            )
        ).scalars() if entries else []:
            allocations_by_key.setdefault(allocation.statement_key, []).append(allocation)
        total_credit = Decimal("0.00")
        total_debit = Decimal("0.00")
        movement_rows = (
            await session.execute(select(AdeLedgerEntry.entry_type, func.sum(AdeLedgerEntry.amount)).group_by(AdeLedgerEntry.entry_type))
        ).all()
        for entry_type, amount in movement_rows:
            if entry_type == "topup":
                total_credit = Decimal(amount or 0)
            elif entry_type == "usage":
                total_debit = Decimal(amount or 0)
        balance = await _ledger_balance(session)
        entry_data = []
        for row in entries:
            source = statement_rows.get(row.statement_key)
            allocations = allocations_by_key.get(row.statement_key, [])
            allocated = sum((Decimal(item.amount) for item in allocations), Decimal("0.00"))
            if row.entry_type == "usage" and source is not None:
                unattributed = max(Decimal(source.debit_amount) - allocated, Decimal("0.00"))
                unattributed += sum(
                    (Decimal(item.amount) for item in allocations if item.search_id is None), Decimal("0.00")
                )
            else:
                unattributed = Decimal("0.00")
            entry_data.append(
                {
                    "statement_key": row.statement_key,
                    "posted_on": row.posted_on,
                    "entry_type": row.entry_type,
                    "amount": row.amount,
                    "balance_after": row.balance_after,
                    "pagopa_payment_id": row.pagopa_payment_id,
                    "linked_by": row.linked_by,
                    "description": source.description if source else "",
                    "office": source.office if source else "",
                    "unattributed_amount": unattributed,
                    "allocations": [
                        {
                            "amount": item.amount,
                            "search_id": item.search_id,
                            "evidence": item.evidence,
                            "evidence_ref": item.evidence_ref,
                        }
                        for item in allocations
                    ],
                }
            )
        allocation_totals = (
            select(
                AdeChargeAllocation.statement_key.label("statement_key"),
                func.sum(AdeChargeAllocation.amount).label("allocated"),
                func.sum(
                    case(
                        (AdeChargeAllocation.search_id.is_(None), AdeChargeAllocation.amount),
                        else_=Decimal("0.00"),
                    )
                ).label("unlinked"),
            )
            .group_by(AdeChargeAllocation.statement_key)
            .subquery()
        )
        unattributed_query = await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        AdeStatementRow.debit_amount
                        - func.coalesce(allocation_totals.c.allocated, Decimal("0.00"))
                        + func.coalesce(allocation_totals.c.unlinked, Decimal("0.00"))
                    ),
                    Decimal("0.00"),
                )
            )
            .select_from(AdeStatementRow)
            .outerjoin(allocation_totals, allocation_totals.c.statement_key == AdeStatementRow.statement_key)
            .where(AdeStatementRow.debit_amount.is_not(None))
        )
        unattributed_total = Decimal(unattributed_query.scalar_one() or 0)
        latest_import = imports[0] if imports else None
        expected_balance = Decimal(latest_import.closing_balance) if latest_import and latest_import.closing_balance is not None else None
        return {
            "entries": entry_data,
            "receipts": [
                {"id": row.id, "receipt_date": row.receipt_date, "amount": row.amount, "source_filename": row.source_filename}
                for row in receipts
            ],
            "receipt_lines": [
                {
                    "id": line.id,
                    "receipt_id": receipt.id,
                    "receipt_date": receipt.receipt_date,
                    "element": line.element,
                    "quantity": line.quantity,
                    "amount": line.amount,
                }
                for line, receipt in receipt_lines
            ],
            "imports": [
                {
                    "id": row.id,
                    "period_start": row.period_start,
                    "period_end": row.period_end,
                    "total_credit": row.total_credit,
                    "total_debit": row.total_debit,
                    "closing_balance": row.closing_balance,
                    "source_filename": row.source_filename,
                    "imported_at": row.imported_at,
                }
                for row in imports
            ],
            "total_credit": total_credit,
            "total_debit": total_debit,
            "balance": balance,
            "unattributed_total": unattributed_total,
            "statement_balance": expected_balance,
            "balance_difference": (balance - expected_balance) if expected_balance is not None else None,
        }
