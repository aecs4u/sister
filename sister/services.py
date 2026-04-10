"""Service layer: BrowserManager and VisuraService for sister."""

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime
from typing import Dict, Optional

from aecs4u_auth.browser import BrowserConfig, PageLogger  # noqa: F401 (PageLogger re-exported)
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager
from playwright.async_api import Page

from .database import (
    cleanup_old_responses,
    save_request,
    save_requests_batch,
    save_response,
)
from .database import (
    get_response as load_stored_response,
)
from .models import (
    AuthenticationError,
    BrowserError,
    QueueFullError,
    VisuraIntestatiRequest,
    VisuraRequest,
    VisuraResponse,
)
from .utils import extract_all_sezioni, run_visura, run_visura_immobile

logger = logging.getLogger("sister")


class BrowserManager:
    def __init__(self):
        self._auth = AuthBrowserManager(BrowserConfig())
        self.last_login_time = None
        self._page_lock = asyncio.Lock()

    @property
    def authenticated(self) -> bool:
        return self._auth.is_authenticated

    @property
    def auth_page(self) -> Optional[Page]:
        session = self._auth.session
        if session and session.is_valid:
            return session.page
        return None

    async def initialize(self):
        """Inizializza il browser e il contexto"""
        try:
            await self._auth.initialize()
            logger.info("Browser inizializzato")
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            raise BrowserError(f"Browser initialization failed: {e}") from e

    async def login(self):
        """Esegue il login SPID e naviga a SISTER"""
        try:
            await self._auth.login(service="sister")
            self.last_login_time = datetime.now()
            logger.info("Login completato con successo")
        except Exception as e:
            logger.error(f"Errore durante il login: {e}")
            raise AuthenticationError(f"Login failed: {e}") from e

    async def start_keep_alive(self):
        """Mantiene la sessione attiva"""
        await self._auth.start_keepalive()

    async def stop_keep_alive(self):
        """Ferma il keep-alive"""
        await self._auth.stop_keepalive()

    async def _ensure_authenticated(self):
        """Assicura che il sistema sia autenticato, ri-autentica se necessario."""
        try:
            await self._auth.ensure_authenticated()
            self.last_login_time = datetime.now()
        except Exception as e:
            logger.error(f"Errore nella re-autenticazione: {e}")
            raise AuthenticationError(f"Re-authentication failed: {e}") from e

    async def _get_authenticated_page(self) -> Page:
        await self._ensure_authenticated()
        page = self.auth_page
        if page is None:
            raise AuthenticationError("Sessione autenticata non disponibile")
        return page

    async def esegui_visura(self, request: VisuraRequest) -> VisuraResponse:
        """Esegue una visura catastale (solo dati catastali, senza intestati)"""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()

                try:
                    result = await run_visura(
                        page,
                        request.provincia,
                        request.comune,
                        request.sezione,
                        request.foglio,
                        request.particella,
                        request.tipo_catasto,
                        extract_intestati=False,
                        subalterno=request.subalterno,
                    )
                except Exception as e:
                    raise BrowserError(f"Failed to execute visura: {e}") from e

            logger.info(f"Visura completata per request {request.request_id}")
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )

        except (AuthenticationError, BrowserError) as e:
            logger.error(f"Errore in visura {request.request_id}: {e}")
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Errore inatteso in visura {request.request_id}: {e}")
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_visura_intestati(self, request: VisuraIntestatiRequest) -> VisuraResponse:
        """Esegue una visura per ottenere gli intestati di un immobile specifico."""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()

                if request.tipo_catasto == "F" and request.subalterno:
                    result = await run_visura_immobile(
                        page,
                        provincia=request.provincia,
                        comune=request.comune,
                        sezione=request.sezione,
                        foglio=request.foglio,
                        particella=request.particella,
                        subalterno=request.subalterno,
                    )
                else:
                    result = await run_visura(
                        page,
                        request.provincia,
                        request.comune,
                        request.sezione,
                        request.foglio,
                        request.particella,
                        request.tipo_catasto,
                        extract_intestati=True,
                    )

            logger.info(f"Visura intestati completata per {request.request_id}")
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )

        except Exception as e:
            logger.error(f"Errore in visura intestati {request.request_id}: {e}")
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                error=str(e),
            )

    async def esegui_extract_sezioni(self, tipo_catasto: str, max_province: int) -> list:
        """Esegue l'estrazione sezioni in modo esclusivo sulla sessione browser condivisa."""
        async with self._page_lock:
            page = await self._get_authenticated_page()
            return await extract_all_sezioni(page, tipo_catasto, max_province)

    async def close(self):
        """Chiude il browser"""
        await self._auth.close()
        logger.info("Browser chiuso")

    async def graceful_shutdown(self):
        """Effettua uno shutdown graceful con logout"""
        logger.info("Iniziando shutdown graceful...")
        await self._auth.graceful_shutdown()
        logger.info("Shutdown graceful completato")


