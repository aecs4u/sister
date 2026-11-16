"""Browser lifecycle manager — wraps aecs4u-auth for SISTER portal automation."""

import asyncio
import functools
import inspect
import json
import logging
import os
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
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
from .network_json import SISTERJsonCapture, extract_property_rows
from .utils import (
    _on_live_sister_page,
    dump_error_page,
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
    run_ispezioni_ipotecarie_riepilogo,
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

# seconds during which a successful session check is trusted (0 = check before every request)
SESSION_RECHECK_SECONDS = float(os.getenv("SISTER_SESSION_RECHECK_SECONDS", "120"))
# the service never logs in by itself in CDP mode (see docs/portal_session.md); set true to let aecs4u-auth try to log in again
AUTO_RELOGIN = os.getenv("SISTER_AUTO_RELOGIN", "false").strip().lower() in ("1", "true", "yes", "si")
# SISTER ends a session this long after the login, whatever the activity (measured 2026-10-26: 29.6-30.4 min)
SESSION_MINUTES = float(os.getenv("SISTER_SESSION_MINUTES", "30"))
# written by `sister app login` (command_tools/ade_login.record_login)
LOGIN_RECORD = Path(__file__).resolve().parents[1] / "logs" / "sister_login.json"


def _recorded_login(path: Optional[Path] = None) -> Optional[datetime]:
    """Local (naive) time of the last ``sister app login``, or None when it was not recorded."""
    try:
        stamp = datetime.fromisoformat(json.loads((path or LOGIN_RECORD).read_text(encoding="utf-8"))["logged_in_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp

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
    "ipotecaria_riepilogo": run_ispezioni_ipotecarie_riepilogo,
}


_PARAM_ALIASES = {
    "sheet": "foglio",
    "parcel": "particella",
    "section": "sezione",
    "subunit": "subalterno",
    "urban_section": "sezione_urbana",
}


def _normalised_request_params(request: GenericSisterRequest) -> dict:
    params = dict(request.params or {})
    for alias, name in _PARAM_ALIASES.items():
        if alias in params:
            params.setdefault(name, params.pop(alias))
    return params


async def _run_with_network_json(page, extraction):
    """Capture local SISTER JSON during an extraction and supplement its result rows."""
    from .utils import record_ambiguous_options

    capture = SISTERJsonCapture(page)
    await capture.start()
    try:
        with record_ambiguous_options() as ambiguous_options:
            result = await extraction()
    finally:
        responses = await capture.stop()

    if not isinstance(result, dict):
        return result

    enriched = dict(result)
    extracted_rows = []
    for response in responses:
        extracted_rows.extend(extract_property_rows(response.get("data")))

    if extracted_rows:
        existing = enriched.get("immobili")
        merged = list(existing) if isinstance(existing, list) else []

        def signature(row):
            if not isinstance(row, dict):
                return ("raw", repr(row))
            return tuple(str(row.get(key, "")).strip().upper() for key in ("Catasto", "Foglio", "Particella", "Sub"))

        seen = {signature(row) for row in merged}
        for row in extracted_rows:
            key = signature(row)
            if key not in seen:
                seen.add(key)
                merged.append(row)
        enriched["immobili"] = merged
        try:
            existing_count = int(enriched.get("total_results") or 0)
        except (TypeError, ValueError):
            existing_count = 0
        enriched["total_results"] = max(existing_count, len(merged))

    if responses:
        enriched["network_json_responses"] = responses
    if ambiguous_options:
        enriched["ambiguous_options"] = ambiguous_options
    return enriched


def _dispatcher_kwargs(dispatcher, request: GenericSisterRequest) -> dict:
    """Build the keyword arguments for a generic dispatcher from the request.

    Dispatchers take different subsets of (foglio, particella, sezione, ...), and ``params`` may spell a name
    either in English or Italian, so normalise the aliases and keep only what the dispatcher accepts.
    """
    params = _normalised_request_params(request)
    kwargs = {
        "tipo_catasto": request.cadastre_type,
        "provincia": request.province,
        "comune": request.municipality,
        # Include known route fields as None so **kwargs dispatchers do not
        # inherit helper defaults (notably the sample parcel in visura_immobile).
        "foglio": None,
        "particella": None,
        "sezione": None,
        "subalterno": None,
        "sezione_urbana": None,
        "indirizzo": None,
        "partita": None,
        "numero_nota": None,
        "anno_nota": None,
        "codice_fiscale": None,
        "identificativo": None,
        **params,
    }
    accepted = inspect.signature(dispatcher).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in accepted}


# Kept as a compatibility alias for callers and the dispatch-contract tests.
_dispatch_kwargs = _dispatcher_kwargs


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
        config = BrowserConfig()
        # Expose SISTER's launch policy alongside the auth package config.
        # The shared auth package currently accepts Chromium launch flags via
        # its module configuration, while viewport is applied after launch.
        object.__setattr__(config, "no_viewport", True)
        object.__setattr__(config, "browser_args", ["--start-maximized"])
        self._auth = AuthBrowserManager(config)
        self.last_login_time = None
        self._session_origin = None
        self._page_lock = asyncio.Lock()
        self._session_checked_at: Optional[float] = None
        self._session_lost_reason: Optional[str] = None

    @property
    def authenticated(self) -> bool:
        return self._auth.is_authenticated

    def session_expires_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """When SISTER ends the session of the recorded login, or None when that is not known.

        The record is ignored when a real session check succeeded after its deadline: the session in the browser then comes
        from a later login that ``sister app login`` did not record (e.g. made by hand in Chrome).
        """
        login = _recorded_login()
        if login is None or login > (now or datetime.now()) + timedelta(minutes=1):
            return None
        deadline = login + timedelta(minutes=SESSION_MINUTES)
        if self.last_login_time is not None and self.last_login_time > deadline:
            return None
        return deadline

    def session_lost_reason(self, now: Optional[datetime] = None) -> Optional[str]:
        """Why the adopted SISTER session can no longer be used, or None when nothing says so.

        Passive on purpose: it never navigates (a check that opens SceltaServizio would disturb the query in flight), so it
        reads what is already known: a failed check, the browser connection, the page the tab is on and the login time.
        """
        if self._session_lost_reason:
            return self._session_lost_reason
        browser = getattr(self._auth, "_browser", None)
        if self.is_cdp and browser is not None and not browser.is_connected():
            return "Chrome non è più raggiungibile: riavvia la sessione"
        page = getattr(self._auth, "_auth_page", None)
        if page is not None:
            if page.is_closed():
                return "La scheda SISTER è stata chiusa: serve un nuovo accesso (sister app login)"
            if not _on_live_sister_page(page.url):
                return "Sessione SISTER scaduta o chiusa: serve un nuovo accesso (sister app login)"
        deadline = self.session_expires_at(now)
        if deadline is not None and (now or datetime.now()) >= deadline:
            return f"Sessione SISTER scaduta alle {deadline:%H:%M}: serve un nuovo accesso (sister app login)"
        return None

    @property
    def is_cdp(self) -> bool:
        return self._auth.is_cdp

    @property
    def session_origin(self) -> Optional[str]:
        return getattr(self._auth, "session_origin", None) or self._session_origin

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
        (see ``sister app login`` for how such a tab is created).
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
                    self._session_lost_reason = None
                    logger.info("Sessione SISTER esistente agganciata: %s", page.url)
                    return True
        except Exception as e:
            logger.warning("Impossibile agganciare una sessione SISTER esistente: %s", e)
        logger.info("Nessuna sessione SISTER esistente nel browser CDP")
        return False

    async def login(self):
        try:
            ensure_session = getattr(self._auth, "ensure_service_session", None)
            if ensure_session is not None:
                session = await ensure_session("sister")
                self._session_origin = getattr(session, "origin", None) or getattr(
                    self._auth, "session_origin", None
                )
                await self._auth.start_keepalive()
            else:
                await self._auth.login(service="sister")
                self._session_origin = "authenticated"
            self.last_login_time = datetime.now()
            self._session_lost_reason = None
            logger.info("Login completato con successo")
        except Exception as e:
            logger.error("Errore durante il login: %s", e)
            raise AuthenticationError(f"Login failed: {e}") from e

    async def start_keep_alive(self):
        await self._auth.start_keepalive()

    async def stop_keep_alive(self):
        await self._auth.stop_keepalive()

    async def _ensure_authenticated(self):
        # The package check opens SceltaServizio on every request, so the next query always starts from that page. Within
        # SESSION_RECHECK_SECONDS of the last good check, on a live SISTER page, it is skipped: the keep-alive keeps the
        # session warm and an expired one still fails the query (and is checked again, since the page is then a dead one).
        page = self.auth_page
        if (
            SESSION_RECHECK_SECONDS > 0
            and self._session_checked_at is not None
            and time.monotonic() - self._session_checked_at < SESSION_RECHECK_SECONDS
            and page is not None
            and _on_live_sister_page(page.url)
        ):
            return
        if self.is_cdp and not AUTO_RELOGIN:
            # The package would answer a failed check with a "recovery" navigation and then a full CIE login in the user's
            # tab, which waits for a phone approval nobody expects and blocks the worker. Check only, never log in.
            session = getattr(self._auth, "_session", None)
            valid = False
            if session is not None and session.service:
                from aecs4u_auth.browser.services import get_service

                # check_session only looks for the province menu of the CURRENT page, which result pages do not have, and the
                # package's keep-alive uses the same test to mark a healthy session invalid. A miss is therefore answered
                # with the package's recovery (go back to the Visure page, look again), never with a login
                if session.is_valid:
                    valid = await get_service(session.service).check_session(session.page)
                if not valid:
                    valid = await self._auth._try_recovery()
            if not valid:
                self._session_checked_at = None
                self._session_lost_reason = "Sessione SISTER scaduta o non valida: serve un nuovo accesso (sister app login)"
                raise AuthenticationError(self._session_lost_reason)
            self.last_login_time = datetime.now()
            self._session_lost_reason = None
            self._session_checked_at = time.monotonic()
            return
        try:
            await self._auth.ensure_authenticated()
            self.last_login_time = datetime.now()
            self._session_lost_reason = None
            self._session_checked_at = time.monotonic()
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
                        extract_intestati=True,
                        subalterno=request.subunit,
                        sezione_urbana=request.urban_section,
                        request_documents=False,
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
                    request_documents=request.request_documents,
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
                    cognome=None,
                    nome=None,
                )
            return VisuraResponse(
                request_id=request.request_id,
                success=True,
                cadastre_type=request.cadastre_type,
                data=result,
            )
        except Exception as e:
            await dump_error_page(self.auth_page, request.request_id, str(e))
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
                    params = _normalised_request_params(request)
                    result = await run_visura_immobile(
                        page,
                        provincia=request.province,
                        comune=request.municipality,
                        foglio=params.get("foglio"),
                        particella=params.get("particella"),
                        subalterno=params.get("subalterno"),
                        sezione=params.get("sezione"),
                        sezione_urbana=params.get("sezione_urbana"),
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
                    codice_fiscale=request.fiscal_code,
                    identificativo=request.identifier,
                    numero_nota=request.note_number,
                    anno_nota=request.note_year,
                    # Legacy queue requests must never be able to bypass the admin-bound paid flow.
                    auto_confirm=False,
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

    async def confirm_mortgage_accept(
        self,
        *,
        expected_amount: str,
        subject_fiscal_code: str,
        office: str,
        scope: str,
        clicked_callback,
    ) -> dict:
        """Confirm one previously quoted preview, only if the same subject and amount are still on screen."""
        from decimal import Decimal, InvalidOperation

        from .result_parsers import omonimo_details, parse_mortgage_inspection_page
        from .utils import PageLogger, _extract_cost_from_page, _omonimi_table_rows

        async with self._page_lock:
            page = await self._get_authenticated_page()
            logger = PageLogger("mortgage_paid_flow")
            before_html = await page.content()
            before_text = await page.inner_text("body")
            if subject_fiscal_code.upper() not in before_html.upper():
                raise ValueError("preview_subject_mismatch")
            cost_text, actual_amount = await _extract_cost_from_page(page)
            try:
                expected = Decimal(str(expected_amount)).quantize(Decimal("0.01"))
                actual = Decimal(str(actual_amount)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("preview_amount_unreadable") from exc
            if actual != expected:
                raise ValueError("preview_amount_changed")

            confirm = page.locator("input[value='Conferma']")
            if await confirm.count() == 0:
                confirm = page.locator("button:has-text('Conferma')")
            if await confirm.count() == 0:
                confirm = page.locator("input[type='submit'][value*='onferma']")
            if await confirm.count() == 0:
                raise ValueError("preview_confirm_button_missing")

            await logger.log(page, "accept_preview")
            before_url = page.url
            await confirm.first.click()
            await clicked_callback()
            await page.wait_for_load_state("networkidle", timeout=60000)
            await logger.log(page, "accept_result")
            result_text = await page.inner_text("body")
            try:
                selection_values = await page.locator("input[name='noteSelezionate']").evaluate_all(
                    "elements => elements.map(element => element.value)"
                )
            except Exception:
                selection_values = []
            try:
                homonym_values = await page.locator(
                    "input[type='radio'][name='omonimoSelezionato'], "
                    "input[type='radio'][property='omonimoSelezionato']"
                ).evaluate_all("elements => elements.map(element => element.value)")
            except Exception:
                homonym_values = []
            homonym_rows = _omonimi_table_rows(await page.content()) if homonym_values else []
            homonyms = [omonimo_details(row, index + 1) for index, row in enumerate(homonym_rows)]
            if len(homonyms) != len(homonym_values) or len(homonyms) > 100:
                homonyms = []
                homonym_values = []
            parsed = parse_mortgage_inspection_page(
                result_text,
                selection_values,
                subject_fiscal_code=subject_fiscal_code,
                office=office,
                scope=scope,
            )
            homonym_page = bool(homonyms)
            effect_visible = parsed is not None or homonym_page
            return {
                "clicked": True,
                "confirmed": effect_visible,
                "cost": {"text": cost_text, "value": str(actual)},
                "next_step": "formalita" if parsed is not None else ("omonimi" if homonym_page else "uncertain"),
                "portal_data": parsed,
                "homonyms": homonyms,
                "homonym_values": [str(value) for value in homonym_values],
                "page_visits": logger.page_visits,
                "url_changed": page.url != before_url,
                "preview_text_changed": result_text != before_text,
            }

    async def select_mortgage_homonym(
        self,
        *,
        expected_values: list[str],
        selected_value: str,
        subject_fiscal_code: str,
        office: str,
        scope: str,
    ) -> dict:
        """Select one explicitly reviewed homonym and parse the resulting accepted-inspection page."""
        from .result_parsers import omonimo_details, parse_mortgage_inspection_page, parse_omonimo_value
        from .utils import PageLogger, _omonimi_table_rows

        expected = [str(value) for value in expected_values]
        if not expected or len(expected) > 100 or selected_value not in expected:
            raise ValueError("homonym_selection_invalid")

        async with self._page_lock:
            page = await self._get_authenticated_page()
            logger = PageLogger("mortgage_paid_flow")
            radio_selector = (
                "input[type='radio'][name='omonimoSelezionato'], "
                "input[type='radio'][property='omonimoSelezionato']"
            )
            radios = page.locator(radio_selector)
            values = await radios.evaluate_all("elements => elements.map(element => element.value)")
            values = [str(value) for value in values]
            if values != expected:
                raise ValueError("homonym_list_changed")
            rows = _omonimi_table_rows(await page.content())
            homonyms = [omonimo_details(row, index + 1) for index, row in enumerate(rows)]
            if len(homonyms) != len(values):
                raise ValueError("homonym_rows_unreadable")

            selected_index = values.index(selected_value)
            selected = homonyms[selected_index]
            candidate_code = str(
                selected.get("codice_fiscale")
                or parse_omonimo_value(selected_value).get("codice_fiscale")
                or ""
            ).upper()
            if not candidate_code or candidate_code != subject_fiscal_code.upper():
                raise ValueError("homonym_subject_mismatch")

            submit = page.locator("input[name='visura'][value='Ricerca']")
            if await submit.count() == 0:
                raise ValueError("homonym_continue_button_missing")
            await logger.log(page, f"homonym_choice_{selected_index + 1}")
            before_url = page.url
            await radios.nth(selected_index).check()
            await submit.first.click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            await logger.log(page, "formalita_after_homonym")

            page_text = await page.inner_text("body")
            try:
                selection_values = await page.locator("input[name='noteSelezionate']").evaluate_all(
                    "elements => elements.map(element => element.value)"
                )
            except Exception:
                selection_values = []
            portal_data = parse_mortgage_inspection_page(
                page_text,
                selection_values,
                subject_fiscal_code=subject_fiscal_code,
                office=office,
                scope=scope,
            )
            return {
                "selected_homonym": selected,
                "portal_data": portal_data,
                "next_step": "formalita" if portal_data is not None else "omonimi",
                "page_visits": logger.page_visits,
                "url_changed": page.url != before_url,
                "result_readable": portal_data is not None,
            }

    async def request_mortgage_documents(
        self,
        *,
        step: str,
        portal_items: list[dict],
        clicked_callback,
    ) -> dict:
        """Submit one quoted notes/title request and require a register-matched Richieste row as evidence."""
        import re

        from .result_parsers import parse_formalita_list
        from .utils import PageLogger, _parse_richieste_table, _richieste_register_pair

        if step not in {"notes", "title"} or not isinstance(portal_items, list) or not portal_items:
            raise ValueError("paid_document_request_invalid")
        expected_by_value = {}
        expected_by_register = {}
        for item in portal_items:
            if not isinstance(item, dict):
                raise ValueError("paid_document_request_invalid")
            ordinal = item.get("ordinal")
            value = str(item.get("selection_value") or "").strip()
            rp = re.sub(r"\D", "", str(item.get("registro_particolare") or "")).lstrip("0") or "0"
            rg = re.sub(r"\D", "", str(item.get("registro_generale") or "")).lstrip("0") or "0"
            if type(ordinal) is not int or ordinal <= 0 or not value or not rp or not rg or value in expected_by_value:
                raise ValueError("paid_document_request_identity_invalid")
            expected_by_value[value] = ordinal
            expected_by_register[ordinal] = (rp, rg)
        if len(expected_by_value) > 100:
            raise ValueError("paid_document_request_selection_too_large")

        async with self._page_lock:
            page = await self._get_authenticated_page()
            logger = PageLogger("mortgage_paid_flow")
            boxes = page.locator("input[name='noteSelezionate']")
            live_values = [str(value) for value in await boxes.evaluate_all("elements => elements.map(element => element.value)")]
            if not live_values or len(live_values) != len(set(live_values)):
                raise ValueError("formalita_checkboxes_unreadable")
            before_values = list(live_values)
            if any(value not in live_values for value in expected_by_value):
                raise ValueError("formalita_page_changed_since_quote")
            page_text = await page.inner_text("body")
            live_formalities = parse_formalita_list(page_text, live_values)
            live_by_value = {
                str(item.get("portal_selection_value") or ""): item
                for item in live_formalities
                if item.get("portal_selection_value")
            }
            for item in portal_items:
                selection_value = str(item["selection_value"])
                rp, rg = expected_by_register[item["ordinal"]]
                live_formality = live_by_value.get(selection_value)
                live_rp = re.sub(r"\D", "", str((live_formality or {}).get("registro_particolare") or ""))
                live_rg = re.sub(r"\D", "", str((live_formality or {}).get("registro_generale") or ""))
                live_rp = live_rp.lstrip("0") or "0"
                live_rg = live_rg.lstrip("0") or "0"
                if live_formality is None or (live_rp, live_rg) != (rp, rg):
                    raise ValueError("formalita_registers_changed_since_quote")

            for index, value in enumerate(live_values):
                checkbox = boxes.nth(index)
                should_check = value in expected_by_value
                is_checked = await checkbox.is_checked()
                if should_check and not is_checked:
                    await checkbox.check()
                elif not should_check and is_checked:
                    await checkbox.uncheck()

            action_label = "Visualizza note" if step == "notes" else "Visualizza titolo"
            action = page.locator(
                f"input[name='visura'][value*='{action_label}' i], "
                f"input[type='submit'][value*='{action_label}' i], "
                f"button:has-text('{action_label}')"
            )
            if await action.count() != 1:
                raise ValueError("paid_document_request_action_missing_or_ambiguous")

            await logger.log(page, f"{step}_before_click")
            await action.first.click()
            await clicked_callback()
            await page.wait_for_load_state("networkidle", timeout=60000)
            await logger.log(page, f"{step}_result")

            rows = _parse_richieste_table(await page.content(), include_pending=True)
            mapped = []
            used_ids = set()
            for ordinal, expected_pair in expected_by_register.items():
                matches = [
                    row for row in rows
                    if row.get("id_richiesta")
                    and _richieste_register_pair(row) == expected_pair
                ]
                if len(matches) != 1 or matches[0]["id_richiesta"] in used_ids:
                    return {
                        "clicked": True,
                        "confirmed": False,
                        "error": "request_row_not_unambiguously_matched",
                        "next_step": "richieste_list",
                        "page_visits": logger.page_visits,
                    }
                row = matches[0]
                request_id = str(row["id_richiesta"])
                used_ids.add(request_id)
                mapped.append(
                    {
                        "ordinal": ordinal,
                        "portal_request_id": request_id,
                        "status": row.get("status") if row.get("status") in {"requested", "available"} else "requested",
                    }
                )
            page_restored = False
            try:
                await page.go_back(wait_until="networkidle", timeout=60000)
                restored_boxes = page.locator("input[name='noteSelezionate']")
                restored_values = [
                    str(value)
                    for value in await restored_boxes.evaluate_all("elements => elements.map(element => element.value)")
                ]
                page_restored = restored_values == before_values
                if page_restored:
                    await logger.log(page, f"{step}_formalita_resumed")
            except Exception:
                page_restored = False
            return {
                "clicked": True,
                "confirmed": len(mapped) == len(expected_by_register),
                "requests": mapped,
                "next_step": "formalita" if page_restored else "richieste_list",
                "page_restored": page_restored,
                "page_visits": logger.page_visits,
            }

    async def inspect_mortgage_request_row(self, portal_request_id: str) -> dict:
        """Read a single Richieste row and try to restore the previous portal page; this never opens a document."""
        from .utils import PageLogger, _read_richieste_request_row

        async with self._page_lock:
            page = await self._get_authenticated_page()
            logger = PageLogger("mortgage_paid_flow")
            result = await _read_richieste_request_row(page, portal_request_id, logger)
            return result

    async def save_mortgage_request(
        self,
        *,
        quote: dict,
        clicked_callback,
    ) -> dict:
        """Save exactly one quoted mortgage document; the private downloader allowlist is this action's request ID."""
        from .utils import PageLogger, _download_richieste_documents

        portal_request_id = str(quote.get("portal_request_id") or "").strip()
        if not portal_request_id or str(quote.get("step") or "") != "richieste_save":
            raise ValueError("paid_save_quote_invalid")
        clicked = False

        async def _mark_clicked():
            nonlocal clicked
            await clicked_callback()
            clicked = True

        async with self._page_lock:
            page = await self._get_authenticated_page()
            logger = PageLogger("mortgage_paid_flow")
            documents = await _download_richieste_documents(
                page,
                logger,
                request_ids={portal_request_id},
                parse_mortgage_notes=quote.get("kind") == "note",
                _paid_request_authorization={
                    "portal_request_id": portal_request_id,
                    "amount": quote.get("amount"),
                    "expected_row_cost": quote.get("expected_row_cost"),
                },
                _paid_click_callback=_mark_clicked,
            )
        saved = [
            document for document in documents
            if str(document.get("id_richiesta") or "") == portal_request_id
            and document.get("document_id") is not None
            and int(document.get("file_size") or 0) > 0
        ]
        if len(saved) != 1:
            blocked = getattr(documents, "blocked_paid", [])
            return {
                "clicked": clicked,
                "confirmed": False,
                "error": (blocked[0].get("reason") if blocked else "paid_save_result_unconfirmed"),
                "portal_request_id": portal_request_id,
                "page_visits": logger.page_visits,
            }
        return {
            "clicked": clicked,
            "confirmed": True,
            "portal_request_id": portal_request_id,
            "document_id": int(saved[0]["document_id"]),
            "filename": saved[0].get("filename"),
            "file_size": int(saved[0].get("file_size") or 0),
            "page_visits": logger.page_visits,
        }

    async def esegui_extract_sezioni(
        self,
        tipo_catasto: str,
        max_province: int = 0,
        *,
        progress_callback=None,
        completed_province_values=None,
        initial_results=None,
    ) -> list:
        async with self._page_lock:
            page = await self._get_authenticated_page()
            return await extract_all_sezioni(
                page,
                tipo_catasto=tipo_catasto,
                max_province=max_province,
                progress_callback=progress_callback,
                completed_province_values=completed_province_values,
                initial_results=initial_results,
            )

    async def download_richieste_documents(
        self,
        *,
        since: str = "",
        skip_known: bool = False,
        request_ids: set[str] | None = None,
        parse_mortgage_notes: bool = False,
    ) -> list[dict]:
        from .utils import PageLogger, _download_richieste_documents

        async with self._page_lock:
            page = await self._get_authenticated_page()
            page_logger = PageLogger("download_richieste")
            return await _download_richieste_documents(
                page,
                page_logger,
                since=since,
                skip_known=skip_known,
                request_ids=request_ids,
                parse_mortgage_notes=parse_mortgage_notes,
            )

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
            # The session lives in the shared Chrome and is owned by sister app login: a restart or
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
