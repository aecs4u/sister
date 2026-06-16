"""SQLModel table definitions for the sister database.

Defines ORM models for visura requests, responses, and structured result
tables (visura_properties, visura_owners). The raw JSON blob is kept in VisuraResponse.data
for audit/compliance; the structured tables enable indexed lookups and joins.

Note: Do NOT use `from __future__ import annotations` here — it breaks
SQLAlchemy's relationship resolution with SQLModel.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar, Optional

import sqlalchemy as sa
from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Column, Index, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class CadastralLocation(SQLModel, table=True):
    """Normalised cadastral location shared across visura_requests, visura_properties, and document_metadata.

    All seven fields default to empty string so the unique constraint can identify duplicates without
    NULL ambiguity (SQLite treats each NULL as distinct in a UNIQUE index).
    """

    __tablename__ = "cadastral_locations"

    id: Optional[int] = Field(default=None, primary_key=True)
    cadastre_type: str = Field(default="")  # F | T | E | ""
    province: str = Field(default="")
    municipality: str = Field(default="")
    sheet: str = Field(default="")
    parcel: str = Field(default="")
    subunit: str = Field(default="")
    section: str = Field(default="")  # SezUrbana (F) / SezCensuaria (T) / generic

    requests: list["VisuraRequest"] = Relationship(back_populates="location")
    properties: list["VisuraProperty"] = Relationship(back_populates="location")
    document_metadata: list["DocumentMetadata"] = Relationship(back_populates="location")

    __table_args__ = (
        Index("idx_location_lookup", "province", "municipality", "sheet", "parcel"),
        UniqueConstraint(
            "cadastre_type", "province", "municipality", "sheet", "parcel", "subunit", "section",
            name="uq_location",
        ),
    )


class CadastralSubject(SQLModel, table=True):
    """Normalised person/legal-entity identity shared by owners, XML subjects, and legal-entity searches."""

    __tablename__ = "cadastral_subjects"

    id: Optional[int] = Field(default=None, primary_key=True)
    fiscal_code: Optional[str] = Field(default=None)
    display_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    gender: Optional[str] = Field(default=None, max_length=1)
    date_of_birth: Optional[str] = None
    birth_location_id: Optional[int] = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    birth_municipality_code: Optional[str] = None
    subject_type: Optional[str] = None  # person | legal_entity | unknown

    birth_location: Optional["CadastralLocation"] = Relationship()

    __table_args__ = (
        # Partial unique index: deduplicate subjects by fiscal code when present.
        # NULL fiscal codes are excluded so anonymous subjects don't collide.
        Index("uq_subject_fiscal_code", "fiscal_code", unique=True, sqlite_where=sa.text("fiscal_code IS NOT NULL")),
    )


class OwnershipRight(SQLModel, table=True):
    """Normalised ownership/right payload shared across response owners and XML owners."""

    __tablename__ = "ownership_rights"

    id: Optional[int] = Field(default=None, primary_key=True)
    right_type: Optional[str] = None
    right_code: Optional[str] = None
    right_description: Optional[str] = None
    ownership_share: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    __table_args__ = (
        UniqueConstraint(
            "right_type",
            "right_code",
            "right_description",
            "ownership_share",
            "start_date",
            "end_date",
            name="uq_ownership_right",
        ),
    )


class VisuraRequest(SQLModel, table=True):
    __tablename__ = "visura_requests"

    request_id: str = Field(primary_key=True)
    request_type: str
    location_id: Optional[int] = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    cache_key: Optional[str] = Field(default=None, index=True)
    cost_text: Optional[str] = None
    cost_value: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.now)

    location: Optional["CadastralLocation"] = Relationship(back_populates="requests")
    response: Optional["VisuraResponse"] = Relationship(back_populates="request")


class VisuraResponse(SQLModel, table=True):
    __tablename__ = "visura_responses"

    request_id: str = Field(foreign_key="visura_requests.request_id", primary_key=True)
    success: bool
    cadastre_type: str
    data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(SA_JSON))
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

    request: Optional["VisuraRequest"] = Relationship(back_populates="response")
    properties: list["VisuraProperty"] = Relationship(
        back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    owners: list["VisuraOwner"] = Relationship(
        back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    page_visits: list["PageVisit"] = Relationship(
        back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    documents: list["VisuraDocument"] = Relationship(
        back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class VisuraProperty(SQLModel, table=True):
    """Structured property data extracted from response JSON."""

    __tablename__ = "visura_properties"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: str = Field(foreign_key="visura_responses.request_id", index=True)
    location_id: Optional[int] = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    subject_id: Optional[int] = Field(default=None, foreign_key="cadastral_subjects.id", index=True)
    property_type: Optional[str] = None  # "building" | "land" | "entity"

    # Common fields (Fabbricati + Terreni)
    address: Optional[str] = None
    partita: Optional[str] = None

    # Fabbricati-specific
    category: Optional[str] = None
    cadastral_class: Optional[str] = None
    consistency: Optional[str] = None
    income: Optional[str] = None
    census_zone: Optional[str] = None

    # Terreni-specific
    quality: Optional[str] = None
    area: Optional[str] = None
    dominical_income: Optional[str] = None
    agricultural_income: Optional[str] = None

    # Soggetto/PNF search results (entity data — NOT cadastral location)
    registered_office: Optional[str] = None
    province: Optional[str] = None  # entity's province
    municipality: Optional[str] = None  # entity's municipality

    location: Optional["CadastralLocation"] = Relationship(back_populates="properties")
    subject: Optional["CadastralSubject"] = Relationship()
    response: Optional["VisuraResponse"] = Relationship(back_populates="properties")


class VisuraOwner(SQLModel, table=True):
    """Structured owner data extracted from response JSON."""

    __tablename__ = "visura_owners"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: str = Field(foreign_key="visura_responses.request_id", index=True)
    subject_id: Optional[int] = Field(default=None, foreign_key="cadastral_subjects.id", index=True)
    right_id: Optional[int] = Field(default=None, foreign_key="ownership_rights.id", index=True)

    response: Optional["VisuraResponse"] = Relationship(back_populates="owners")
    subject: Optional["CadastralSubject"] = Relationship()
    right: Optional["OwnershipRight"] = Relationship()


# ---------------------------------------------------------------------------
# Field mappings: HTML table headers → Python field names
# ---------------------------------------------------------------------------

PROPERTY_FIELD_MAP = {
    # Common (Fabbricati + Terreni)
    "Indirizzo": "address",
    "Partita": "partita",
    # Fabbricati-specific
    "Categoria": "category",
    "Classe": "cadastral_class",
    "Consistenza": "consistency",
    "Rendita": "income",
    "Zona cens": "census_zone",
    "Zona censuaria": "census_zone",
    # Terreni-specific
    "Qualita": "quality",
    "Qualità": "quality",
    "Superficie": "area",
    "Reddito Dominicale": "dominical_income",
    "Reddito Agrario": "agricultural_income",
    # Soggetto/PNF results
    "Sede": "registered_office",
    "Provincia": "province",
    "Comune": "municipality",
}

PROPERTY_SUBJECT_FIELD_MAP = {
    "Denominazione": "display_name",
    "Codice Fiscale": "fiscal_code",
    "Codice fiscale": "fiscal_code",
}

# HTML table headers that map to CadastralLocation fields (not VisuraProperty)
PROPERTY_LOCATION_FIELD_MAP = {
    "Foglio": "sheet",
    "Particella": "parcel",
    "Sub": "subunit",
}


class PageVisit(SQLModel, table=True):
    """Browser page visit metadata captured during automation."""

    __tablename__ = "page_visits"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: str = Field(foreign_key="visura_responses.request_id", index=True)
    step: str = Field(default="")
    url: Optional[str] = None
    screenshot_url: Optional[str] = None
    form_elements_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    errors_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    timestamp: Optional[datetime] = None

    response: Optional["VisuraResponse"] = Relationship(back_populates="page_visits")


class VisuraDocument(SQLModel, table=True):
    """Downloaded visura document (PDF/XML/P7M) from SISTER Richieste — file metadata only."""

    __tablename__ = "visura_documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: Optional[str] = Field(default=None, foreign_key="visura_responses.request_id", index=True)
    document_type: str = Field(default="")  # visura_immobile, visura_soggetto, visura_pnf
    file_format: str = Field(default="")  # PDF, XML, P7M
    filename: str = Field(default="")
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    subject: Optional[str] = None  # description from Richieste table
    requested_at: Optional[str] = None  # request timestamp from Richieste table
    created_at: datetime = Field(default_factory=datetime.now)

    response: Optional["VisuraResponse"] = Relationship(back_populates="documents")
    document_metadata: Optional["DocumentMetadata"] = Relationship(back_populates="document")


class DocumentMetadata(SQLModel, table=True):
    """Parsed XML header and cadastral location for a visura document (1:1 with visura_documents).

    Only present for XML/P7M file types — PDF-only documents leave this row absent.
    Cadastral location is normalised via location_id → cadastral_locations.
    """

    __tablename__ = "document_metadata"

    id: int = Field(foreign_key="visura_documents.id", primary_key=True)
    location_id: Optional[int] = Field(default=None, foreign_key="cadastral_locations.id", index=True)

    # DatiRichiesta — non-location fields kept here
    municipality_code: Optional[str] = None  # 4-char ISTAT municipality code (different from municipality name)
    view_subtype: Optional[str] = None  # attuale | storica | storica_analitica | storica_sintetica | storica_completa
    protocol: Optional[str] = None
    year: Optional[str] = None

    # TitoloVisura
    title: Optional[str] = None
    reference_date: Optional[str] = None  # reference date DD/MM/YYYY
    registry_view_type: Optional[str] = None  # STORICA | ATTUALE | SINTETICA
    service_type: Optional[str] = None
    generation_date: Optional[str] = None
    generation_time: Optional[str] = None
    source_system: Optional[str] = None

    # DatiLiquidazione
    liquidation_protocol: Optional[str] = None
    liquidation_year: Optional[str] = None
    liquidation_units: Optional[int] = None
    requester: Optional[str] = None  # Richiedente.Descrizione

    # StoriaImmobileFabbricati.SuperficieF — inlined (0..1 per document)
    historical_total_area: Optional[Decimal] = None
    historical_excluded_area: Optional[Decimal] = None
    content: Optional[str] = Field(default=None, sa_column=Column(Text))

    document: Optional["VisuraDocument"] = Relationship(back_populates="document_metadata")
    location: Optional["CadastralLocation"] = Relationship(back_populates="document_metadata")

    __table_args__ = (Index("idx_document_metadata_location", "location_id"),)

    # Maps XML attribute/element names → DocumentMetadata Python field names.
    source_names: ClassVar[dict] = {
        # TitoloVisura attributes
        "Titolo": "title",
        "SituazioneAl": "reference_date",
        "TipoVisura": "registry_view_type",
        "TipoServizio": "service_type",
        "Data": "generation_date",
        "Ora": "generation_time",
        "Provenienza": "source_system",
        # DatiLiquidazione attributes
        "Protocollo": "liquidation_protocol",
        "Anno": "liquidation_year",
        "UnitaImmobiliari": "liquidation_units",
        # Richiedente attribute
        "Descrizione": "requester",
        # DatiRichiesta non-location attribute
        "CodiceComune": "municipality_code",
        # StoriaImmobileFabbricati.SuperficieF attributes
        "Totale": "historical_total_area",
        "TotaleE": "historical_excluded_area",
    }

    # Maps XML attribute names that belong in CadastralLocation (for parse reference).
    location_source_names: ClassVar[dict] = {
        "Provincia": "province",
        "Comune": "municipality",
        "Foglio": "sheet",
        "ParticellaNum": "parcel",
        "Subalterno": "subunit",
        "SezUrbana": "section",    # Fabbricati
        "SezCensuaria": "section",  # Terreni
        "TipoCatasto": "cadastre_type",
    }


class FeedbackConfig(SQLModel, table=True):
    __tablename__ = "feedback_config"

    id: int = Field(default=1, primary_key=True)
    cc_emails: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]")
    )
    bcc_emails: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]")
    )
    invitation_subject: str = Field(
        default="Il tuo feedback è importante",
        sa_column=sa.Column(sa.Text, nullable=False, server_default="Il tuo feedback è importante"),
    )
    invitation_intro: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    invitation_bullets: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]")
    )
    invitation_cta_text: str = Field(
        default="Lascia il tuo feedback →",
        sa_column=sa.Column(sa.Text, nullable=False, server_default="Lascia il tuo feedback →"),
    )
    invitation_privacy_note: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    invitation_signature: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))
    invitation_unsub_text: str = Field(
        default="Non vuoi più ricevere queste email?",
        sa_column=sa.Column(sa.Text, nullable=False, server_default="Non vuoi più ricevere queste email?"),
    )
    invitation_unsub_link_text: str = Field(
        default="Disiscriviti qui", sa_column=sa.Column(sa.Text, nullable=False, server_default="Disiscriviti qui")
    )
    grace_period_days: int = Field(default=30, sa_column=sa.Column(sa.Integer, nullable=False, server_default="30"))


class FeedbackUnsubscribe(SQLModel, table=True):
    __tablename__ = "feedback_unsubscribes"

    email: str = Field(primary_key=True)
    unsubscribed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False),
    )


OWNER_SUBJECT_FIELD_MAP = {
    "Nominativo o denominazione": "display_name",
    "Nominativo": "display_name",
    "Cognome": "last_name",
    "Nome": "first_name",
    "Codice fiscale": "fiscal_code",
    "Codice Fiscale": "fiscal_code",
}

OWNER_RIGHT_FIELD_MAP = {
    "Titolarità": "right_type",
    "Titolarita": "right_type",
    "Quota": "ownership_share",
}

OWNER_FIELD_MAP = {**OWNER_SUBJECT_FIELD_MAP, **OWNER_RIGHT_FIELD_MAP}
