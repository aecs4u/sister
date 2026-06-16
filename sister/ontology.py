"""Pydantic v2 ontology schemas for SISTER cadastral data.

Covers every entity type, query response shape, dossier envelope, and
document structure produced by the SISTER portal automation.

Hierarchy:
  Core entities     → Immobile*, Intestato, Soggetto*, LocalizzazioneImmobile
  Query responses   → Risposta* (one per query type)
  Dossier envelopes → Dossier* (single-step, intestati, soggetto, PNF,
                       coppia, workflow, batch)
  Documents         → VisuraDocument, VisuraFabbricati*, VisuraTerreni*
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Tipo catasto literals
# ---------------------------------------------------------------------------

TipoCatasto = Literal["F", "T", "E"]
TipoCatastoFT = Literal["F", "T"]


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------


class LocalizzazioneImmobile(BaseModel):
    """Parcel coordinates identifying a cadastral unit."""

    provincia: str
    comune: str
    foglio: str
    particella: str
    subalterno: Optional[str] = None
    sezione: Optional[str] = None
    sezione_urbana: Optional[str] = None


class ImmobileFabbricati(BaseModel):
    """A single Catasto Fabbricati row as returned by SISTER search."""

    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    indirizzo: Optional[str] = None
    zona_censuaria: Optional[str] = None
    categoria: Optional[str] = None
    classe: Optional[str] = None
    consistenza: Optional[str] = None
    rendita: Optional[str] = None
    partita: Optional[str] = None
    altri_dati: Optional[str] = None


class ImmobileTerreni(BaseModel):
    """A single Catasto Terreni row as returned by SISTER search."""

    foglio: Optional[str] = None
    particella: Optional[str] = None
    qualita: Optional[str] = None
    classe: Optional[str] = None
    ha: Optional[str] = None
    are: Optional[str] = None
    ca: Optional[str] = None
    reddito_dominicale: Optional[str] = None
    reddito_agrario: Optional[str] = None
    partita: Optional[str] = None
    porzioni: Optional[str] = None


class Intestato(BaseModel):
    """An owner (natural or legal person) attached to a cadastral unit."""

    nominativo: Optional[str] = None
    codice_fiscale: Optional[str] = None
    titolarita: Optional[str] = None
    quota: Optional[str] = None
    altri_dati: Optional[str] = None


class SoggettoFisicoResult(BaseModel):
    """A natural-person search result row (from Visura per Soggetto)."""

    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    indirizzo: Optional[str] = None
    partita: Optional[str] = None
    # Additional columns vary by tipo_catasto


class SoggettoGiuridicoResult(BaseModel):
    """A legal-entity search result row (from Visura Persona Giuridica)."""

    denominazione: Optional[str] = None
    sede: Optional[str] = None
    codice_fiscale: Optional[str] = None
    provincia_result: Optional[str] = None
    comune_result: Optional[str] = None


# ---------------------------------------------------------------------------
# Page visit (browser automation metadata)
# ---------------------------------------------------------------------------


class PageVisit(BaseModel):
    step: str
    url: Optional[str] = None
    screenshot_url: Optional[str] = None
    timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Query response data payloads (the `data` field inside dossier envelopes)
# ---------------------------------------------------------------------------


class DatiRicercaImmobile(BaseModel):
    """Payload for single-immobile search (req_F / req_T / wf_search)."""

    immobili: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0
    intestati: list[dict[str, Any]] = Field(default_factory=list)
    skipped_soppresso: int = 0
    downloaded_pdfs: list[str] = Field(default_factory=list)
    page_visits: list[dict[str, Any]] = Field(default_factory=list)


class DatiIntestati(BaseModel):
    """Payload for intestati query (intestati_F / intestati_T)."""

    immobili: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0
    intestati: list[dict[str, Any]] = Field(default_factory=list)
    skipped_soppresso: int = 0
    page_visits: list[dict[str, Any]] = Field(default_factory=list)


class DatiSoggetto(BaseModel):
    """Payload for soggetto (persona fisica) query."""

    soggetto: str  # codice fiscale
    immobili: list[dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0


class DatiPersonaGiuridica(BaseModel):
    """Payload for persona giuridica (PNF) query."""

    soggetto: str  # P.IVA or denominazione
    immobili: list[dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0
    error: Optional[str] = None  # "NESSUNA CORRISPONDENZA TROVATA"


class DatiElencoImmobili(BaseModel):
    """Payload for elenco immobili query."""

    immobili: list[dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0


# ---------------------------------------------------------------------------
# Dossier envelopes — single-step responses
# ---------------------------------------------------------------------------


class DossierBase(BaseModel):
    """Fields common to all single-step dossier JSON files."""

    request_id: str
    success: bool
    tipo_catasto: TipoCatasto
    error: Optional[str] = None
    exported_at: Optional[datetime] = None
    timestamp: Optional[datetime] = None  # older files use this key


class DossierRicercaImmobile(DossierBase):
    """req_F / req_T / wf_search dossiers."""

    tipo_catasto: TipoCatastoFT
    data: Optional[DatiRicercaImmobile] = None


class DossierIntestati(DossierBase):
    """intestati_F / intestati_T / wf_intestati dossiers."""

    tipo_catasto: TipoCatastoFT
    data: Optional[DatiIntestati] = None


class DossierSoggetto(DossierBase):
    """soggetto_F / soggetto_T / soggetto_E dossiers (persona fisica)."""

    data: Optional[DatiSoggetto] = None


class DossierPersonaGiuridica(DossierBase):
    """pnf_E / pnf_F / pnf_T dossiers (persona giuridica)."""

    data: Optional[DatiPersonaGiuridica] = None


class DossierCoppia(BaseModel):
    """multi_response dossier — paired F+T results for the same parcel."""

    request_id: str
    tipo_catasto: Literal["E"] = "E"
    fabbricati: Optional[DossierRicercaImmobile] = None
    terreni: Optional[DossierRicercaImmobile] = None
    exported_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Dossier envelope — workflow
# ---------------------------------------------------------------------------


class WorkflowStepData(BaseModel):
    """Partial data stored per workflow step."""

    immobili: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0


class WorkflowStep(BaseModel):
    step: str
    status: Literal["completed", "failed", "skipped", "pending"] = "pending"
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class RiskSeverityCounts(BaseModel):
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0


class RiskScores(BaseModel):
    risk_flags: list[str] = Field(default_factory=list)
    total_flags: int = 0
    severity_counts: RiskSeverityCounts = Field(default_factory=RiskSeverityCounts)
    total_properties: int = 0
    total_owners: int = 0
    geographic_concentration: list[str] = Field(default_factory=list)


class WorkflowAggregate(BaseModel):
    """Cross-step aggregate computed by the workflow engine."""

    properties: list[dict[str, Any]] = Field(default_factory=list)
    owners: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    risk_scores: RiskScores = Field(default_factory=RiskScores)
    ranked_properties: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowSummary(BaseModel):
    total_steps: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    properties: int = 0
    owners: int = 0
    addresses: int = 0
    risk_flags: int = 0
    links: int = 0
    timeline_events: int = 0


class DossierWorkflow(BaseModel):
    """Workflow dossier (full-due-diligence, patrimonio, fondiario, aziendale)."""

    workflow_id: str
    preset: str
    description: Optional[str] = None
    steps: list[WorkflowStep] = Field(default_factory=list)
    aggregate: WorkflowAggregate = Field(default_factory=WorkflowAggregate)
    summary: WorkflowSummary = Field(default_factory=WorkflowSummary)
    exported_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Dossier envelope — batch
# ---------------------------------------------------------------------------


class BatchItem(BaseModel):
    """One entry in a batch dossier array."""

    opendata_id: Optional[str] = None
    organization_name: Optional[str] = None
    vat_number: Optional[str] = None
    endpoint: Optional[str] = None
    api_path: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    source_file: Optional[str] = None
    request_id: Optional[str] = None
    tipo_catasto: Optional[str] = None
    status: str = "pending"  # pending | success | error
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: Optional[datetime] = None


DossierBatch = list[BatchItem]


# ---------------------------------------------------------------------------
# Documents — VisuraDocument runtime view
# ---------------------------------------------------------------------------


class VisuraDocument(BaseModel):
    """Runtime view of a VisuraDocument row (downloaded PDF/XML/P7M)."""

    id: Optional[int] = None
    response_id: Optional[str] = None
    document_type: str  # visura_immobile | visura_soggetto | visura_pnf
    file_format: str  # PDF | XML | P7M
    filename: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    oggetto: Optional[str] = None
    richiesta_del: Optional[str] = None
    # Parsed from XML
    provincia: Optional[str] = None
    comune: Optional[str] = None
    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    sezione_urbana: Optional[str] = None
    tipo_catasto: Optional[TipoCatastoFT] = None
    intestati: list[Intestato] = Field(default_factory=list)
    dati_immobile: Optional[Union[ImmobileFabbricati, ImmobileTerreni]] = None
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# XML visura parsed document structures
# ---------------------------------------------------------------------------


class DatiRichiesta(BaseModel):
    """DatiRichiesta section from parsed XML visura."""

    comune: Optional[str] = None
    foglio: Optional[str] = None
    particella: Optional[str] = None
    subalterno: Optional[str] = None
    sezione: Optional[str] = None
    tipo_catasto: Optional[str] = None
    data_richiesta: Optional[str] = None


class SituazioneAttualeRow(BaseModel):
    """One property row from the SituazioneAttuale section."""

    # Fabbricati fields
    categoria: Optional[str] = None
    classe: Optional[str] = None
    consistenza: Optional[str] = None
    rendita_catastale: Optional[str] = None
    # Terreni fields
    qualita: Optional[str] = None
    ha: Optional[str] = None
    are: Optional[str] = None
    ca: Optional[str] = None
    reddito_dominicale: Optional[str] = None
    reddito_agrario: Optional[str] = None
    # Common
    partita: Optional[str] = None
    indirizzo: Optional[str] = None


class StoriaIntestazione(BaseModel):
    """Ownership history entry from StoriaIntestazione section."""

    nominativo: Optional[str] = None
    codice_fiscale: Optional[str] = None
    titolarita: Optional[str] = None
    quota: Optional[str] = None
    data_inizio: Optional[str] = None
    data_fine: Optional[str] = None


class MutazioneSoggettiva(BaseModel):
    """Ownership transfer record from MutazioneSoggettiva section."""

    tipo_mutazione: Optional[str] = None
    data_effetto: Optional[str] = None
    numero_nota: Optional[str] = None
    progressivo: Optional[str] = None
    soggetti: list[Intestato] = Field(default_factory=list)


class VisuraFabbricatiAttuale(BaseModel):
    """Parsed Catasto Fabbricati visura (XML document, situazione attuale)."""

    titolo: Optional[str] = None
    locazione: Optional[LocalizzazioneImmobile] = None
    dati_richiesta: Optional[DatiRichiesta] = None
    situazione_attuale: list[SituazioneAttualeRow] = Field(default_factory=list)
    intestati: list[Intestato] = Field(default_factory=list)
    storia_intestazione: list[StoriaIntestazione] = Field(default_factory=list)
    mutazioni_soggettive: list[MutazioneSoggettiva] = Field(default_factory=list)


class VisuraTerreniAttuale(BaseModel):
    """Parsed Catasto Terreni visura (XML document, situazione attuale)."""

    titolo: Optional[str] = None
    locazione: Optional[LocalizzazioneImmobile] = None
    dati_richiesta: Optional[DatiRichiesta] = None
    situazione_attuale: list[SituazioneAttualeRow] = Field(default_factory=list)
    intestati: list[Intestato] = Field(default_factory=list)
    storia_intestazione: list[StoriaIntestazione] = Field(default_factory=list)
    mutazioni_soggettive: list[MutazioneSoggettiva] = Field(default_factory=list)


class VisuraSoggettoAttuale(BaseModel):
    """Parsed soggetto/PNF visura document (list of properties per subject)."""

    soggetto: Optional[str] = None  # codice fiscale or P.IVA
    tipo_catasto: Optional[TipoCatasto] = None
    immobili: list[Union[ImmobileFabbricati, ImmobileTerreni]] = Field(default_factory=list)
    total_results: int = 0


# ---------------------------------------------------------------------------
# Discriminated union of all dossier types (for generic loading)
# ---------------------------------------------------------------------------

AnyDossier = Union[
    DossierRicercaImmobile,
    DossierIntestati,
    DossierSoggetto,
    DossierPersonaGiuridica,
    DossierCoppia,
    DossierWorkflow,
    DossierBatch,
]

AnyVisuraDocument = Union[
    VisuraFabbricatiAttuale,
    VisuraTerreniAttuale,
    VisuraSoggettoAttuale,
]
