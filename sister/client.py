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
    """Async HTTP client for the sister service.

    Maintains a persistent httpx.AsyncClient across requests to reuse
    TCP connections. Use as a context manager for explicit cleanup, or
    let the finalizer close the client.
    """

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
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle ------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

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
        client = self._get_client()
        resp = await client.request(method, path, json=json, params=params)
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

    async def soggetto(
        self,
        *,
        codice_fiscale: str,
        tipo_catasto: str | None = None,
        provincia: str | None = None,
    ) -> dict:
        """Submit a national subject search by codice fiscale (POST /visura/soggetto)."""
        payload: dict[str, Any] = {
            "codice_fiscale": codice_fiscale.upper(),
        }
        if tipo_catasto:
            payload["tipo_catasto"] = tipo_catasto.upper()
        if provincia:
            payload["provincia"] = provincia
        return await self._request("POST", "/visura/soggetto", json=payload)

    async def persona_giuridica(
        self,
        *,
        identificativo: str,
        tipo_catasto: str | None = None,
        provincia: str | None = None,
    ) -> dict:
        """Submit a legal entity search by P.IVA or name (POST /visura/persona-giuridica)."""
        payload: dict[str, Any] = {"identificativo": identificativo}
        if tipo_catasto:
            payload["tipo_catasto"] = tipo_catasto.upper()
        if provincia:
            payload["provincia"] = provincia
        return await self._request("POST", "/visura/persona-giuridica", json=payload)

    async def elenco_immobili(
        self,
        *,
        provincia: str,
        comune: str,
        tipo_catasto: str | None = None,
        foglio: str | None = None,
        sezione: str | None = None,
    ) -> dict:
        """Submit a property listing request (POST /visura/elenco-immobili)."""
        payload: dict[str, Any] = {"provincia": provincia, "comune": comune}
        if tipo_catasto:
            payload["tipo_catasto"] = tipo_catasto.upper()
        if foglio:
            payload["foglio"] = foglio
        if sezione:
            payload["sezione"] = sezione
        return await self._request("POST", "/visura/elenco-immobili", json=payload)

    async def generic_search(
        self,
        *,
        search_type: str,
        provincia: str,
        comune: str | None = None,
        tipo_catasto: str | None = None,
        **params,
    ) -> dict:
        """Submit a generic SISTER search (POST /visura/{search_type})."""
        query: dict[str, Any] = {"provincia": provincia}
        if comune:
            query["comune"] = comune
        if tipo_catasto:
            query["tipo_catasto"] = tipo_catasto.upper()
        for k, v in params.items():
            if v is not None:
                query[k] = v
        return await self._request("POST", f"/visura/{search_type}", params=query)

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
