"""Browser lifecycle manager — wraps aecs4u-auth for SISTER portal automation."""

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime
from typing import Optional

from aecs4u_auth.browser import BrowserConfig
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager
from playwright.async_api import Page

from .models import (
    AuthenticationError,
    BrowserError,
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaRequest,
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

logger = logging.getLogger("sister")

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

_NOARGS_DISPATCHERS = {
    "riepilogo_visure": run_riepilogo_visure,
    "richieste": run_consultazione_richieste,
    "ipotecaria_stato": run_ispezioni_ipotecarie_stato,
    "ipotecaria_elenchi": run_ispezioni_ipotecarie_elenchi,
}


class BrowserManager:
    """Manages Playwright browser lifecycle and dispatches SISTER portal commands."""

    def __init__(self):
        self._auth = AuthBrowserManager(BrowserConfig())
        self.last_login_time = None
        self._page_lock = asyncio.Lock()

    @property
    def authenticated(self) -> bool:
        return self._auth.is_authenticated

    @property
    def is_cdp(self) -> bool:
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
                from aecs4u_auth.browser import manager as _auth_manager
                if hasattr(_auth_manager, '_CHROMIUM_ARGS'):
                    if "--start-maximized" not in _auth_manager._CHROMIUM_ARGS:
                        _auth_manager._CHROMIUM_ARGS.append("--start-maximized")

            await self._auth.initialize()

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

    # ------------------------------------------------------------------
    # Execution methods — each acquires the page lock and runs a command
    # ------------------------------------------------------------------

    async def esegui_visura(self, request: VisuraRequest) -> VisuraResponse:
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
                except Exception as inner_e:
                    return VisuraResponse(
                        request_id=request.request_id,
                        success=False,
                        tipo_catasto=request.tipo_catasto,
                        data=None,
                        error=str(inner_e),
                    )

            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                data=None,
                error=str(e),
            )

    async def esegui_visura_intestati(self, request: VisuraIntestatiRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura(
                    page,
                    request.provincia,
                    request.comune,
                    request.sezione,
                    request.foglio,
                    request.particella,
                    request.tipo_catasto,
                    extract_intestati=True,
                    subalterno=request.subalterno,
                    sezione_urbana=request.sezione_urbana,
                    target_index=getattr(request, "target_index", None),
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                tipo_catasto=request.tipo_catasto,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                tipo_catasto=request.tipo_catasto,
                data=None,
                error=str(e),
            )

    async def esegui_visura_soggetto(self, request: VisuraSoggettoRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura_soggetto(page, request.codice_fiscale)
            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, data=None, error=str(e),
            )

    async def esegui_visura_persona_giuridica(self, request: VisuraPersonaGiuridicaRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura_persona_giuridica(
                    page, request.identificativo,
                    tipo_catasto=request.tipo_catasto,
                    provincia=request.provincia,
                )
            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, data=None, error=str(e),
            )

    async def esegui_elenco_immobili(self, request: ElencoImmobiliRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_elenco_immobili(
                    page,
                    tipo_catasto=request.tipo_catasto,
                    provincia=request.provincia,
                    comune=request.comune,
                    sezione=getattr(request, "sezione", None),
                )
            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, data=None, error=str(e),
            )

    async def esegui_generic(self, request: GenericSisterRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                search_type = request.search_type

                if search_type == "visura_immobile":
                    result = await run_visura_immobile(
                        page,
                        provincia=request.provincia, comune=request.comune,
                        foglio=request.foglio, particella=request.particella,
                        tipo_catasto=request.tipo_catasto,
                        subalterno=request.params.get("subalterno") if request.params else None,
                        sezione=request.params.get("sezione") if request.params else None,
                    )
                elif search_type in _GENERIC_DISPATCHERS:
                    dispatcher = _GENERIC_DISPATCHERS[search_type]
                    result = await dispatcher(
                        page,
                        tipo_catasto=request.tipo_catasto,
                        provincia=request.provincia, comune=request.comune,
                        foglio=request.foglio, particella=request.particella,
                        **(request.params or {}),
                    )
                elif search_type in _NOARGS_DISPATCHERS:
                    dispatcher = _NOARGS_DISPATCHERS[search_type]
                    result = await dispatcher(page)
                else:
                    return VisuraResponse(
                        request_id=request.request_id, success=False,
                        tipo_catasto=request.tipo_catasto, data=None,
                        error=f"Tipo di ricerca sconosciuto: {search_type}",
                    )

            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, data=None, error=str(e),
            )

    async def esegui_ispezione_ipotecaria(self, request: IspezioneIpotecariaRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_ispezione_ipotecaria(
                    page,
                    tipo_catasto=request.tipo_catasto,
                    provincia=request.provincia, comune=request.comune,
                    foglio=request.foglio, particella=request.particella,
                    tipo_ricerca=request.tipo_ricerca,
                    subalterno=getattr(request, "subalterno", None),
                    sezione=getattr(request, "sezione", None),
                    auto_confirm=getattr(request, "auto_confirm", False),
                )
            return VisuraResponse(
                request_id=request.request_id, success=True,
                tipo_catasto=request.tipo_catasto, data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id, success=False,
                tipo_catasto=request.tipo_catasto, data=None, error=str(e),
            )

    async def esegui_extract_sezioni(self, tipo_catasto: str, max_province: int = 0) -> list:
        async with self._page_lock:
            page = await self._get_authenticated_page()
            return await extract_all_sezioni(page, tipo_catasto=tipo_catasto, max_province=max_province)

    async def download_richieste_documents(self) -> list[dict]:
        from .utils import _download_richieste_documents, PageLogger
        async with self._page_lock:
            page = await self._get_authenticated_page()
            page_logger = PageLogger("download_richieste")
            return await _download_richieste_documents(page, page_logger)

    async def close(self):
        await self._auth.close()
        logger.info("Browser chiuso")

    async def graceful_shutdown(self):
        logger.info("Iniziando shutdown graceful...")
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
