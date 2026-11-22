"""Copy the stored documents to the private GCS bucket (``sister.document_storage``).

Only the files the ``document`` table lists are considered, and only those missing from the bucket are sent:
existing objects are never overwritten and nothing is deleted. Dry run unless ``--apply``.

    uv run sister documents sync-gcs                 # report
    uv run sister documents sync-gcs --apply         # upload the missing files
"""

from __future__ import annotations

import asyncio
import mimetypes
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from sister import document_storage  # noqa: E402
from sister.database import _get_engine, document_file_path  # noqa: E402

_MEDIA = {".p7m": "application/pkcs7-mime", ".pdf": "application/pdf", ".xml": "application/xml"}


async def _file_names() -> list[str]:
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT DISTINCT file_name FROM document WHERE file_name IS NOT NULL"))
            return sorted(row[0] for row in rows)
    finally:
        await engine.dispose()


async def _run(apply: bool) -> int:
    names = await _file_names()
    local, no_disk, bad = [], [], []
    for name in names:
        if document_storage.object_key(name) is None:
            bad.append(name)
        elif (path := document_file_path(name)) and Path(path).is_file():
            local.append(name)
        else:
            no_disk.append(name)
    present = await document_storage.remote_available(local)
    todo = [name for name in local if name not in present]
    print(f"bucket gs://{document_storage.bucket_name()}/{document_storage.prefix()}")
    print(f"documents in the database: {len(names)}; on disk: {len(local)}; already in the bucket: {len(present)}")
    print(f"to upload: {len(todo)}; not on disk (cannot be sent): {len(no_disk)}; unusable names: {len(bad)}")
    if not apply:
        for name in todo[:20]:
            print("  would upload", name)
        return 0
    provider = document_storage.get_provider()
    failures = 0
    for name in todo:
        path = Path(document_file_path(name) or "")
        try:
            await provider.upload(
                key=document_storage.object_key(name) or "",
                data=path.read_bytes(),
                content_type=_MEDIA.get(path.suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream",
                filename=name,
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAILED {name}: {type(exc).__name__}: {exc}")
    print(f"uploaded {len(todo) - failures}, failed {failures}")
    return 1 if failures else 0


def run(*, apply: bool = False) -> int:
    if not document_storage.enabled():
        print("SISTER_DOCUMENTS_GCS_BUCKET is not set")
        return 2
    return asyncio.run(_run(apply))
