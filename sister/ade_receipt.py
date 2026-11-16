"""Fail-closed parser for text extracted from AdE inspection receipts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class ReceiptParseError(ValueError):
    """The receipt text does not contain a complete, internally consistent breakdown."""


@dataclass(frozen=True)
class ReceiptLine:
    ordinal: int
    element: str
    quantity: int
    unit_amount: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Receipt:
    receipt_date: date
    amount: Decimal
    payment_method: str | None
    lines: tuple[ReceiptLine, ...]
    receipt_data: dict[str, Any]


_MONEY_TOKEN = re.compile(r"(?<![\w/])(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+(?:,\d{2}))(?!\w)")
_DATE = re.compile(r"(?<!\d)(\d{2}/\d{2}/\d{4})(?!\d)")


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def _money(value: str) -> Decimal:
    if "," not in value and re.search(r"\.\d{2}$", value):
        raise ReceiptParseError(f"Importo con separatore decimale non italiano: {value!r}")
    try:
        amount = Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ReceiptParseError(f"Importo non riconosciuto: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ReceiptParseError(f"Importo negativo o non finito: {value!r}")
    return amount.quantize(Decimal("0.01"))


def _date_from_title(text: str) -> date:
    folded = _fold(text)
    match = re.search(r"ricevuta\s+per\s+ispezioni\s+del\s+(\d{2}/\d{2}/\d{4})", folded)
    if not match:
        raise ReceiptParseError("Titolo 'RICEVUTA PER ISPEZIONI DEL ...' non trovato")
    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError as exc:
        raise ReceiptParseError("Data della ricevuta non valida") from exc


def _table_header_index(lines: list[str]) -> tuple[int, bool]:
    for index, line in enumerate(lines):
        labels = [_fold(part) for part in re.split(r"\s{2,}", line.strip()) if part.strip()]
        if any(label == "elementi" or "elementi" in label for label in labels) and any(
            label == "importo" or "importo" in label for label in labels
        ):
            return index, any("quantit" in label for label in labels)
    raise ReceiptParseError("Tabella con intestazioni Elementi e Importo non trovata")


def parse_receipt_text(text: str) -> Receipt:
    """Parse extracted PDF text; unidentified lines or mismatched totals are rejected."""
    if not isinstance(text, str) or not text.strip():
        raise ReceiptParseError("Testo della ricevuta vuoto")
    receipt_date = _date_from_title(text)
    lines_text = [" ".join(line.split()) for line in text.replace("\f", "\n").splitlines() if line.strip()]
    header_index, has_quantity_column = _table_header_index(lines_text)
    payment_method = None
    for line in lines_text:
        folded = _fold(line)
        if any(label in folded for label in ("modalita di pagamento", "mezzo di pagamento", "pagamento effettuato con")):
            payment_method = re.split(r"[:\-]", line, maxsplit=1)[-1].strip() or None
            break

    receipt_lines: list[ReceiptLine] = []
    for raw in lines_text[header_index + 1 :]:
        folded = _fold(raw)
        if any(label in folded for label in ("totale", "quietanza", "importo complessivo")):
            continue
        if "," not in raw and re.search(r"\b\d+\.\d{2}(?:\D|$)", raw):
            raise ReceiptParseError(f"Importo con separatore decimale non italiano: {raw!r}")
        amounts = list(_MONEY_TOKEN.finditer(raw))
        if not amounts:
            continue
        if len(amounts) != 1:
            raise ReceiptParseError(f"Riga di dettaglio ambigua: {raw!r}")
        match = amounts[0]
        element = raw[: match.start()].strip(" :-\t")
        if not element or _fold(element) in {"elementi", "importo"}:
            continue
        amount = _money(match.group(1))
        quantity = 1
        if has_quantity_column:
            quantity_match = re.search(r"(?:^|\s)(\d+)\s+(?:x\s+)?", element, re.I)
            if quantity_match:
                quantity = int(quantity_match.group(1))
                element = (element[: quantity_match.start()] + element[quantity_match.end() :]).strip()
        if quantity <= 0:
            raise ReceiptParseError(f"Quantità non valida nella riga {raw!r}")
        receipt_lines.append(
            ReceiptLine(
                ordinal=len(receipt_lines) + 1,
                element=element,
                quantity=quantity,
                unit_amount=(amount / quantity).quantize(Decimal("0.01")),
                amount=amount,
            )
        )
    if not receipt_lines:
        raise ReceiptParseError("Nessuna riga Elementi/Importo riconosciuta")

    total = sum((line.amount for line in receipt_lines), Decimal("0.00"))
    explicit_totals: list[Decimal] = []
    for line in lines_text:
        folded = _fold(line)
        if any(label in folded for label in ("totale", "quietanza", "importo complessivo")):
            amounts = list(_MONEY_TOKEN.finditer(line))
            if amounts:
                explicit_totals.append(_money(amounts[-1].group(1)))
    if explicit_totals and any(value != total for value in explicit_totals):
        raise ReceiptParseError("Il totale della quietanza non quadra con le righe Elementi/Importo")
    inspection_numbers = sorted(
        {
            f"T {match.group(1)}"
            for match in re.finditer(r"\bT\s*(?:1\s*)?(\d{3,})\b", text, flags=re.I)
        }
    )
    portal_request_ids = sorted(
        {
            match.group(1)
            for match in re.finditer(r"\bidRichiesta\s*[=:]\s*([A-Za-z0-9_-]{1,64})\b", text, flags=re.I)
        }
    )
    return Receipt(
        receipt_date=receipt_date,
        amount=total,
        payment_method=payment_method,
        lines=tuple(receipt_lines),
        receipt_data={
            "text_lines": lines_text,
            "inspection_numbers": inspection_numbers,
            "portal_request_ids": portal_request_ids,
        },
    )
