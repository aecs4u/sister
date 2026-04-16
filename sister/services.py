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
    AuthenticationError,
    BrowserError,
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
from .utils import (
    extract_all_sezioni,
    run_consultazione_richieste,
    run_elaborato_planimetrico,
    run_elenco_immobili,
    run_export_mappa,
    run_ispezione_ipotecaria,
    run_ispezioni,
    run_ispezioni_cartacee,
    run_ispezioni_ipotecarie_elenchi,
    run_ispezioni_ipotecarie_stato,
    run_originali_impianto,
    run_punti_fiduciali,
    run_ricerca_indirizzo,
    run_ricerca_mappa,
    run_ricerca_nota,
    run_ricerca_partita,
    run_riepilogo_visure,
    run_visura,
    run_visura_immobile,
    run_visura_persona_giuridica,
    run_visura_soggetto,
)

_GENERIC_DISPATCHERS = {
    "indirizzo": run_ricerca_indirizzo,
    "partita": run_ricerca_partita,
    "nota": run_ricerca_nota,
    "mappa": run_ricerca_mappa,
    "export_mappa": run_export_mappa,
    "originali": run_originali_impianto,
    "fiduciali": run_punti_fiduciali,
    "ispezioni": run_ispezioni,
    "ispezioni_cart": run_ispezioni_cartacee,
    "elaborato_planimetrico": run_elaborato_planimetrico,
}

