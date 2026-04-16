import asyncio  # noqa: F401 (used by tests via main_module.asyncio)
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from rich.logging import RichHandler

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
    WorkflowInput,
)
from .routes import (
    download_documents,
    execute_workflow,
    execute_workflow_stream,
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
from .services import PageLogger, VisuraService

# Carica variabili d'ambiente da .env
load_dotenv()

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
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global visura_service
    await init_db()
    PageLogger.reset_session()
    try:
        visura_service = VisuraService()
        await visura_service.initialize(background_auth=True)
        logger.info("Servizio visure avviato (autenticazione in background)")
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

app = FastAPI(title="SISTER - Cadastral Data Service", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Theme, static files, and web UI
# ---------------------------------------------------------------------------

from pathlib import Path
from fastapi.staticfiles import StaticFiles

try:
    from aecs4u_theme import ThemeConfig, setup_theme

    _sister_dir = Path(__file__).parent
    _templates_dir = _sister_dir / "templates"
    _static_dir = _sister_dir / "static"

    # --- Auth setup (before theme) ---
    try:
        from aecs4u_auth import AuthConfig, setup_auth

        auth_config = AuthConfig(
            AECS4U_SITE_ID="sister",
            AECS4U_SITE_NAME="SISTER",
            CLERK_AFTER_SIGN_IN_URL="/web/",
            CLERK_AFTER_SIGN_UP_URL="/web/",
        )
        auth_setup = setup_auth(
            app,
            config=auth_config,
            include_routes=True,
            mount_static=True,
            setup_exception_handlers=True,
        )
        app.state.auth_setup = auth_setup
        app.state.auth_config = auth_config
        logger.info("Autenticazione configurata (mode=%s)", auth_config.AUTH_MODE)
    except ImportError:
        logger.warning("aecs4u-auth non disponibile: autenticazione disabilitata")
    except Exception as e:
        logger.warning("Errore configurazione auth: %s", e)

    # --- Theme setup ---
    theme_config = ThemeConfig(
        site_id="sister",
        site_name="SISTER",
        site_tagline="Cadastral Data Extraction Service",
        primary_color="#1e40af",
        sidebar_enabled=True,
        footer_enabled=True,
        footer_copyright="AECS4U Srl",
    )

    theme_setup = setup_theme(
        app,
        config=theme_config,
        templates_dir=str(_templates_dir),
        mount_static=True,
    )
    app.state.theme_setup = theme_setup

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


@app.post("/visura/workflow")
async def _execute_workflow(
    request: WorkflowInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await execute_workflow(request, service)


@app.post("/visura/workflow/stream")
async def _execute_workflow_stream(
    request: WorkflowInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await execute_workflow_stream(request, service)


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


@app.post("/visura/{search_type}")
async def _richiedi_generic(
    search_type: str,
    provincia: str,
    force: bool = False,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
    comune: Optional[str] = None,
    tipo_catasto: str = "T",
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    indirizzo: Optional[str] = None,
    numero_nota: Optional[str] = None,
    anno_nota: Optional[str] = None,
    partita: Optional[str] = None,
):
    valid_types = {"indirizzo", "partita", "nota", "mappa", "export-mappa", "originali", "fiduciali", "ispezioni", "ispezioni-cartacee", "elaborato-planimetrico", "riepilogo-visure", "richieste", "ipotecaria-stato", "ipotecaria-elenchi"}
    normalized = search_type.replace("-", "_")
    if normalized.replace("_", "-") not in {t.replace("_", "-") for t in valid_types}:
        raise HTTPException(status_code=404, detail=f"Search type '{search_type}' not found")

    params = {}
    if foglio:
        params["foglio"] = foglio
    if particella:
        params["particella"] = particella
    if indirizzo:
        params["indirizzo"] = indirizzo
    if numero_nota:
        params["numero_nota"] = numero_nota
    if anno_nota:
        params["anno_nota"] = anno_nota
    if partita:
        params["partita"] = partita

    return await richiedi_generic_sister(
        search_type=normalized,
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
    return JSONResponse({
        "status": "degraded",
        "auth": {"state": "unavailable", "message": "Browser service not initialized"},
        "auth_ready": False,
        "authenticated": False,
        "queue_size": 0,
        "pending_requests": 0,
        "database": db_stats,
    })


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
