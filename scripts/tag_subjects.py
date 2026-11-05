#!/usr/bin/env python3
"""Tag the persons and companies of the archive from the data we already have (PVP roles, auctioned properties, politicians).

Dry run by default: prints what would be added. ``--apply`` stores it and writes a plan file; ``--revert PLAN`` removes
exactly what that run added. Only ever adds tags, so it is safe to run again. Rules: ``sister/subject_autotag.py``.

    python scripts/tag_subjects.py                       # what would be added
    python scripts/tag_subjects.py --apply               # add it, plan saved in data/tag_plans/
    python scripts/tag_subjects.py --only politici       # one family: pvp, aste, politici, catasto
    python scripts/tag_subjects.py --revert data/tag_plans/subject_tags_20261105_190000.json
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)

from sister import subject_autotag  # noqa: E402

logging.disable(logging.CRITICAL)


def _table(rows: list[dict]) -> None:
    counts = Counter((row["tag"], row["kind"]) for row in rows)
    width = max((len(tag) for tag, _kind in counts), default=10)
    for (tag, kind), count in sorted(counts.items()):
        print(f"  {tag:<{width}}  {kind:<8}  {count:>5}")
    print(f"  {'totale':<{width}}  {'':<8}  {len(rows):>5}")


async def main(args: argparse.Namespace) -> int:
    if args.revert:
        rows = json.loads(Path(args.revert).read_text(encoding="utf-8"))["rows"]
        print(f"Piano {args.revert}: {len(rows)} tag da togliere.")
        if not args.apply:
            print("Simulazione: aggiungi --apply per toglierli.")
            return 0
        print(f"Tolti {await subject_autotag.revert_rows(rows)} tag.")
        return 0

    only = [f.strip() for f in args.only.split(",") if f.strip()]
    unknown = sorted(set(only) - set(subject_autotag.FAMILIES))
    if unknown:
        print(f"Famiglie sconosciute: {', '.join(unknown)} (valide: {', '.join(subject_autotag.FAMILIES)})")
        return 2
    politicians = Path(args.politicians)
    if "politici" in only and not politicians.exists():
        print(f"Elenco dei politici non trovato: {politicians}")
        return 2
    plan, skipped = await subject_autotag.build_plan(politicians, only)
    rows = await subject_autotag.new_rows(plan)
    tagged = len(plan)
    print(f"Soggetti con almeno un tag secondo le regole: {tagged}; tag ancora da aggiungere: {len(rows)}.")
    _table(rows)
    if skipped:
        print(f"\nPolitici non taggati perché il nome non corrisponde ({len(skipped)}), da verificare:")
        for item in skipped:
            print(f"  {item['code']}  archivio: {item['archive']}  elenco: {item['list']}")
    if not rows:
        return 0
    if not args.apply:
        print("\nSimulazione: aggiungi --apply per salvarli.")
        return 0
    added = await subject_autotag.apply_rows(rows)
    plan_dir = Path(args.plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"subject_tags_{datetime.now():%Y%m%d_%H%M%S}.json"
    plan_file.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nAggiunti {added} tag. Piano per annullare: {plan_file}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="store the tags (default: dry run)")
    parser.add_argument("--only", default=",".join(subject_autotag.FAMILIES), help="comma-separated families (default: all)")
    parser.add_argument("--politicians", default=str(PROJECT_ROOT / "inputs" / "politici_siciliani.md"))
    parser.add_argument("--plan-dir", default=str(PROJECT_ROOT / "data" / "tag_plans"))
    parser.add_argument("--revert", help="plan file of an earlier --apply: remove what it added (with --apply)")
    sys.exit(asyncio.run(main(parser.parse_args())))
