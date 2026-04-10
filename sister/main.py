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
    SezioniExtractionRequest,
    VisuraInput,
    VisuraIntestatiInput,
    VisuraRequest,
    VisuraResponse,
)
from .routes import (
    extract_sezioni,
    graceful_shutdown_endpoint,
    health_check,
    ottieni_visura,
    richiedi_intestati_immobile,
    richiedi_visura,
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
    visura_service = VisuraService()
    await visura_service.initialize()
    logger.info("Servizio visure avviato")
    yield
    logger.info("Shutdown in corso, eseguendo logout...")
    if visura_service:
        await visura_service.graceful_shutdown()
    logger.info("Servizio visure fermato con graceful shutdown")


# ---------------------------------------------------------------------------
# App + route registration
# ---------------------------------------------------------------------------

app = FastAPI(title="Servizio Visure Catastali", lifespan=lifespan)


@app.post("/visura")
async def _richiedi_visura(
    request: VisuraInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_visura(request, service)


@app.post("/visura/intestati")
async def _richiedi_intestati_immobile(
    request: VisuraIntestatiInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    return await richiedi_intestati_immobile(request, service)


@app.get("/health")
async def _health_check(service: VisuraService = Depends(get_visura_service)):
    return await health_check(service)


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
