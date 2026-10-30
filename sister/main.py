import asyncio  # noqa: F401 (used by tests via main_module.asyncio)
import inspect
import logging
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from dotenv import load_dotenv

# Load configuration before importing modules that read environment variables at import time.
load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)  # shared workspace-root defaults (project .env above wins)

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
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
    cancel_section_extraction,
    download_documents,
    extract_sezioni,
    get_section_extraction,
    graceful_shutdown_endpoint,
    health_check,
    ottieni_visura,
    list_section_extractions,
    richiedi_elenco_immobili,
    richiedi_generic_sister,
    richiedi_intestati_immobile,
    richiedi_ispezione_ipotecaria,
    richiedi_visura,
    richiedi_visura_persona_giuridica,
    richiedi_visura_soggetto,
    resume_section_extraction,
    visura_history,
)
from .passwords import plaintext_users_allowed, require_hashed_passwords, verify_local_password
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


async def get_visura_service() -> VisuraService:
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


async def require_api_key_dependency(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """Async FastAPI adapter; keep the sync helper callable by existing tests/tools."""
    require_api_key(x_api_key)


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
async def _reuse_previous(request: Request) -> None:
    """``?reuse_previous=true``: an identical query answered before the cache window is returned (stale), not re-run."""
    from .services import reuse_previous_var

    reuse_previous_var.set(request.query_params.get("reuse_previous", "").lower() in {"1", "true", "yes"})


app = FastAPI(
    title="SISTER - Cadastral Data Service",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(_reuse_previous)],
)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Keep this authenticated application and its user data out of search indexes."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "static" / "icons" / "favicon.ico", media_type="image/x-icon")


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt(request: Request):
    """Publish a current security contact through SISTER's public contact page."""
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    contact = request.url_for("web_contact")
    return PlainTextResponse(
        f"Contact: {contact}\nExpires: {expires}\nPreferred-Languages: it, en\n",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# Delivery: compress HTML/JSON; exclude SSE and, where the installed Starlette supports the option, already-compressed
# formats. Versioned theme assets (?v=) are immutable; SISTER's own static files are revalidated through their ETag.
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware  # noqa: E402

_gzip_options: dict[str, object] = {"minimum_size": 1024}
if "exclude_content_types" in inspect.signature(GZipMiddleware).parameters:
    _gzip_options["exclude_content_types"] = (
        *DEFAULT_EXCLUDED_CONTENT_TYPES,
        "application/pdf",
        "image/",
        "application/zip",
    )
else:
    logger.warning(
        "Installed Starlette does not support custom GZip content-type exclusions; "
        "using its built-in exclusions only"
    )
app.add_middleware(GZipMiddleware, **_gzip_options)


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

    response = get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")
    html = response.body.decode("utf-8")
    html = html.replace("    <html>", '    <html lang="it">', 1)
    html = html.replace(
        "    </head>",
        '    <link rel="stylesheet" href="/static/css/swagger_ui.css?v=20261030a">\n    </head>',
        1,
    )
    html = html.replace(
        "    <body>",
        '''    <body>
    <a class="sister-swagger-skip" href="#main-content">Salta alla documentazione API</a>
    <header class="sister-swagger-header">
      <nav aria-label="Navigazione SISTER"><a href="/">SISTER</a></nav>
      <div><h1>Documentazione API</h1><p>Esplora le operazioni disponibili in SISTER.</p></div>
      <button id="sister-swagger-theme" type="button" aria-pressed="false">Tema scuro</button>
    </header>
    <main id="main-content" class="sister-swagger-main">''',
        1,
    )
    html = html.replace('    <script src="', '    </main>\n    <script src="', 1)
    html = html.replace(
        "    </body>",
        '    <script src="/static/js/swagger_ui.js?v=20261030a" defer></script>\n    </body>',
        1,
    )
    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        },
    )


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
    if exc.status_code == 500:
        request_id = _error_request_id(request)
        logger.error("HTTP 500 response id=%s path=%s", request_id, request.url.path)
        if wants_html and theme is not None:
            try:
                response = theme.render(
                    "error.html",
                    request,
                    user=getattr(request.state, "user", None),
                    status_code=500,
                    detail="",
                    request_id=request_id,
                )
                response.status_code = 500
                response.headers["X-Request-ID"] = request_id
                response.headers["Cache-Control"] = "no-store"
                return response
            except Exception:  # never let the error page itself fail the request
                logger.exception("Rendering error.html failed for request id=%s", request_id)
        return JSONResponse(
            {"detail": "Internal Server Error", "request_id": request_id},
            status_code=500,
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        )
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


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Render generic server failures safely and give operators a correlation id."""
    request_id = _error_request_id(request)
    logger.exception("Unhandled request error id=%s path=%s", request_id, request.url.path)
    wants_html = "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(
        ("/visura", "/auth", "/api", "/sezioni", "/health")
    )
    theme = getattr(request.app.state, "theme_setup", None)
    if wants_html and theme is not None:
        try:
            response = theme.render(
                "error.html",
                request,
                user=getattr(request.state, "user", None),
                status_code=500,
                detail="",
                request_id=request_id,
            )
            response.status_code = 500
            response.headers["X-Request-ID"] = request_id
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception:
            logger.exception("Rendering error.html failed for request id=%s", request_id)
    return JSONResponse(
        {"detail": "Internal Server Error", "request_id": request_id},
        status_code=500,
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )


def _error_request_id(request: Request) -> str:
    """Reuse the tracing middleware ID so the user-facing support code matches the server log."""
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) and request_id else uuid4().hex[:16]

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

        # Like crowdaction: Clerk SSO next to the local (file-backed) accounts when the Clerk keys are configured
        # (NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY / CLERK_SECRET_KEY are read from the environment), local only otherwise.
        _clerk_configured = bool(
            os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "").strip() and os.getenv("CLERK_SECRET_KEY", "").strip()
        )
        auth_config = AuthConfig(
            AECS4U_SITE_ID="sister",
            AECS4U_SITE_NAME="SISTER",
            AUTH_MODE="clerk_and_local" if _clerk_configured else "local",
            CLERK_AFTER_SIGN_IN_URL="/",
            CLERK_AFTER_SIGN_UP_URL="/",
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
        if _clerk_configured:
            # The package's demo callback page has no CSP nonce on its inline script; the nonce-aware one from
            # aecs4u_auth.routers.clerk wins (same path). Only its page router: the session sync stays the package's.
            from aecs4u_auth.routers.clerk import create_clerk_router

            app.include_router(create_clerk_router(prefix="/auth", session_url="/auth/clerk/session").page_router)
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
        # Account keys (never passwords) the admin purchase form offers: purchases match on ``id:<username>``.
        app.state.local_user_keys = sorted({f"id:{name}" for name, _ in _local_users.values()})
        _allow_plaintext = plaintext_users_allowed(os.getenv("ALLOW_PLAINTEXT_USERS"))
        try:
            plaintext_count = require_hashed_passwords(
                (password for _, password in _local_users.values()),
                allow_plaintext=_allow_plaintext,
            )
        except ValueError as exc:
            logger.critical("Refusing local authentication startup: %s", exc)
            raise RuntimeError(str(exc)) from exc
        if plaintext_count:
            logger.warning(
                "%d local user(s) have a plain-text password in %s (use scripts/hash_local_password.py)",
                plaintext_count,
                _local_users_path.name,
            )
        if _local_users:
            logger.info("Loaded %d local authentication users", len(_local_users))
        else:
            logger.warning("No local authentication users loaded from %s", _local_users_path)

        # Local users receive the admin role only when their username is listed in SISTER_ADMIN_USERS.
        _admin_usernames = {
            name.strip().casefold() for name in os.getenv("SISTER_ADMIN_USERS", "").split(",") if name.strip()
        }
        if not _admin_usernames:
            logger.warning(
                "SISTER_ADMIN_USERS non configurata: nessun utente locale riceve il ruolo admin"
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
    theme_config.site_name = "Sister"
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
    if not theme_config.favicon:
        theme_config.favicon = "/static/icons/favicon.ico"
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

    # Theme pages (catasto/forms.html, catasto/results.html) extend this shell, which carries the SISTER sidebar.
    theme_setup.templates.env.globals["base_template"] = "_base.html"
    theme_setup.templates.env.globals["csrf_token"] = _csrf_token
    theme_setup.templates.env.globals["csrf_input"] = _csrf_input
    # Templates hide operator-only controls with the same explicit role check used by protected routes.
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

    # Cadastral map assets and tile/identify API used by /documents?view=cadastral-map
    try:
        from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
        from land_registry import consumer as land_registry_consumer
        from land_registry.consumer import install_land_registry_consumer

        # Keep Land Registry's reusable map components beside the map engine,
        # while exposing them to SISTER templates through a namespaced loader.
        land_registry_templates = Path(land_registry_consumer.__file__).resolve().parent / "templates"
        template_env = theme_setup.templates.env
        template_env.loader = ChoiceLoader([
            PrefixLoader({"land_registry": FileSystemLoader(land_registry_templates)}),
            template_env.loader,
        ])
        # The installed land_registry (0.1.4) still places a parcel without outline at the map centre, which shows
        # a Palermo parcel in Milan, and has no marker clustering. Until a release carries both, SISTER serves its
        # copy of the two files (sister/static/land_registry/, from ../land-registry) at the package's URLs;
        # these routes come first, so they win over the package's static mount.
        from fastapi.responses import FileResponse

        def _land_registry_override(name: str, media_type: str):
            path = _static_dir / "land_registry" / name

            async def serve():
                return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

            return serve

        for override_name, override_type in (
            ("document_parcel_map.js", "text/javascript"),
            ("document_parcel_map.css", "text/css"),
        ):
            app.add_api_route(
                f"/land-registry/static/{override_name}",
                _land_registry_override(override_name, override_type),
                methods=["GET"],
                include_in_schema=False,
            )
        install_land_registry_consumer(app, prefix="/land-registry")
    except ImportError as e:
        logger.warning("land_registry consumer non disponibile: %s", e)

    # Mount outputs directory for screenshots
    from .database import OUTPUTS_DIR

    _outputs_dir = Path(OUTPUTS_DIR)
    _outputs_dir.mkdir(exist_ok=True)
    app.mount("/outputs", StaticFiles(directory=str(_outputs_dir)), name="outputs")

    # Include web routes
    from .web import _require_auth, router as web_router

    try:
        from aecs4u_email import create_report_sharing_router
    except ImportError as e:
        # Older shared-venv installs may not include report sharing yet; keep the
        # rest of SISTER's web UI available in that case.
        logger.warning("Email report-sharing routes non disponibili: %s", e)
    else:
        # Register these before the web router's /api/ui/{path} proxy.
        app.include_router(create_report_sharing_router(auth_dependency=_require_auth, app_name="SISTER"))

    app.include_router(web_router)

    try:
        from .purchases_billing import mount_billing

        mount_billing(app)  # dormant unless STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET and SISTER_DOCUMENT_PRICE_ID are set
    except Exception as exc:  # noqa: BLE001 - a payment setup problem must not take the rest of the UI down
        logger.error("Pagamenti documenti non attivati: %s", exc)

    try:
        from .feedback_admin import router as feedback_admin_router

        app.include_router(feedback_admin_router)
        logger.info("Feedback admin router registrato")
    except ImportError as e:
        logger.warning("Feedback admin router non disponibile: %s", e)

    logger.info("Web UI inizializzata")
except ImportError as exc:
    # A missing import used to switch the whole web UI off silently (every page 404 while /health stayed green).
    logger.error("Web UI non disponibile: %s", exc)
    if os.getenv("SISTER_ALLOW_NO_WEB_UI", "").strip().lower() not in {"1", "true", "yes"}:
        raise


@app.post("/visura")
async def _richiedi_visura(
    request: VisuraInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_visura(request, service, force=force)


@app.post("/visura/intestati")
async def _richiedi_intestati_immobile(
    request: VisuraIntestatiInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_intestati_immobile(request, service, force=force)


@app.post("/visura/soggetto")
async def _richiedi_visura_soggetto(
    request: VisuraSoggettoInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_visura_soggetto(request, service, force=force)


@app.post("/visura/persona-giuridica")
async def _richiedi_visura_persona_giuridica(
    request: VisuraPersonaGiuridicaInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_visura_persona_giuridica(request, service, force=force)


@app.post("/visura/elenco-immobili")
async def _richiedi_elenco_immobili(
    request: ElencoImmobiliInput,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_elenco_immobili(request, service, force=force)


@app.post("/visura/download-documents")
async def _download_documents(
    since: str = "",
    skip_known: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await download_documents(service, since=since, skip_known=skip_known)


@app.post("/visura/ispezione-ipotecaria")
async def _richiedi_ispezione_ipotecaria(
    request: IspezioneIpotecariaInput,
    force: bool = False,
    reuse_cached: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await richiedi_ispezione_ipotecaria(
        request, service, force=force, reuse_cached=reuse_cached
    )


async def _workflow_backend_request(method: str, path: str, **kwargs):
    """Forward workflow lifecycle calls to OpenData, the owner of persisted runs."""
    import httpx

    base_url = os.getenv("OPENDATA_API_URL", "http://localhost:8024").rstrip("/")
    headers = {}
    if api_key := os.getenv("OPENDATA_API_KEY"):
        headers["X-API-Key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.0)) as client:
            response = await client.request(method, f"{base_url}{path}", headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Workflow service unavailable: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)
    if not response.content:
        return {}
    return response.json()


async def _run_workflow_backend(payload: dict):
    return await _workflow_backend_request("POST", "/catasto/workflow/run", json=payload)


@app.post("/catasto/workflow/run")
async def _workflow_run(
    body: dict,
    resume: bool = False,
    workflow_id: Optional[str] = None,
    _: None = Depends(require_api_key_dependency),
):
    if resume:
        if not workflow_id:
            raise HTTPException(status_code=422, detail="workflow_id is required to resume a run")
        return await _workflow_backend_request(
            "POST",
            "/catasto/workflow/run",
            params={"resume": "true", "workflow_id": workflow_id},
            json=body,
        )
    return await _run_workflow_backend(body)


@app.get("/catasto/workflow/runs/{workflow_id}")
async def _workflow_status(workflow_id: str, _: None = Depends(require_api_key_dependency)):
    return await _workflow_backend_request("GET", f"/catasto/workflow/runs/{workflow_id}")


@app.post("/catasto/workflow/runs/{workflow_id}/cancel")
async def _workflow_cancel(workflow_id: str, _: None = Depends(require_api_key_dependency)):
    return await _workflow_backend_request("POST", f"/catasto/workflow/runs/{workflow_id}/cancel")


@app.post("/visura/workflow")
async def _execute_workflow(body: dict, _: None = Depends(require_api_key_dependency)):
    """Compatibility route for existing SISTER workflow clients."""
    return await _run_workflow_backend(body)


@app.post("/visura/{search_type}")
async def _richiedi_generic(
    request: Request,
    search_type: str,
    provincia: str,
    force: bool = False,
    comune: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    """Single-step query by search type (indirizzo, partita, nota, mappa, ...).

    Every other query parameter is an input of the SISTER form: the accepted ones are listed per query in
    ``sister.query_forms`` (the same spec that generates ``sister query <command>`` and ``/forms``).
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
        if key not in {"provincia", "force", "reuse_previous", "comune", "tipo_catasto"} and value != ""
    }
    internal = {"ispezioni-cartacee": "ispezioni_cart"}.get(name, name.replace("-", "_"))
    # a subject search covers Fabbricati and Terreni ("E"); it used to fall back to "T" and listed only the Terreni
    tipo_catasto = (tipo_catasto or ("E" if internal in {"soggetto_immobili", "soggetto_documento"} else "T")).upper()
    return await richiedi_generic_sister(
        search_type=internal,
        provincia=provincia,
        service=service,
        comune=comune,
        tipo_catasto=tipo_catasto,
        params=params,
        force=force,
    )


