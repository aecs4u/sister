#!/usr/bin/env python3
"""Turn a markdown list of codici (``codici_fiscali.all.md``: tables with a `code` in the second column) into a batch plan.

    ../.venv/bin/python scripts/build_batch_from_list.py /mnt/mobile/data/aecs4u.it/sister/codici_fiscali.all.md \\
        --output-dir /mnt/mobile/data/aecs4u.it/sister/outputs/batch_da_richiedere_20261027

16 characters -> ``soggetto`` (persona fisica), 11 digits -> ``soggetto-immobili`` with ``azienda`` (persona giuridica); both with
``tipo_catasto`` E (Fabbricati and Terreni) and, with ``--tipo-richiesta storica``, "Tipo richiesta = Storica" (persone fisiche only; the persona giuridica form has no such control, the
portal's default Attualità applies). A code with a wrong check character is left out and reported. Run the plan with
``scripts/run_in_session.py --batch <output-dir>``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sister.fiscal_code import valid_codice_fiscale, valid_partita_iva  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tipo-richiesta", choices=("attualita", "storica"), help="'Tipo richiesta' of the portal form (default: the portal's, Attualità)")
    parser.add_argument("--name", default="Codici ancora da richiedere (Fabbricati e Terreni)")
    args = parser.parse_args()

    codes = list(dict.fromkeys(re.findall(r"^\| \d+ \| `([A-Z0-9]+)` \|", args.list_file.read_text(encoding="utf-8"), re.M)))
    # only the persona fisica form has "Tipo richiesta" (the persona giuridica form has none: Attualità only)
    extra = {"tipo_richiesta": args.tipo_richiesta} if args.tipo_richiesta else {}
    queries, skipped = [], []
    for code in codes:
        if len(code) == 16 and valid_codice_fiscale(code):
            queries.append({"id": f"pf-{code}", "command": "soggetto", "params": {"codice_fiscale": code, "tipo_catasto": "E", **extra}})
        elif len(code) == 11 and (valid_partita_iva(code) or code.startswith("00000000")):
            queries.append({"id": f"pi-{code}", "command": "soggetto-immobili",
                            "params": {"codice_fiscale": code, "azienda": True, "tipo_catasto": "E"}})
        else:
            skipped.append(code)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "version": 1,
        "name": args.name,
        "description": f"Da {args.list_file.name}. Sola lettura.",
        "options": {"wait": True, "reuse": "reuse", "max_in_flight": 1, "retries": 0, "on_error": "continue",
                    "poll_timeout": 300, "output_dir": str(args.output_dir.resolve())},
        "queries": queries,
    }
    (args.output_dir / "batch.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(queries)} righe in {args.output_dir / 'batch.json'}; saltati (formato o controllo errato): {skipped or 'nessuno'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
