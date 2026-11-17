"""Build and execute the PDF-parseable SISTER query batch."""

from __future__ import annotations

import argparse
from pathlib import Path

from .build_pdf_parseable_batch import DEFAULT_BATCH, DEFAULT_DOCUMENTS, DEFAULT_INVENTORY


def run(
    *,
    documents_dir: Path = DEFAULT_DOCUMENTS,
    batch_csv: Path = DEFAULT_BATCH,
    inventory_csv: Path = DEFAULT_INVENTORY,
    output_dir: Path = Path("outputs/pdf_parseable_batch"),
) -> int:
    """Build the input and submit it using the shared `query batch` implementation."""
    from . import build_pdf_parseable_batch

    status = build_pdf_parseable_batch.run(
        documents_dir=documents_dir, batch_csv=batch_csv, inventory_csv=inventory_csv
    )
    if status:
        return status

    from sister.cli.query import batch

    try:
        batch(
            input_file=str(batch_csv),
            command="auto",
            wait=True,
            output_dir=str(output_dir),
            output=None,
            dry_run=False,
            force=False,
            redownload=None,
            resume=False,
            journal=None,
            max_in_flight=None,
            retries=None,
            poll_timeout=None,
            on_error=None,
            skip_invalid=False,
            stop_at=None,
        )
    except Exception as exc:
        import typer

        if isinstance(exc, typer.Exit):
            return exc.exit_code
        raise
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents-dir", type=Path, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--batch-csv", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--inventory-csv", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/pdf_parseable_batch"))
    args = parser.parse_args(argv)
    return run(
        documents_dir=args.documents_dir,
        batch_csv=args.batch_csv,
        inventory_csv=args.inventory_csv,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
