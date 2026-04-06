import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Self
from uuid import uuid4

from aecs4u_auth.browser import BrowserConfig, PageLogger
from aecs4u_auth.browser import BrowserManager as AuthBrowserManager
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import Page
from pydantic import BaseModel, Field, field_validator, model_validator
from rich.logging import RichHandler

from utils import extract_all_sezioni, run_visura, run_visura_immobile

# Carica variabili d'ambiente da .env
load_dotenv()

# Configurazione logging con Rich
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
logger = logging.getLogger("visura-api")


# Custom Exception Classes
class VisuraError(Exception):
    """Base exception for visura-related errors"""

    pass


class AuthenticationError(VisuraError):
    """Raised when authentication fails"""

    pass


class BrowserError(VisuraError):
    """Raised when browser operations fail"""

    pass


class QueueFullError(VisuraError):
    """Raised when the request queue is at capacity"""

    pass


@dataclass
class VisuraRequest:
    request_id: str
    tipo_catasto: str
    provincia: str
    comune: str
    foglio: str
    particella: str
    sezione: Optional[str] = None
    subalterno: Optional[str] = None  # Opzionale: restringe la ricerca per fabbricati
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class VisuraIntestatiRequest:
    """Richiesta per ottenere gli intestati di un immobile specifico"""

    request_id: str
    tipo_catasto: str
    provincia: str
    comune: str
    foglio: str
    particella: str
    subalterno: Optional[str] = None
    sezione: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class VisuraResponse:
    request_id: str
    success: bool
    tipo_catasto: str
    data: Optional[Dict] = None
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


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
        self.request_queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max_size)
        self.response_store: Dict[str, VisuraResponse] = {}
        self.pending_request_ids: set[str] = set()
        self.expired_request_ids: Dict[str, datetime] = {}
        self.response_ttl_seconds = self._parse_positive_int_env("RESPONSE_TTL_SECONDS", 6 * 3600)
        self.response_max_items = self._parse_positive_int_env("RESPONSE_MAX_ITEMS", 5000)
        self.processing = False
        self._worker_task: Optional[asyncio.Task] = None

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
                        self._store_response(response)
                        logger.info(f"Processata richiesta visura {request.request_id}")
                        should_sleep = True

                    elif isinstance(request, VisuraIntestatiRequest):
                        response = await self.browser_manager.esegui_visura_intestati(request)
                        self._store_response(response)
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

    async def _stop_worker(self):
        """Ferma il worker in modo pulito e attende la sua terminazione."""
        task = self._worker_task
        self.processing = False

        if task is None:
            return

        if not task.done():
            await self.request_queue.put(None)
            try:
                await asyncio.wait_for(task, timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Timeout fermando il worker, forzo cancellazione task")
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        self._worker_task = None
        self.pending_request_ids.clear()

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

    def _store_response(self, response: VisuraResponse):
        self.response_store[response.request_id] = response
        self.expired_request_ids.pop(response.request_id, None)
        self._cleanup_response_store()

    async def add_request(self, request: VisuraRequest) -> str:
        """Aggiunge una richiesta alla coda"""
        if not self.processing:
            raise RuntimeError("Servizio non in esecuzione: impossibile accodare richieste")

        if self.request_queue.full():
            raise QueueFullError(f"Coda piena (max {self.queue_max_size})")

        self.pending_request_ids.add(request.request_id)
        self.expired_request_ids.pop(request.request_id, None)
        await self.request_queue.put(request)
        logger.info(
            f"Richiesta visura {request.request_id} aggiunta alla coda (posizione: {self.request_queue.qsize()})"
        )
        return request.request_id

    async def add_intestati_request(self, request: VisuraIntestatiRequest) -> str:
        """Aggiunge una richiesta intestati alla coda"""
        if not self.processing:
            raise RuntimeError("Servizio non in esecuzione: impossibile accodare richieste")

        if self.request_queue.full():
            raise QueueFullError(f"Coda piena (max {self.queue_max_size})")

        self.pending_request_ids.add(request.request_id)
        self.expired_request_ids.pop(request.request_id, None)
        await self.request_queue.put(request)
        logger.info(
            f"Richiesta intestati {request.request_id} aggiunta alla coda (posizione: {self.request_queue.qsize()})"
        )
        return request.request_id

    async def get_response(self, request_id: str) -> Optional[VisuraResponse]:
        """Ottiene la risposta per un request_id"""
        response = self.response_store.get(request_id)
        if response and self._is_response_expired(response):
            self.response_store.pop(request_id, None)
            self._mark_request_expired(request_id)
            return None
        return response

    def get_request_state(self, request_id: str) -> str:
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


# Global service instance - initialized during lifespan
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
        return  # Nessuna protezione se API_KEY non è impostata

    if not x_api_key or not secrets.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="API key non valida")


