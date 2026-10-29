"""Batch engine for ``sister query batch``: submission file, planning, resumable execution.

A batch is a list of single-step queries (``sister.query_forms``) read from a **JSON submission file** (format in
``docs/batch_format.md``, JSON Schema in ``docs/batch_submission.schema.json``) or from the older CSV. Both are turned
into the same :class:`BatchFile`, then:

1. :func:`plan_batch` validates **every** query before anything is sent (unknown command, unknown/missing parameters,
   bad choices, duplicate ids) and drops exact duplicates, so a typo on row 60 does not surface after 59 paid requests.
2. :class:`BatchRunner` submits the plan with bounded concurrency (``max_in_flight``), optionally waits for each result,
   retries transient failures, and records every state change in an append-only **journal** (JSON Lines).
3. A later run with ``resume=True`` skips what completed, **re-polls** what was submitted but not finished (never
   submits it again: a repeated request can be paid twice) and re-runs what failed.

The module has no CLI or console code; progress goes through the ``emit`` callback.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .client import VisuraAPIError, VisuraClient
from .query_forms import _ALIASES, QUERY_FORMS, get_query_form, param_names, validate_params

BATCH_FORMAT_VERSION = 1
DEFAULT_COMMAND = "search"
JOURNAL_NAME = "batch_journal.jsonl"

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

# Row states. ``queued`` = submitted, not waited for; the others are final unless noted.
QUEUED = "queued"
COMPLETED = "completed"
ERROR = "error"  # the portal/service failed the request (retried ``retries`` times)
NEEDS_HUMAN = "needs_human"  # a CAPTCHA nobody solved: a human action, never retried
TIMEOUT = "timeout"  # still running when polling stopped; resume polls it again
# SISTER's own answer to a search: the subject does not exist. It comes back as an error, but a repeat gets the same
# answer, so ``--resume`` does not send it again (a changed query is still a new request).
FINAL_ERRORS = ("NESSUNA CORRISPONDENZA", "Superato il limite massimo")
INVALID = "invalid"
DUPLICATE = "duplicate"
DISABLED = "disabled"
SKIPPED = "skipped"  # not started because ``on_error`` is ``stop``

OK_STATUSES = frozenset({COMPLETED, QUEUED})
NOT_RUN_STATUSES = frozenset({DUPLICATE, DISABLED, SKIPPED})
TAG_PERSISTENCE_ERROR = "Tag persistence failed:"


# ---------------------------------------------------------------------------
# Submission file
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _text(value: Any) -> str | None:
    """A parameter as the CLI/API sends it: strings; ``true``/``false`` for booleans; ``None`` = not given."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        value = str(value).strip()
        return value or None
    raise ValueError("i parametri devono essere stringhe, numeri o booleani")


ParamValue = str | int | float | bool | None