async def _health_is_privileged(request: Request, x_api_key: Optional[str]) -> bool:
    """Detail (queue sizes, DB totals, auth mode) only for the API key or a signed-in admin; probes get the status."""
    configured = os.getenv("API_KEY") or ""
    if configured and x_api_key and secrets.compare_digest(x_api_key, configured):
        return True
    try:
        from .web import _is_admin, _optional_user

        user = await _optional_user(request)
        return bool(user) and _is_admin(user)
    except Exception:  # noqa: BLE001 - a broken auth backend must not take the probe down
        return False


@app.api_route("/health", methods=["GET", "HEAD"])
async def _health_check(request: Request, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    privileged = await _health_is_privileged(request, x_api_key)
    if visura_service is not None:
        response = await health_check(visura_service)
        if privileged:
            return response
        try:
            payload = response if isinstance(response, dict) else json.loads(response.body)
            status = payload.get("status", "degraded")
        except (ValueError, TypeError, AttributeError):
            status = "degraded"
        return JSONResponse({"status": status})
    if not privileged:
        return JSONResponse({"status": "degraded"})
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
    _: None = Depends(require_api_key_dependency),
):
    return await visura_history(provincia, comune, foglio, particella, tipo_catasto, limit, offset)


@app.get("/visura/{request_id}")
async def _ottieni_visura(
    request_id: str,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await ottieni_visura(request_id, service)


@app.post("/visura/queue/clear")
async def _clear_queue(
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
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
    _: None = Depends(require_api_key_dependency),
):
    return await extract_sezioni(request, service)


@app.get("/sezioni/extract")
async def _list_section_extractions(
    limit: int = 50,
    _: None = Depends(require_api_key_dependency),
):
    return await list_section_extractions(limit)


@app.get("/sezioni/extract/{job_id}")
async def _get_section_extraction(
    job_id: str,
    _: None = Depends(require_api_key_dependency),
):
    return await get_section_extraction(job_id)


@app.post("/sezioni/extract/{job_id}/cancel")
async def _cancel_section_extraction(
    job_id: str,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await cancel_section_extraction(job_id, service)


@app.post("/sezioni/extract/{job_id}/resume")
async def _resume_section_extraction(
    job_id: str,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key_dependency),
):
    return await resume_section_extraction(job_id, service)
