"""Data models, exceptions, and Pydantic input schemas for sister.

WorkflowInput, WORKFLOW_PRESETS, STEP_METADATA, and _DEPTH_ORDER have been
moved to opendata/opendata/workflows/models.py. Sister retains only the
atomic cadastral scraping primitives.
"""

from datetime import datetime
from typing import Any, ClassVar, Dict, Optional, Self

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel

# Workflow symbols live in aecs4u_workflow (shared package).
from aecs4u_workflow.models import (  # noqa: F401
    STEP_METADATA,
    WORKFLOW_PRESETS,
    WorkflowInput,
    _DEPTH_ORDER,
)

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


def _default_timestamp() -> datetime:
    return datetime.now()


def _coerce_timestamp(v: Optional[datetime]) -> datetime:
    return v if v is not None else datetime.now()


class VisuraRequest(SQLModel):
    names: ClassVar[dict[str, str]] = {
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
        "comune": "municipality",
        "foglio": "sheet",
        "particella": "parcel",
        "sezione": "section",
        "sezione_urbana": "urban_section",
        "subalterno": "subunit",
    }

    request_id: str
    cadastre_type: str
    province: str
    municipality: str
    sheet: str
    parcel: str
    section: Optional[str] = None
    urban_section: Optional[str] = None
    subunit: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class VisuraIntestatiRequest(SQLModel):
    """Richiesta per ottenere gli intestati di un immobile specifico"""

    names: ClassVar[dict[str, str]] = {
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
        "comune": "municipality",
        "foglio": "sheet",
        "particella": "parcel",
        "subalterno": "subunit",
        "sezione": "section",
        "sezione_urbana": "urban_section",
    }

    request_id: str
    cadastre_type: str
    province: str
    municipality: str
    sheet: str
    parcel: str
    subunit: Optional[str] = None
    section: Optional[str] = None
    urban_section: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class VisuraResponse(SQLModel):
    names: ClassVar[dict[str, str]] = {
        "tipo_catasto": "cadastre_type",
    }

    request_id: str
    success: bool
    cadastre_type: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class SubmitResult(SQLModel):
    """Result of submitting a request — either cached or queued."""

    request_id: str
    cached: bool = False
    response: Optional[VisuraResponse] = None


# ---------------------------------------------------------------------------
# Pydantic input models (API request bodies)
# ---------------------------------------------------------------------------


