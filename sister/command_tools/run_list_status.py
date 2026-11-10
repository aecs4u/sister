"""Durable lifecycle events for ``batch run-session`` runs.

The batch journal records row outcomes, but a resumed run can reuse the same
directory. These sidecars keep the separate run sessions visible to the admin
dashboard without changing the batch plan or journal.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunListStatus:
    """Write one current status and an append-only event stream per batch directory."""

    def __init__(self, directories: list[Path]):
        self.directories = [Path(directory) for directory in directories]
        self.task_id = uuid4().hex
        self.pid = os.getpid()
        self.started_at = _now()
        self.state = "running"
        self.phase = "starting"
        self.current_batch: str | None = None
        self.outcome: str | None = None
        self._warned = False

    def start(self) -> None:
        self._write("started")

    def set_phase(self, phase: str, current_batch: str | None = None) -> None:
        changed = phase != self.phase or current_batch != self.current_batch
        self.phase = phase
        self.current_batch = current_batch
        self._write("phase" if changed else None)

    def heartbeat(self) -> None:
        """Refresh liveness while a batch is active without growing the event log."""
        self._write(None)

    def finish(self, outcome: str) -> None:
        self.state = "finished"
        self.outcome = outcome
        self.phase = "finished"
        self.current_batch = None
        self._write("finished")

    def _write(self, event: str | None) -> None:
        updated_at = _now()
        common = {
            "task_id": self.task_id,
            "pid": self.pid,
            "state": self.state,
            "phase": self.phase,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "updated_at": updated_at,
            "current_batch": self.current_batch,
        }
        failed = False
        for directory in self.directories:
            status = {**common, "batch_dir": directory.name}
            try:
                _atomic_json(directory / "run_list_status.json", status)
                if event:
                    history_event = {**status, "event": event}
                    with (directory / "run_list_history.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(history_event, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                failed = True
        if failed and not self._warned:
            print("Avviso: impossibile salvare lo stato amministrativo di run-list; il batch continua.", file=sys.stderr)
            self._warned = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    """Replace status as a whole so the dashboard never sees a partial JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
