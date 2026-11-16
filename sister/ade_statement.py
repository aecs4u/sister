"""Fail-closed parser for saved AdE *Estratto conto consultazioni* pages."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup


class StatementParseError(ValueError):
    """The saved page does not expose enough labelled evidence to import safely."""


@dataclass(frozen=True)
class StatementRow:
    statement_key: str
    posted_on: date
    office: str | None
    description: str
    credit_amount: Decimal | None
    debit_amount: Decimal | None
    row_data: dict[str, Any]


@dataclass(frozen=True)
class StatementSnapshot:
    period_start: date
    period_end: date
    total_credit: Decimal
    total_debit: Decimal
    closing_balance: Decimal | None
    rows: tuple[StatementRow, ...]


_ITALIAN_DATE = re.compile(r"(?<!\d)(\d{2}/\d{2}/\d{4})(?!\d)")
_RADIO_DATE = re.compile(r"^\d+~(?P<date>\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})~")
_MONEY = re.compile(r"(?<![\w/])(-?\d{1,3}(?:\.\d{3})*(?:,\d{2})|-?\d+(?:,\d{2})?)(?!\w)")


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def _parse_date(raw: str) -> date:
    value = raw.strip()
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise StatementParseError(f"Data non riconosciuta: {value!r}")


def _parse_money(raw: str, *, required: bool = False) -> Decimal | None:
    value = " ".join(raw.replace("\xa0", " ").split())
    if not value or value in {"-", "—", "–"}:
        if required:
            raise StatementParseError("Importo mancante")
        return None
    if "," not in value and re.search(r"\.\d{2}(?:\D|$)", value):
        raise StatementParseError(f"Importo con separatore decimale non italiano: {value!r}")
    found = _MONEY.search(value)
    if found is None:
        raise StatementParseError(f"Importo non riconosciuto: {value!r}")
    try:
        amount = Decimal(found.group(1).replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise StatementParseError(f"Importo non riconosciuto: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise StatementParseError(f"Importo negativo o non finito: {value!r}")
    return amount.quantize(Decimal("0.01"))


def _header_indices(cells: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, raw in enumerate(cells):
        label = _fold(raw)
        if any(token in label for token in ("accredito", "entrata")):
            result["credit"] = index
        elif any(token in label for token in ("addebito", "uscita")):
            result["debit"] = index
        elif any(token in label for token in ("data", "giorno")):
            result["date"] = index
        elif any(token in label for token in ("causale", "descrizione", "operazione")):
            result["description"] = index
        elif any(token in label for token in ("ufficio", "conservatoria", "area")):
            result["office"] = index
    required = {"credit", "debit", "description"}
    if not required.issubset(result):
        return {}
    return result


def _row_date_from_key(key: str) -> date | None:
    match = _RADIO_DATE.match(key)
    if match:
        return _parse_date(match.group("date"))
    return None


def _statement_rows(soup: BeautifulSoup) -> tuple[StatementRow, ...]:
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        header_map: dict[str, int] = {}
        header_position = -1
        for position, tr in enumerate(trs):
            headers = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"], recursive=False)]
            candidate = _header_indices(headers)
            if candidate:
                header_map = candidate
                header_position = position
                break
        if not header_map:
            continue

        parsed: list[StatementRow] = []
        for tr in trs[header_position + 1 :]:
            radio = tr.find("input", attrs={"type": re.compile("^radio$", re.I)})
            if radio is None:
                continue
            key = str(radio.get("value") or "").strip()
            row_match = _RADIO_DATE.match(key)
            if not key or row_match is None:
                raise StatementParseError("La riga movimento non contiene una chiave radio riconoscibile")
            cells = tr.find_all(["th", "td"], recursive=False)
            texts = [cell.get_text(" ", strip=True) for cell in cells]

            def at(field: str) -> str:
                index = header_map.get(field)
                return texts[index] if index is not None and index < len(texts) else ""

            date_text = at("date")
            posted_on = _parse_date(date_text) if date_text else _row_date_from_key(key)
            if posted_on is None:
                raise StatementParseError(f"Data mancante per il movimento {key!r}")
            credit_amount = _parse_money(at("credit"))
            debit_amount = _parse_money(at("debit"))
            if credit_amount == Decimal("0.00") and debit_amount not in (None, Decimal("0.00")):
                credit_amount = None
            if debit_amount == Decimal("0.00") and credit_amount not in (None, Decimal("0.00")):
                debit_amount = None
            if (credit_amount is None) == (debit_amount is None):
                raise StatementParseError(f"Il movimento {key!r} deve avere un solo accredito o addebito")
            description = at("description").strip()
            if not description:
                raise StatementParseError(f"Causale mancante per il movimento {key!r}")
            parsed.append(
                StatementRow(
                    statement_key=key,
                    posted_on=posted_on,
                    office=at("office").strip() or None,
                    description=description,
                    credit_amount=credit_amount,
                    debit_amount=debit_amount,
                    row_data={"cells": texts},
                )
            )
        if parsed:
            return tuple(parsed)
    raise StatementParseError("Tabella movimenti con intestazioni accredito/addebito non trovata")


def _statement_totals(soup: BeautifulSoup) -> tuple[Decimal, Decimal] | None:
    """Read a labelled total row using the same column labels as the movements table."""
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        header_map: dict[str, int] = {}
        header_position = -1
        for position, tr in enumerate(trs):
            headers = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"], recursive=False)]
            candidate = _header_indices(headers)
            if candidate:
                header_map = candidate
                header_position = position
                break
        if not header_map:
            continue

        description_index = header_map.get("description")
        credit_index = header_map.get("credit")
        debit_index = header_map.get("debit")
        totals: list[tuple[Decimal, Decimal]] = []
        for tr in trs[header_position + 1 :]:
            cells = tr.find_all(["th", "td"], recursive=False)
            texts = [cell.get_text(" ", strip=True) for cell in cells]
            if description_index is None or description_index >= len(texts):
                continue
            if _fold(texts[description_index]).strip(" :") not in {"totale", "totali", "totale periodo"}:
                continue
            if credit_index is None or debit_index is None or max(credit_index, debit_index) >= len(texts):
                raise StatementParseError("La riga TOTALE non contiene le colonne accredito e addebito")
            credit = _parse_money(texts[credit_index], required=True)
            debit = _parse_money(texts[debit_index], required=True)
            assert credit is not None and debit is not None
            totals.append((credit, debit))
        if totals:
            if len(totals) != 1:
                raise StatementParseError("Riga TOTALE duplicata nello statement")
            return totals[0]
    return None


def _labelled_money(text: str, labels: tuple[str, ...]) -> Decimal | None:
    folded = _fold(text)
    folded = re.sub(r"\balla\s+data\s+\d{2}/\d{2}/\d{4}\b", " ", folded)
    for label in labels:
        normalized = _fold(label)
        pattern = re.compile(re.escape(normalized) + r"\s*[:\-]?\s*(?:€\s*)?" + _MONEY.pattern, re.I)
        match = pattern.search(folded)
        if match:
            return _parse_money(match.group(1), required=True)
    return None


def _period(text: str) -> tuple[date, date]:
    folded = _fold(text)
    match = re.search(
        r"(?:dal|da)\s+(?:giorno\s+)?(\d{2}/\d{2}/\d{4})"
        r"\s+(?:al|a)\s+(?:giorno\s+)?(\d{2}/\d{2}/\d{4})",
        folded,
        re.I,
    )
    if not match:
        raise StatementParseError("Periodo dello statement non trovato nella pagina")
    start, end = (_parse_date(match.group(1)), _parse_date(match.group(2)))
    if end < start:
        raise StatementParseError("Periodo dello statement invertito")
    return start, end


def parse_statement_html(html: str | bytes) -> StatementSnapshot:
    """Parse a saved statement page by labelled columns and radio keys, never by guessed column positions."""
    if not isinstance(html, (str, bytes)) or not html.strip():
        raise StatementParseError("Pagina dello statement vuota")
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    period_start, period_end = _period(page_text)
    rows = _statement_rows(soup)
    total_credit = _labelled_money(page_text, ("totale accrediti", "totale accredito"))
    total_debit = _labelled_money(page_text, ("totale addebiti", "totale addebito"))
    if total_credit is None and total_debit is None:
        total_row = _statement_totals(soup)
        if total_row is not None:
            total_credit, total_debit = total_row
    if total_credit is None or total_debit is None:
        raise StatementParseError("Totali accrediti/addebiti non trovati nella pagina")
    closing_balance = _labelled_money(
        page_text,
        ("deposito castelletto", "saldo del periodo", "saldo disponibile", "saldo finale", "deposito residuo"),
    )

    if len({row.statement_key for row in rows}) != len(rows):
        raise StatementParseError("Chiave radio duplicata nello statement")
    if any(not period_start <= row.posted_on <= period_end for row in rows):
        raise StatementParseError("Una data movimento non rientra nel periodo dichiarato")
    actual_credit = sum((row.credit_amount or Decimal("0.00") for row in rows), Decimal("0.00"))
    actual_debit = sum((row.debit_amount or Decimal("0.00") for row in rows), Decimal("0.00"))
    if actual_credit != total_credit or actual_debit != total_debit:
        raise StatementParseError(
            f"I movimenti ({actual_credit}/{actual_debit}) non quadrano con i totali dichiarati "
            f"({total_credit}/{total_debit})"
        )
    if closing_balance is not None and closing_balance != total_credit - total_debit:
        raise StatementParseError("Il deposito finale non quadra con accrediti meno addebiti")
    return StatementSnapshot(
        period_start=period_start,
        period_end=period_end,
        total_credit=total_credit,
        total_debit=total_debit,
        closing_balance=closing_balance,
        rows=rows,
    )


def parse_statement_detail_html(html: str | bytes) -> dict[str, Any]:
    """Capture explicit inspection/request/register identifiers from a saved *Dettaglio* page.

    The parser deliberately returns no attribution when the visible detail page contains no stable identifier.
    """
    if not isinstance(html, (str, bytes)) or not html.strip():
        raise StatementParseError("Dettaglio statement vuoto")
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    source_text = " ".join([visible_text, *(str(anchor.get("href") or "") for anchor in soup.find_all("a"))])
    inspection_numbers = sorted(
        {
            f"T {match.group(1)}"
            for match in re.finditer(r"\bT\s*(?:1\s*)?(\d{3,})\b", source_text, flags=re.I)
        }
    )
    portal_request_ids = sorted(
        {
            match.group(1)
            for match in re.finditer(r"\bidRichiesta\s*[=:]\s*([A-Za-z0-9_-]{1,64})\b", source_text, flags=re.I)
        }
    )
    registers = []
    for row in soup.find_all(["tr", "li", "p"]):
        row_text = row.get_text(" ", strip=True)
        folded = _fold(row_text)
        if "registro particolare" not in folded or "registro generale" not in folded:
            continue
        rp = re.search(r"registro\s+particolare\s*(?:n(?:umero)?\.?\s*)?([0-9]+)", folded, re.I)
        rg = re.search(r"registro\s+generale\s*(?:n(?:umero)?\.?\s*)?([0-9]+)", folded, re.I)
        if rp and rg:
            registers.append({"registro_particolare": rp.group(1), "registro_generale": rg.group(1)})
    return {
        "inspection_numbers": inspection_numbers,
        "portal_request_ids": portal_request_ids,
        "registers": registers,
        "identified": bool(inspection_numbers or portal_request_ids or registers),
        "visible_text": visible_text[:20000],
    }
