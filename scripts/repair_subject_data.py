"""Complete and correct the birth data of ``cadastral_subjects`` (dry run unless ``--apply``).

For every subject: a display name that still carries the Intestati-page text (``... nato a ROMA (RM) il 01/01/1970``) is
cleaned, a missing birth date / sex / birth-place code is taken from the codice fiscale (only when that is unambiguous),
an ISO birth date becomes ``DD/MM/YYYY``, a sex that contradicts the codice fiscale is corrected and first/last name are
split for two-word names. A birth date that contradicts the codice fiscale is only reported.

    ../.venv/bin/python scripts/repair_subject_data.py --backup-dir <dir>                  # report
    ../.venv/bin/python scripts/repair_subject_data.py --backup-dir <dir> --apply          # backup, plan, change
    ../.venv/bin/python scripts/repair_subject_data.py --revert <backup-dir>/subject_repair_plan_<ts>.json

``--apply`` writes a pg_dump of the schema and ``subject_repair_plan_<ts>.json`` (old and new value of every changed
column); ``--revert`` restores the old values where the column still holds the new one.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cleanup_extracted_xml import backup  # noqa: E402
from sqlalchemy import text  # noqa: E402

from sister.subject_repair import plan_subject  # noqa: E402

COLUMNS = ("display_name", "last_name", "first_name", "gender", "date_of_birth", "birth_municipality_code")


async def load_rows() -> list[dict]:
    from sister.database import _get_engine

    async with _get_engine().connect() as conn:
        result = await conn.execute(text(f"SELECT id, fiscal_code, {', '.join(COLUMNS)} FROM cadastral_subjects ORDER BY id"))
        return [dict(row._mapping) for row in result]


def build_plan(rows: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    plan, notes = [], []
    for row in rows:
        changes, row_notes = plan_subject(row, today)
        if changes:
            plan.append({"id": row["id"], "fiscal_code": row["fiscal_code"], "changes": {k: list(v) for k, v in changes.items()}})
        if row_notes:
            notes.append({"id": row["id"], "fiscal_code": row["fiscal_code"], "notes": row_notes})
    return plan, notes


async def apply_plan(plan: list[dict], *, revert: bool) -> int:
    from sister.database import _get_engine

    done = 0
    async with _get_engine().connect() as conn:
        transaction = await conn.begin()
        for entry in plan:
            for column, (old, new) in entry["changes"].items():
                if column not in COLUMNS:
                    raise SystemExit(f"unexpected column in plan: {column}")
                wanted, current = (old, new) if revert else (new, old)
                clause = "IS NOT DISTINCT FROM :current"  # only touch what still holds the value the plan was made from
                result = await conn.execute(
                    text(f"UPDATE cadastral_subjects SET {column} = :wanted WHERE id = :id AND {column} {clause}"),  # noqa: S608
                    {"wanted": wanted, "current": current, "id": entry["id"]},
                )
                done += result.rowcount or 0
        await transaction.commit()
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-dir", type=Path, help="where the dump and the plan go (required with --apply)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revert", type=Path, metavar="PLAN", help="undo a saved plan")
    args = parser.parse_args()

    if args.revert:
        plan = json.loads(args.revert.read_text())["plan"]
        print("restored values:", asyncio.run(apply_plan(plan, revert=True)))
        return

    today = date.today()
    rows = asyncio.run(load_rows())
    plan, notes = build_plan(rows, today)
    per_column = collections.Counter(column for entry in plan for column in entry["changes"])
    print(f"subjects: {len(rows)} | rows to change: {len(plan)}")
    for column, count in per_column.most_common():
        print(f"  {column:26} {count}")
    for column in COLUMNS:
        examples = [(e["id"], e["changes"][column]) for e in plan if column in e["changes"]][:3]
        for subject_id, (old, new) in examples:
            print(f"    e.g. #{subject_id} {column}: {old!r} -> {new!r}")
    print(f"\nreported, not changed: {len(notes)}")
    for entry in notes[:12]:
        print(f"  #{entry['id']} {entry['fiscal_code']}: {'; '.join(entry['notes'])}")
    if len(notes) > 12:
        print(f"  ... and {len(notes) - 12} more")

    if not args.apply:
        print("\ndry run: nothing changed (use --apply)")
        return
    if not args.backup_dir:
        parser.error("--apply needs --backup-dir")
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    print("\nbackup:", backup(args.backup_dir))
    path = args.backup_dir / f"subject_repair_plan_{stamp}.json"
    path.write_text(json.dumps({"plan": plan, "reported": notes}, indent=1, ensure_ascii=False, default=str))
    print("plan:", path)
    print("updated values:", asyncio.run(apply_plan(plan, revert=False)))


if __name__ == "__main__":
    main()
