"""Data models, exceptions, and Pydantic input schemas for sister.

WorkflowInput, WORKFLOW_PRESETS, STEP_METADATA, and _DEPTH_ORDER have been
moved to opendata/opendata/workflows/models.py. Sister retains only the
atomic cadastral scraping primitives.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Self

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Custom Exception Classes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass
class VisuraRequest:
    request_id: str
    tipo_catasto: str
    provincia: str
    comune: str
    foglio: str
    particella: str
    sezione: Optional[str] = None
    sezione_urbana: Optional[str] = None
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
    sezione_urbana: Optional[str] = None
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


@dataclass
class SubmitResult:
    """Result of submitting a request — either cached or queued."""

    request_id: str
    cached: bool = False
    response: Optional[VisuraResponse] = None


# ---------------------------------------------------------------------------
# Pydantic input models (API request bodies)
# ---------------------------------------------------------------------------


class VisuraInput(BaseModel):
    """Richiesta per una visura catastale (solo dati catastali, senza intestati)"""

    provincia: str = Field(..., min_length=1, description="Nome della provincia")
    comune: str = Field(..., min_length=1, description="Nome del comune")
    foglio: str = Field(..., min_length=1, description="Numero di foglio")
    particella: str = Field(..., min_length=1, description="Numero di particella")
    sezione: Optional[str] = Field(None, description="Sezione (opzionale)")
    sezione_urbana: Optional[str] = Field(None, description="Sezione urbana (opzionale, e.g. RA, PA)")
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
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati (default T)"
    )
    subalterno: Optional[str] = Field(None, description="Numero di subalterno (obbligatorio per Fabbricati)")
    sezione: Optional[str] = Field(None, description="Sezione (opzionale)")
    sezione_urbana: Optional[str] = Field(None, description="Sezione urbana (opzionale, e.g. RA, PA)")

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            return None
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


class VisuraSoggettoInput(BaseModel):
    """Richiesta per una ricerca per soggetto (codice fiscale) su SISTER"""

    codice_fiscale: str = Field(..., min_length=11, max_length=16, description="Codice fiscale del soggetto")
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TFE]$", description="'T' = Terreni, 'F' = Fabbricati, 'E' = Entrambi (default)"
    )
    provincia: Optional[str] = Field(None, description="Provincia (ometti per ricerca nazionale)")

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F", "E"}:
            raise ValueError(f"tipo_catasto deve essere 'T', 'F' o 'E', ricevuto {value}")
        return normalized

    @field_validator("codice_fiscale", mode="before")
    @classmethod
    def normalize_codice_fiscale(cls, value: str) -> str:
        return value.strip().upper()


@dataclass
class VisuraSoggettoRequest:
    """Internal request for soggetto search"""

    request_id: str
    codice_fiscale: str
    tipo_catasto: str = "E"
    provincia: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class VisuraPersonaGiuridicaInput(BaseModel):
    """Richiesta per ricerca persona giuridica (P.IVA o denominazione)"""

    identificativo: str = Field(..., min_length=1, description="P.IVA (11 cifre) o denominazione azienda")
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TFE]$", description="'T' = Terreni, 'F' = Fabbricati, 'E' = Entrambi"
    )
    provincia: Optional[str] = Field(None, description="Provincia (ometti per ricerca nazionale)")

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F", "E"}:
            raise ValueError(f"tipo_catasto deve essere 'T', 'F' o 'E', ricevuto {value}")
        return normalized


@dataclass
class VisuraPersonaGiuridicaRequest:
    request_id: str
    identificativo: str
    tipo_catasto: str = "E"
    provincia: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class ElencoImmobiliInput(BaseModel):
    """Richiesta per elenco immobili di un comune"""

    provincia: str = Field(..., min_length=1, description="Nome della provincia")
    comune: str = Field(..., min_length=1, description="Nome del comune")
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati"
    )
    foglio: Optional[str] = Field(None, description="Foglio (opzionale, filtra per foglio)")
    sezione: Optional[str] = Field(None, description="Sezione (opzionale)")

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


@dataclass
class ElencoImmobiliRequest:
    request_id: str
    provincia: str
    comune: str
    tipo_catasto: str = "T"
    foglio: Optional[str] = None
    sezione: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class GenericSisterRequest:
    """Generic request for SISTER search types (IND, PART, NOTA, EM, EXPM, OOII, FID, ISP, ISPCART)."""

    request_id: str
    search_type: str  # indirizzo, partita, nota, mappa, export_mappa, originali, fiduciali, ispezioni, ispezioni_cart
    provincia: str
    comune: Optional[str] = None
    tipo_catasto: str = "T"
    params: Optional[Dict] = None  # type-specific params (foglio, indirizzo, numero_nota, etc.)
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.params is None:
            self.params = {}


@dataclass
class IspezioneIpotecariaRequest:
    """Request for Ispezione Ipotecaria (paid inspection service)."""

    request_id: str
    tipo_ricerca: str  # immobile, persona_fisica, persona_giuridica, nota
    provincia: str
    comune: Optional[str] = None
    tipo_catasto: str = "T"
    codice_fiscale: Optional[str] = None
    identificativo: Optional[str] = None
    foglio: Optional[str] = None
    particella: Optional[str] = None
    numero_nota: Optional[str] = None
    anno_nota: Optional[str] = None
    auto_confirm: bool = False
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class IspezioneIpotecariaInput(BaseModel):
    """API input for Ispezione Ipotecaria (paid inspection)."""

    tipo_ricerca: str = Field(
        ..., description="Search type: immobile, persona_fisica, persona_giuridica, nota"
    )
    provincia: str = Field(..., min_length=1, description="Province name")
    comune: Optional[str] = Field(None, description="Municipality name")
    tipo_catasto: Optional[str] = Field(
        None, pattern=r"^[TF]$", description="'T' = Terreni, 'F' = Fabbricati"
    )
    codice_fiscale: Optional[str] = Field(None, description="Codice fiscale (for persona_fisica)")
    identificativo: Optional[str] = Field(None, description="P.IVA or company name (for persona_giuridica)")
    foglio: Optional[str] = Field(None, description="Sheet number (for immobile)")
    particella: Optional[str] = Field(None, description="Parcel number (for immobile)")
    numero_nota: Optional[str] = Field(None, description="Note number (for nota)")
    anno_nota: Optional[str] = Field(None, description="Note year (for nota)")
    auto_confirm: bool = Field(False, description="Auto-confirm cost without prompting")

    @field_validator("tipo_ricerca", mode="before")
    @classmethod
    def validate_tipo_ricerca(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        valid = {"immobile", "persona_fisica", "persona_giuridica", "nota"}
        if normalized not in valid:
            raise ValueError(f"tipo_ricerca must be one of {valid}, got '{value}'")
        return normalized

    @field_validator("tipo_catasto", mode="before")
    @classmethod
    def validate_tipo_catasto(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


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