class VisuraInput(SQLModel):
    """Richiesta per una visura catastale (solo dati catastali, senza intestati)"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "provincia": "province",
        "comune": "municipality",
        "foglio": "sheet",
        "particella": "parcel",
        "sezione": "section",
        "sezione_urbana": "urban_section",
        "subalterno": "subunit",
        "tipo_catasto": "cadastre_type",
    }

    province: str = Field(..., validation_alias=AliasChoices("province", "provincia"), min_length=1, description="Province name")
    municipality: str = Field(..., validation_alias=AliasChoices("municipality", "comune"), min_length=1, description="Municipality name")
    sheet: str = Field(..., validation_alias=AliasChoices("sheet", "foglio"), min_length=1, description="Sheet number")
    parcel: str = Field(..., validation_alias=AliasChoices("parcel", "particella"), min_length=1, description="Parcel number")
    section: Optional[str] = Field(None, validation_alias=AliasChoices("section", "sezione"))
    urban_section: Optional[str] = Field(None, validation_alias=AliasChoices("urban_section", "sezione_urbana"))
    subunit: Optional[str] = Field(None, validation_alias=AliasChoices("subunit", "subalterno"))
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TF]$",
        description="'T' = Terreni, 'F' = Fabbricati (se omesso esegue entrambi)",
    )

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


class VisuraIntestatiInput(SQLModel):
    """Richiesta per ottenere gli intestati di un immobile specifico"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "provincia": "province",
        "comune": "municipality",
        "foglio": "sheet",
        "particella": "parcel",
        "tipo_catasto": "cadastre_type",
        "subalterno": "subunit",
        "sezione": "section",
        "sezione_urbana": "urban_section",
    }

    province: str = Field(..., validation_alias=AliasChoices("province", "provincia"), min_length=1, description="Province name")
    municipality: str = Field(..., validation_alias=AliasChoices("municipality", "comune"), min_length=1, description="Municipality name")
    sheet: str = Field(..., validation_alias=AliasChoices("sheet", "foglio"), min_length=1, description="Sheet number")
    parcel: str = Field(..., validation_alias=AliasChoices("parcel", "particella"), min_length=1, description="Parcel number")
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TF]$",
        description="'T' = Terreni, 'F' = Fabbricati (default T)",
    )
    subunit: Optional[str] = Field(None, validation_alias=AliasChoices("subunit", "subalterno"), description="Subunit number (required for Fabbricati)")
    section: Optional[str] = Field(None, validation_alias=AliasChoices("section", "sezione"))
    urban_section: Optional[str] = Field(None, validation_alias=AliasChoices("urban_section", "sezione_urbana"))

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            return None
        return normalized

    @field_validator("subunit", mode="before")
    @classmethod
    def normalize_subunit(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_subunit(self) -> Self:
        if self.cadastre_type == "F" and not self.subunit:
            raise ValueError("subalterno è obbligatorio per i fabbricati (tipo_catasto='F')")
        if self.cadastre_type == "T" and self.subunit:
            raise ValueError("subalterno non va indicato per i terreni (tipo_catasto='T')")
        return self


class VisuraSoggettoInput(SQLModel):
    """Richiesta per una ricerca per soggetto (codice fiscale) su SISTER"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "codice_fiscale": "fiscal_code",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }

    fiscal_code: str = Field(..., validation_alias=AliasChoices("fiscal_code", "codice_fiscale"), min_length=11, max_length=16, description="Codice fiscale del soggetto")
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TFE]$",
        description="'T' = Terreni, 'F' = Fabbricati, 'E' = Entrambi (default)",
    )
    province: Optional[str] = Field(None, validation_alias=AliasChoices("province", "provincia"), description="Province (omit for nationwide search)")

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F", "E"}:
            raise ValueError(f"tipo_catasto deve essere 'T', 'F' o 'E', ricevuto {value}")
        return normalized

    @field_validator("fiscal_code", mode="before")
    @classmethod
    def normalize_fiscal_code(cls, value: str) -> str:
        return value.strip().upper()


class VisuraSoggettoRequest(SQLModel):
    """Internal request for soggetto search"""

    names: ClassVar[dict[str, str]] = {
        "codice_fiscale": "fiscal_code",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }

    request_id: str
    fiscal_code: str
    cadastre_type: str = "E"
    province: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class VisuraPersonaGiuridicaInput(SQLModel):
    """Richiesta per ricerca persona giuridica (P.IVA o denominazione)"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "identificativo": "identifier",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }

    identifier: str = Field(..., validation_alias=AliasChoices("identifier", "identificativo"), min_length=1, description="P.IVA (11 cifre) o denominazione azienda")
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TFE]$",
        description="'T' = Terreni, 'F' = Fabbricati, 'E' = Entrambi",
    )
    province: Optional[str] = Field(None, validation_alias=AliasChoices("province", "provincia"), description="Province (omit for nationwide search)")

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F", "E"}:
            raise ValueError(f"tipo_catasto deve essere 'T', 'F' o 'E', ricevuto {value}")
        return normalized


