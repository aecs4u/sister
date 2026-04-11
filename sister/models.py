"""Data models, exceptions, and Pydantic input schemas for sister."""

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
