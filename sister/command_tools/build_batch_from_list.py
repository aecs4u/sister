#!/usr/bin/env python3
"""Build a resumable SISTER plan (``batch.json``) from a Markdown batch list.

The format is described in ``docs/batch_markdown_format.md``: tables marked ``<!-- sister-batch-table -->`` (one query
per row, columns = query fields, parameters and metadata) in any mix of commands. The older tax-code lists
(``codici_fiscali.all.md``: 16-character codes become ``soggetto`` queries, 11-digit VAT numbers ``azienda`` queries) are
still read. Rows keep their metadata and run through the same ``sister batch run-list`` workflow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sister.batch_markdown import load_markdown  # noqa: E402


def _norm(value) -> str:
    return " ".join(str(value).split()).casefold() if value is not None else ""


def drop_duplicate_queries(queries: list[dict], defaults: dict | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Keep the first of the rows that ask the same thing (same command and parameters, e.g. one codice fiscale or one
    parcel listed twice); return ``(kept, [(dropped id, kept id), ...])``.

    A row with ``reuse: force`` is repeated on purpose and a disabled row does not run: both always stay. Rows that
    differ in any parameter (``tipo_catasto``, ``subalterno``, ``provincia``...) are different questions and stay.
    """
    base_command = (defaults or {}).get("command")
    base_params = (defaults or {}).get("params") or {}
    first: dict[tuple, str] = {}
    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []
    for query in queries:
        if query.get("reuse") == "force" or query.get("enabled") is False:
            kept.append(query)
            continue
        params = {**base_params, **(query.get("params") or {})}
        key = (
            _norm(query.get("command") or base_command),
            tuple(sorted((name, _norm(value)) for name, value in params.items() if value is not None)),
        )
        if key in first:
            dropped.append((str(query.get("id")), first[key]))
            continue
        first[key] = str(query.get("id"))
        kept.append(query)
    return kept, dropped


EXTRA_CHOICES = ("none", "immobili", "intestati", "all")


def apply_extra(queries: list[dict], extra: str, defaults: dict | None = None) -> int:
    """Set, on every row, which extra data the run reads (``--extra``); returns the number of rows changed.

    * ``immobili`` — parcel rows (``search`` / ``intestati``): press *Immobili* for each owner of the Intestati page and
      read the owner's other properties (``immobili_intestatari``).
    * ``intestati`` — subject rows (``soggetto`` / ``soggetto-immobili`` / ``azienda``): also read the owners (co-owners)
      of each property (``con_intestati``); a ``soggetto`` row becomes ``soggetto-immobili``.
    * ``all`` both, ``none`` neither. The owners of a parcel row's own immobili are always read in phase 1.
    """
    base_command = (defaults or {}).get("command")
    want_immobili = extra in ("immobili", "all")
    want_intestati = extra in ("intestati", "all")
    changed = 0
    for query in queries:
        command = query.get("command") or base_command
        params = dict(query.get("params") or {})
        before = (command, dict(params))
        if command in ("search", "intestati"):
            params["immobili_intestatari"] = want_immobili
        elif command in ("soggetto", "soggetto-immobili", "azienda"):
            if command == "soggetto" and want_intestati:
                command = "soggetto-immobili"
            if command != "soggetto":
                params["con_intestati"] = want_intestati
        else:
            continue
        if (command, params) != before:
            changed += 1
        query["command"] = command
        query["params"] = params
    return changed


