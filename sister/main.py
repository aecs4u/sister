import asyncio  # noqa: F401 (used by tests via main_module.asyncio)
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load configuration before importing modules that read environment variables at import time.
load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)  # shared workspace-root defaults (project .env above wins)

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from rich.logging import RichHandler
from starlette.exceptions import HTTPException as StarletteHTTPException

# Re-export for tests using main_module.*
from .database import (  # noqa: F401
    cleanup_old_responses,
    count_responses,
    find_responses,
    init_db,
    save_request,
    save_requests_batch,
    save_response,
)
from .database import get_response as load_stored_response  # noqa: F401
from .models import (  # noqa: F401
    ElencoImmobiliInput,
    IspezioneIpotecariaInput,
    SezioniExtractionRequest,
    VisuraInput,
    VisuraIntestatiInput,
    VisuraPersonaGiuridicaInput,
    VisuraRequest,
    VisuraResponse,
    VisuraSoggettoInput,
)
from .routes import (
    download_documents,
    extract_sezioni,
    graceful_shutdown_endpoint,
    health_check,
    ottieni_visura,
    richiedi_elenco_immobili,
    richiedi_generic_sister,
    richiedi_intestati_immobile,
    richiedi_ispezione_ipotecaria,
    richiedi_visura,
    richiedi_visura_persona_giuridica,
    richiedi_visura_soggetto,
    visura_history,
)
from .passwords import is_hashed_password, verify_local_password
from .services import PageLogger, VisuraService

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_level = os.getenv("LOG_LEVEL", "INFO").upper()

log_handlers: list[logging.Handler] = [
    RichHandler(
        rich_tracebacks=True,
        tracebacks_show_locals=True,
        show_time=True,
        show_path=False,
        markup=True,
    ),
]

try:
    if not os.path.exists("./logs"):
        os.makedirs("./logs", exist_ok=True)
    file_handler = logging.FileHandler("./logs/visura.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    log_handlers.append(file_handler)
except (PermissionError, OSError):
    pass

logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(message)s",
    datefmt="[%X]",
    handlers=log_handlers,
)
logger = logging.getLogger("sister")


# ---------------------------------------------------------------------------
# Global state and dependencies
# ---------------------------------------------------------------------------

visura_service: Optional[VisuraService] = None
api_key = os.getenv("API_KEY")
shutdown_api_key = os.getenv("SHUTDOWN_API_KEY")

if not shutdown_api_key:
    logger.warning("SHUTDOWN_API_KEY non configurata: endpoint /shutdown disabilitato")
if not api_key:
    logger.warning("API_KEY non configurata: endpoint operativi accessibili senza autenticazione")


def get_visura_service() -> VisuraService:
    """Dependency to get the visura service"""
    if visura_service is None:
        raise HTTPException(status_code=503, detail="Servizio non inizializzato")
    return visura_service


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Verifica API key per endpoint operativi (se API_KEY è configurata)."""
    if not api_key:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="API key non valida")


def require_shutdown_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Verifica API key per endpoint amministrativi sensibili."""
    if not shutdown_api_key:
        raise HTTPException(status_code=503, detail="Endpoint disabilitato: SHUTDOWN_API_KEY non configurata")
    if not x_api_key or not secrets.compare_digest(x_api_key, shutdown_api_key):
        raise HTTPException(status_code=401, detail="API key non valida")


# ---------------------------------------------------------------------------
# Lifespan helpers
# ---------------------------------------------------------------------------


