"""Live progress bar of ``sister query batch`` runs, read from their journals.

    ../.venv/bin/python scripts/batch_progress.py <output_dir> [<output_dir> ...]
    ../.venv/bin/python scripts/batch_progress.py --once <output_dir>      # print one snapshot and exit

Each ``output_dir`` holds ``batch.json`` (the plan) and ``batch_journal.jsonl`` (one line per state change). The bar moves
after every row the journal marks as finished and shows how many rows found properties, how many got SISTER's own
"nessuna corrispondenza", how many failed for another reason (a session that expired is listed apart) and the time left.
It only reads files: it never talks to the service or to SISTER.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sister.search_outcome import FOUND, INVALID_CODE, NO_MATCH, PENDING, SESSION_EXPIRED, TOO_MANY, classify  # noqa: E402



def snapshot(directory: Path) -> dict:
    """Counts of one batch directory: the last journal state of every row decides."""
    plan = json.loads((directory / "batch.json").read_text(encoding="utf-8"))
    ids = {q.get("id") for q in plan.get("queries", [])}
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
    found = no_match = failed = session = pending = 0
    stamps = []
    for row in last.values():
        outcome = classify(row.get("status"), row.get("error"))
        if outcome == FOUND:
            found += 1
        elif outcome in (NO_MATCH, INVALID_CODE, TOO_MANY):  # SISTER's own final answers
            no_match += 1
        elif outcome == SESSION_EXPIRED:
            session += 1
        elif outcome == PENDING:
            pending += 1
        else:
            failed += 1
        if row.get("status") in ("completed", "error") and row.get("updated_at"):
            stamps.append(datetime.fromisoformat(row["updated_at"]).timestamp())
    done = found + no_match
    rate = None
    if len(stamps) >= 2 and max(stamps) > min(stamps):
        recent = sorted(stamps)[-60:]
        rate = (len(recent) - 1) / (recent[-1] - recent[0]) if recent[-1] > recent[0] else None
    return {
        "name": plan.get("name") or directory.name,
        "dir": directory.name,
        "total": total,
        "done": done,
        "found": found,
        "no_match": no_match,
        "failed": failed,
        "session": session,
        "pending": pending,
        "rate": rate,
        "last": max(stamps) if stamps else 0,
    }


def eta(snap: dict) -> str:
    left = snap["total"] - snap["done"]
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
        bar = _bar(snap["done"] / total)
        percent = 100 * snap["done"] / total
        head = Text.assemble((f"{snap['name']}", "bold"), f"  ({snap['dir']})")
        line = Table.grid(padding=(0, 1))
        line.add_row(bar, Text(f"{snap['done']}/{snap['total']}  {percent:5.1f}%  restano {eta(snap)}"))
        detail = Text.assemble(
            ("con immobili ", "dim"), (str(snap["found"]), "green"),
            ("  nessuna corrispondenza ", "dim"), (str(snap["no_match"]), "yellow"),
            ("  in attesa ", "dim"), (str(snap["pending"]), "cyan"),
            ("  errori ", "dim"), (str(snap["failed"]), "red" if snap["failed"] else "default"),
            ("  sessione scaduta ", "dim"), (str(snap["session"]), "red" if snap["session"] else "default"),
        )
        parts += [head, line, detail, Text("")]
    return Group(*parts)


def render_session(snaps: list[dict], login: datetime, deadline: datetime, now: datetime | None = None):
    """Whole-run view for ``run_in_session``: all plans together, the time left in the SISTER session, then each plan."""
    now = now or datetime.now()
    total = sum(s["total"] for s in snaps)
    done = sum(s["done"] for s in snaps)
    left = max((deadline - now).total_seconds(), 0)
    span = max((deadline - login).total_seconds(), 1)
    active = max(snaps, key=lambda s: s["last"]) if snaps else None
    rate = active["rate"] if active and active["last"] else None

    overall = Table.grid(padding=(0, 1))
    overall.add_row(Text("totale   ", style="bold"), _bar(done / max(total, 1), "green"), Text(f"{done}/{total}  {100 * done / max(total, 1):5.1f}%"))
    overall.add_row(
        Text("sessione ", style="bold"),
        _bar(1 - left / span, "red" if left < 180 else "yellow" if left < 600 else "cyan"),
        Text(f"restano {int(left // 60)}m{int(left % 60):02d}s, chiusura alle {deadline:%H:%M}"),
    )
    note = None
    if rate and total > done:
        fits = int(rate * left)
        note = f"{rate * 60:.0f} righe/min: in questa sessione ne restano circa {min(fits, total - done)} su {total - done}"
        if fits < total - done:
            note += " (le altre alla prossima sessione)"
    return Group(overall, Text(f"          {note}", style="dim") if note else Text(""), Text(""), render(snaps))


class Dashboard:
    """The journals of several plans, re-read only when one of them changed."""

    def __init__(self, directories: list[Path], login: datetime, deadline: datetime):
        self.directories, self.login, self.deadline = directories, login, deadline
        self._marks = {d: self._mtime(d) for d in directories}
        self._snaps = [snapshot(d) for d in directories]

    @staticmethod
    def _mtime(directory: Path) -> float:
        journal = directory / "batch_journal.jsonl"
        return journal.stat().st_mtime if journal.exists() else 0

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

    marks = {d: (d / "batch_journal.jsonl").stat().st_mtime if (d / "batch_journal.jsonl").exists() else 0 for d in args.directories}
    snaps = take()
    with Live(render(snaps), console=console, refresh_per_second=4) as live:
        try:
            while True:
                changed = False
                for d in args.directories:
                    journal = d / "batch_journal.jsonl"
                    mtime = journal.stat().st_mtime if journal.exists() else 0
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
