"""Capture parseable SISTER JSON responses as local extraction sources.

Only responses from the authenticated SISTER origin are considered. Parsed
payloads are kept local, authentication/session fields are removed, and
recognizable cadastral property rows can supplement DOM extraction. Nothing in
this module sends response data to Jev.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urlsplit

logger = logging.getLogger("sister.network_json")

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_RESPONSES = 100
_JSON_PREFIX = re.compile(r"^\s*(?:\ufeff|\)\]\}',?\s*)*")
_JSONP = re.compile(r"^[\w.$]+\s*\((.*)\)\s*;?\s*$", re.DOTALL)
_JSONP_START = re.compile(r"^[\w.$]+\s*\(")
_SECRET_KEY_PARTS = ("password", "passwd", "token", "csrf", "session", "cookie", "authorization", "captcha", "otp")


def _json_like(text: str):
    """Parse JSON, anti-XSSI-prefixed JSON, or JSONP without evaluating code."""
    candidate = _JSON_PREFIX.sub("", text, count=1).strip()
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        match = _JSONP.fullmatch(candidate)
        if not match:
            return None
        try:
            value = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            return None
    return value if isinstance(value, (dict, list)) else None


def _safe_json(value, depth: int = 0):
    """Bound payload size/depth and omit authentication material recursively."""
    if depth > 20:
        return None
    if isinstance(value, dict):
        output = {}
        for key, item in list(value.items())[:5000]:
            key_text = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                continue
            output[key_text[:200]] = _safe_json(item, depth + 1)
        return output
    if isinstance(value, list):
        return [_safe_json(item, depth + 1) for item in value[:5000]]
    if isinstance(value, str):
        return value[:20000]
    if value is None or type(value) in (bool, int, float):
        return value
    return str(value)[:20000]


class SISTERJsonCapture:
    """Asynchronously collect bounded, parseable responses from SISTER."""

    def __init__(self, page):
        self.page = page
        self.responses: list[dict] = []
        self._tasks: set[asyncio.Task] = set()
        self._bytes = 0
        self._started = False
        self._host = (urlsplit(getattr(page, "url", "")).hostname or "").lower()

    def _is_sister_host(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return bool(host) and (
            host == self._host
            or host == "sister3.agenziaentrate.gov.it"
            or host.endswith(".agenziaentrate.gov.it")
        )

    def _on_response(self, response) -> None:
        task = asyncio.create_task(self._capture_response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture_response(self, response) -> None:
        if len(self.responses) >= MAX_RESPONSES:
            return
        try:
            url = str(response.url)
            status = int(response.status)
            if status < 200 or status >= 300 or not self._is_sister_host(url):
                return
            headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            path = urlsplit(url).path
            disposition = headers.get("content-disposition", "").lower()
            declared = int(headers.get("content-length", "0") or 0)
            json_hint = (
                "json" in content_type
                or path.lower().endswith(".json")
                or ("filename" in disposition and ".json" in disposition)
            )
            if declared > MAX_RESPONSE_BYTES:
                return
            raw = await response.body()
            if len(raw) > MAX_RESPONSE_BYTES or self._bytes + len(raw) > MAX_TOTAL_BYTES:
                return
            text = raw.decode("utf-8-sig", errors="replace")
            stripped = text.lstrip()
            if not json_hint and not stripped.startswith(("{", "[", ")]}'")) and not _JSONP_START.match(stripped):
                return
            parsed = _json_like(text)
            if parsed is None:
                return
            self._bytes += len(raw)
            self.responses.append(
                {
                    "url": path[:500],
                    "content_type": content_type[:120],
                    "data": _safe_json(parsed),
                }
            )
        except Exception as exc:
            logger.debug("SISTER JSON response skipped (%s)", type(exc).__name__)

    async def start(self):
        if self._started or not hasattr(self.page, "on"):
            return self
        self.page.on("response", self._on_response)
        self._started = True
        return self

    async def stop(self) -> list[dict]:
        if self._started:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self._started = False
        while self._tasks:
            pending = tuple(self._tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            # Do not depend on scheduled done callbacks to mutate the set before
            # the next loop iteration; completed gather tasks may return inline.
            self._tasks.difference_update(pending)
        return list(self.responses)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_exc):
        await self.stop()


def extract_property_rows(payload) -> list[dict[str, str]]:
    """Find property-shaped records in a parsed JSON payload, without guessing.

    A row is accepted only when it has both a sheet and a parcel identifier.
    Existing SISTER field names are used so the normal database importer can
    persist the records.
    """
    aliases = {
        "foglio": "Foglio",
        "sheet": "Foglio",
        "particella": "Particella",
        "particella1": "Particella",
        "mappale": "Particella",
        "parcel": "Particella",
        "sub": "Sub",
        "subalterno": "Sub",
        "subunit": "Sub",
        "catasto": "Catasto",
        "tipo_catasto": "Catasto",
        "provincia_catastale": "Provincia Catastale",
        "provincia": "Provincia Catastale",
        "comune_catastale": "Comune Catastale",
        "comune": "Comune Catastale",
        "codice_provincia_catastale": "Codice Provincia Catastale",
        "codice_comune_catastale": "Codice Comune Catastale",
        "sezione": "Sezione",
        "classamento": "Classamento",
        "categoria": "Categoria",
        "rendita": "Rendita Catastale",
        "rendita_catastale": "Rendita Catastale",
        "indirizzo": "Indirizzo",
        "address": "Indirizzo",
        "titolarita": "Titolarità",
        "quota": "Quota",
        "reddito_dominicale": "Reddito dominicale",
        "reddito_agrario": "Reddito agrario",
        "superficie": "Superficie",
    }

    def key_name(value) -> str:
        value = str(value).strip().lower()
        value = value.replace("à", "a").replace("è", "e").replace("ì", "i").replace("ò", "o").replace("ù", "u")
        return re.sub(r"[^a-z0-9]+", "_", value).strip("_")

    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def visit(value):
        if isinstance(value, dict):
            normalized = {key_name(k): v for k, v in value.items()}
            row = {
                aliases[key]: str(item).strip()
                for key, item in normalized.items()
                if key in aliases and item is not None and not isinstance(item, (dict, list))
            }
            record_text = " ".join(str(item) for item in value.values() if not isinstance(item, (dict, list)))
            if row.get("Foglio") and row.get("Particella"):
                if not re.search(r"soppress", record_text, re.IGNORECASE):
                    signature = tuple(row.get(k, "").strip().upper() for k in ("Catasto", "Foglio", "Particella", "Sub"))
                    if signature not in seen:
                        seen.add(signature)
                        found.append(row)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return found
