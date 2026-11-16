"""Measured AdE prices and ownership-aware quotes for paid mortgage-search steps."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

TARIFF_MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class MeasuredRate:
    kind: str
    amount: Decimal
    measured_on: date
    source: str

    def is_stale(self, as_of: date | None = None) -> bool:
        return ((as_of or date.today()) - self.measured_on).days > TARIFF_MAX_AGE_DAYS

    def public_dict(self, as_of: date | None = None) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "unit_amount": str(self.amount),
            "currency": "EUR",
            "measured_on": self.measured_on.isoformat(),
            "source": self.source,
            "stale_after_days": TARIFF_MAX_AGE_DAYS,
            "stale": self.is_stale(as_of),
        }


RATES = {
    "inspection_office": MeasuredRate(
        "inspection", Decimal("6.40"), date(2026, 11, 11), "accepted office cost pages T 91777 and T 79390; 11/11 receipt"
    ),
    "inspection_national": MeasuredRate(
        "inspection", Decimal("19.20"), date(2026, 11, 12), "T 79048 national cost page; not accepted"
    ),
    "note": MeasuredRate(
        "note", Decimal("4.00"), date(2026, 11, 11), "11/11 receipt and 06/11 and 13/11 account-statement rows"
    ),
    "title": MeasuredRate("title", Decimal("8.00"), date(2026, 11, 11), "11/11 receipt and fee notice"),
}


@dataclass(frozen=True)
class QuoteLine:
    kind: str
    ordinals: tuple[int, ...]
    quantity: int
    unit_amount: Decimal
    amount: Decimal

    def public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ordinals": list(self.ordinals),
            "quantity": self.quantity,
            "unit_amount": str(self.unit_amount),
            "amount": str(self.amount),
            "currency": "EUR",
        }


@dataclass(frozen=True)
class PaidQuote:
    search_id: int
    step: str
    selected: tuple[int, ...]
    already_owned: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]
    lines: tuple[QuoteLine, ...]
    new_cost: Decimal | None
    rate: MeasuredRate
    estimate: bool
    quoted_at: datetime

    def public_dict(self, as_of: date | None = None) -> dict[str, Any]:
        return {
            "search_id": self.search_id,
            "step": self.step,
            "selected": list(self.selected),
            "already_owned": [dict(row) for row in self.already_owned],
            "blocked": [dict(row) for row in self.blocked],
            "lines": [line.public_dict() for line in self.lines],
            "new_cost": str(self.new_cost) if self.new_cost is not None else None,
            "currency": "EUR",
            "estimate": self.estimate,
            "source": self.rate.source,
            "measured_on": self.rate.measured_on.isoformat(),
            "stale_after_days": TARIFF_MAX_AGE_DAYS,
            "stale": self.rate.is_stale(as_of),
            "quoted_at": self.quoted_at.isoformat(),
        }


def _office_key(value: Any) -> str:
    return " ".join(str(value or "").upper().split())


async def _quote_search(search_id: int, ordinals: list[int], *, kind: str, fmt: str, as_of: date | None = None) -> PaidQuote:
    from sqlalchemy import select

    from .database import _get_session_factory
    from .db_models import (
        Document,
        DocumentExtraction,
        MortgageInspection,
        MortgageSearch,
        MortgageSearchFormality,
        MortgageSearchRequest,
    )

    today = as_of or date.today()
    selected = tuple(dict.fromkeys(int(value) for value in ordinals))
    rate = RATES[kind]
    async with _get_session_factory()() as session:
        search = await session.get(MortgageSearch, int(search_id))
        if search is None:
            return PaidQuote(int(search_id), kind, selected, (), ({"reason": "search_not_found"},), (), None, rate, True, datetime.now(timezone.utc))
        rows = (
            await session.execute(
                select(MortgageSearchFormality).where(
                    MortgageSearchFormality.search_id == search.id,
                    MortgageSearchFormality.ordinal.in_(selected),
                )
            )
        ).scalars().all() if selected else []
        by_ordinal = {row.ordinal: row for row in rows}
        requests = (
            await session.execute(
                select(MortgageSearchRequest).where(
                    MortgageSearchRequest.search_id == search.id,
                    MortgageSearchRequest.kind == kind,
                    MortgageSearchRequest.formality_id.in_([row.id for row in rows]),
                )
            )
        ).scalars().all() if rows else []
        document_ids = {
            document_id
            for row in rows
            for document_id in (row.note_document_id, row.title_document_id)
            if document_id is not None
        }
        document_ids.update(request.document_id for request in requests if request.document_id is not None)
        document_names = dict(
            (await session.execute(select(Document.id, Document.file_name).where(Document.id.in_(document_ids)))).all()
        ) if document_ids else {}

        related_documents = (
            await session.execute(
                select(
                    MortgageInspection.registro_particolare,
                    MortgageInspection.registro_generale,
                    MortgageInspection.office,
                    MortgageInspection.presentazione_data,
                    Document.id,
                    Document.file_name,
                )
                .join(DocumentExtraction, DocumentExtraction.id == MortgageInspection.extraction_id)
                .join(Document, Document.id == DocumentExtraction.document_id)
                .where(Document.document_type == "ispezione_ipotecaria")
            )
        ).all()

    pending_by_formality: dict[int, list[Any]] = {}
    for request in requests:
        pending_by_formality.setdefault(request.formality_id, []).append(request)
    existing_registers = {}
    for rp, rg, office, presentation_date, document_id, file_name in related_documents:
        register_key = (
            str(rp or "").strip(),
            str(rg or "").strip(),
            _office_key(office),
            presentation_date.year if presentation_date else None,
        )
        existing_registers.setdefault(register_key, {"document_id": document_id, "file_name": file_name})

    already_owned, blocked, payable = [], [], []
    for ordinal in selected:
        formality = by_ordinal.get(ordinal)
        if formality is None:
            blocked.append({"ordinal": ordinal, "reason": "formality_not_found"})
            continue
        if fmt.upper() != "PDF":
            blocked.append({"ordinal": ordinal, "reason": "xml_not_supported"})
            continue
        if kind == "note" and not formality.electronic_note:
            blocked.append({"ordinal": ordinal, "reason": "note_not_available_electronically"})
            continue
        if kind == "title" and not formality.electronic_title:
            blocked.append({"ordinal": ordinal, "reason": "no_electronic_title"})
            continue
        requests_for_row = pending_by_formality.get(formality.id, [])
        owned_request = next(
            (request for request in requests_for_row if request.status in {"requested", "available", "downloaded", "uncertain"}),
            None,
        )
        row_document_id = formality.note_document_id if kind == "note" else formality.title_document_id
        year = formality.formality_date.year if formality.formality_date else None
        register_document = existing_registers.get((
            str(formality.registro_particolare or "").strip(),
            str(formality.registro_generale or "").strip(),
            _office_key(search.office),
            year,
        ))
        if row_document_id is not None or owned_request is not None or (year is not None and register_document):
            already_owned.append(
                {
                    "ordinal": ordinal,
                    "registro_particolare": formality.registro_particolare,
                    "registro_generale": formality.registro_generale,
                    "document_id": row_document_id or (owned_request.document_id if owned_request else None)
                    or (register_document["document_id"] if register_document else None),
                    "file_name": document_names.get(row_document_id)
                    or (document_names.get(owned_request.document_id) if owned_request and owned_request.document_id else None)
                    or (register_document["file_name"] if register_document else None),
                    "status": owned_request.status if owned_request else "archived",
                }
            )
            continue
        payable.append(ordinal)

    if rate.is_stale(today) and payable:
        blocked.extend({"ordinal": ordinal, "reason": "tariff_stale"} for ordinal in payable)
        amount = None
        lines = ()
    else:
        amount = rate.amount * len(payable)
        lines = (QuoteLine(kind, tuple(payable), len(payable), rate.amount, amount),) if payable else ()
    return PaidQuote(
        int(search_id), kind, selected, tuple(already_owned), tuple(blocked), tuple(lines), amount, rate,
        True, datetime.now(timezone.utc)
    )


async def quote_note_request(search_id: int, ordinals: list[int], fmt: str = "PDF", *, as_of: date | None = None) -> PaidQuote:
    return await _quote_search(search_id, ordinals, kind="note", fmt=fmt, as_of=as_of)


async def quote_title_request(search_id: int, ordinal: int, fmt: str = "PDF", *, as_of: date | None = None) -> PaidQuote:
    return await _quote_search(search_id, [ordinal], kind="title", fmt=fmt, as_of=as_of)


async def get_formality_portal_items(search_id: int, ordinals: list[int]) -> list[dict[str, Any]]:
    """Return the stored portal selection identity for payable formalities in a quote.

    The confirmation token binds these values so the browser executor can compare them with the live form before
    selecting anything. Missing selection values fail closed; ordinals alone are not sufficient to identify a portal
    checkbox after the result page has changed.
    """
    from sqlalchemy import select

    from .database import _get_session_factory
    from .db_models import MortgageSearchFormality

    selected = tuple(dict.fromkeys(int(value) for value in ordinals))
    if not selected:
        return []
    async with _get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    MortgageSearchFormality.ordinal,
                    MortgageSearchFormality.portal_selection_value,
                    MortgageSearchFormality.registro_particolare,
                    MortgageSearchFormality.registro_generale,
                    MortgageSearchFormality.formality_date,
                ).where(
                    MortgageSearchFormality.search_id == int(search_id),
                    MortgageSearchFormality.ordinal.in_(selected),
                )
            )
        ).all()
    by_ordinal = {int(row.ordinal): row for row in rows}
    if set(by_ordinal) != set(selected):
        return []
    result = []
    for ordinal in selected:
        row = by_ordinal[ordinal]
        selection_value = str(row.portal_selection_value or "").strip()
        if not selection_value:
            return []
        result.append(
            {
                "ordinal": ordinal,
                "selection_value": selection_value,
                "registro_particolare": str(row.registro_particolare or ""),
                "registro_generale": str(row.registro_generale or ""),
                "formality_date": row.formality_date.isoformat() if row.formality_date else None,
            }
        )
    return result


async def quote_richieste_save(search_id: int, portal_request_id: str, portal_row: dict[str, Any]) -> dict[str, Any]:
    """Quote one exact, currently downloadable mortgage request row before its potentially paid Salva click."""
    import re

    from sqlalchemy import select

    from .database import _get_session_factory
    from .db_models import MortgageSearchFormality, MortgageSearchRequest

    portal_request_id = str(portal_request_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", portal_request_id):
        raise ValueError("invalid_portal_request_id")
    if not isinstance(portal_row, dict) or portal_row.get("id_richiesta") != portal_request_id:
        raise ValueError("portal_request_row_mismatch")
    if not portal_row.get("salva_href") or portal_row.get("status") != "available":
        raise ValueError("portal_request_not_available")

    async with _get_session_factory()() as session:
        row = (
            await session.execute(
                select(MortgageSearchRequest, MortgageSearchFormality)
                .join(MortgageSearchFormality, MortgageSearchFormality.id == MortgageSearchRequest.formality_id)
                .where(
                    MortgageSearchRequest.search_id == int(search_id),
                    MortgageSearchRequest.portal_request_id == portal_request_id,
                    MortgageSearchFormality.search_id == int(search_id),
                )
            )
        ).first()
        if row is None:
            raise ValueError("mortgage_request_not_found")
        request, formality = row
        if request.status not in {"requested", "available"}:
            raise ValueError("mortgage_request_not_ready_for_save")
        if str(request.format or portal_row.get("formato") or "").upper() != "PDF":
            raise ValueError("mortgage_request_format_not_supported")

        cost_text = str(portal_row.get("costo") or "").strip()
        raw = re.sub(r"[^0-9,.-]", "", cost_text)
        portal_amount = None
        if raw:
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
            try:
                portal_amount = Decimal(raw).quantize(Decimal("0.01"))
            except Exception:
                portal_amount = None
        rate = RATES[request.kind]
        if portal_amount == 0:
            raise ValueError("zero_cost_non_receipt_requires_review")
        if portal_amount is not None and portal_amount > 0:
            amount = portal_amount
            source = "richieste_row"
            estimate = False
            measured_on = None
        else:
            if cost_text:
                raise ValueError("richieste_row_cost_unparseable")
            if rate.is_stale():
                raise ValueError("mortgage_request_tariff_stale")
            amount = rate.amount
            source = rate.source
            estimate = True
            measured_on = rate.measured_on.isoformat()
        request.status = "available"
        await session.commit()

    return {
        "step": "richieste_save",
        "search_id": int(search_id),
        "portal_request_id": portal_request_id,
        "kind": request.kind,
        "format": "PDF",
        "selected": [int(formality.ordinal)],
        "already_owned": [],
        "blocked": [],
        "lines": [
            {
                "kind": request.kind,
                "ordinals": [int(formality.ordinal)],
                "quantity": 1,
                "unit_amount": str(amount),
                "amount": str(amount),
                "currency": "EUR",
            }
        ],
        "ordinal": int(formality.ordinal),
        "registro_particolare": str(formality.registro_particolare or ""),
        "registro_generale": str(formality.registro_generale or ""),
        "amount": str(amount),
        "new_cost": str(amount),
        "currency": "EUR",
        "source": source,
        "estimate": estimate,
        "measured_on": measured_on,
        "expected_row_cost": cost_text,
        "expected_object": str(portal_row.get("oggetto") or ""),
        "expected_requested_at": str(portal_row.get("richiesta_del") or ""),
        "quoted_at": datetime.now(timezone.utc).isoformat(),
    }


async def quote_inspection(search_id: int, *, portal_amount: Decimal | None = None, as_of: date | None = None) -> PaidQuote:
    from .database import _get_session_factory
    from .db_models import MortgageSearch

    today = as_of or date.today()
    async with _get_session_factory()() as session:
        search = await session.get(MortgageSearch, int(search_id))
    if search is None:
        rate = RATES["inspection_office"]
        return PaidQuote(int(search_id), "inspection", (), (), ({"reason": "search_not_found"},), (), None, rate, True, datetime.now(timezone.utc))
    rate = RATES["inspection_national" if search.scope == "national" else "inspection_office"]
    if portal_amount is not None:
        amount = Decimal(portal_amount)
        line = QuoteLine("inspection", (), 1, amount, amount)
        return PaidQuote(int(search_id), "inspection", (), (), (), (line,), amount, rate, False, datetime.now(timezone.utc))
    if rate.is_stale(today):
        return PaidQuote(
            int(search_id), "inspection", (), (), ({"reason": "tariff_stale"},), (), None, rate, True,
            datetime.now(timezone.utc)
        )
    return PaidQuote(
        int(search_id), "inspection", (), (), (),
        (QuoteLine("inspection", (), 1, rate.amount, rate.amount),), rate.amount, rate, True, datetime.now(timezone.utc)
    )
