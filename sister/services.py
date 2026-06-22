"""Service layer: VisuraService — request queue, cache, and auth lifecycle.

BrowserManager lives in sister.browser and is re-exported here for
backwards compatibility.
"""

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime
from typing import Dict, Optional

from aecs4u_auth.browser import PageLogger  # noqa: F401 (re-exported for main.py)

from .browser import BrowserManager  # noqa: F401 (re-exported)
from .database import (
    cleanup_old_responses,
    compute_cache_key,
    find_cached_response,
    save_request,
    save_requests_batch,
    save_response,
)
from .database import (
    get_response as load_stored_response,
)
from .models import (
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaRequest,
    QueueFullError,
    SubmitResult,
    VisuraIntestatiRequest,
    VisuraPersonaGiuridicaRequest,
    VisuraRequest,
    VisuraResponse,
    VisuraSoggettoRequest,
)

logger = logging.getLogger("sister")


class VisuraService:
    """Queue-based request dispatcher with response caching and persistence."""

    def __init__(self):
        self.browser_manager = BrowserManager()
        self.queue_max_size = self._parse_positive_int_env("QUEUE_MAX_SIZE", 100)
        self.response_cleanup_interval_seconds = self._parse_positive_int_env("RESPONSE_CLEANUP_INTERVAL_SECONDS", 60)
        self.request_queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max_size)
        self._queue_lock = asyncio.Lock()
        self.response_store: Dict[str, VisuraResponse] = {}
        self.pending_request_ids: set[str] = set()
        self.expired_request_ids: Dict[str, datetime] = {}
        self.response_ttl_seconds = self._parse_positive_int_env("RESPONSE_TTL_SECONDS", 6 * 3600)
        self.response_max_items = self._parse_positive_int_env("RESPONSE_MAX_ITEMS", 5000)
        self.processing = False
        self._worker_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    @staticmethod
    def _parse_positive_int_env(var_name: str, default: int) -> int:
        raw = os.getenv(var_name)
        if raw is None:
            return default
        try:
            value = int(raw)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            logger.warning("%s non valido (%r), uso default=%d", var_name, raw, default)
            return default

    # ------------------------------------------------------------------
    # Auth lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, background_auth: bool = True, defer_auth: bool = False):
        """Start the worker and cleanup tasks.

        background_auth=True  → auth runs in a background task (default, non-blocking startup)
        background_auth=False → auth blocks startup (used in tests / sync init)
        defer_auth=True       → worker starts but no auth is attempted; call start_browser() later
        """
        self.processing = True
        self._worker_task = asyncio.create_task(self._process_requests(), name="visura-request-worker")
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup(), name="visura-cache-cleanup")

        if defer_auth:
            logger.info("Browser auth differita — usa /web/browser per avviare la sessione")
            return

        if background_auth:
            self._auth_task = asyncio.create_task(self._background_auth(), name="visura-browser-auth")
        else:
            await self._do_auth()

    async def _do_auth(self):
        await self.browser_manager.initialize()
        await self.browser_manager.login()
        await self.browser_manager.start_keep_alive()
        self._auth_ready = True
        logger.info("Browser autenticato e pronto")

    async def _background_auth(self):
        from .models import AuthenticationError as _AuthError
        from .models import BrowserError as _BrowserError
        logger.info("Autenticazione browser in corso...")
        try:
            await self._do_auth()
        except _AuthError as e:
            cause = str(e)
            if "active session" in cause.lower() or "già in sessione" in cause.lower():
                await self._try_close_stale_session()
            msg = f"Autenticazione fallita — riprova dal pannello Browser. ({e})"
            self._auth_failed_message = msg
            logger.error("Browser auth failed: %s", e)
        except _BrowserError as e:
            msg = f"Errore inizializzazione browser — riprova dal pannello Browser. ({e})"
            self._auth_failed_message = msg
            logger.error("Browser init error: %s", e)
        except Exception as e:
            msg = f"Errore inatteso durante l'autenticazione: {type(e).__name__}: {e}"
            self._auth_failed_message = msg
            logger.error("Browser auth unexpected error: %s", msg)

    async def _try_close_stale_session(self):
        try:
            page = self.browser_manager._auth._auth_page
            if not page or page.is_closed():
                ctx = self.browser_manager._auth._context
                if ctx:
                    page = await ctx.new_page()
                else:
                    return

            logout_urls = [
                "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessionsSis",
                "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessions",
            ]
            for url in logout_urls:
                try:
                    await page.goto(url, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    logger.info("Sessione SISTER chiusa via %s", url)
                except Exception:
                    pass

            # Close the temp page if we created one
            if page != self.browser_manager._auth._auth_page:
                with suppress(Exception):
                    await page.close()

        except Exception as e:
            logger.warning("Impossibile chiudere sessione stale: %s", e)

    @property
    def auth_ready(self) -> bool:
        return getattr(self, "_auth_ready", False)

    @property
    def auth_status(self) -> dict:
        mode = "cdp" if self.browser_manager.is_cdp else "local"
        if self.auth_ready:
            return {"state": "ready", "mode": mode, "message": "Browser authenticated"}
        failed_msg = getattr(self, "_auth_failed_message", None)
        if failed_msg:
            return {"state": "unavailable", "mode": mode, "message": failed_msg}
        auth_task = getattr(self, "_auth_task", None)
        if auth_task is not None and not auth_task.done():
            return {"state": "connecting", "mode": mode, "message": "Authentication in progress..."}
        if self.processing:
            return {"state": "idle", "mode": mode, "message": "Browser not started — use the control panel to start"}
        return {"state": "unavailable", "mode": mode, "message": "Browser not initialized"}

    # ------------------------------------------------------------------
    # Request worker
    # ------------------------------------------------------------------

    async def _process_requests(self):
        try:
            while True:
                request = await self.request_queue.get()
                should_sleep = False

                try:
                    if request is None:
                        logger.info("Ricevuto segnale di stop worker")
                        return

                    if not self.auth_ready:
                        logger.debug("Waiting for browser authentication before processing request")
                        for _ in range(60):
                            if self.auth_ready:
                                break
                            await asyncio.sleep(5)
                        if not self.auth_ready:
                            logger.error("Autenticazione non completata — richiesta scartata")
                            self.request_queue.task_done()
                            continue

                    if isinstance(request, VisuraRequest):
                        response = await self.browser_manager.esegui_visura(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta visura %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, VisuraIntestatiRequest):
                        response = await self.browser_manager.esegui_visura_intestati(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta intestati %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, VisuraSoggettoRequest):
                        response = await self.browser_manager.esegui_visura_soggetto(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta soggetto %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, VisuraPersonaGiuridicaRequest):
                        response = await self.browser_manager.esegui_visura_persona_giuridica(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta PNF %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, ElencoImmobiliRequest):
                        response = await self.browser_manager.esegui_elenco_immobili(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta elenco immobili %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, IspezioneIpotecariaRequest):
                        response = await self.browser_manager.esegui_ispezione_ipotecaria(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta ipotecaria %s", request.request_id)
                        should_sleep = True

                    elif isinstance(request, GenericSisterRequest):
                        response = await self.browser_manager.esegui_generic(request)
                        await self._store_response(response)
                        logger.info("Processata richiesta %s %s", request.search_type, request.request_id)
                        should_sleep = True

                    else:
                        logger.error("Tipo di richiesta sconosciuto: %s", type(request))

                except Exception as e:
                    logger.error("Errore nel processare richieste: %s", e)
                    await asyncio.sleep(5)
                finally:
                    if isinstance(
                        request,
                        (
                            VisuraRequest,
                            VisuraIntestatiRequest,
                            VisuraSoggettoRequest,
                            VisuraPersonaGiuridicaRequest,
                            ElencoImmobiliRequest,
                            GenericSisterRequest,
                            IspezioneIpotecariaRequest,
                        ),
                    ):
                        self.pending_request_ids.discard(request.request_id)
                    self.request_queue.task_done()

                if should_sleep:
                    await asyncio.sleep(2)
        finally:
            self.processing = False
            logger.info("Worker richieste terminato")

    # ------------------------------------------------------------------
    # Cache and response store
    # ------------------------------------------------------------------

    async def _periodic_cleanup(self):
        from .database import is_db_writable

        try:
            while self.processing:
                self._cleanup_response_store()
                if is_db_writable():
                    try:
                        deleted = await cleanup_old_responses(self.response_ttl_seconds)
                        if deleted:
                            logger.info("Cleanup database: rimossi %d record scaduti", deleted)
                    except Exception as e:
                        logger.warning("Errore cleanup database: %s", e)
                await asyncio.sleep(self.response_cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Errore nel cleanup periodico cache: %s", e)
        finally:
            logger.info("Task cleanup cache terminato")

    async def _stop_cleanup_task(self):
        task = self._cleanup_task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._cleanup_task = None

    async def _stop_worker(self):
        task = self._worker_task
        self.processing = False

        if task is None:
            await self._stop_cleanup_task()
            return

        if not task.done():
            try:
                self.request_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.warning("Coda piena durante stop worker; cancello task worker senza sentinel")
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=15)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("Timeout fermando il worker, forzo cancellazione task")
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        self._worker_task = None
        self.pending_request_ids.clear()
        await self._stop_cleanup_task()

    def _is_response_expired(self, response: VisuraResponse) -> bool:
        return (datetime.now() - response.timestamp).total_seconds() > self.response_ttl_seconds

    def _cleanup_response_store(self):
        expired_ids = [rid for rid, resp in self.response_store.items() if self._is_response_expired(resp)]
        for request_id in expired_ids:
            self.response_store.pop(request_id, None)
            self._mark_request_expired(request_id)

        while len(self.response_store) > self.response_max_items:
            oldest_request_id = next(iter(self.response_store))
            self.response_store.pop(oldest_request_id, None)
            self._mark_request_expired(oldest_request_id)

    def _mark_request_expired(self, request_id: str):
        self.expired_request_ids[request_id] = datetime.now()
        while len(self.expired_request_ids) > self.response_max_items:
            oldest_request_id = next(iter(self.expired_request_ids))
            self.expired_request_ids.pop(oldest_request_id, None)

    async def _store_response(self, response: VisuraResponse):
        from .database import is_db_writable

        self.response_store[response.request_id] = response
        self.expired_request_ids.pop(response.request_id, None)
        self._cleanup_response_store()
        if is_db_writable():
            await save_response(
                request_id=response.request_id,
                success=response.success,
                tipo_catasto=response.cadastre_type,
                data=response.data,
                error=response.error,
            )

    @staticmethod
    def _response_from_db_record(record: dict) -> VisuraResponse:
        timestamp_raw = record.get("created_at")
        timestamp = datetime.now()
        if isinstance(timestamp_raw, str):
            with suppress(ValueError):
                timestamp = datetime.fromisoformat(timestamp_raw)

        return VisuraResponse(
            request_id=record["request_id"],
            success=bool(record["success"]),
            cadastre_type=record["tipo_catasto"],
            data=record.get("data"),
            error=record.get("error"),
            timestamp=timestamp,
        )

    @staticmethod
    def _request_cache_params(request_type: str, request) -> dict:
        params = {"cadastre_type": getattr(request, "cadastre_type", "")}
        for attr in (
            "province",
            "municipality",
            "sheet",
            "parcel",
            "section",
            "subunit",
            "fiscal_code",
            "identifier",
            "search_type",
        ):
            val = getattr(request, attr, None)
            if val:
                params[attr] = val
        if hasattr(request, "params") and request.params:
            params.update(request.params)
        return params

    async def _check_cache(self, request_type: str, request, force: bool = False) -> Optional[SubmitResult]:
        if force:
            return None
        params = self._request_cache_params(request_type, request)
        cache_key = compute_cache_key(request_type, **params)
        record = await find_cached_response(cache_key, self.response_ttl_seconds)
        if record is None:
            return None
        response = self._response_from_db_record(record)
        logger.info("Cache hit for %s %s (key=%s...)", request_type, request.request_id, cache_key[:12])
        return SubmitResult(request_id=record["request_id"], cached=True, response=response)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_single_request(
        self, request_type: str, request: VisuraRequest | VisuraIntestatiRequest | VisuraSoggettoRequest
    ):
        from .database import is_db_writable

        if not is_db_writable():
            return
        params = self._request_cache_params(request_type, request)
        cache_key = compute_cache_key(request_type, **params)
        try:
            await save_request(
                request_id=request.request_id,
                request_type=request_type,
                tipo_catasto=request.cadastre_type,
                provincia=getattr(request, "province", None) or "",
                comune=getattr(request, "municipality", None) or "",
                foglio=getattr(request, "sheet", None) or "",
                particella=getattr(request, "parcel", None) or getattr(request, "fiscal_code", ""),
                sezione=getattr(request, "section", None),
                subalterno=getattr(request, "subunit", None),
                cache_key=cache_key,
            )
        except Exception as e:
            logger.error("Errore persistenza richiesta %s: %s", request.request_id, e)
            raise RuntimeError("Errore durante il salvataggio della richiesta") from e

    async def _persist_request_batch(self, requests: list[VisuraRequest]):
        from .database import is_db_writable

        if not is_db_writable():
            return
        rows = [
            {
                "request_id": request.request_id,
                "request_type": "visura",
                "tipo_catasto": request.cadastre_type,
                "provincia": request.province,
                "comune": request.municipality,
                "foglio": request.sheet,
                "particella": request.parcel,
                "sezione": request.section,
                "subalterno": request.subunit,
            }
            for request in requests
        ]
        try:
            await save_requests_batch(rows)
        except Exception as e:
            logger.error("Errore persistenza batch richieste (%d item): %s", len(requests), e)
            raise RuntimeError("Errore durante il salvataggio delle richieste") from e

    # ------------------------------------------------------------------
    # Queue management (public API)
    # ------------------------------------------------------------------

    def _queue_limit(self) -> int:
        queue_maxsize = self.request_queue.maxsize
        return queue_maxsize if queue_maxsize > 0 else self.queue_max_size

    def _ensure_processing(self):
        if not self.processing:
            raise RuntimeError("Servizio non in esecuzione: impossibile accodare richieste")

    def _ensure_capacity(self, required_slots: int):
        queue_maxsize = self.request_queue.maxsize
        if queue_maxsize > 0 and self.request_queue.qsize() + required_slots > queue_maxsize:
            raise QueueFullError(f"Coda piena (max {self._queue_limit()})")

    def _enqueue_request_nowait(self, request):
        self.pending_request_ids.add(request.request_id)
        self.expired_request_ids.pop(request.request_id, None)
        try:
            self.request_queue.put_nowait(request)
        except asyncio.QueueFull as e:
            self.pending_request_ids.discard(request.request_id)
            raise QueueFullError(f"Coda piena (max {self._queue_limit()})") from e

    async def _add_single(self, request_type: str, request, force: bool = False) -> SubmitResult:
        cached = await self._check_cache(request_type, request, force=force)
        if cached is not None:
            return cached
        async with self._queue_lock:
            self._ensure_processing()
            self._ensure_capacity(required_slots=1)
            await self._persist_single_request(request_type, request)
            self._enqueue_request_nowait(request)
        logger.info(
            "Richiesta %s %s aggiunta alla coda (posizione: %d)",
            request_type,
            request.request_id,
            self.request_queue.qsize(),
        )
        return SubmitResult(request_id=request.request_id)

    async def add_request(self, request: VisuraRequest, force: bool = False) -> str | SubmitResult:
        return await self._add_single("visura", request, force=force)

    async def add_intestati_request(self, request: VisuraIntestatiRequest, force: bool = False) -> str | SubmitResult:
        return await self._add_single("intestati", request, force=force)

    async def add_soggetto_request(self, request: VisuraSoggettoRequest, force: bool = False) -> str | SubmitResult:
        return await self._add_single("soggetto", request, force=force)

    async def add_persona_giuridica_request(
        self, request: VisuraPersonaGiuridicaRequest, force: bool = False
    ) -> str | SubmitResult:
        return await self._add_single("persona_giuridica", request, force=force)

    async def add_generic_request(self, request: GenericSisterRequest, force: bool = False) -> str | SubmitResult:
        return await self._add_single(request.search_type, request, force=force)

    async def add_ispezione_ipotecaria_request(
        self, request: IspezioneIpotecariaRequest, force: bool = False
    ) -> str | SubmitResult:
        return await self._add_single(f"ipotecaria_{request.search_type}", request, force=force)

    async def add_elenco_immobili_request(
        self, request: ElencoImmobiliRequest, force: bool = False
    ) -> str | SubmitResult:
        return await self._add_single("elenco_immobili", request, force=force)

    async def add_requests_batch(self, requests: list[VisuraRequest], force: bool = False) -> list[str | SubmitResult]:
        if not requests:
            return []

        results: list[str | SubmitResult] = []
        to_enqueue: list[VisuraRequest] = []

        for request in requests:
            if not force:
                cached = await self._check_cache("visura", request, force=False)
                if cached is not None:
                    results.append(cached)
                    continue
            to_enqueue.append(request)
            results.append(SubmitResult(request_id=request.request_id))

        if to_enqueue:
            async with self._queue_lock:
                self._ensure_processing()
                self._ensure_capacity(required_slots=len(to_enqueue))
                await self._persist_request_batch(to_enqueue)
                for request in to_enqueue:
                    self._enqueue_request_nowait(request)
            for request in to_enqueue:
                logger.info("Richiesta visura %s aggiunta alla coda", request.request_id)

        return results

    # ------------------------------------------------------------------
    # Response query
    # ------------------------------------------------------------------

    async def get_response(self, request_id: str) -> Optional[VisuraResponse]:
        response = self.response_store.get(request_id)
        if response is None:
            try:
                record = await load_stored_response(request_id)
            except Exception as e:
                logger.warning("Errore lettura risposta da database per %s: %s", request_id, e)
                record = None
            if record is not None:
                response = self._response_from_db_record(record)
                self.response_store[request_id] = response

        if response and self._is_response_expired(response):
            self.response_store.pop(request_id, None)
            self._mark_request_expired(request_id)
            return None
        return response

    def get_request_state(self, request_id: str) -> str:
        self._cleanup_response_store()
        if request_id in self.response_store:
            return "completed"
        if request_id in self.pending_request_ids:
            return "processing"
        if request_id in self.expired_request_ids:
            return "expired"
        return "not_found"

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self):
        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            auth_task.cancel()
            try:
                await auth_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._stop_worker()
        await self.browser_manager.close()

    async def graceful_shutdown(self):
        logger.info("Iniziando graceful shutdown del servizio...")

        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            auth_task.cancel()
            try:
                await auth_task
            except (asyncio.CancelledError, Exception):
                pass

        await self._stop_worker()
        await self.browser_manager.graceful_shutdown()
        logger.info("Graceful shutdown del servizio completato")

    # ------------------------------------------------------------------
    # Browser session control (manual start/stop/restart)
    # ------------------------------------------------------------------

    async def start_browser(self) -> dict:
        """Start browser authentication if not already running or ready."""
        if self.auth_ready:
            return self.auth_status
        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            return self.auth_status  # already authenticating
        # Clear any previous failure so auth_status shows "connecting"
        if hasattr(self, "_auth_failed_message"):
            del self._auth_failed_message
        self._auth_task = asyncio.create_task(self._background_auth(), name="visura-browser-auth")
        logger.info("Browser auth avviato manualmente")
        return self.auth_status

    async def stop_browser(self, force: bool = False) -> dict:
        """Close the browser session. If force=False, waits for the queue to drain first."""
        if not force and not self.request_queue.empty():
            await self.request_queue.join()
        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            auth_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await auth_task
        await self.browser_manager.close()
        self._auth_ready = False
        logger.info("Browser session fermata (force=%s)", force)
        return self.auth_status

    async def restart_browser(self) -> dict:
        """Stop then immediately restart the browser session."""
        await self.stop_browser(force=True)
        return await self.start_browser()
