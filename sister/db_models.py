"""SQLModel table definitions for the sister database.

Defines ORM models for visura requests, responses, and structured result
tables (immobili, intestati). The raw JSON blob is kept in VisuraResponseDB.data
for audit/compliance; the structured tables enable indexed lookups and joins.

Note: Do NOT use `from __future__ import annotations` here — it breaks
SQLAlchemy's relationship resolution with SQLModel.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Column, Index, Text
from sqlmodel import Field, Relationship, SQLModel


class VisuraRequestDB(SQLModel, table=True):
    __tablename__ = "visura_requests"

    request_id: str = Field(primary_key=True)
    request_type: str
    tipo_catasto: str
    provincia: str = Field(default="")
    comune: str = Field(default="")
    foglio: str = Field(default="")
    particella: str = Field(default="")
    sezione: Optional[str] = None
    subalterno: Optional[str] = None
    cache_key: Optional[str] = Field(default=None, index=True)
    cost_text: Optional[str] = None
    cost_value: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.now)

    response: Optional["VisuraResponseDB"] = Relationship(back_populates="request")

    __table_args__ = (
        Index("idx_requests_lookup", "provincia", "comune", "foglio", "particella", "tipo_catasto"),
    )


class VisuraResponseDB(SQLModel, table=True):
    __tablename__ = "visura_responses"

    request_id: str = Field(foreign_key="visura_requests.request_id", primary_key=True)
    success: bool
    tipo_catasto: str
    data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(SA_JSON))
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

    request: Optional["VisuraRequestDB"] = Relationship(back_populates="response")
    immobili: list["ImmobileDB"] = Relationship(back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    intestati: list["IntestatoDB"] = Relationship(back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    page_visits: list["PageVisitDB"] = Relationship(back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    documents: list["VisuraDocumentDB"] = Relationship(back_populates="response", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class ImmobileDB(SQLModel, table=True):
    """Structured property data extracted from response JSON."""

    __tablename__ = "immobili"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: str = Field(foreign_key="visura_responses.request_id", index=True)
    tipo_catasto: str = Field(default="")

    # Common fields (Fabbricati + Terreni)
    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    indirizzo: Optional[str] = None
    partita: Optional[str] = None

    # Fabbricati-specific
    categoria: Optional[str] = None
    classe: Optional[str] = None
    consistenza: Optional[str] = None
    rendita: Optional[str] = None
    zona_censuaria: Optional[str] = None

    # Terreni-specific
    qualita: Optional[str] = None
    superficie: Optional[str] = None
    reddito_dominicale: Optional[str] = None
    reddito_agrario: Optional[str] = None

    # Soggetto/PNF search results
    denominazione: Optional[str] = None
    sede: Optional[str] = None
    codice_fiscale: Optional[str] = Field(default=None, index=True)
    provincia_result: Optional[str] = None
    comune_result: Optional[str] = None

    response: Optional["VisuraResponseDB"] = Relationship(back_populates="immobili")


class IntestatoDB(SQLModel, table=True):
    """Structured owner data extracted from response JSON."""

    __tablename__ = "intestati"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: str = Field(foreign_key="visura_responses.request_id", index=True)
    nominativo: Optional[str] = None
    codice_fiscale: Optional[str] = Field(default=None, index=True)
    titolarita: Optional[str] = None
    quota: Optional[str] = None

    response: Optional["VisuraResponseDB"] = Relationship(back_populates="intestati")


# ---------------------------------------------------------------------------
# Field mapping: HTML table headers → ImmobileDB column names
# ---------------------------------------------------------------------------

IMMOBILE_FIELD_MAP = {
    "Foglio": "foglio",
    "Particella": "particella",
    "Sub": "subalterno",
    "Indirizzo": "indirizzo",
    "Partita": "partita",
    "Categoria": "categoria",
    "Classe": "classe",
    "Consistenza": "consistenza",
    "Rendita": "rendita",
    "Zona cens": "zona_censuaria",
    "Zona censuaria": "zona_censuaria",
    "Qualita": "qualita",
    "Qualità": "qualita",
    "Superficie": "superficie",
    "Reddito Dominicale": "reddito_dominicale",
    "Reddito Agrario": "reddito_agrario",
    # Soggetto/PNF results
    "Denominazione": "denominazione",
    "Sede": "sede",
    "Codice Fiscale": "codice_fiscale",
    "Codice fiscale": "codice_fiscale",
    "Provincia": "provincia_result",
    "Comune": "comune_result",
}

class WorkflowRunDB(SQLModel, table=True):
    """Tracks a workflow execution (preset, status, input/output)."""

    __tablename__ = "workflow_runs"

    workflow_id: str = Field(primary_key=True)
    preset: str
    status: str = Field(default="running")  # running, completed, partial, failed
    input_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    output_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    steps: list["WorkflowStepDB"] = Relationship(back_populates="run")


class WorkflowStepDB(SQLModel, table=True):
    """Tracks individual steps within a workflow run."""

    __tablename__ = "workflow_steps"

    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_id: str = Field(foreign_key="workflow_runs.workflow_id", index=True)
    step_key: str  # deterministic key e.g. "intestati:F:TRIESTE:9:166:3"
    status: str = Field(default="pending")  # pending, completed, error
    result_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    run: Optional["WorkflowRunDB"] = Relationship(back_populates="steps")

    __table_args__ = (
        Index("idx_workflow_steps_lookup", "workflow_id", "step_key"),
    )


class PageVisitDB(SQLModel, table=True):
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

    response: Optional["VisuraResponseDB"] = Relationship(back_populates="page_visits")


class VisuraDocumentDB(SQLModel, table=True):
    """Downloaded visura document (PDF/XML/P7M) from SISTER Richieste."""

    __tablename__ = "visura_documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    response_id: Optional[str] = Field(default=None, foreign_key="visura_responses.request_id", index=True)
    document_type: str = Field(default="")  # visura_immobile, visura_soggetto, visura_pnf
    file_format: str = Field(default="")  # PDF, XML, P7M
    filename: str = Field(default="")
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    oggetto: Optional[str] = None  # description from Richieste table
    richiesta_del: Optional[str] = None  # request timestamp from Richieste table

    # Parsed metadata from XML content
    provincia: Optional[str] = None
    comune: Optional[str] = None
    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    sezione_urbana: Optional[str] = None
    tipo_catasto: Optional[str] = None  # T or F
    intestati_json: Optional[str] = Field(default=None, sa_column=Column(Text))  # parsed owners from XML
    dati_immobile_json: Optional[str] = Field(default=None, sa_column=Column(Text))  # parsed property data from XML
    xml_content: Optional[str] = Field(default=None, sa_column=Column(Text))  # raw XML content

    created_at: datetime = Field(default_factory=datetime.now)

    response: Optional["VisuraResponseDB"] = Relationship(back_populates="documents")

    __table_args__ = (
        Index("idx_documents_lookup", "provincia", "comune", "foglio", "particella"),
    )


INTESTATO_FIELD_MAP = {
    "Nominativo o denominazione": "nominativo",
    "Nominativo": "nominativo",
    "Cognome": "nominativo",
    "Nome": "nominativo",
    "Codice fiscale": "codice_fiscale",
    "Codice Fiscale": "codice_fiscale",
    "Titolarità": "titolarita",
    "Titolarita": "titolarita",
    "Quota": "quota",
}