def _chrome_cdp_cmd(port: int) -> list[str]:
    """Build the Chrome launch command for CDP mode.

    Uses a dedicated user-data-dir so it starts as an independent process
    even when a regular Chrome is already running (Chrome is single-instance
    per profile; a separate dir bypasses that constraint).
    """
    from pathlib import Path as _Path

    profile_dir = os.getenv("SISTER_CHROME_PROFILE", str(_Path.home() / ".local/share/sister/chrome-profile"))
    return [
        "google-chrome",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


async def _ensure_chrome_cdp() -> None:
    """If BROWSER_CDP_ENDPOINT is configured, check it is reachable and launch Chrome if not."""
    import httpx

    cdp_endpoint = os.getenv("BROWSER_CDP_ENDPOINT", "").strip()
    if not cdp_endpoint:
        return

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cdp_endpoint}/json/version")
            if resp.status_code == 200:
                info = resp.json()
                logger.info("Chrome CDP già attivo: %s", info.get("Browser", cdp_endpoint))
                return
    except Exception:
        pass

    from urllib.parse import urlparse

    port = urlparse(cdp_endpoint).port or 9222
    cmd = _chrome_cdp_cmd(port)
    logger.info("Chrome CDP non raggiungibile su %s — avvio: %s", cdp_endpoint, " ".join(cmd))
    try:
        await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait up to 5 s for Chrome to start accepting connections
        for _ in range(5):
            await asyncio.sleep(1)
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.get(f"{cdp_endpoint}/json/version")
                    if resp.status_code == 200:
                        info = resp.json()
                        logger.info("Chrome avviato: %s", info.get("Browser", "ok"))
                        return
            except Exception:
                pass
        logger.warning("Chrome avviato ma CDP non ancora raggiungibile su %s", cdp_endpoint)
    except FileNotFoundError:
        logger.error("google-chrome non trovato in PATH — impossibile avviare CDP")
    except Exception as e:
        logger.error("Errore avvio Chrome: %s", e)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global visura_service
    await init_db()
    PageLogger.reset_session()
    if os.getenv("SISTER_NO_QUERY"):
        logger.info("No-query mode (SISTER_NO_QUERY set) — browser service skipped")
    else:
        await _ensure_chrome_cdp()
        try:
            visura_service = VisuraService()
            # No login at startup: only attach to a session created by scripts/ade_login.py
            await visura_service.initialize(attach_only=True)
            logger.info("Servizio visure avviato (aggancio sessione SISTER esistente in background)")
        except Exception as e:
            logger.warning("Browser service unavailable — web UI will run in read-only mode: %s", e)
            visura_service = None

    try:
        yield
    finally:
        logger.info("Shutdown in corso, eseguendo logout...")
        if visura_service:
            try:
                await visura_service.graceful_shutdown()
            except Exception as e:
                logger.error("Errore durante graceful shutdown: %s", e)
                try:
                    await visura_service.browser_manager.close()
                except Exception:
                    pass
        logger.info("Servizio visure fermato con graceful shutdown")


# ---------------------------------------------------------------------------
# App + route registration
# ---------------------------------------------------------------------------

# The interactive API docs are served by the routes below (behind the same authentication as the web UI), not by
# FastAPI's built-in public ones.
app = FastAPI(
    title="SISTER - Cadastral Data Service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)

# Delivery: compress HTML/JSON (SSE is excluded by default, already-compressed formats are skipped). Versioned theme
# assets (?v=) are immutable; SISTER's own static files are revalidated through their ETag.
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware  # noqa: E402

app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/pdf", "image/", "application/zip"),
)


class StaticCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            if response.status_code < 400:
                versioned = "v=" in str(request.url)
                long_lived = versioned and request.url.path.startswith(
                    ("/static/aecs4u-theme/", "/static/aecs4u-auth/")
                )
                # Unversioned files (sister.css, sister_forms.js...) are revalidated via ETag so edits show up at once.
                response.headers.setdefault(
                    "Cache-Control", "public, max-age=31536000, immutable" if long_lived else "no-cache"
                )
            else:
                response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(StaticCacheMiddleware)


async def _docs_auth(request: Request):
    """Same fail-closed check as the web UI (imported lazily: sister.web is loaded after the app is created)."""
    from .web import _require_auth

    return await _require_auth(request)


@app.get("/openapi.json", include_in_schema=False)
async def openapi_schema(_user=Depends(_docs_auth)):
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def swagger_ui(_user=Depends(_docs_auth)):
    from fastapi.openapi.docs import get_swagger_ui_html

    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", include_in_schema=False)
