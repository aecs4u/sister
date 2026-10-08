"""Browser lifecycle manager — wraps aecs4u-auth for SISTER portal automation."""

import asyncio
import functools
import inspect
import logging
from contextlib import suppress
from datetime import datetime
from typing import Optional

from aecs4u_auth.browser import BrowserConfig
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager
from aecs4u_auth.browser.page_logger import PageLogger
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
    form_context,
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
    run_soggetto_documento,
    run_soggetto_immobili,
    run_visura,
    run_visura_immobile,
    run_visura_persona_giuridica,
    run_visura_soggetto,
    run_visura_storica,
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
    "visura_storica": run_visura_storica,
    "soggetto_documento": run_soggetto_documento,
    "soggetto_immobili": run_soggetto_immobili,
}

_NOARGS_DISPATCHERS = {
    "riepilogo_visure": run_riepilogo_visure,
    "richieste": run_consultazione_richieste,
    "ipotecaria_stato": run_ispezioni_ipotecarie_stato,
    "ipotecaria_elenchi": run_ispezioni_ipotecarie_elenchi,
}


_PARAM_ALIASES = {"sheet": "foglio", "parcel": "particella", "section": "sezione", "subunit": "subalterno"}


def _dispatcher_kwargs(dispatcher, request: GenericSisterRequest) -> dict:
    """Build the keyword arguments for a generic dispatcher from the request.

    Dispatchers take different subsets of (foglio, particella, sezione, ...), and ``params`` may spell a name
    either in English or Italian, so normalise the aliases and keep only what the dispatcher accepts.
    """
    params = dict(request.params or {})
    for alias, name in _PARAM_ALIASES.items():
        if alias in params:
            params.setdefault(name, params.pop(alias))
    kwargs = {
        "tipo_catasto": request.cadastre_type,
        "provincia": request.province,
        "comune": request.municipality,
        **params,
    }
    accepted = inspect.signature(dispatcher).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in accepted}


