"""Markdown serialisation of the batch submission file (``docs/batch_markdown_format.md``).

One reader for every ``inputs/*.md`` list: the marked query tables (``<!-- sister-batch-table -->``, the older
``sister-batch-table:v1`` included) and the legacy codici lists (``| # | `CODE` | ... |``). Both end as the same
:class:`~sister.batch.BatchFile` that the JSON and CSV files give, so validation, journal and resume are shared.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .batch import BatchFile, BatchQuery
from .fiscal_code import valid_codice_fiscale, valid_partita_iva
from .query_forms import get_query_form, param_names, validate_params

TABLE_MARKER = re.compile(r"<!--\s*sister-batch-table(?::v\d+)?\s*(\{.*\})?\s*-->")
DOCUMENT_MARKER = re.compile(r"<!--\s*sister-batch\s+(\{.*\})\s*-->")
LEGACY_ROW = re.compile(r"^\| \d+ \| `([A-Z0-9]+)` \|")
QUERY_FIELDS = ("id", "enabled", "reuse", "retries")
DOCUMENT_KEYS = {"name", "description", "defaults", "options"}
EMPTY = {"", "—", "-"}


@dataclass
class ParsedBatch:
    """A batch plus where each of its queries sits in the Markdown (``lines[i]`` = 1-based line of ``queries[i]``)."""

    batch: BatchFile
    lines: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class PartiallyParsedBatch:
    """Valid rows from a Markdown batch plus row-level parse errors."""

    batch: BatchFile | None
    lines: list[int] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)


def is_table_markdown(text: str) -> bool:
    return TABLE_MARKER.search(text) is not None


def cells(line: str) -> list[str]:
    """Split a table row; pipes inside a value are written ``&#124;`` and line breaks ``<br>``."""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [html.unescape(cell.strip().replace("<br>", "\n")) for cell in stripped[1:-1].split("|")]


def _is_separator(row: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)


def _value(cell: str) -> str:
    cell = cell.strip()
    if len(cell) > 1 and cell.startswith("`") and cell.endswith("`"):
        cell = cell[1:-1].strip()
    return "" if cell in EMPTY else cell


def _json(raw: str | None, where: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{where}: JSON non valido nel commento ({exc})") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{where}: il commento deve contenere un oggetto JSON")
    return value