class VisuraService:
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
            logger.warning(f"{var_name} non valido ({raw!r}), uso default={default}")
            return default

    async def initialize(self):
        """Inizializza il servizio"""
        await self.browser_manager.initialize()
        await self.browser_manager.login()
        await self.browser_manager.start_keep_alive()

        # Avvia il worker per processare le richieste
        self.processing = True
        self._worker_task = asyncio.create_task(self._process_requests(), name="visura-request-worker")
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup(), name="visura-cache-cleanup")

    async def _process_requests(self):
        """Processa le richieste in coda"""
        try:
            while True:
                request = await self.request_queue.get()
                should_sleep = False

                try:
                    if request is None:
                        logger.info("Ricevuto segnale di stop worker")
                        return

                    if isinstance(request, VisuraRequest):
                        response = await self.browser_manager.esegui_visura(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta visura {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, VisuraIntestatiRequest):
                        response = await self.browser_manager.esegui_visura_intestati(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta intestati {request.request_id}")
                        should_sleep = True

                    else:
                        logger.error(f"Tipo di richiesta sconosciuto: {type(request)}")

                except Exception as e:
                    logger.error(f"Errore nel processare richieste: {e}")
                    await asyncio.sleep(5)
                finally:
                    if isinstance(request, (VisuraRequest, VisuraIntestatiRequest)):
                        self.pending_request_ids.discard(request.request_id)
                    self.request_queue.task_done()

                # Pausa tra le richieste per non sovraccaricare SISTER
                if should_sleep:
                    await asyncio.sleep(2)
        finally:
            self.processing = False
            logger.info("Worker richieste terminato")

    async def _periodic_cleanup(self):
        """Pulisce periodicamente le risposte scadute in cache e nel database."""
        try:
            while self.processing:
                self._cleanup_response_store()
                try:
                    deleted = await cleanup_old_responses(self.response_ttl_seconds)
                    if deleted:
                        logger.info(f"Cleanup database: rimossi {deleted} record scaduti")
                except Exception as e:
                    logger.warning(f"Errore cleanup database: {e}")
                await asyncio.sleep(self.response_cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Errore nel cleanup periodico cache: {e}")
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
        """Ferma il worker in modo pulito e attende la sua terminazione."""
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
        age_seconds = (datetime.now() - response.timestamp).total_seconds()
        return age_seconds > self.response_ttl_seconds

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
        self.response_store[response.request_id] = response
        self.expired_request_ids.pop(response.request_id, None)
        self._cleanup_response_store()
        await save_response(
            request_id=response.request_id,
            success=response.success,
            tipo_catasto=response.tipo_catasto,
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
            tipo_catasto=record["tipo_catasto"],
            data=record.get("data"),
            error=record.get("error"),
            timestamp=timestamp,
        )

    async def _persist_single_request(self, request_type: str, request: VisuraRequest | VisuraIntestatiRequest):
        try:
            await save_request(
                request_id=request.request_id,
                request_type=request_type,
                tipo_catasto=request.tipo_catasto,
                provincia=request.provincia,
                comune=request.comune,
                foglio=request.foglio,
                particella=request.particella,
                sezione=request.sezione,
                subalterno=request.subalterno,
            )
        except Exception as e:
            logger.error(f"Errore persistenza richiesta {request.request_id}: {e}")
            raise RuntimeError("Errore durante il salvataggio della richiesta") from e

    async def _persist_request_batch(self, requests: list[VisuraRequest]):
        rows = [
            {
                "request_id": request.request_id,
                "request_type": "visura",
                "tipo_catasto": request.tipo_catasto,
                "provincia": request.provincia,
                "comune": request.comune,
                "foglio": request.foglio,
                "particella": request.particella,
                "sezione": request.sezione,
                "subalterno": request.subalterno,
            }
            for request in requests
        ]
        try:
            await save_requests_batch(rows)
        except Exception as e:
            logger.error(f"Errore persistenza batch richieste ({len(requests)} item): {e}")
            raise RuntimeError("Errore durante il salvataggio delle richieste") from e

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

    def _enqueue_request_nowait(self, request: VisuraRequest | VisuraIntestatiRequest):
        self.pending_request_ids.add(request.request_id)
        self.expired_request_ids.pop(request.request_id, None)
        try:
            self.request_queue.put_nowait(request)
        except asyncio.QueueFull as e:
            self.pending_request_ids.discard(request.request_id)
            raise QueueFullError(f"Coda piena (max {self._queue_limit()})") from e

    async def add_request(self, request: VisuraRequest) -> str:
        """Aggiunge una richiesta alla coda"""
        async with self._queue_lock:
            self._ensure_processing()
            self._ensure_capacity(required_slots=1)
            await self._persist_single_request("visura", request)
            self._enqueue_request_nowait(request)
        logger.info(
            f"Richiesta visura {request.request_id} aggiunta alla coda (posizione: {self.request_queue.qsize()})"
        )
        return request.request_id

    async def add_intestati_request(self, request: VisuraIntestatiRequest) -> str:
        """Aggiunge una richiesta intestati alla coda"""
        async with self._queue_lock:
            self._ensure_processing()
            self._ensure_capacity(required_slots=1)
            await self._persist_single_request("intestati", request)
            self._enqueue_request_nowait(request)
        logger.info(
            f"Richiesta intestati {request.request_id} aggiunta alla coda (posizione: {self.request_queue.qsize()})"
        )
        return request.request_id

    async def add_requests_batch(self, requests: list[VisuraRequest]) -> list[str]:
        """Accoda più richieste in modo atomico lato producer."""
        if not requests:
            return []

        async with self._queue_lock:
            self._ensure_processing()
            self._ensure_capacity(required_slots=len(requests))
            await self._persist_request_batch(requests)
            for request in requests:
                self._enqueue_request_nowait(request)

        for request in requests:
            logger.info(f"Richiesta visura {request.request_id} aggiunta alla coda")
        return [request.request_id for request in requests]

    async def get_response(self, request_id: str) -> Optional[VisuraResponse]:
        """Ottiene la risposta per un request_id"""
        response = self.response_store.get(request_id)
        if response is None:
            try:
                record = await load_stored_response(request_id)
            except Exception as e:
                logger.warning(f"Errore lettura risposta da database per {request_id}: {e}")
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

    async def shutdown(self):
        """Chiude il servizio"""
        await self._stop_worker()
        await self.browser_manager.close()

    async def graceful_shutdown(self):
        """Chiude il servizio con logout graceful"""
        logger.info("Iniziando graceful shutdown del servizio...")
        await self._stop_worker()
        await self.browser_manager.graceful_shutdown()
        logger.info("Graceful shutdown del servizio completato")