def _with_form_fields(query):
    """Expose the request's SISTER form inputs (``form_fields`` / generic ``params``) to the form filler."""

    def decorate(method):
        @functools.wraps(method)
        async def wrapper(self, request, *args, **kwargs):
            name = query(request) if callable(query) else query
            fields = getattr(request, "form_fields", None) or getattr(request, "params", None) or {}
            with form_context(name, fields):
                return await method(self, request, *args, **kwargs)

        return wrapper

    return decorate


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

                if hasattr(_auth_manager, "_CHROMIUM_ARGS"):
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

    async def attach_existing_session(self) -> bool:
        """Adopt an already-authenticated SISTER tab in the shared CDP Chrome, without logging in.

        Returns True when a tab with a valid SISTER session was found and adopted
        (see ``scripts/ade_login.py`` for how such a tab is created).
        """
        from aecs4u_auth.browser.services import get_service
        from aecs4u_auth.browser.session import AuthenticatedSession

        if not self._auth.config.cdp_endpoint:
            return False
        try:
            if self._auth._browser is None or not self._auth._browser.is_connected():
                await self._auth.initialize()
            navigator = get_service("sister")
            for page in reversed(list(self._auth._context.pages)):
                if page.is_closed() or "sister3.agenziaentrate.gov.it" not in page.url:
                    continue
                # A tab left mid-flow (e.g. on a result page) still holds a live session: send it back to Visure
                # without logging in; recover_session returns False when the portal redirects to the login.
                if await navigator.check_session(page) or await navigator.recover_session(
                    page, PageLogger("attach", base_dir=self._auth.config.page_log_dir)
                ):
                    self._auth._auth_page = page
                    self._auth._session = AuthenticatedSession(
                        page=page, auth_method=self._auth.config.auth_method, service="sister"
                    )
                    self.last_login_time = datetime.now()
                    logger.info("Sessione SISTER esistente agganciata: %s", page.url)
                    return True
        except Exception as e:
            logger.warning("Impossibile agganciare una sessione SISTER esistente: %s", e)
        logger.info("Nessuna sessione SISTER esistente nel browser CDP")
        return False

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

    @_with_form_fields("search")
    async def esegui_visura(self, request: VisuraRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                try:
                    result = await run_visura(
                        page,
                        request.province,
                        request.municipality,
                        request.section,
                        request.sheet,
                        request.parcel,
                        request.cadastre_type,
                        extract_intestati=False,
                        subalterno=request.subunit,
                        sezione_urbana=request.urban_section,
                    )
                except Exception as inner_e:
                    return VisuraResponse(
                        request_id=request.request_id,
                        success=False,
                        cadastre_type=request.cadastre_type,
                        data=None,
                        error=str(inner_e),
                    )

            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except (AuthenticationError, BrowserError) as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    @_with_form_fields("intestati")
    async def esegui_visura_intestati(self, request: VisuraIntestatiRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura(
                    page,
                    request.province,
                    request.municipality,
                    request.section,
                    request.sheet,
                    request.parcel,
                    request.cadastre_type,
                    extract_intestati=True,
                    subalterno=request.subunit,
                    sezione_urbana=request.urban_section,
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    @_with_form_fields("soggetto")
    async def esegui_visura_soggetto(self, request: VisuraSoggettoRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura_soggetto(
                    page,
                    request.fiscal_code,
                    tipo_catasto=request.cadastre_type or "E",
                    provincia=request.province,
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    @_with_form_fields("azienda")
    async def esegui_visura_persona_giuridica(self, request: VisuraPersonaGiuridicaRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_visura_persona_giuridica(
                    page,
                    request.identifier,
                    tipo_catasto=request.cadastre_type,
                    provincia=request.province,
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    @_with_form_fields("elenco")
    async def esegui_elenco_immobili(self, request: ElencoImmobiliRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_elenco_immobili(
                    page,
                    tipo_catasto=request.cadastre_type,
                    provincia=request.province,
                    comune=request.municipality,
                    foglio=getattr(request, "sheet", None),
                    sezione=getattr(request, "section", None),
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    @_with_form_fields(lambda request: request.search_type)
    async def esegui_generic(self, request: GenericSisterRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                search_type = request.search_type

                if search_type == "visura_immobile":
                    result = await run_visura_immobile(
                        page,
                        provincia=request.province,
                        comune=request.municipality,
                        foglio=request.params.get("sheet") or request.params.get("foglio") if request.params else None,
                        particella=request.params.get("parcel") or request.params.get("particella") if request.params else None,
                        tipo_catasto=request.cadastre_type,
                        subalterno=request.params.get("subunit") or request.params.get("subalterno") if request.params else None,
                        sezione=request.params.get("section") or request.params.get("sezione") if request.params else None,
                    )
                elif search_type in _GENERIC_DISPATCHERS:
                    dispatcher = _GENERIC_DISPATCHERS[search_type]
                    result = await dispatcher(page, **_dispatcher_kwargs(dispatcher, request))
                elif search_type in _NOARGS_DISPATCHERS:
                    dispatcher = _NOARGS_DISPATCHERS[search_type]
                    result = await dispatcher(page)
                else:
                    return VisuraResponse(
                        request_id=request.request_id,
                        success=False,
                        cadastre_type=request.cadastre_type,
                        data=None,
                        error=f"Tipo di ricerca sconosciuto: {search_type}",
                    )

            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    async def esegui_ispezione_ipotecaria(self, request: IspezioneIpotecariaRequest) -> VisuraResponse:
        try:
            async with self._page_lock:
                page = await self._get_authenticated_page()
                result = await run_ispezione_ipotecaria(
                    page,
                    tipo_catasto=request.cadastre_type,
                    provincia=request.province,
                    comune=request.municipality,
                    foglio=request.sheet,
                    particella=request.parcel,
                    tipo_ricerca=request.search_type,
                    subalterno=getattr(request, "subunit", None),
                    sezione=getattr(request, "section", None),
                    auto_confirm=getattr(request, "auto_confirm", False),
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            return VisuraResponse(
                request_id=request.request_id,
                success=False,
                cadastre_type=request.cadastre_type,
                data=None,
                error=str(e),
            )

    async def esegui_extract_sezioni(self, tipo_catasto: str, max_province: int = 0) -> list:
        async with self._page_lock:
            page = await self._get_authenticated_page()
            return await extract_all_sezioni(page, tipo_catasto=tipo_catasto, max_province=max_province)

    async def download_richieste_documents(self) -> list[dict]:
        from .utils import PageLogger, _download_richieste_documents

        async with self._page_lock:
            page = await self._get_authenticated_page()
            page_logger = PageLogger("download_richieste")
            return await _download_richieste_documents(page, page_logger)

    async def close_sister_session(self):
        """Release the session SISTER holds for this user (it allows only one, so a leftover blocks the next login)."""
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
            if self._auth.session:
                self._auth.session.invalidate()
        except Exception as e:
            logger.warning("Errore chiusura sessione SISTER: %s", e)

    async def close(self):
        await self._auth.close()
        logger.info("Browser chiuso")

    async def graceful_shutdown(self):
        logger.info("Iniziando shutdown graceful...")
        if self.is_cdp:
            # The session lives in the shared Chrome and is owned by scripts/ade_login.py: a restart or
            # --reload of this service must only disconnect, never log the user out of SISTER.
            # close() would click "Torna al portale" and move the tab off the Visure page, so disconnect directly.
            await self._auth.stop_keepalive()
            if self._auth._playwright:
                with suppress(Exception):
                    await self._auth._playwright.stop()
            logger.info("Shutdown graceful completato (CDP: sessione SISTER lasciata attiva)")
            return
        await self.close_sister_session()
        await self._auth.graceful_shutdown()
        logger.info("Shutdown graceful completato")
