#!/usr/bin/env python3
"""Download what is still missing from the list of codici, in one SISTER session.

    ../.venv/bin/python scripts/download_remaining.py --logged-in-at 14:05            # after ade_login.py
    ../.venv/bin/python scripts/download_remaining.py --logged-in-at 14:05 --dry-run  # prune and plan only, nothing is sent

Steps: (1) drop from the list (``codici_fiscali.all.md``) the codes SISTER already answered for Fabbricati and Terreni
(``prune_code_list``), (2) rebuild the batch plan from what is left (``build_batch_from_list``; the journal of the plan directory
is kept, so ``--resume`` skips what is done), (3) run it inside the session with the progress bar (``run_in_session``: real
preflight, clean stop ``--minutes`` after the login), (4) prune the list again and say how many codes remain. A session lasts ~30
minutes from the login: run it again after the next login until the list is empty.

``--logged-in-at`` is required: the time of the login (the time you typed "fatto" is not the login time). Nothing logs in here.
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DATA = Path("/mnt/mobile/data/aecs4u.it/sister")


def run(*command: str) -> int:
    return subprocess.run([sys.executable, *command], cwd=ROOT).returncode


def remaining(list_file: Path) -> int:
    return len(re.findall(r"^\| \d+ \| `", list_file.read_text(encoding="utf-8"), re.M))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logged-in-at", required=True, help="HH:MM of the SISTER login (required)")
    parser.add_argument("--list", type=Path, default=DATA / "codici_fiscali.all.md", dest="list_file")
    parser.add_argument("--batch-dir", type=Path, default=DATA / "outputs" / "batch_da_richiedere")
    parser.add_argument("--minutes", default="26", help="minutes after the login to stop (SISTER closes at ~30)")
    parser.add_argument("--preflight-cf", default=None, help="code for the proof search (empty: none)")
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--tipo-richiesta", choices=("attualita", "storica"),
                        help="'Tipo richiesta' for the persone fisiche (the persona giuridica form has none: Attualità)")
    parser.add_argument("--dry-run", action="store_true", help="prune the list and the plan, send nothing")
    args = parser.parse_args()

    if not re.fullmatch(r"\d{1,2}:\d{2}", args.logged_in_at):
        parser.error("--logged-in-at must look like 14:05")
    if not args.dry_run:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                print(f"la porta {args.port} e' occupata: un servizio SISTER e' ancora acceso. Fermalo per pid (ss -ltnp | grep {args.port}) e rilancia.")
                return 2

    before = remaining(args.list_file)
    print(f"1/4 lista: {before} codici")
    if run("scripts/prune_code_list.py", str(args.list_file), *(["--dry-run"] if args.dry_run else [])):
        return 1
    if args.dry_run:
        print("(prova a vuoto: la lista non e' stata riscritta, il piano non viene rigenerato)")
        return 0
    left = remaining(args.list_file)
    if left == 0:
        print("niente da scaricare: la lista e' vuota")
        return 0

    print(f"2/4 piano: {left} righe in {args.batch_dir}")
    build = ["scripts/build_batch_from_list.py", str(args.list_file), "--output-dir", str(args.batch_dir)]
    if args.tipo_richiesta:
        build += ["--tipo-richiesta", args.tipo_richiesta]
    if run(*build):
        return 1

    print("3/4 sessione SISTER")
    session = ["scripts/run_in_session.py", "--batch", str(args.batch_dir), "--minutes", str(args.minutes),
               "--logged-in-at", args.logged_in_at, "--port", str(args.port)]
    if args.preflight_cf:
        session += ["--preflight-cf", args.preflight_cf]
    status = run(*session)

    print("4/4 lista aggiornata")
    run("scripts/prune_code_list.py", str(args.list_file))
    left = remaining(args.list_file)
    print(f"restano {left} codici su {before}" + ("" if left == 0 else ": dopo il prossimo accesso rilancia lo stesso comando con la nuova ora"))
    return status


if __name__ == "__main__":
    sys.exit(main())
