"""Live progress bars for ``run-list`` batches.

    uv run sister batch progress <output_dir> [<output_dir> ...]
    uv run sister batch progress --once <output_dir>      # print one snapshot and exit

Each ``output_dir`` holds ``batch.json`` (the plan), ``batch_journal.jsonl`` (one line per row state change), and, during
``run-list``, ``service.log``. The main bar counts finished rows. The office bar reads structured progress markers from
the service log while a fiscal-code match is being opened across multiple provincial offices. This tool only reads files:
it never talks to the service or to SISTER.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sister.search_outcome import FOUND, INVALID_CODE, NO_MATCH, PENDING, SESSION_EXPIRED, TOO_MANY, classify  # noqa: E402

_OFFICE_PROGRESS = re.compile(
    r"RUN_LIST_OFFICE_PROGRESS subject=(?P<subject>\S+) record=(?P<record>\d+)/(?P<record_total>\d+) "
    r"offices=(?P<done>\d+)/(?P<total>\d+) province=(?P<province>\S+) state=(?P<state>opening|completed|failed)"
)


def _last_office_progress(directory: Path) -> dict | None:
    """Read the latest structured provincial-office marker written by the browser worker."""
    path = directory / "service.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 262_144))
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = _OFFICE_PROGRESS.search(line)
        if not match:
            continue
        values = match.groupdict()
        try:
            return {
                "subject": values["subject"],
                "record": int(values["record"]),
                "record_total": int(values["record_total"]),
                "done": int(values["done"]),
                "total": int(values["total"]),
                "province": values["province"],
                "state": values["state"],
            }
        except ValueError:
            continue
    return None


def _file_marks(directory: Path) -> tuple[int, int]:
    """Nanosecond mtimes for the row journal and live browser log."""
    marks = []
    for name in ("batch_journal.jsonl", "service.log"):
        try:
            marks.append((directory / name).stat().st_mtime_ns)
        except OSError:
            marks.append(0)
    return tuple(marks)


def snapshot(directory: Path) -> dict:
    """Counts of one batch directory and its latest in-flight provincial-office marker."""
    plan = json.loads((directory / "batch.json").read_text(encoding="utf-8"))
    queries = {q.get("id"): q for q in plan.get("queries", [])}
    ids = set(queries)
    total = len(ids)
    last: dict[str, dict] = {}
    journal = directory / "batch_journal.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:  # a line still being written
                continue
            if row.get("id") in ids:  # rows dropped from the plan (already answered elsewhere) are not counted
                last[row.get("id")] = row
    found = no_match = refine = failed = session = pending = 0
    stamps = []
    for row in last.values():
        outcome = classify(row.get("status"), row.get("error"))
        if outcome == FOUND:
            found += 1
        elif outcome in (NO_MATCH, INVALID_CODE):  # SISTER's own final answers
            no_match += 1
        elif outcome == TOO_MANY:
            refine += 1
        elif outcome == SESSION_EXPIRED:
            session += 1
        elif outcome == PENDING:
            pending += 1
        else:
            failed += 1
        if row.get("status") in ("completed", "error") and row.get("updated_at"):
            stamps.append(datetime.fromisoformat(row["updated_at"]).timestamp())
    done = found + no_match + refine
    processed = done + failed + session
    active_subjects = {
        str((queries.get(row_id, {}).get("params") or {}).get("identificativo") or "").strip()
        for row_id, row in last.items()
        if row.get("status") == "queued"
        or (row.get("status") == "incomplete" and row.get("omonimo_prossimo"))
    }
    office_progress = _last_office_progress(directory)
    if office_progress and office_progress["subject"] not in active_subjects:
        office_progress = None
    rate = None
    if len(stamps) >= 2 and max(stamps) > min(stamps):
        recent = sorted(stamps)[-60:]
        rate = (len(recent) - 1) / (recent[-1] - recent[0]) if recent[-1] > recent[0] else None
    return {
        "name": plan.get("name") or directory.name,
        "dir": directory.name,
        "total": total,
        "done": done,
        "processed": processed,
        "found": found,
        "no_match": no_match,
        "refine": refine,
        "failed": failed,
        "session": session,
        "pending": pending,
        "office_progress": office_progress,
        "rate": rate,
        "last": max(stamps) if stamps else 0,
    }


def is_finished(directory: Path) -> bool:
    """Every row of the batch has its final answer (results, or SISTER's own "no match" / "too many").

    ``run-list`` resumes a batch only while something is left in it (pending rows, errors to repeat, an interrupted
    session); a finished one is not resumed, a new batch folder is started instead. Otherwise the journal of the
    finished run keeps every row "done" and a row with ``reuse: force`` would never be sent again.
    """
    try:
        snap = snapshot(directory)
    except (OSError, ValueError):
        return False
    return snap["total"] > 0 and snap["done"] >= snap["total"]


def eta(snap: dict) -> str:
    left = snap["total"] - snap.get("processed", snap["done"])
    if left <= 0:
        return "finito"
    if not snap["rate"]:
        return "-"
    seconds = int(left / snap["rate"])
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


def _bar(fraction: float, color: str = "green", width: int = 48) -> Text:
    filled = round(width * min(max(fraction, 0.0), 1.0))
    return Text.assemble(("█" * filled, color), ("░" * (width - filled), "dim"))


def render(snaps: list[dict]):
    parts = []
    for snap in snaps:
        total = max(snap["total"], 1)
        processed = snap.get("processed", snap["done"])
        bar = _bar(processed / total)
        percent = 100 * processed / total
        head = Text.assemble((f"{snap['name']}", "bold"), f"  ({snap['dir']})")
        line = Table.grid(padding=(0, 1))
        line.add_row(bar, Text(f"{processed}/{snap['total']} processate  {percent:5.1f}%  restano {eta(snap)}"))
        detail = Text.assemble(
            ("con immobili ", "dim"), (str(snap["found"]), "green"),
            ("  nessuna corrispondenza ", "dim"), (str(snap["no_match"]), "yellow"),
            ("  criteri da affinare ", "dim"),
            (str(snap.get("refine", 0)), "yellow" if snap.get("refine") else "default"),
            ("  in attesa ", "dim"), (str(snap["pending"]), "cyan"),
            ("  errori ", "dim"), (str(snap["failed"]), "red" if snap["failed"] else "default"),
            ("  sessione scaduta ", "dim"), (str(snap["session"]), "red" if snap["session"] else "default"),
        )
        parts += [head, line, detail]
        office = snap.get("office_progress")
        if office and office["record_total"] > 1:
            matches_done = max(office["record"] - 1, 0)
            if office["state"] == "completed" and office["done"] >= office["total"]:
                matches_done = office["record"]
            matches_done = min(matches_done, office["record_total"])
            match_line = Table.grid(padding=(0, 1))
            match_line.add_row(
                Text(f"match {office['subject']}", style="dim"),
                _bar(matches_done / office["record_total"], "green"),
                Text(
                    f"{matches_done}/{office['record_total']} completati"
                    + (f"  in corso {office['record']}/{office['record_total']}" if matches_done < office["record"] else "")
                ),
            )
            parts.append(match_line)
        if office and office["total"] > 1:
            office_total = max(office["total"], 1)
            if office["state"] == "failed":
                status = f"{office['done']}/{office_total} tentati  errore {office['province']}"
            else:
                status = f"{office['done']}/{office_total} completati"
                if office["province"] != "-":
                    status += f"  " + (
                        f"apertura {office['province']}" if office["state"] == "opening" else office["province"]
                    )
            office_line = Table.grid(padding=(0, 1))
            office_line.add_row(
                Text(
                    f"uffici {office['subject']} · match {office['record']}/{office['record_total']}",
                    style="dim",
                ),
                _bar(office["done"] / office_total, "cyan"),
                Text(status),
            )
            parts.append(office_line)
        parts.append(Text(""))
    return Group(*parts)


def render_session(snaps: list[dict], login: datetime, deadline: datetime, now: datetime | None = None):
    """Whole-run view for ``run_in_session``: all plans together, the time left in the SISTER session, then each plan."""
    now = now or datetime.now()
    total = sum(s["total"] for s in snaps)
    processed = sum(s.get("processed", s["done"]) for s in snaps)
    left = max((deadline - now).total_seconds(), 0)
    span = max((deadline - login).total_seconds(), 1)
    active = max(snaps, key=lambda s: s["last"]) if snaps else None
    rate = active["rate"] if active and active["last"] else None

    overall = Table.grid(padding=(0, 1))
    overall.add_row(
        Text("totale   ", style="bold"),
        _bar(processed / max(total, 1), "green"),
        Text(f"{processed}/{total} processate  {100 * processed / max(total, 1):5.1f}%"),
    )
    overall.add_row(
        Text("sessione ", style="bold"),
        _bar(1 - left / span, "red" if left < 180 else "yellow" if left < 600 else "cyan"),
        Text(f"restano {int(left // 60)}m{int(left % 60):02d}s, chiusura alle {deadline:%H:%M}"),
    )
    note = None
    if rate and total > processed:
        fits = int(rate * left)
        note = f"{rate * 60:.0f} righe/min: in questa sessione ne restano circa {min(fits, total - processed)} su {total - processed}"
        if fits < total - processed:
            note += " (le altre alla prossima sessione)"
    return Group(overall, Text(f"          {note}", style="dim") if note else Text(""), Text(""), render(snaps))


class Dashboard:
    """Batch journals and service logs for several plans, re-read only when one changes."""

    def __init__(self, directories: list[Path], login: datetime, deadline: datetime):
        self.directories, self.login, self.deadline = directories, login, deadline
        self._marks = {d: self._mtime(d) for d in directories}
        self._snaps = [snapshot(d) for d in directories]

    @staticmethod
    def _mtime(directory: Path) -> tuple[int, int]:
        return _file_marks(directory)

    def snaps(self) -> list[dict]:
        for index, directory in enumerate(self.directories):
            mtime = self._mtime(directory)
            if mtime != self._marks[directory]:
                self._marks[directory], self._snaps[index] = mtime, snapshot(directory)
        return self._snaps

    def view(self):
        return render_session(self.snaps(), self.login, self.deadline)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directories", nargs="+", type=Path, help="batch output directories")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between journal checks (default 0.5)")
    args = parser.parse_args()
    console = Console()

    def take() -> list[dict]:
        return [snapshot(d) for d in args.directories]

    if args.once or not console.is_terminal:
        console.print(render(take()))
        return 0

    marks = {d: _file_marks(d) for d in args.directories}
    snaps = take()
    with Live(render(snaps), console=console, refresh_per_second=4) as live:
        try:
            while True:
                changed = False
                for d in args.directories:
                    mtime = _file_marks(d)
                    if mtime != marks[d]:
                        marks[d], changed = mtime, True
                if changed:
                    snaps = take()
                    live.update(render(snaps))
                if all(s["done"] >= s["total"] for s in snaps):
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