def make_captcha_free(queries: list[dict], defaults: dict | None = None) -> int:
    """Turn every row that would request a document into its HTML-only counterpart; returns the number of rows changed.

    The *Tipo di visura* form is the only page with a CAPTCHA, and only the queries that request documents reach it:

    * ``visura-storica`` becomes ``search`` (the property and its owners, read from the pages; no ``vista``);
    * ``soggetto-documento`` becomes ``soggetto-immobili`` (the properties of the subject; no ``vista`` / ``tipo_richiesta``);
    * ``search`` / ``intestati`` get ``richiedi_documenti=false``.

    Other commands are not touched. The list file itself is never changed.
    """
    base_command = (defaults or {}).get("command")
    changed = 0
    for query in queries:
        command = query.get("command") or base_command
        params = dict(query.get("params") or {})
        before = (command, dict(params))
        if command == "visura-storica":
            command = "search"
            params.pop("vista", None)
            params["richiedi_documenti"] = False
        elif command == "soggetto-documento":
            command = "soggetto-immobili"
            for name in ("vista", "tipo_richiesta"):
                params.pop(name, None)
        elif command in ("search", "intestati"):
            params["richiedi_documenti"] = False
        else:
            continue
        if (command, params) != before:
            changed += 1
        query["command"] = command
        query["params"] = params
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tipo-richiesta", choices=("attualita", "storica"), help="'Tipo richiesta' for person searches")
    parser.add_argument(
        "--extra",
        choices=EXTRA_CHOICES,
        help="extra data to read besides the list itself: immobili (other properties of each owner of a parcel), "
        "intestati (co-owners of each property of a subject), all or none; default: as written in the list",
    )
    parser.add_argument(
        "--captcha-free",
        action="store_true",
        help="read only the pages without a CAPTCHA: rows that request documents become their HTML-only counterparts "
        "(visura-storica -> search, soggetto-documento -> soggetto-immobili, richiedi_documenti=false)",
    )
    parser.add_argument("--name")
    args = parser.parse_args()

    try:
        parsed = load_markdown(args.list_file.read_text(encoding="utf-8"), args.tipo_richiesta)
    except ValueError as exc:
        print(f"{args.list_file}: {exc}", file=sys.stderr)
        return 2
    batch, skipped = parsed.batch, parsed.skipped
    queries = [query.model_dump(mode="json", exclude_defaults=True) for query in batch.queries]
    if batch.name and batch.name.startswith("PVP sales parcels"):
        for query in queries:
            query.setdefault("metadata", {})["tags"] = list(dict.fromkeys([*query.get("metadata", {}).get("tags", []), "PVP"]))

    defaults = batch.defaults.model_dump(mode="json", exclude_defaults=True)
    if args.captcha_free:
        print(f"--captcha-free: {make_captcha_free(queries, defaults)} righe senza richiesta di documenti")
    if args.extra:
        print(f"--extra {args.extra}: {apply_extra(queries, args.extra, defaults)} righe aggiornate")
    queries, duplicates = drop_duplicate_queries(queries, defaults)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "version": 1,
        "name": args.name or batch.name or f"Da {args.list_file.name}",
        "description": batch.description or f"Da {args.list_file.name}. Sola lettura.",
        "options": {
            "wait": True,
            "reuse": "reuse",
            "max_in_flight": 1,
            "retries": 0,
            "on_error": "continue",
            "poll_timeout": 600,  # above the service budget (SISTER_OMONIMI_MAX_SECONDS 150 + one record): no false timeouts
            **batch.options.model_dump(mode="json", exclude_defaults=True),
            "output_dir": str(args.output_dir.resolve()),
        },
        "queries": queries,
    }
    if batch.defaults.command or batch.defaults.params:
        plan["defaults"] = batch.defaults.model_dump(mode="json", exclude_defaults=True)
    (args.output_dir / "batch.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    if duplicates:
        print(f"{len(duplicates)} righe duplicate tolte (stesso comando e parametri; resta la prima): "
              + ", ".join(f"{gone}={kept}" for gone, kept in duplicates[:5]) + (" ..." if len(duplicates) > 5 else ""))
    if skipped:
        print(f"{len(queries)} righe in {args.output_dir / 'batch.json'}; saltati (formato o controllo errato): {skipped}")
    else:
        print(f"{len(queries)} righe in {args.output_dir / 'batch.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