def _clean_params(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("params deve essere un oggetto {parametro: valore}")
    return {str(key).strip().lower().replace("-", "_"): _text(val) for key, val in value.items()}


class BatchOptions(_Strict):
    """How the batch runs. The CLI flags override these values."""

    wait: bool = Field(False, description="Wait for each result (otherwise rows are only submitted: status 'queued').")
    reuse: Literal["ask", "reuse", "force"] = Field(
        "ask",
        description="Identical query already answered: 'ask' = reuse it, asking before downloading again when the "
        "answer is stale (no terminal = no); 'reuse' = always reuse the previous answer; 'force' = always send a "
        "new request (possibly paid).",
    )
    max_in_flight: int = Field(
        1, ge=1, le=8, description="Rows handled at once. The service has one portal session, so 1 is the safe default."
    )
    retries: int = Field(
        1, ge=0, le=5, description="Extra attempts for a row that fails with a transient error or status 'error'."
    )
    retry_delay: float = Field(5.0, ge=0, description="Seconds before the first retry; doubles on each further retry.")
    poll_interval: float | None = Field(None, gt=0, description="Seconds between result polls (client default).")
    poll_timeout: float | None = Field(
        None,
        gt=0,
        description="Seconds to wait for one request before giving up (client default). A CAPTCHA row needs "
        "enough time for a human to type it.",
    )
    on_error: Literal["continue", "stop"] = Field(
        "continue", description="'stop' = after the first failed row, do not start the others (they stay pending)."
    )
    output_dir: str | None = Field(
        None, description="Folder for one JSON per row (batch_NNNN_<command>.json) and the journal (resume)."
    )


class BatchDefaults(_Strict):
    """Values every query starts from; a query's own values win."""

    command: str | None = Field(None, description="Query command used by queries without one (default: search).")
    params: dict[str, ParamValue] = Field(default_factory=dict, description="Parameters merged under each query's.")

    @field_validator("params", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> dict[str, str | None]:
        return _clean_params(value)


class BatchQuery(_Strict):
    """One query of the batch."""

    id: str | None = Field(
        None,
        pattern=_ID_PATTERN,
        description="Unique id, used by the journal and for resume. Default: q0001, q0002, ...",
    )
    command: str | None = Field(None, description="A `sister query` command (see `sister queries`).")
    params: dict[str, ParamValue] = Field(
        default_factory=dict,
        description="Parameters of the command (same names as its CLI options). Numbers and booleans are accepted; "
        "null removes a value inherited from defaults.params.",
    )
    enabled: bool = Field(True, description="false = keep the query in the file but do not run it.")
    reuse: Literal["ask", "reuse", "force"] | None = Field(None, description="Overrides options.reuse for this query.")
    retries: int | None = Field(None, ge=0, le=5, description="Overrides options.retries for this query.")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Free-form notes copied to the result (source files, reason, ...). Not sent."
    )

    @field_validator("params", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> dict[str, str | None]:
        return _clean_params(value)


class BatchFile(_Strict):
    """The JSON submission file."""

    schema_uri: str | None = Field(None, alias="$schema")
    version: Literal[1] = Field(BATCH_FORMAT_VERSION, description="Format version.")
    name: str | None = Field(None, max_length=200, description="Label of the batch, copied to the report.")
    description: str | None = None
    defaults: BatchDefaults = Field(default_factory=BatchDefaults)
    options: BatchOptions = Field(default_factory=BatchOptions)
    queries: list[BatchQuery] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "BatchFile":
        seen: set[str] = set()
        for query in self.queries:
            if query.id is None:
                continue
            if query.id in seen:
                raise ValueError(f"id duplicato: {query.id}")
            seen.add(query.id)
        return self


def load_json_batch(path: Path) -> BatchFile:
    return BatchFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_csv_batch(path: Path, command: str | None = None) -> BatchFile:
    """The legacy CSV as a batch: one query per row, columns = parameters of the row's command.

    ``#`` lines are comments. A ``command`` column sets each row's command, an ``id`` column its id; any other column
    that is not a parameter of the command is kept as metadata (the old files carry ``source_files``/``scope_note``).
    """
    with path.open(encoding="utf-8") as fh:
        lines = [line for line in fh if not line.strip().startswith("#")]
    queries: list[BatchQuery] = []
    for row in csv.DictReader(io.StringIO("".join(lines))):
        row = {k.strip().lower(): v.strip() for k, v in row.items() if k and v and v.strip()}
        if not row:
            continue
        row_command = row.pop("command", None)
        query_id = row.pop("id", None)
        spec = get_query_form(row_command or command or DEFAULT_COMMAND)
        known = set(param_names(spec.command)) if spec else set(row)
        queries.append(
            BatchQuery(
                id=query_id,
                command=row_command,
                params={k: v for k, v in row.items() if k in known},
                metadata={k: v for k, v in row.items() if k not in known},
            )
        )
    if not queries:
        raise ValueError("nessuna riga valida nel file di input")
    return BatchFile(defaults=BatchDefaults(command=command), queries=queries)


def load_batch(path: Path, command: str | None = None) -> BatchFile:
    """Read a submission file: ``.json`` = the JSON format, anything else = CSV."""
    if path.suffix.lower() == ".json":
        batch = load_json_batch(path)
        if command:
            batch.defaults.command = command
        return batch
    return load_csv_batch(path, command)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class PlannedQuery:
    """A query resolved against the defaults and checked against its spec."""

    index: int  # 1-based position in the file
    id: str
    command: str  # canonical `sister query` command ("" when unknown)
    params: dict[str, str]
    reuse: str
    retries: int
    metadata: dict[str, Any]
    fingerprint: str
    state: str = "ready"  # ready | invalid | duplicate | disabled
    error: str = ""
    duplicate_of: str = ""

    @property
    def label(self) -> str:
        shown = " ".join(f"{k}={v}" for k, v in list(self.params.items())[:4])
        return f"[{self.command}] {shown}".strip()


def fingerprint(command: str, params: dict[str, str]) -> str:
    """Identity of a query: same command and parameters = same request."""
    payload = json.dumps({"command": command, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def plan_batch(batch: BatchFile, options: BatchOptions | None = None) -> list[PlannedQuery]:
    """Resolve defaults and validate every query. Nothing is sent; problems are recorded on each item, not raised."""
    options = options or batch.options
    plan: list[PlannedQuery] = []
    seen_fp: dict[str, str] = {}
    used_ids = {q.id for q in batch.queries if q.id}
    for index, query in enumerate(batch.queries, 1):
        query_id = query.id or _auto_id(index, used_ids)
        merged = {**batch.defaults.params, **query.params}
        params = {k: v for k, v in merged.items() if v not in (None, "")}
        name = query.command or batch.defaults.command or DEFAULT_COMMAND
        spec = get_query_form(name)
        item = PlannedQuery(
            index=index,
            id=query_id,
            command=spec.command if spec else "",
            params=params,
            reuse=query.reuse or options.reuse,
            retries=options.retries if query.retries is None else query.retries,
            metadata=query.metadata,
            fingerprint="",
        )
        plan.append(item)
        if spec is None:
            item.state, item.error = "invalid", f"comando sconosciuto: {name}"
            continue
        item.fingerprint = fingerprint(spec.command, params)
        unknown = sorted(set(params) - set(param_names(spec.command)))
        try:
            if unknown:
                raise ValueError(f"parametri non previsti da '{spec.command}': {', '.join(unknown)}")
            validate_params(spec.command, params)
        except ValueError as exc:
            item.state, item.error = "invalid", str(exc)
            continue
        if not query.enabled:
            item.state = "disabled"
        elif item.fingerprint in seen_fp:
            item.state, item.duplicate_of = "duplicate", seen_fp[item.fingerprint]
        else:
            seen_fp[item.fingerprint] = item.id
    return plan


def _auto_id(index: int, used: set[str]) -> str:
    query_id = f"q{index:04d}"
    while query_id in used:
        query_id += "x"
    used.add(query_id)
    return query_id


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class BatchJournal:
    """Append-only JSON Lines record of each row's state: the last line of an id wins.

    Written after every state change (submitted, finished), so a crash, a service reload or Ctrl-C loses nothing that
    was already sent.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if not self.path.exists():
            return entries
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:  # a half-written last line after a crash
                continue
            if isinstance(entry, dict) and entry.get("id"):
                entries[entry["id"]] = entry
        return entries

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

Emit = Callable[..., None]
Confirm = Callable[[dict], bool]


@dataclass
class BatchReport:
    name: str | None
    entries: list[dict] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return counts

    @property
    def failed(self) -> int:
        """Rows that need attention: neither done nor intentionally left out."""
        return sum(1 for e in self.entries if e["status"] not in OK_STATUSES | NOT_RUN_STATUSES)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": len(self.entries),
            "counts": self.counts,
            "results": self.entries,
        }


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, VisuraAPIError):
        return exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, httpx.TransportError)


def _metadata_tags(metadata: dict[str, Any]) -> list[str]:
    value = metadata.get("tags")
    values = value.replace(";", ",").split(",") if isinstance(value, str) else value or []
    return list(dict.fromkeys(str(tag).strip().upper() for tag in values if str(tag).strip()))


class TagPersistenceError(RuntimeError):
    """The query ran or was resumed, but its source tags could not be stored."""


class BatchRunner:
    """Run a plan against the service. ``emit(event, item, **info)`` reports progress (events: ``skip``, ``start``,
    ``submitted``, ``result``, ``retry``, ``done``, ``written``)."""

    def __init__(
        self,
        client: VisuraClient,
        options: BatchOptions,
        *,
        journal: BatchJournal | None = None,
        resume: bool = False,
        confirm: Confirm | None = None,
        emit: Emit | None = None,
    ):
        self.client = client
        self.options = options
        self.journal = journal
        self.resume = resume
        self.confirm = confirm
        self.emit = emit or (lambda *args, **kwargs: None)
        self._prior = journal.load() if journal and resume else {}
        self._stop = False
        self._prompt_lock = asyncio.Lock()

    async def run(self, plan: list[PlannedQuery], name: str | None = None) -> BatchReport:
        report = BatchReport(name=name)
        gate = asyncio.Semaphore(self.options.max_in_flight)
        total = len(plan)

        async def one(item: PlannedQuery) -> dict:
            if item.state != "ready":
                return self._not_run(item, item.state)
            async with gate:
                if self._stop:
                    return self._not_run(item, SKIPPED)
                entry = await self._execute(item, total)
            if entry["status"] not in OK_STATUSES and self.options.on_error == "stop":
                self._stop = True
            return entry

        try:
            report.entries = list(await asyncio.gather(*(one(item) for item in plan)))
        finally:
            report.finished_at = _now()
        return report

    # -- one row ------------------------------------------------------------------------------------------------

    def _not_run(self, item: PlannedQuery, state: str) -> dict:
        status = state if state != "ready" else SKIPPED
        entry = self._entry(item, status)
        if item.error:
            entry["error"] = item.error
        if item.duplicate_of:
            entry["duplicate_of"] = item.duplicate_of
        # not journaled: invalid rows are fixed in the file, skipped rows stay pending for the next run
        self.emit("skip", item, status=status)
        return entry

    def _entry(self, item: PlannedQuery, status: str, **extra: Any) -> dict:
        entry: dict[str, Any] = {"row": item.index, "id": item.id, "command": item.command, "status": status}
        for key in ("source_files", "scope_note"):  # the CSV files of the earlier batches carry these at top level
            if item.metadata.get(key):
                entry[key] = item.metadata[key]
        if item.metadata:
            entry["metadata"] = item.metadata
        entry.update(label=item.label, fingerprint=item.fingerprint, **extra)
        return entry

    def _record(self, entry: dict) -> None:
        if self.journal:
            self.journal.append({**{k: v for k, v in entry.items() if k != "results"}, "updated_at": _now()})

    async def _execute(self, item: PlannedQuery, total: int) -> dict:
        prior = self._prior.get(item.id)
        if prior and prior.get("fingerprint") != item.fingerprint:
            prior = None  # the query was edited: it is a new request
        request_ids: list[str] = []
        force = item.reuse == "force"
        if prior:
            status = prior.get("status")
            if status == COMPLETED or (status == QUEUED and not self.options.wait):
                try:
                    await self._tag_requests(item, prior.get("request_ids", []))
                except TagPersistenceError as exc:
                    entry = self._entry(
                        item,
                        ERROR,
                        error=f"{TAG_PERSISTENCE_ERROR} {exc}",
                        request_ids=prior.get("request_ids", []),
                        retryable=False,
                    )
                    self._record(entry)
                    self._write_row(item, entry)
                    return entry
                self.emit("skip", item, status=status, resumed=True)
                return {
                    **self._entry(item, status),
                    **{k: prior[k] for k in ("request_ids", "result_file") if k in prior},
                }
            if status == ERROR and any(marker in str(prior.get("error", "")) for marker in FINAL_ERRORS):
                try:
                    await self._tag_requests(item, prior.get("request_ids", []))
                except TagPersistenceError as exc:
                    entry = self._entry(
                        item,
                        ERROR,
                        error=f"{TAG_PERSISTENCE_ERROR} {exc}",
                        request_ids=prior.get("request_ids", []),
                        retryable=False,
                    )
                    self._record(entry)
                    self._write_row(item, entry)
                    return entry
                self.emit("skip", item, status=status, resumed=True)
                return {
                    **self._entry(item, status, error=prior.get("error", "")),
                    **{k: prior[k] for k in ("request_ids", "result_file") if k in prior},
                }
            if (
                status == ERROR
                and str(prior.get("error", "")).startswith(TAG_PERSISTENCE_ERROR)
                and prior.get("request_ids")
            ):
                request_ids = list(prior["request_ids"])
            elif status in (QUEUED, TIMEOUT) and prior.get("request_ids"):
                request_ids = list(prior["request_ids"])  # already sent: only wait for it again
            else:
                force = True  # failed before: a new request, not the cached failure

        self.emit("start", item, total=total)
        attempts = 0
        while True:
            attempts += 1
            entry = await self._attempt(item, request_ids, force)
            entry["attempts"] = attempts
            retryable = entry.pop("retryable", True)
            if not (entry["status"] == ERROR and retryable and attempts <= item.retries):
                break
            delay = self.options.retry_delay * 2 ** (attempts - 1)
            self.emit("retry", item, attempt=attempts, delay=delay, error=entry.get("error", ""))
            await asyncio.sleep(delay)
            request_ids, force = [], True
        self._record(entry)
        self._write_row(item, entry)
        self.emit("done", item, entry=entry)
        return entry

    async def _attempt(self, item: PlannedQuery, request_ids: list[str], force: bool) -> dict:
        try:
            if not request_ids:
                request_ids = await self._submit(item, force)
                self._record(self._entry(item, QUEUED, request_ids=request_ids))
                self.emit("submitted", item, request_ids=request_ids)
            if not request_ids:
                return self._entry(item, ERROR, error="il servizio non ha restituito alcun request_id")
            try:
                await self._tag_requests(item, request_ids)
            except TagPersistenceError as exc:
                return self._entry(
                    item,
                    ERROR,
                    error=f"{TAG_PERSISTENCE_ERROR} {exc}",
                    request_ids=request_ids,
                    retryable=False,
                )
            if not self.options.wait:
                return self._entry(item, QUEUED, request_ids=request_ids)
            return await self._wait(item, request_ids)
        except Exception as exc:  # one broken row must not take the others down; transient ones are retried
            detail = f"HTTP {exc.status_code}: {exc.detail}" if isinstance(exc, VisuraAPIError) else repr(exc)
            return self._entry(item, ERROR, error=detail, request_ids=request_ids, retryable=_transient(exc))

    async def _tag_requests(self, item: PlannedQuery, request_ids: list[str]) -> None:
        tags = _metadata_tags(item.metadata)
        if not tags or not request_ids:
            return
        try:
            from .database import add_request_tags

            await add_request_tags(request_ids, tags)
        except Exception as exc:
            raise TagPersistenceError(str(exc)) from exc

    async def _submit(self, item: PlannedQuery, force: bool) -> list[str]:
        params = dict(item.params)
        if force:
            result = await self.client.submit(item.command, params, force=True)
        else:
            result = await self.client.submit(item.command, params, reuse_previous=True)
            if result.get("stale") and item.reuse != "reuse" and await self._ask(result):
                result = await self.client.submit(item.command, dict(item.params), force=True)
        return VisuraClient.request_ids(result)

    async def _ask(self, stale_result: dict) -> bool:
        """Download a stale answer again? One question at a time, whatever ``max_in_flight`` is."""
        if self.confirm is None:
            return False
        async with self._prompt_lock:
            # Confirmation is an interactive gate: while the operator decides,
            # pause the batch event loop so concurrent rows cannot submit paid work.
            return bool(self.confirm(stale_result))

    async def _wait(self, item: PlannedQuery, request_ids: list[str]) -> dict:
        results: dict[str, dict] = {}
        for rid in request_ids:
            try:
                result = await self.client.wait_for_result(
                    rid, poll_interval=self.options.poll_interval, poll_timeout=self.options.poll_timeout
                )
            except TimeoutError as exc:
                result = {"status": TIMEOUT, "error": str(exc)}
            results[rid] = result
            self.emit("result", item, request_id=rid, result=result)
        statuses = {r.get("status") for r in results.values()}
        if statuses == {COMPLETED}:
            status = COMPLETED
        elif NEEDS_HUMAN in statuses:
            status = NEEDS_HUMAN
        elif TIMEOUT in statuses:
            status = TIMEOUT
        else:
            status = ERROR
        extra: dict[str, Any] = {"request_ids": request_ids, "results": results}
        if status == ERROR:
            extra["error"] = "; ".join(
                f"{rid}: {r.get('error') or r.get('status')}"
                for rid, r in results.items()
                if r.get("status") != COMPLETED
            )
        return self._entry(item, status, **extra)

    def _write_row(self, item: PlannedQuery, entry: dict) -> None:
        folder = self.options.output_dir
        if not folder or "results" not in entry:
            return
        name = f"batch_{item.index:04d}_{item.command}.json"
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        entry["result_file"] = name
        self._record(entry)
        self.emit("written", item, path=str(path))


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


def batch_json_schema() -> dict:
    """JSON Schema of the submission file, with the commands and the parameters of each taken from ``query_forms``.

    Editors use it for completion/validation of ``params``. Parameters a query may inherit from ``defaults.params``
    are not required here (the schema cannot merge them); :func:`plan_batch` checks the merged result.
    """
    schema = BatchFile.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://sister.aecs4u.it/schemas/batch_submission.schema.json"
    schema["title"] = "SISTER batch submission"
    aliases: dict[str, list[str]] = {}
    for alias, target in _ALIASES.items():
        aliases.setdefault(target, []).append(alias)
    commands = sorted(QUERY_FORMS)
    names = sorted({*commands, *_ALIASES})
    defs = schema["$defs"]
    defs["BatchQuery"]["properties"]["command"] = {
        "description": defs["BatchQuery"]["properties"]["command"].get("description", ""),
        "enum": names,
    }
    defs["BatchDefaults"]["properties"]["command"] = {
        "description": defs["BatchDefaults"]["properties"]["command"].get("description", ""),
        "enum": names,
    }
    value = {"type": ["string", "number", "boolean", "null"]}
    defs["BatchQuery"]["allOf"] = [
        {
            "if": {"required": ["command"], "properties": {"command": {"enum": [cmd, *sorted(aliases.get(cmd, []))]}}},
            "then": {
                "properties": {
                    "params": {
                        "type": "object",
                        "properties": {name: value for name in param_names(cmd)},
                        "additionalProperties": False,
                    }
                }
            },
        }
        for cmd in commands
    ]
    return schema
