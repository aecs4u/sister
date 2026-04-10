"""Async HTTP client for the sister service.

Wraps the REST API with typed methods and built-in polling support.
Configuration is read from environment variables:

    VISURA_API_URL        — Base URL (default: http://localhost:8025)
    VISURA_API_KEY        — Optional X-API-Key header value
    VISURA_API_TIMEOUT    — HTTP request timeout in seconds (default: 30)
    VISURA_POLL_INTERVAL  — Seconds between polls (default: 5)
    VISURA_POLL_TIMEOUT   — Max seconds to wait for a result (default: 300)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://localhost:8025"
DEFAULT_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_POLL_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class VisuraAPIError(Exception):
    """Error returned by the sister service."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class VisuraClient:
    """Async HTTP client for the sister service."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ):
        self.base_url = (base_url or os.getenv("VISURA_API_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.getenv("VISURA_API_KEY", "")
        self.timeout = timeout or float(os.getenv("VISURA_API_TIMEOUT", DEFAULT_TIMEOUT))
        self.poll_interval = poll_interval or float(os.getenv("VISURA_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
        self.poll_timeout = poll_timeout or float(os.getenv("VISURA_POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT))

    # -- internal helpers -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, url, headers=self._headers(), json=json, params=params)
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise VisuraAPIError(resp.status_code, detail)
        return resp.json()

    # -- public API -----------------------------------------------------------

    async def search(
        self,
        *,
        provincia: str,
        comune: str,
        foglio: str,
        particella: str,
        tipo_catasto: str | None = None,
        sezione: str | None = None,
        subalterno: str | None = None,
    ) -> dict:
        """Submit an immobili search (POST /visura)."""
        payload: dict[str, Any] = {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "particella": particella,
        }
        if tipo_catasto:
            payload["tipo_catasto"] = tipo_catasto.upper()
        if sezione:
            payload["sezione"] = sezione
        if subalterno:
            payload["subalterno"] = subalterno
        return await self._request("POST", "/visura", json=payload)

    async def intestati(
        self,
        *,
        provincia: str,
        comune: str,
        foglio: str,
        particella: str,
        tipo_catasto: str,
        subalterno: str | None = None,
        sezione: str | None = None,
    ) -> dict:
        """Submit an owners (intestati) lookup (POST /visura/intestati)."""
        payload: dict[str, Any] = {
            "provincia": provincia,
            "comune": comune,
            "foglio": foglio,
            "particella": particella,
            "tipo_catasto": tipo_catasto.upper(),
        }
        if subalterno:
            payload["subalterno"] = subalterno
        if sezione:
            payload["sezione"] = sezione
        return await self._request("POST", "/visura/intestati", json=payload)

    async def get_result(self, request_id: str) -> dict:
        """Poll a single request result (GET /visura/{request_id})."""
        return await self._request("GET", f"/visura/{request_id}")

    async def wait_for_result(
        self,
        request_id: str,
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> dict:
        """Poll until the request completes or times out.

        Returns the final response dict (status will be 'completed' or 'error').
        Raises ``TimeoutError`` if ``poll_timeout`` is exceeded.
        """
        interval = poll_interval or self.poll_interval
        timeout = poll_timeout or self.poll_timeout
        start = time.monotonic()

        while True:
            result = await self.get_result(request_id)
            status = result.get("status", "")
            if status in ("completed", "error", "expired"):
                return result
            elapsed = time.monotonic() - start
            if elapsed + interval > timeout:
                raise TimeoutError(
                    f"Timed out after {elapsed:.0f}s waiting for {request_id} (last status: {status})"
                )
            await asyncio.sleep(interval)

    async def history(
        self,
        *,
        provincia: str | None = None,
        comune: str | None = None,
        foglio: str | None = None,
        particella: str | None = None,
        tipo_catasto: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Query response history (GET /visura/history)."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if provincia:
            params["provincia"] = provincia
        if comune:
            params["comune"] = comune
        if foglio:
            params["foglio"] = foglio
        if particella:
            params["particella"] = particella
        if tipo_catasto:
            params["tipo_catasto"] = tipo_catasto.upper()
        return await self._request("GET", "/visura/history", params=params)

    async def health(self) -> dict:
        """Check service health (GET /health)."""
        return await self._request("GET", "/health")