async def redoc_ui(_user=Depends(_docs_auth)):
    from fastapi.openapi.docs import get_redoc_html

    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Browsers get a themed HTML error page; API clients keep the JSON body."""
    wants_html = "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(
        ("/visura", "/auth", "/api", "/sezioni", "/health")
    )
    theme = getattr(request.app.state, "theme_setup", None)
    if wants_html and theme is not None and exc.status_code in (401, 403, 404):
        try:
            response = theme.render(
                "error.html",
                request,
                user=getattr(request.state, "user", None),
                status_code=exc.status_code,
                detail=exc.detail if isinstance(exc.detail, str) else "",
            )
            response.status_code = exc.status_code
            return response
        except Exception:  # never let the error page itself fail the request
            logger.exception("Rendering error.html failed")
    return await fastapi_http_exception_handler(request, exc)

# ---------------------------------------------------------------------------
# Theme, static files, and web UI
# ---------------------------------------------------------------------------
try:
    from aecs4u_theme import ThemeConfig, setup_theme

    _sister_dir = Path(__file__).parent
    _templates_dir = _sister_dir / "templates"
    _static_dir = _sister_dir / "static"

    # --- Auth setup (before theme) ---
    try:
        import aecs4u_auth
        from aecs4u_auth import AuthConfig, setup_auth

        auth_config = AuthConfig(
            AECS4U_SITE_ID="sister",
            AECS4U_SITE_NAME="SISTER",
            CLERK_AFTER_SIGN_IN_URL="/web/",
            CLERK_AFTER_SIGN_UP_URL="/web/",
        )
        require_auth = os.getenv("REQUIRE_AUTHENTICATION", "true").lower() not in ("false", "0", "no")
        # Per-path rate limits: /auth/* (login brute force) is much stricter than normal browsing/polling. The client
        # is the socket peer, not X-Forwarded-For (spoofable); behind a trusted proxy supply the real address instead.
        from aecs4u_auth.middleware import FixedWindowRateLimiter, FixedWindowRateLimitMiddleware, RateLimitPolicy

        app.add_middleware(
            FixedWindowRateLimitMiddleware,
            rate_limiter=FixedWindowRateLimiter(
                policies={
                    "default": RateLimitPolicy(int(os.getenv("SISTER_RATE_LIMIT_PER_MIN", "600")), 60),
                    "auth": RateLimitPolicy(int(os.getenv("SISTER_AUTH_RATE_LIMIT_PER_MIN", "20")), 60),
                }
            ),
            client_id_func=lambda request: request.client.host if request.client else "unknown",
        )
        # CSRF for the signed-in browser UI (JWT-in-cookie auth, so the token is derived from that cookie; see
        # sister/csrf.py). API-key/bearer clients, the login itself and tests carry no auth cookie: untouched.
        from .csrf import CookieCSRFMiddleware

        app.add_middleware(
            CookieCSRFMiddleware,
            cookie_name=auth_config.session_cookie_name,
            secret_getter=lambda: auth_config.effective_session_secret,
        )
        auth_setup = setup_auth(
            app,
            config=auth_config,
            include_routes=True,
            # The default auth mount exposes the package's internal static/
            # directory, which only contains clerk-auth.js. The auth page
            # templates reference the complete demo/static bundle instead.
            mount_static=False,
            setup_exception_handlers=False,
        )
        _auth_static_dir = Path(aecs4u_auth.__file__).parent / "demo" / "static"
        if _auth_static_dir.is_dir():
            app.mount(
                "/static/aecs4u-auth",
                StaticFiles(directory=str(_auth_static_dir)),
                name="aecs4u_auth_static",
            )
        from urllib.parse import quote as _urlquote

        from aecs4u_auth.dependencies import RedirectToLogin
        from aecs4u_auth.routers.auth import router as auth_router
        from fastapi.responses import RedirectResponse

        from .auth_pages import router as auth_pages_router

        app.include_router(auth_pages_router)  # SISTER's own sign-in/out pages win over the package's (same paths)
        app.include_router(auth_router)

        if require_auth:

            @app.exception_handler(RedirectToLogin)
            async def _redirect_to_login(request: Request, exc: RedirectToLogin):
                url = "/auth/login"
                if exc.return_url:
                    # The login page reads ?next_url= and only accepts relative targets: pass path + query.
                    from urllib.parse import urlsplit as _urlsplit

                    _target = _urlsplit(exc.return_url)
                    _next = _target.path + (f"?{_target.query}" if _target.query else "")
                    url = f"{url}?next_url={_urlquote(_next, safe='')}"
                return RedirectResponse(url=url, status_code=302)

        # Register file-backed local password authentication. Each non-comment
        # line in local_users.txt is ``username password``.
        from aecs4u_auth import set_password_verify_callback
        from aecs4u_auth import set_user_by_id_callback
        from aecs4u_auth.dependencies import set_user_by_username_callback

        _local_users_path = Path(
            os.getenv(
                "LOCAL_USERS_FILE",
                str(Path(__file__).resolve().parents[2] / "local_users.txt"),
            )
        ).expanduser()

        @dataclass(frozen=True)
        class _LocalUser:
            id: str
            email: str
            username: str
            full_name: str | None = None
            role: str = "user"
            is_active: bool = True
            is_superuser: bool = False

        def _load_local_users() -> dict[str, tuple[str, str]]:
            users: dict[str, tuple[str, str]] = {}
            try:
                lines = _local_users_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.error(
                    "Could not read local users file %s (%s)",
                    _local_users_path,
                    type(exc).__name__,
                )
                return users

            for line_number, line in enumerate(lines, start=1):
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                fields = entry.split(maxsplit=1)
                if len(fields) != 2 or not fields[0] or not fields[1]:
                    logger.warning("Ignoring malformed local user entry on line %d", line_number)
                    continue

                username, password = fields
                normalized_username = username.casefold()
                if normalized_username in users:
                    logger.warning("Ignoring duplicate local user entry on line %d", line_number)
                    continue
                users[normalized_username] = (username, password)
            return users

        _local_users = _load_local_users()
        _plain = [name for name, (_, pw) in _local_users.items() if not is_hashed_password(pw)]
        if _plain:
            logger.warning(
                "%d local user(s) have a plain-text password in %s (use scripts/hash_local_password.py)",
                len(_plain),
                _local_users_path.name,
            )
        if _local_users:
            logger.info("Loaded %d local authentication users", len(_local_users))
        else:
            logger.warning("No local authentication users loaded from %s", _local_users_path)

        # Opt-in: only the usernames listed in SISTER_ADMIN_USERS (comma separated) get role "admin". Nobody is an admin
        # by default, and admin gating of Browser Control / imports stays off until the variable is set.
        _admin_usernames = {
            name.strip().casefold() for name in os.getenv("SISTER_ADMIN_USERS", "").split(",") if name.strip()
        }
        if not _admin_usernames:
            logger.warning(
                "SISTER_ADMIN_USERS non configurata: Browser Control e import sono aperti a ogni utente autenticato"
            )

        def _get_local_user(username: str, _db=None):
            entry = _local_users.get(str(username).strip().casefold())
            if entry is None:
                return None
            canonical_username = entry[0]
            is_admin = canonical_username.casefold() in _admin_usernames
            return _LocalUser(
                id=canonical_username.casefold(),
                email=canonical_username if "@" in canonical_username else "",
                username=canonical_username,
                role="admin" if is_admin else "user",
                is_superuser=is_admin,
            )

        def _verify_local_password(username: str, password: str):
            username_bytes = str(username).strip().casefold().encode("utf-8")
            matched_username = None
            for normalized_username, (canonical_username, stored_password) in _local_users.items():
                user_matches = secrets.compare_digest(username_bytes, normalized_username.encode("utf-8"))
                # Always evaluate the password check so timing does not reveal which usernames exist.
                password_matches = verify_local_password(str(password), stored_password)
                if user_matches and password_matches:
                    matched_username = canonical_username
            return _get_local_user(matched_username) if matched_username else None

        set_password_verify_callback(_verify_local_password)
        set_user_by_username_callback(_get_local_user)
        set_user_by_id_callback(_get_local_user)

        app.state.auth_setup = auth_setup
        from .security import CspNonceMiddleware

        app.add_middleware(CspNonceMiddleware)  # added after setup_auth => wraps (rewrites) the CSP header it sets

        from .security import SecureCookieMiddleware

        app.add_middleware(
            SecureCookieMiddleware,
            cookie_names=(auth_config.session_cookie_name,),
            force=os.getenv("SISTER_COOKIE_SECURE", "auto").lower() == "always",
        )
        app.state.auth_config = auth_config
        auth_mode = getattr(auth_config, "AUTH_MODE", getattr(auth_config, "auth_mode", "unknown"))
        logger.info("Autenticazione configurata (mode=%s)", auth_mode)
    except ImportError:
        logger.warning("aecs4u-auth non disponibile: autenticazione disabilitata")
    except Exception as e:
        logger.warning("Errore configurazione auth: %s", e)

    # --- Theme setup --- read the theme settings from env and provide SISTER's default logo.
    theme_config = ThemeConfig()
    # Self-hosted vendor assets (sister/static/vendor): no render-blocking third-party CDN requests for the core UI.
    theme_config.bootstrap_css_url = "/static/vendor/bootstrap-5.3.2/css/bootstrap.min.css"
    theme_config.bootstrap_css_integrity = ""
    theme_config.bootstrap_js_url = "/static/vendor/bootstrap-5.3.2/js/bootstrap.bundle.min.js"
    theme_config.bootstrap_js_integrity = ""
    theme_config.fontawesome_css_url = "/static/vendor/fontawesome-6.5.0/css/all.min.css"
    theme_config.fontawesome_css_integrity = ""
    theme_config.fonts_css_url = "/static/vendor/fonts/fonts.css"  # Inter + Lexend, self-hosted
    if not theme_config.site_logo:
        theme_config.site_logo = "/static/images/sister-logo.svg"
    theme_setup = setup_theme(
        app,
        config=theme_config,
        templates_dir=str(_templates_dir),
    )
    app.state.theme_setup = theme_setup
    from .display import register as _register_display_filters

    _register_display_filters(theme_setup.templates.env)
    from .web import _is_admin as _web_is_admin

    from jinja2 import pass_context
    from markupsafe import Markup

    from .csrf import FORM_FIELD as _CSRF_FIELD
    from .csrf import token_for_request

    def _csrf_token(request) -> str:
        cfg = getattr(app.state, "auth_config", None)
        if request is None or cfg is None:
            return ""
        return token_for_request(request, secret=cfg.effective_session_secret, cookie_name=cfg.session_cookie_name)

    @pass_context
    def _csrf_input(ctx) -> Markup:
        token = _csrf_token(ctx.get("request"))
        return Markup(f'<input type="hidden" name="{_CSRF_FIELD}" value="{token}">') if token else Markup("")

    theme_setup.templates.env.globals["csrf_token"] = _csrf_token
    theme_setup.templates.env.globals["csrf_input"] = _csrf_input
    # Templates hide operator-only controls with can_admin(user); a no-op until SISTER_ADMIN_USERS is configured.
    theme_setup.templates.env.globals["can_admin"] = _web_is_admin
    # Defensive fallback: inject_jinja2 (called by setup_theme_from_env) should
    # register get_locale, but guard against running under the wrong venv where
    # the registration might fail silently.
    if "get_locale" not in theme_setup.templates.env.globals:
        theme_setup.templates.env.globals["get_locale"] = (
            lambda req: getattr(getattr(req, "state", None), "locale", "it")
        )

    # Mount sister-specific static files
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    # Mount outputs directory for screenshots
    from .database import OUTPUTS_DIR

    _outputs_dir = Path(OUTPUTS_DIR)
    _outputs_dir.mkdir(exist_ok=True)
    app.mount("/outputs", StaticFiles(directory=str(_outputs_dir)), name="outputs")

    # Include web routes
    from .web import router as web_router

    app.include_router(web_router)

    try:
        from .feedback_admin import router as feedback_admin_router

        app.include_router(feedback_admin_router)
        logger.info("Feedback admin router registrato")
    except ImportError as e:
        logger.warning("Feedback admin router non disponibile: %s", e)

    logger.info("Web UI inizializzata")
except ImportError:
    logger.warning("aecs4u-theme non disponibile: web UI disabilitata")


@app.post("/visura")
async def _richiedi_visura(
    request: VisuraInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_visura(request, service, force=force)


@app.post("/visura/intestati")
async def _richiedi_intestati_immobile(
    request: VisuraIntestatiInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_intestati_immobile(request, service, force=force)


@app.post("/visura/soggetto")
async def _richiedi_visura_soggetto(
    request: VisuraSoggettoInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_visura_soggetto(request, service, force=force)


@app.post("/visura/persona-giuridica")
async def _richiedi_visura_persona_giuridica(
    request: VisuraPersonaGiuridicaInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_visura_persona_giuridica(request, service, force=force)


@app.post("/visura/elenco-immobili")
async def _richiedi_elenco_immobili(
    request: ElencoImmobiliInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_elenco_immobili(request, service, force=force)


@app.post("/visura/download-documents")
async def _download_documents(
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await download_documents(service)


@app.post("/visura/ispezione-ipotecaria")
async def _richiedi_ispezione_ipotecaria(
    request: IspezioneIpotecariaInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_ispezione_ipotecaria(request, service, force=force)


@app.post("/visura/workflow")
async def _execute_workflow(
    body: dict,
    _: None = Depends(require_api_key),
):
    """Execute a named multi-step workflow using Sister's own query service."""
    from pydantic import ValidationError

    from .workflows import run_workflow

    try:
        result = await run_workflow(body)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(result, status_code=422 if result.get("event") == "error" else 200)