def require_shutdown_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Verifica API key per endpoint amministrativi sensibili."""
    if not shutdown_api_key:
        raise HTTPException(status_code=503, detail="Endpoint disabilitato: SHUTDOWN_API_KEY non configurata")

    if not x_api_key or not secrets.compare_digest(x_api_key, shutdown_api_key):
        raise HTTPException(status_code=401, detail="API key non valida")


# Signal handler per shutdown graceful
# Nota: NON usiamo signal handler custom perché sys.exit() uccide il processo
# prima che il logout async possa completare. Uvicorn gestisce già SIGINT/SIGTERM
# e passa per il lifespan shutdown dove il logout viene eseguito correttamente.


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global visura_service
    PageLogger.reset_session()  # Nuova sessione di log per ogni avvio
    visura_service = VisuraService()
    await visura_service.initialize()
    logger.info("Servizio visure avviato")
    yield
    # Shutdown — uvicorn arriva qui dopo SIGINT/SIGTERM
    logger.info("Shutdown in corso, eseguendo logout...")
    if visura_service:
        await visura_service.graceful_shutdown()
    logger.info("Servizio visure fermato con graceful shutdown")


# API FastAPI
app = FastAPI(title="Servizio Visure Catastali", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Modelli di richiesta
# ---------------------------------------------------------------------------


class VisuraInput(BaseModel):
    """Richiesta per una visura catastale (solo dati catastali, senza intestati)"""

    provincia: str = Field(..., min_length=1, description="Nome della provincia")
    comune: str = Field(..., min_length=1, description="Nome del comune")
    foglio: str = Field(..., min_length=1, description="Numero di foglio")
    particella: str = Field(..., min_length=1, description="Numero di particella")
    sezione: Optional[str] = Field(None, description="Sezione (opzionale)")
    subalterno: Optional[str] = Field(None, description="Subalterno (opzionale, restringe la ricerca per fabbricati)")
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati (se omesso esegue entrambi)"
    )

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


class VisuraIntestatiInput(BaseModel):
    """Richiesta per ottenere gli intestati di un immobile specifico"""

    provincia: str = Field(..., min_length=1, description="Nome della provincia")
    comune: str = Field(..., min_length=1, description="Nome del comune")
    foglio: str = Field(..., min_length=1, description="Numero di foglio")
    particella: str = Field(..., min_length=1, description="Numero di particella")
    tipo_catasto: str = Field(..., pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati")
    subalterno: Optional[str] = Field(None, description="Numero di subalterno (obbligatorio per Fabbricati)")
    sezione: Optional[str] = Field(None, description="Sezione (opzionale)")

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized

    @field_validator("subalterno", mode="before")
    @classmethod
    def normalize_subalterno(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_subalterno(self) -> Self:
        if self.tipo_catasto == "F" and not self.subalterno:
            raise ValueError("subalterno è obbligatorio per i fabbricati (tipo_catasto='F')")
        if self.tipo_catasto == "T" and self.subalterno:
            raise ValueError("subalterno non va indicato per i terreni (tipo_catasto='T')")
        return self


class SezioniExtractionRequest(BaseModel):
    """Richiesta per l'estrazione delle sezioni territoriali"""

    tipo_catasto: str = Field("T", pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati")
    max_province: int = Field(
        200, ge=1, le=200, description="Numero massimo di province da processare (default: tutte)"
    )

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@app.post("/visura")
async def richiedi_visura(
    request: VisuraInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    """Richiede una visura catastale fornendo direttamente i dati catastali"""
    try:
        sezione = None if request.sezione == "_" else request.sezione

        tipos_catasto = [request.tipo_catasto] if request.tipo_catasto else ["T", "F"]
        request_ids = []

        for tipo_catasto in tipos_catasto:
            request_id = f"req_{tipo_catasto}_{uuid4().hex}"
            visura_req = VisuraRequest(
                request_id=request_id,
                tipo_catasto=tipo_catasto,
                provincia=request.provincia,
                comune=request.comune,
                sezione=sezione,
                foglio=request.foglio,
                particella=request.particella,
                subalterno=request.subalterno,
            )
            await service.add_request(visura_req)
            request_ids.append(request_id)

        return JSONResponse(
            {
                "request_ids": request_ids,
                "tipos_catasto": tipos_catasto,
                "status": "queued",
                "message": f"Richieste aggiunte alla coda per {request.comune} F.{request.foglio} P.{request.particella}",
            }
        )

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Errore nella richiesta visura: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/visura/{request_id}")
async def ottieni_visura(request_id: str, service: VisuraService = Depends(get_visura_service)):
    """Ottiene il risultato di una visura"""
    try:
        response = await service.get_response(request_id)

        if response is None:
            request_state = service.get_request_state(request_id)

            if request_state == "processing":
                return JSONResponse(
                    {"request_id": request_id, "status": "processing", "message": "Richiesta in elaborazione"}
                )

            if request_state == "expired":
                return JSONResponse(
                    {
                        "request_id": request_id,
                        "status": "expired",
                        "message": "Risultato non più disponibile (cache scaduta)",
                    },
                    status_code=410,
                )

            raise HTTPException(status_code=404, detail="request_id non trovato")

        return JSONResponse(
            {
                "request_id": request_id,
                "tipo_catasto": response.tipo_catasto,
                "status": "completed" if response.success else "error",
                "data": response.data,
                "error": response.error,
                "timestamp": response.timestamp.isoformat(),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Errore nell'ottenere visura: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/visura/intestati")
async def richiedi_intestati_immobile(
    request: VisuraIntestatiInput,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    """Richiede gli intestati per un immobile specifico."""
    try:
        sezione = None if request.sezione == "_" else request.sezione

        request_id = f"intestati_{request.tipo_catasto}_{uuid4().hex}"

        intestati_request = VisuraIntestatiRequest(
            request_id=request_id,
            tipo_catasto=request.tipo_catasto,
            provincia=request.provincia,
            comune=request.comune,
            foglio=request.foglio,
            particella=request.particella,
            subalterno=request.subalterno,
            sezione=sezione,
        )

        await service.add_intestati_request(intestati_request)

        return JSONResponse(
            {
                "request_id": request_id,
                "tipo_catasto": request.tipo_catasto,
                "subalterno": request.subalterno,
                "status": "queued",
                "message": f"Richiesta intestati aggiunta alla coda per {request.comune} F.{request.foglio} P.{request.particella}",
                "queue_position": service.request_queue.qsize(),
            }
        )

    except HTTPException:
        raise
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Errore nella richiesta intestati: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check(service: VisuraService = Depends(get_visura_service)):
    """Controlla lo stato del servizio"""
    return JSONResponse(
        {
            "status": "healthy",
            "authenticated": service.browser_manager.authenticated,
            "queue_size": service.request_queue.qsize(),
            "pending_requests": len(service.pending_request_ids),
            "cached_responses": len(service.response_store),
            "response_ttl_seconds": service.response_ttl_seconds,
            "response_max_items": service.response_max_items,
        }
    )


@app.post("/shutdown")
async def graceful_shutdown_endpoint(
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_shutdown_api_key),
):
    """Effettua uno shutdown graceful del servizio"""
    try:
        logger.info("Shutdown graceful richiesto via API")
        await service.graceful_shutdown()
        return JSONResponse({"status": "success", "message": "Shutdown graceful completato"})
    except Exception as e:
        logger.error(f"Errore durante shutdown graceful via API: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sezioni/extract")
async def extract_sezioni(
    request: SezioniExtractionRequest,
    service: VisuraService = Depends(get_visura_service),
    _: None = Depends(require_api_key),
):
    """
    Estrae le sezioni territoriali d'Italia per il tipo catasto specificato.
    ATTENZIONE: Questa operazione può richiedere diverse ore!
    I dati vengono restituiti nella risposta.
    """
    try:
        logger.info(
            f"Iniziando estrazione sezioni per tipo catasto: {request.tipo_catasto}, max province: {request.max_province}"
        )

        sezioni_data = await service.browser_manager.esegui_extract_sezioni(request.tipo_catasto, request.max_province)

        if not sezioni_data:
            return JSONResponse({"status": "no_data", "message": "Nessuna sezione estratta", "count": 0})

        logger.info(f"Estrazione sezioni completata: {len(sezioni_data)} totali")

        return JSONResponse(
            {
                "status": "success",
                "message": f"Estrazione completata per tipo catasto {request.tipo_catasto}",
                "total_extracted": len(sezioni_data),
                "tipo_catasto": request.tipo_catasto,
                "sezioni": sezioni_data,
            }
        )

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Errore durante estrazione sezioni: {e}")
        raise HTTPException(status_code=500, detail=str(e))
