"""Original document files (``.p7m``, ``.pdf``, ``.xml``, planimetrie) in a private Google Cloud Storage bucket.

Locally the files live in ``SISTER_FILES_BASE``. A deployed container has no such disk, so the rows of the
``document`` table point at files that do not exist there. With ``SISTER_DOCUMENTS_GCS_BUCKET`` set, a file missing
on disk is read from the bucket (through ``aecs4u-storage``) into a local cache and served from there; the code that
reads ``doc["file_path"]`` (view, download, planimetrie, OCR) does not change.

* ``SISTER_DOCUMENTS_GCS_BUCKET``: bucket name (fallback ``GCS_BUCKET``). Unset = feature off, disk only.
* ``SISTER_DOCUMENTS_GCS_PREFIX``: folder in the bucket, default ``documents``. Object = ``<prefix>/<file_name>``.
* ``SISTER_DOCUMENTS_CACHE_DIR``: where downloads are kept, default ``<tmp>/sister-documents`` (Cloud Run: in memory).
* ``GCS_CREDENTIALS_FILE`` / ``GCS_PROJECT``: optional; on Cloud Run the service account is used (Application Default
  Credentials) and needs ``roles/storage.objectViewer`` (``objectAdmin`` for the upload command).

The bucket holds personal data: keep it private (uniform access, public access prevention enforced).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("sister")

DEFAULT_PREFIX = "documents"
_EXISTS_TTL_SECONDS = 300.0
_exists_cache: dict[str, tuple[float, bool]] = {}
_provider = None
_download_locks: dict[str, asyncio.Lock] = {}


def bucket_name() -> str:
    return (os.getenv("SISTER_DOCUMENTS_GCS_BUCKET") or os.getenv("GCS_BUCKET") or "").strip()


def enabled() -> bool:
    return bool(bucket_name())


def prefix() -> str:
    return (os.getenv("SISTER_DOCUMENTS_GCS_PREFIX") or DEFAULT_PREFIX).strip().strip("/")


def cache_dir() -> Path:
    configured = os.getenv("SISTER_DOCUMENTS_CACHE_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "sister-documents"


def object_key(file_name: str | None) -> str | None:
    """Bucket key of a stored document, or ``None`` for a name that is not a plain file name."""
    name = (file_name or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
        return None
    return f"{prefix()}/{name}" if prefix() else name


def get_provider():
    """The GCS provider of ``aecs4u-storage`` (created once)."""
    global _provider
    if _provider is None:
        from aecs4u_storage.providers.gcs import GCSStorageProvider

        _provider = GCSStorageProvider(
            bucket=bucket_name(),
            project=os.getenv("GCS_PROJECT") or None,
            credentials_file=os.getenv("GCS_CREDENTIALS_FILE") or None,
        )
    return _provider


async def remote_exists(file_name: str) -> bool:
    """Whether the bucket holds the file (answers are reused for a few minutes; errors count as missing)."""
    key = object_key(file_name)
    if not enabled() or key is None:
        return False
    cached = _exists_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _EXISTS_TTL_SECONDS:
        return cached[1]
    try:
        found = bool(await get_provider().exists(key))
    except Exception as exc:  # noqa: BLE001 - a storage outage must not break the page
        logger.warning("Document storage lookup failed: %s: %s", type(exc).__name__, exc)
        return False
    _exists_cache[key] = (now, found)
    return found


async def remote_available(file_names: Iterable[str]) -> set[str]:
    """The names, out of ``file_names``, that the bucket holds."""
    names = list({name for name in file_names if name})
    if not enabled() or not names:
        return set()
    semaphore = asyncio.Semaphore(8)

    async def check(name: str) -> tuple[str, bool]:
        async with semaphore:
            return name, await remote_exists(name)

    return {name for name, found in await asyncio.gather(*(check(name) for name in names)) if found}


async def ensure_local(file_name: str | None, local_path: str | None) -> str | None:
    """Path of a readable copy of the document: the disk file when present, else a download from the bucket.

    Returns ``None`` when neither exists (or the feature is off and the disk file is missing).
    """
    if local_path and Path(local_path).is_file():
        return local_path
    key = object_key(file_name)
    if not enabled() or key is None:
        return None
    target = cache_dir() / str(file_name)
    if target.is_file():
        return str(target)
    lock = _download_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if target.is_file():
            return str(target)
        try:
            data = await get_provider().download(key)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            partial = target.with_name(f".{target.name}.part")
            partial.write_bytes(data)
            partial.chmod(0o600)
            partial.replace(target)  # atomic: a reader never sees half a file
        except Exception as exc:  # noqa: BLE001 - missing object, permissions, network
            logger.warning("Document %s not available from storage: %s: %s", file_name, type(exc).__name__, exc)
            return None
    _exists_cache[key] = (time.monotonic(), True)
    return str(target)