@app.post("/visura/{search_type}")
async def _richiedi_generic(
    request: Request,
    search_type: str,
    provincia: str,
    force: bool = False,
    comune: Optional[str] = None,
    tipo_catasto: str = "T",
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    """Single-step query by search type (indirizzo, partita, nota, mappa, ...).

    Every other query parameter is an input of the SISTER form: the accepted ones are listed per query in
    ``sister.query_forms`` (the same spec that generates ``sister query <command>`` and ``/web/forms``).
    """
    from .query_forms import QUERY_FORMS

    known_types = {q.search_type for q in QUERY_FORMS.values() if q.method == "generic_search"}
    name = search_type.replace("_", "-")
    name = {"ispezioni-cart": "ispezioni-cartacee"}.get(name, name)  # the CLI/older clients say ispezioni_cart
    if name not in known_types:
        raise HTTPException(status_code=404, detail=f"Search type '{search_type}' not found")

    params = {
        key: value
        for key, value in request.query_params.items()
        if key not in {"provincia", "force", "comune", "tipo_catasto"} and value != ""
    }
    internal = {"ispezioni-cartacee": "ispezioni_cart"}.get(name, name.replace("-", "_"))
    return await richiedi_generic_sister(
        search_type=internal,
        provincia=provincia,
        service=service,
        comune=comune,
        tipo_catasto=tipo_catasto,
        params=params,
        force=force,
    )


@app.get("/health")
async def _health_check():
    if visura_service is not None:
        return await health_check(visura_service)
    db_stats = await count_responses()
    return JSONResponse(
        {
            "status": "degraded",
            "auth": {"state": "unavailable", "message": "Browser service not initialized"},
            "auth_ready": False,
            "authenticated": False,
            "queue_size": 0,
            "pending_requests": 0,
            "database": db_stats,
        }
    )


@app.get("/visura/history")
async def _visura_history(
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_api_key),
):
    return await visura_history(provincia, comune, foglio, particella, tipo_catasto, limit, offset)


@app.get("/visura/{request_id}")
async def _ottieni_visura(
    request_id: str,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await ottieni_visura(request_id, service)


@app.post("/visura/queue/clear")
async def _clear_queue(
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    """Drain all pending requests from the queue without stopping the worker."""
    drained = 0
    while not service.request_queue.empty():
        try:
            service.request_queue.get_nowait()
            service.request_queue.task_done()
            drained += 1
        except Exception:
            break
    service.pending_request_ids.clear()
    logger.info("Queue cleared: %d requests drained", drained)
    return {"drained": drained, "queue_size": service.request_queue.qsize()}


@app.post("/shutdown")
async def _graceful_shutdown_endpoint(
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_shutdown_api_key),
):
    return await graceful_shutdown_endpoint(service)


@app.post("/sezioni/extract")
async def _extract_sezioni(
    request: SezioniExtractionRequest,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await extract_sezioni(request, service)