# No-args dispatchers (riepilogo, richieste — don't take standard search params)
_NOARGS_DISPATCHERS = {
    "riepilogo_visure": run_riepilogo_visure,
    "richieste": run_consultazione_richieste,
    "ipotecaria_stato": run_ispezioni_ipotecarie_stato,
    "ipotecaria_elenchi": run_ispezioni_ipotecarie_elenchi,
}

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
    def is_cdp(self) -> bool:
        """True when connected to a shared browser via CDP."""
        return self._auth.is_cdp

    @property
    def auth_page(self) -> Optional[Page]:
        session = self._auth.session
        if session and session.is_valid:
            return session.page
        return None

    async def initialize(self):
        """Initialize the browser.

        When BROWSER_CDP_ENDPOINT is set in the environment, aecs4u-auth
        connects to a running Chrome/Chromium process via CDP instead of
        launching a new one.  This allows sister and opendata (or any other
        service using aecs4u-auth) to share the same browser and session.
        """
        try:
            if not self._auth.config.cdp_endpoint:
                # Local launch — inject --start-maximized for full viewport
                from aecs4u_auth.browser import manager as _auth_manager
                if hasattr(_auth_manager, '_CHROMIUM_ARGS'):
                    if "--start-maximized" not in _auth_manager._CHROMIUM_ARGS:
                        _auth_manager._CHROMIUM_ARGS.append("--start-maximized")

            await self._auth.initialize()

            # For local launch, re-create context with no_viewport=True
            if not self.is_cdp:
                browser = self._auth._browser
                if browser:
                    _orig_new_context = browser.new_context
                    self._auth._auth_page = None
                    if self._auth._context:
                        await self._auth._context.close()
                    self._auth._context = await _orig_new_context(no_viewport=True)

            mode = "CDP" if self.is_cdp else "local"
            logger.info("Browser inizializzato (%s)", mode)
        except Exception as e:
            logger.error("Failed to initialize browser: %s", e)
            raise BrowserError(f"Browser initialization failed: {e}") from e

    async def login(self):
        """Esegue il login SPID e naviga a SISTER"""
        try:
            await self._auth.login(service="sister")
            self.last_login_time = datetime.now()
            logger.info("Login completato con successo")
        except Exception as e:
            logger.error("Errore durante il login: %s", e)
            raise AuthenticationError(f"Login failed: {e}") from e

    async def start_keep_alive(self):
        await self._auth.start_keepalive()

    async def stop_keep_alive(self):
        await self._auth.stop_keepalive()

    async def _ensure_authenticated(self):
        try:
            await self._auth.ensure_authenticated()
            self.last_login_time = datetime.now()
        except Exception as e:
            logger.error("Errore nella re-autenticazione: %s", e)
            raise AuthenticationError(f"Re-authentication failed: {e}") from e

    async def _get_authenticated_page(self) -> Page:
        # In CDP mode, check if the connection is still alive and reconnect if needed
        if self.is_cdp and (self._auth._browser is None or not self._auth._browser.is_connected()):
            logger.warning("CDP connection lost — reconnecting")
            await self._auth.initialize()
            await self.login()
            await self.start_keep_alive()

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
                        sezione_urbana=request.sezione_urbana,
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
                        sezione_urbana=request.sezione_urbana,
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
                        sezione_urbana=request.sezione_urbana,
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

    async def esegui_visura_soggetto(self, request: VisuraSoggettoRequest) -> VisuraResponse:
        """Esegue una ricerca nazionale per soggetto (codice fiscale)."""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                try:
                    result = await run_visura_soggetto(
                        page,
                        codice_fiscale=request.codice_fiscale,
                        tipo_catasto=request.tipo_catasto,
                        provincia=request.provincia,
                        motivo="Esplorazione",
                    )
                except Exception as e:
                    raise BrowserError(f"Failed to execute soggetto search: {e}") from e

            logger.info("Ricerca soggetto completata per request %s", request.request_id)
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )

        except (AuthenticationError, BrowserError) as e:
            logger.error("Errore in ricerca soggetto %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                error=str(e),
            )
        except Exception as e:
            logger.error("Errore inatteso in ricerca soggetto %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_visura_persona_giuridica(self, request: VisuraPersonaGiuridicaRequest) -> VisuraResponse:
        """Esegue una ricerca per persona giuridica (P.IVA o denominazione)."""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                try:
                    result = await run_visura_persona_giuridica(
                        page,
                        identificativo=request.identificativo,
                        tipo_catasto=request.tipo_catasto,
                        provincia=request.provincia,
                        motivo="Esplorazione",
                    )
                except Exception as e:
                    raise BrowserError(f"Failed to execute PNF search: {e}") from e

            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            logger.error("Errore in ricerca PNF %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=str(e),
            )
        except Exception as e:
            logger.error("Errore inatteso in ricerca PNF %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_elenco_immobili(self, request: ElencoImmobiliRequest) -> VisuraResponse:
        """Esegue un elenco immobili per un comune."""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                try:
                    result = await run_elenco_immobili(
                        page,
                        provincia=request.provincia,
                        comune=request.comune,
                        tipo_catasto=request.tipo_catasto,
                        foglio=request.foglio,
                        sezione=request.sezione,
                        motivo="Esplorazione",
                    )
                except Exception as e:
                    raise BrowserError(f"Failed to execute EIMM: {e}") from e

            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            logger.error("Errore in elenco immobili %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=str(e),
            )
        except Exception as e:
            logger.error("Errore inatteso in elenco immobili %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_generic(self, request: GenericSisterRequest) -> VisuraResponse:
        """Execute a generic SISTER search."""
        dispatcher = _GENERIC_DISPATCHERS.get(request.search_type)
        noargs_dispatcher = _NOARGS_DISPATCHERS.get(request.search_type)

        if not dispatcher and not noargs_dispatcher:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto,
                error=f"Unknown search type: {request.search_type}",
            )
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                if noargs_dispatcher:
                    result = await noargs_dispatcher(page)
                else:
                    kwargs = {
                        "page": page,
                        "provincia": request.provincia,
                        **({"comune": request.comune} if request.comune else {}),
                        **({"tipo_catasto": request.tipo_catasto} if request.tipo_catasto else {}),
                        **request.params,
                    }
                    result = await dispatcher(**kwargs)

            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            logger.error("Errore in %s %s: %s", request.search_type, request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=str(e),
            )
        except Exception as e:
            logger.error("Errore inatteso in %s %s: %s", request.search_type, request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_ispezione_ipotecaria(self, request: IspezioneIpotecariaRequest) -> VisuraResponse:
        """Execute an Ispezione Ipotecaria (paid inspection)."""
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                try:
                    result = await run_ispezione_ipotecaria(
                        page,
                        provincia=request.provincia,
                        comune=request.comune,
                        tipo_ricerca=request.tipo_ricerca,
                        codice_fiscale=request.codice_fiscale,
                        identificativo=request.identificativo,
                        foglio=request.foglio,
                        particella=request.particella,
                        numero_nota=request.numero_nota,
                        anno_nota=request.anno_nota,
                        tipo_catasto=request.tipo_catasto,
                        auto_confirm=request.auto_confirm,
                    )
                except Exception as e:
                    raise BrowserError(f"Failed to execute ispezione ipotecaria: {e}") from e

            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            logger.error("Errore in ispezione ipotecaria %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=str(e),
            )
        except Exception as e:
            logger.error("Errore inatteso in ispezione ipotecaria %s: %s", request.request_id, e)
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, error=f"Errore inatteso: {str(e)}",
            )

    async def esegui_extract_sezioni(self, tipo_catasto: str, max_province: int) -> list:
        """Esegue l'estrazione sezioni in modo esclusivo sulla sessione browser condivisa."""
        async with self._page_lock:
            page = await self._get_authenticated_page()
            return await extract_all_sezioni(page, tipo_catasto, max_province)

    async def download_richieste_documents(self) -> list[dict]:
        """Download all available documents from the SISTER Richieste page."""
        from .utils import _download_richieste_documents, PageLogger
        async with self._page_lock:
            page = await self._get_authenticated_page()
            page_logger = PageLogger("download_richieste")
            return await _download_richieste_documents(page, page_logger)

    async def close(self):
        """Close browser resources (delegates to aecs4u-auth)."""
        await self._auth.close()
        logger.info("Browser chiuso")

    async def graceful_shutdown(self):
        """Logout from SISTER session, then close browser resources."""
        logger.info("Iniziando shutdown graceful...")
        # Close SISTER sessions before the general logout
        try:
            page = self.auth_page
            if page and not page.is_closed():
                for url in [
                    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessionsSis",
                    "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessions",
                ]:
                    with suppress(Exception):
                        await page.goto(url, timeout=10000)
                        logger.info("Sessione SISTER chiusa: %s", url)
        except Exception as e:
            logger.warning("Errore chiusura sessione SISTER: %s", e)
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

    async def initialize(self, background_auth: bool = True):
        """Inizializza il servizio.

        Args:
            background_auth: If True, browser init + login run in a background task
                so the app starts immediately. Queries wait until auth completes.
        """
        self.processing = True
        self._worker_task = asyncio.create_task(self._process_requests(), name="visura-request-worker")
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup(), name="visura-cache-cleanup")

        if background_auth:
            self._auth_task = asyncio.create_task(self._background_auth(), name="visura-browser-auth")
        else:
            await self._do_auth()

    async def _do_auth(self):
        """Run browser initialization and login."""
        await self.browser_manager.initialize()
        await self.browser_manager.login()
        await self.browser_manager.start_keep_alive()
        self._auth_ready = True
        logger.info("Browser autenticato e pronto")

    async def _background_auth(self):
        """Background task: initialize browser and login with retry.

        On "active session" errors, attempts to close the stale session first
        by navigating to the SISTER logout URL.
        """
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                logger.info("Autenticazione browser in corso (tentativo %d/%d)...", attempt, max_retries)
                await self._do_auth()
                return
            except Exception as e:
                err_msg = str(e)
                logger.error("Autenticazione fallita (tentativo %d/%d): %s", attempt, max_retries, err_msg)

                # If session locked, try to close it via logout URL
                if "active session" in err_msg.lower() or "già in sessione" in err_msg.lower():
                    await self._try_close_stale_session()

                if attempt < max_retries:
                    wait = 15 * attempt
                    logger.info("Riprovo tra %ds...", wait)
                    await asyncio.sleep(wait)
        self._auth_failed_message = f"Authentication failed after {max_retries} attempts"
        logger.error("Autenticazione browser fallita dopo %d tentativi — le query browser non funzioneranno", max_retries)

    async def _try_close_stale_session(self):
        """Attempt to close a stale SISTER session by hitting the logout URL."""
        try:
            page = self.browser_manager._auth._auth_page
            if not page or page.is_closed():
                # Create a temporary page
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
                await page.close()

        except Exception as e:
            logger.warning("Impossibile chiudere sessione stale: %s", e)

    @property
    def auth_ready(self) -> bool:
        return getattr(self, "_auth_ready", False)

    @property
    def auth_status(self) -> dict:
        """Structured auth/browser health state for /health and dashboard."""
        mode = "cdp" if self.browser_manager.is_cdp else "local"
        if self.auth_ready:
            return {"state": "ready", "mode": mode, "message": "Browser authenticated"}
        failed_msg = getattr(self, "_auth_failed_message", None)
        if failed_msg:
            return {"state": "unavailable", "mode": mode, "message": failed_msg}
        auth_task = getattr(self, "_auth_task", None)
        if auth_task is not None and not auth_task.done():
            return {"state": "connecting", "mode": mode, "message": "Authentication in progress..."}
        return {"state": "unavailable", "mode": mode, "message": "Browser not initialized"}

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

                    # Wait for auth to be ready before processing browser requests
                    if not self.auth_ready:
                        logger.info("In attesa dell'autenticazione browser...")
                        for _ in range(60):  # wait up to 5 minutes
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
                        logger.info(f"Processata richiesta visura {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, VisuraIntestatiRequest):
                        response = await self.browser_manager.esegui_visura_intestati(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta intestati {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, VisuraSoggettoRequest):
                        response = await self.browser_manager.esegui_visura_soggetto(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta soggetto {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, VisuraPersonaGiuridicaRequest):
                        response = await self.browser_manager.esegui_visura_persona_giuridica(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta PNF {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, ElencoImmobiliRequest):
                        response = await self.browser_manager.esegui_elenco_immobili(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta elenco immobili {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, IspezioneIpotecariaRequest):
                        response = await self.browser_manager.esegui_ispezione_ipotecaria(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta ipotecaria {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, GenericSisterRequest):
                        response = await self.browser_manager.esegui_generic(request)
                        await self._store_response(response)
                        logger.info(f"Processata richiesta {request.search_type} {request.request_id}")
                        should_sleep = True

                    else:
                        logger.error(f"Tipo di richiesta sconosciuto: {type(request)}")

                except Exception as e:
                    logger.error(f"Errore nel processare richieste: {e}")
                    await asyncio.sleep(5)
                finally:
                    if isinstance(request, (VisuraRequest, VisuraIntestatiRequest, VisuraSoggettoRequest, VisuraPersonaGiuridicaRequest, ElencoImmobiliRequest, GenericSisterRequest, IspezioneIpotecariaRequest)):
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

    @staticmethod
    def _request_cache_params(request_type: str, request) -> dict:
        """Extract cache-relevant parameters from a request."""
        params = {"tipo_catasto": getattr(request, "tipo_catasto", "")}
        for attr in ("provincia", "comune", "foglio", "particella", "sezione", "subalterno",
                      "codice_fiscale", "identificativo", "search_type"):
            val = getattr(request, attr, None)
            if val:
                params[attr] = val
        # For GenericSisterRequest, include the params dict
        if hasattr(request, "params") and request.params:
            params.update(request.params)
        return params

    async def _check_cache(self, request_type: str, request, force: bool = False) -> Optional[SubmitResult]:
        """Check if a cached response exists. Returns SubmitResult if cached, None otherwise."""
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

    async def _persist_single_request(
        self, request_type: str, request: VisuraRequest | VisuraIntestatiRequest | VisuraSoggettoRequest
    ):
        params = self._request_cache_params(request_type, request)
        cache_key = compute_cache_key(request_type, **params)
        try:
            await save_request(
                request_id=request.request_id,
                request_type=request_type,
                tipo_catasto=request.tipo_catasto,
                provincia=getattr(request, "provincia", None) or "",
                comune=getattr(request, "comune", None) or "",
                foglio=getattr(request, "foglio", None) or "",
                particella=getattr(request, "particella", None) or getattr(request, "codice_fiscale", ""),
                sezione=getattr(request, "sezione", None),
                subalterno=getattr(request, "subalterno", None),
                cache_key=cache_key,
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

    async def _add_single(self, request_type: str, request, force: bool = False) -> SubmitResult:
        """Generic add: check cache, then enqueue if needed."""
        cached = await self._check_cache(request_type, request, force=force)
        if cached is not None:
            return cached
        async with self._queue_lock:
            self._ensure_processing()
            self._ensure_capacity(required_slots=1)
            await self._persist_single_request(request_type, request)
            self._enqueue_request_nowait(request)
        logger.info(
            f"Richiesta {request_type} {request.request_id} aggiunta alla coda (posizione: {self.request_queue.qsize()})"
        )
        return SubmitResult(request_id=request.request_id)

    async def add_request(self, request: VisuraRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta alla coda (or returns cached)."""
        return await self._add_single("visura", request, force=force)

    async def add_intestati_request(self, request: VisuraIntestatiRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta intestati alla coda."""
        return await self._add_single("intestati", request, force=force)

    async def add_soggetto_request(self, request: VisuraSoggettoRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta soggetto alla coda."""
        return await self._add_single("soggetto", request, force=force)

    async def add_persona_giuridica_request(self, request: VisuraPersonaGiuridicaRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta persona giuridica alla coda."""
        return await self._add_single("persona_giuridica", request, force=force)

    async def add_generic_request(self, request: GenericSisterRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta generica alla coda."""
        return await self._add_single(request.search_type, request, force=force)

    async def add_ispezione_ipotecaria_request(self, request: IspezioneIpotecariaRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta ispezione ipotecaria alla coda."""
        return await self._add_single(f"ipotecaria_{request.tipo_ricerca}", request, force=force)

    async def add_elenco_immobili_request(self, request: ElencoImmobiliRequest, force: bool = False) -> str | SubmitResult:
        """Aggiunge una richiesta elenco immobili alla coda."""
        return await self._add_single("elenco_immobili", request, force=force)

    async def add_requests_batch(self, requests: list[VisuraRequest], force: bool = False) -> list[str | SubmitResult]:
        """Accoda più richieste in modo atomico lato producer."""
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
                logger.info(f"Richiesta visura {request.request_id} aggiunta alla coda")

        return results

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
        """Chiude il servizio (senza logout graceful)"""
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
        """Chiude il servizio con logout graceful"""
        logger.info("Iniziando graceful shutdown del servizio...")

        # Cancel background auth if still running
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
