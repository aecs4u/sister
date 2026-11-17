"""Build a Markdown batch plan, choose its resume folder, and run it in the current SISTER session."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _latest_batch(list_file: Path, output_root: Path) -> Path | None:
    candidates = sorted(
        path for path in output_root.glob(f"{list_file.stem}_*") if path.is_dir() and (path / "batch_journal.jsonl").is_file()
    )
    if not candidates:
        return None
    from .batch_progress import is_finished

    latest = candidates[-1]
    if is_finished(latest):
        print(f"Saved batch for {list_file.name} is finished ({latest}): starting a new one")
        return None
    print(f"Resuming saved batch for {list_file.name}: {latest}")
    return latest


def run(
    *,
    list_file: Path,
    batch_dir: Path | None = None,
    tipo_richiesta: str | None = None,
    extra: str | None = None,
    captcha_free: bool = False,
    preflight_cf: str | None = None,
    email_progress: bool = False,
    email_to: str = "demo@aecs4u.com",
    stop_stale_service: bool = False,
    minutes: float = 26,
    logged_in_at: str | None = None,
    grace_seconds: float = 60,
    port: int = 8027,
    cdp: str | None = None,
    dry_run: bool = False,
    keep_answered: bool = False,
    probe: bool = False,
    output_root: Path | None = None,
) -> int:
    """Create or resume a batch folder, then delegate the authenticated run to ``run-session``."""
    source = list_file.expanduser().resolve()
    if not source.is_file():
        print(f"Source list is not a file: {source}", file=sys.stderr)
        return 2
    login_time = logged_in_at or datetime.now().strftime("%H:%M")
    try:
        datetime.strptime(login_time, "%H:%M")
    except ValueError:
        print("--logged-in-at must look like 14:05", file=sys.stderr)
        return 2

    batch_root = output_root or ROOT / "outputs" / "batches"
    selected = batch_dir.expanduser().resolve() if batch_dir else _latest_batch(source, batch_root)
    if selected is None:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        selected = batch_root / f"{source.stem}_{run_id}"

    temporary = tempfile.TemporaryDirectory(prefix="sister-run-list-") if dry_run else None
    try:
        if temporary:
            selected = Path(temporary.name)
        selected.mkdir(parents=True, exist_ok=True)

        from .build_batch_from_list import run as build_list
        from .run_in_session import run as run_session

        build_result = build_list(
            source,
            selected,
            tipo_richiesta=tipo_richiesta,
            extra=extra,
            captcha_free=captcha_free,
        )
        if build_result:
            return build_result

        return run_session(
            batch=[selected],
            minutes=minutes,
            logged_in_at=login_time,
            grace_seconds=grace_seconds,
            port=port,
            preflight_cf=preflight_cf,
            stop_stale_service=stop_stale_service,
            cdp=cdp,
            email_progress=email_progress,
            email_to=email_to,
            dry_run=dry_run,
            keep_answered=keep_answered,
            probe=probe,
        )
    finally:
        if temporary:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("list_file", type=Path)
    parser.add_argument("batch_dir", type=Path, nargs="?")
    parser.add_argument("--preflight-cf", default=os.getenv("SISTER_PREFLIGHT_CF"))
    parser.add_argument("--email-progress", action="store_true")
    parser.add_argument("--email-to", default="demo@aecs4u.com")
    parser.add_argument("--stop-stale-service", action="store_true")
    parser.add_argument("--minutes", type=float, default=26)
    parser.add_argument("--logged-in-at")
    parser.add_argument("--grace-seconds", type=float, default=60)
    parser.add_argument("--port", type=int, default=8027)
    parser.add_argument("--cdp")
    parser.add_argument("--tipo-richiesta", choices=("attualita", "storica"))
    parser.add_argument("--extra", choices=("none", "immobili", "intestati", "all"))
    parser.add_argument("--captcha-free", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-answered", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--resume", action="store_true", help="accepted for compatibility; the latest batch resumes by default")
    args = parser.parse_args(argv)
    return run(
        list_file=args.list_file,
        batch_dir=args.batch_dir,
        tipo_richiesta=args.tipo_richiesta,
        extra=args.extra,
        captcha_free=args.captcha_free,
        preflight_cf=args.preflight_cf,
        email_progress=args.email_progress,
        email_to=args.email_to,
        stop_stale_service=args.stop_stale_service,
        minutes=args.minutes,
        logged_in_at=args.logged_in_at,
        grace_seconds=args.grace_seconds,
        port=args.port,
        cdp=args.cdp,
        dry_run=args.dry_run,
        keep_answered=args.keep_answered,
        probe=args.probe,
    )


if __name__ == "__main__":
    raise SystemExit(main())