def _flag(value: str, where: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "si", "sì", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise ValueError(f"{where}: 'enabled' deve essere true o false, non {value!r}")


def _tags(metadata: dict[str, Any]) -> None:
    raw = metadata.pop("tags", None)
    if raw is None:
        return
    items = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
    tags = [str(tag).strip().upper() for tag in items if str(tag).strip()]
    if tags:
        metadata["tags"] = list(dict.fromkeys(tags))


def parse_markdown(text: str) -> ParsedBatch:
    """Read a Markdown batch file. Raises ``ValueError`` with the line number of the first bad row."""
    parsed = _parse_markdown(text, tolerate_row_errors=False)
    assert isinstance(parsed, ParsedBatch)
    return parsed


def parse_markdown_partial(text: str) -> PartiallyParsedBatch:
    """Read all parseable rows and collect row errors instead of stopping at the first one."""
    parsed = _parse_markdown(text, tolerate_row_errors=True)
    assert isinstance(parsed, PartiallyParsedBatch)
    return parsed


def _parse_markdown(text: str, *, tolerate_row_errors: bool) -> ParsedBatch | PartiallyParsedBatch:
    lines = text.splitlines()
    document: dict[str, Any] = {}
    title: str | None = None
    intro: list[str] = []
    queries: list[BatchQuery] = []
    where: list[int] = []
    table: dict[str, Any] | None = None  # the marker waiting for / reading its table
    headers: list[str] | None = None
    seen_table = False
    intro_done = False
    issues: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        marker = TABLE_MARKER.search(line)
        if marker:
            table, headers, seen_table = _json(marker.group(1), f"riga {number}"), None, True
            continue
        doc_marker = DOCUMENT_MARKER.search(line)
        if doc_marker:
            document = _json(doc_marker.group(1), f"riga {number}")
            unknown = set(document) - DOCUMENT_KEYS
            if unknown:
                raise ValueError(f"riga {number}: chiavi non ammesse in sister-batch: {sorted(unknown)}")
            continue
        row = cells(line)
        if table is not None and row:
            if headers is None:
                headers = [_value(cell).lower().replace("-", "_").replace(" ", "_") for cell in row]
                continue
            if _is_separator(row):
                continue
            if len(row) != len(headers):
                message = f"{len(row)} celle, attese {len(headers)}"
                if not tolerate_row_errors:
                    raise ValueError(f"riga {number}: {message}")
                issues.append({"line": number, "id": None, "level": "error", "message": message})
                continue
            try:
                query = _row_query(dict(zip(headers, row)), table, document, number)
            except ValueError as exc:
                if not tolerate_row_errors:
                    raise
                message = str(exc).removeprefix(f"riga {number}: ")
                issues.append({"line": number, "id": None, "level": "error", "message": message[:300]})
                continue
            queries.append(query)
            where.append(number)
            continue
        if table is not None and headers is not None:
            table, headers = None, None  # the table ended
        if not seen_table:
            if title is None and line.startswith("# "):
                title = line[2:].strip()
            elif title is not None and line.strip() and not line.startswith(("#", ">")) and not intro_done:
                intro.append(line.strip())
            elif intro:
                intro_done = True  # the description is the first paragraph after the title
    if not queries and not (tolerate_row_errors and issues):
        raise ValueError("nessuna riga di query: manca una tabella dopo <!-- sister-batch-table -->")
    if tolerate_row_errors:
        # BatchFile enforces unique explicit IDs. In tolerant mode, report later occurrences as row errors
        # so they do not prevent the other valid rows from contributing to the summary.
        unique_queries: list[BatchQuery] = []
        unique_lines: list[int] = []
        seen_ids: set[str] = set()
        for query, number in zip(queries, where):
            if query.id and query.id in seen_ids:
                issues.append(
                    {
                        "line": number,
                        "id": query.id,
                        "level": "error",
                        "message": f"id duplicato: {query.id}",
                    }
                )
                continue
            if query.id:
                seen_ids.add(query.id)
            unique_queries.append(query)
            unique_lines.append(number)
        queries, where = unique_queries, unique_lines
        if not queries:
            return PartiallyParsedBatch(None, [], issues)
    payload: dict[str, Any] = {k: v for k, v in document.items() if k in DOCUMENT_KEYS}
    payload.setdefault("name", title[:200] if title else None)
    payload.setdefault("description", " ".join(intro)[:1000] or None)
    payload["queries"] = queries
    batch = BatchFile.model_validate({k: v for k, v in payload.items() if v is not None})
    if tolerate_row_errors:
        return PartiallyParsedBatch(batch, where, issues)
    return ParsedBatch(batch, where)


def _row_query(row: dict[str, str], table: dict[str, Any], document: dict[str, Any], number: int) -> BatchQuery:
    default_command = table.get("command") or (document.get("defaults") or {}).get("command")
    command = _value(row.pop("command", "")) or default_command
    spec = get_query_form(command) if command else None
    if spec is None:
        raise ValueError(f"riga {number}: comando mancante o sconosciuto: {command!r}")
    allowed = set(param_names(spec.command))
    fields = {key: _value(row.pop(key)) for key in QUERY_FIELDS if key in row}
    params = dict(table.get("params") or {})
    metadata = dict(table.get("metadata") or {})
    for key, cell in row.items():
        value = _value(cell)
        if not value:
            continue
        if key.startswith("meta_"):  # informational column that shares a name with a parameter (e.g. nome)
            metadata[key[5:]] = value
        else:
            (params if key in allowed else metadata)[key] = value
    _tags(metadata)
    try:
        validate_params(spec.command, {k: str(v) for k, v in params.items() if v is not None})
    except ValueError as exc:
        raise ValueError(f"riga {number}: {exc}") from exc
    query: dict[str, Any] = {"command": spec.command, "params": params, "metadata": metadata}
    for key, value in fields.items():
        if not value:
            continue
        if key == "enabled":
            query[key] = _flag(value, f"riga {number}")
        elif key == "retries":
            query[key] = int(value)
        else:
            query[key] = value
    return BatchQuery.model_validate(query)


def parse_legacy_code_list(text: str, tipo_richiesta: str | None = None) -> ParsedBatch:
    """The older ``codici_fiscali.all.md`` lists: 16-character codes are ``soggetto``, 11-digit ones ``azienda``."""
    codes = list(dict.fromkeys(re.findall(r"^\| \d+ \| `([A-Z0-9]+)` \|", text, re.M)))
    extra = {"tipo_richiesta": tipo_richiesta} if tipo_richiesta else {}  # only the persona fisica form has it
    queries: list[BatchQuery] = []
    skipped: list[str] = []
    for code in codes:
        if len(code) == 16 and valid_codice_fiscale(code):
            params = {"codice_fiscale": code, "tipo_catasto": "E", **extra}
            queries.append(BatchQuery(id=f"pf-{code}", command="soggetto", params=params))
        elif len(code) == 11 and (valid_partita_iva(code) or code.startswith("00000000")):
            params = {"identificativo": code, "tipo_catasto": "E"}
            queries.append(BatchQuery(id=f"pi-{code}", command="azienda", params=params))
        else:
            skipped.append(code)
    if not queries:
        raise ValueError("nessuna riga di query: né tabelle sister-batch-table né codici fiscali/partite IVA")
    name = "Codici ancora da richiedere (Fabbricati e Terreni)"
    return ParsedBatch(BatchFile(name=name, queries=queries), skipped=skipped)


def load_markdown(text: str, tipo_richiesta: str | None = None) -> ParsedBatch:
    """Any supported Markdown list as a batch."""
    if is_table_markdown(text):
        return parse_markdown(text)
    return parse_legacy_code_list(text, tipo_richiesta)


def legacy_codes_to_markdown(text: str) -> str:
    """Rewrite a legacy codici list as the marked-table format (same codes, with their name and source)."""
    sections = {
        "soggetto": ("Persone fisiche", "codice_fiscale", 16),
        "azienda": ("Persone giuridiche", "identificativo", 11),
    }
    rows: dict[str, list[tuple[str, str, str]]] = {"soggetto": [], "azienda": []}
    for line in text.splitlines():
        match = LEGACY_ROW.match(line)
        if not match:
            continue
        code, parts = match.group(1), cells(line)
        name, source = (parts[2:4] + ["", ""])[:2]
        kind = "soggetto" if len(code) == 16 else "azienda" if len(code) == 11 else None
        if kind:
            rows[kind].append((code, _value(name), _value(source).strip("`")))
    original = text.splitlines()
    title = next((ln[2:].strip() for ln in original if ln.startswith("# ")), "Codici da richiedere a SISTER")
    head = original[original.index(f"# {title}") + 1 :] if f"# {title}" in original else []
    # keep the explanation up to the first section; the old "Totale" line is dropped (the tool counts the rows now)
    prose = [
        ln
        for ln in head[: next((i for i, ln in enumerate(head) if ln.startswith("## ")), len(head))]
        if "Totale:" not in ln
    ]
    while prose and not prose[0].strip():
        prose.pop(0)
    out = [f"# {title}", "", *prose]
    while out and not out[-1].strip():
        out.pop()
    out += (
        [""]
        if any("dati personali" in ln for ln in prose)
        else ["", "> Contiene dati personali: non committare nel repository.", ""]
    )
    for command, (label, column, _) in sections.items():
        marker = json.dumps({"command": command, "params": {"tipo_catasto": "E"}}, ensure_ascii=False)
        out += [
            f"## {label}",
            "",
            f"<!-- sister-batch-table {marker} -->",
            f"| id | {column} | intestatario | fonte |",
            "|---|---|---|---|",
        ]
        prefix = "pf" if command == "soggetto" else "pi"
        out += [f"| {prefix}-{code} | {code} | {name} | {source} |" for code, name, source in rows[command]]
        out.append("")
    return "\n".join(out)