class VisuraPersonaGiuridicaRequest(SQLModel):
    names: ClassVar[dict[str, str]] = {
        "identificativo": "identifier",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }

    request_id: str
    identifier: str
    cadastre_type: str = "E"
    province: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class ElencoImmobiliInput(SQLModel):
    """Richiesta per elenco immobili di un comune"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "provincia": "province",
        "comune": "municipality",
        "tipo_catasto": "cadastre_type",
        "foglio": "sheet",
        "sezione": "section",
    }

    province: str = Field(..., validation_alias=AliasChoices("province", "provincia"), min_length=1, description="Province name")
    municipality: str = Field(..., validation_alias=AliasChoices("municipality", "comune"), min_length=1, description="Municipality name")
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TF]$",
        description="'T' = Terreni, 'F' = Fabbricati",
    )
    sheet: Optional[str] = Field(None, validation_alias=AliasChoices("sheet", "foglio"), description="Sheet number (optional, filters by sheet)")
    section: Optional[str] = Field(None, validation_alias=AliasChoices("section", "sezione"), description="Section (optional)")

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


class ElencoImmobiliRequest(SQLModel):
    names: ClassVar[dict[str, str]] = {
        "provincia": "province",
        "comune": "municipality",
        "tipo_catasto": "cadastre_type",
        "foglio": "sheet",
        "sezione": "section",
    }

    request_id: str
    province: str
    municipality: str
    cadastre_type: str = "T"
    sheet: Optional[str] = None
    section: Optional[str] = None
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class GenericSisterRequest(SQLModel):
    """Generic request for SISTER search types (IND, PART, NOTA, EM, EXPM, OOII, FID, ISP, ISPCART)."""

    names: ClassVar[dict[str, str]] = {
        "provincia": "province",
        "comune": "municipality",
        "tipo_catasto": "cadastre_type",
    }

    request_id: str
    search_type: str
    province: str
    municipality: Optional[str] = None
    cadastre_type: str = "T"
    params: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class IspezioneIpotecariaRequest(SQLModel):
    """Request for Ispezione Ipotecaria (paid inspection service)."""

    names: ClassVar[dict[str, str]] = {
        "tipo_ricerca": "search_type",
        "provincia": "province",
        "comune": "municipality",
        "tipo_catasto": "cadastre_type",
        "codice_fiscale": "fiscal_code",
        "identificativo": "identifier",
        "foglio": "sheet",
        "particella": "parcel",
        "numero_nota": "note_number",
        "anno_nota": "note_year",
    }

    request_id: str
    search_type: str
    province: str
    municipality: Optional[str] = None
    cadastre_type: str = "T"
    fiscal_code: Optional[str] = None
    identifier: Optional[str] = None
    sheet: Optional[str] = None
    parcel: Optional[str] = None
    note_number: Optional[str] = None
    note_year: Optional[str] = None
    auto_confirm: bool = False
    timestamp: datetime = Field(default_factory=_default_timestamp)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _set_timestamp(cls, v: Optional[datetime]) -> datetime:
        return _coerce_timestamp(v)


class IspezioneIpotecariaInput(SQLModel):
    """API input for Ispezione Ipotecaria (paid inspection)."""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "tipo_ricerca": "search_type",
        "provincia": "province",
        "comune": "municipality",
        "tipo_catasto": "cadastre_type",
        "codice_fiscale": "fiscal_code",
        "identificativo": "identifier",
        "foglio": "sheet",
        "particella": "parcel",
        "numero_nota": "note_number",
        "anno_nota": "note_year",
    }

    search_type: str = Field(..., validation_alias=AliasChoices("search_type", "tipo_ricerca"), description="Search type: immobile, persona_fisica, persona_giuridica, nota")
    province: str = Field(..., validation_alias=AliasChoices("province", "provincia"), min_length=1, description="Province name")
    municipality: Optional[str] = Field(None, validation_alias=AliasChoices("municipality", "comune"), description="Municipality name")
    cadastre_type: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TF]$",
        description="'T' = Terreni, 'F' = Fabbricati",
    )
    fiscal_code: Optional[str] = Field(None, validation_alias=AliasChoices("fiscal_code", "codice_fiscale"), description="Codice fiscale (for persona_fisica)")
    identifier: Optional[str] = Field(None, validation_alias=AliasChoices("identifier", "identificativo"), description="P.IVA or company name (for persona_giuridica)")
    sheet: Optional[str] = Field(None, validation_alias=AliasChoices("sheet", "foglio"), description="Sheet number (for immobile)")
    parcel: Optional[str] = Field(None, validation_alias=AliasChoices("parcel", "particella"), description="Parcel number (for immobile)")
    note_number: Optional[str] = Field(None, validation_alias=AliasChoices("note_number", "numero_nota"), description="Note number (for nota)")
    note_year: Optional[str] = Field(None, validation_alias=AliasChoices("note_year", "anno_nota"), description="Note year (for nota)")
    auto_confirm: bool = Field(False, description="Auto-confirm cost without prompting")

    @field_validator("search_type", mode="before")
    @classmethod
    def validate_search_type(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        valid = {"immobile", "persona_fisica", "persona_giuridica", "nota"}
        if normalized not in valid:
            raise ValueError(f"tipo_ricerca must be one of {valid}, got '{value}'")
        return normalized

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized


class SezioniExtractionRequest(SQLModel):
    """Richiesta per l'estrazione delle sezioni territoriali"""

    model_config = ConfigDict(populate_by_name=True)

    names: ClassVar[dict[str, str]] = {
        "tipo_catasto": "cadastre_type",
        "max_province": "max_provinces",
    }

    cadastre_type: str = Field(
        "T",
        validation_alias=AliasChoices("cadastre_type", "tipo_catasto"),
        pattern=r"^[TF]$",
        description="'T' = Terreni, 'F' = Fabbricati",
    )
    max_provinces: int = Field(
        200,
        validation_alias=AliasChoices("max_provinces", "max_province"),
        ge=1,
        le=200,
        description="Max number of provinces to process (default: all)",
    )

    @field_validator("cadastre_type", mode="before")
    @classmethod
    def validate_cadastre_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"T", "F"}:
            raise ValueError(f"tipo_catasto deve essere 'T' o 'F', ricevuto {value}")
        return normalized
