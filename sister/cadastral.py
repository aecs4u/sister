from datetime import datetime
from typing import Any, ClassVar, Optional

from sqlalchemy import JSON, Column
from sqlalchemy.orm import Session, joinedload
from sqlmodel import Field, Relationship, SQLModel, select

from .db_models import CadastralLocation, CadastralSubject

# ─── Queries (legal-entity search + single property/prospect records) ─────────
#
# query_type: "entity_record" | "entity_search"
# entity_record: single-property/prospect lookup — carries entity_type/result/source_timestamp/query_datetime
# entity_search: legal-entity subject search — carries scope, outcome, callback
#
# Table consolidated from single-property/prospect lookups and legal-entity searches.


class CadastralQuery(SQLModel, table=True):
    __tablename__ = "cadastral_queries"

    query_type: str  # "entity_record" | "entity_search"
    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    timestamp: datetime
    owner: str
    query_datetime: datetime | None = None  # entity_record only
    entity_type: str | None = None  # entity_record only — "property" | "prospect"
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # entity_record only
    source_timestamp: int | None = None  # entity_record only — raw API timestamp
    scope: str | None = None  # entity_search only — "national" | "local"
    callback: bool | None = None  # entity_search only
    outcome: str | None = None  # entity_search only

    location_parameters: Optional["CadastralLocationParameters"] = Relationship(back_populates="query")
    property_properties: Optional["CadastralPropertyProperty"] = Relationship(back_populates="query")
    prospect_properties: list["CadastralProspectProperty"] = Relationship(back_populates="query")
    prospect_owners: list["CadastralProspectOwner"] = Relationship(back_populates="query")
    parameter: Optional["CadastralLegalEntitySearchParameter"] = Relationship(back_populates="query")
    entities: list["CadastralLegalEntitySearchEntity"] = Relationship(back_populates="query")

    names: ClassVar[dict] = {
        "Id": "id",
        "Endpoint": "endpoint",
        "Stato": "status",
        "stato": "status",
        # "Timestamp" (capital T) → entity_search datetime field (ISO string from API)
        "Timestamp": "timestamp",
        "Owner": "owner",
        "Datetime": "query_datetime",
        "callback": "callback",
        "risultato": "result",
        # "timestamp" (lowercase) → entity_record raw Unix int (different API key, different type)
        "timestamp": "source_timestamp",
        "esito": "outcome",
    }


class CadastralInspection(SQLModel, table=True):
    __tablename__ = "cadastral_inspections"

    id: str | None = Field(default=None, primary_key=True)
    entity: str
    callback: bool
    inspection_type: str
    requester: str
    document: str
    outcome: str

    location_parameters: Optional["CadastralLocationParameters"] = Relationship(back_populates="inspection")

    names: ClassVar[dict] = {
        "Entita": "entity",
        "Callback": "callback",
        "TipoVisura": "inspection_type",
        "Richiedente": "requester",
        "Documento": "document",
        "Esito": "outcome",
    }


# ─── Location Parameters (merged Property + Prospect + Inspection) ────────────
#
# Exactly one of query_id / inspection_id is set per row.
# subunit: present for prospect and inspection types; NULL for property.
# property_id: present for inspection type only.


class CadastralLocationParameters(SQLModel, table=True):
    __tablename__ = "cadastral_location_parameters"

    id: int | None = Field(default=None, primary_key=True)
    query_id: str | None = Field(default=None, foreign_key="cadastral_queries.id", index=True)
    inspection_id: str | None = Field(default=None, foreign_key="cadastral_inspections.id", index=True)
    location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    property_id: str | None = None  # inspection only

    query: Optional["CadastralQuery"] = Relationship(back_populates="location_parameters")
    inspection: Optional["CadastralInspection"] = Relationship(back_populates="location_parameters")
    location: Optional["CadastralLocation"] = Relationship()

    names: ClassVar[dict] = {
        "idImmobile": "property_id",
    }

    location_names: ClassVar[dict] = {
        "tipoCatasto": "cadastre_type",
        "provincia": "province",
        "comune": "municipality",
        "sezione": "section",
        "sezioneUrbana": "section",
        "foglio": "sheet",
        "particella": "parcel",
        "subalterno": "subunit",
    }


class CadastralPropertyProperty(SQLModel, table=True):
    __tablename__ = "cadastral_property_properties"

    id: str = Field(foreign_key="cadastral_queries.id", primary_key=True)

    query: Optional["CadastralQuery"] = Relationship(back_populates="property_properties")


class CadastralProspectProperty(SQLModel, table=True):
    __tablename__ = "cadastral_prospect_properties"

    id: str = Field(foreign_key="cadastral_queries.id", primary_key=True)

    query: Optional["CadastralQuery"] = Relationship(back_populates="prospect_properties")


class CadastralProspectOwner(SQLModel, table=True):
    __tablename__ = "cadastral_prospect_owners"

    id: str = Field(foreign_key="cadastral_queries.id", primary_key=True)

    query: Optional["CadastralQuery"] = Relationship(back_populates="prospect_owners")


# ─── Legal Entity Search ─────────────────────────────────────────────────────


class CadastralLegalEntitySearchParameter(SQLModel, table=True):
    __tablename__ = "legal_entity_search_parameters"

    id: str = Field(foreign_key="cadastral_queries.id", primary_key=True)
    tax_code: str
    cadastre_type: str
    province: str

    query: Optional["CadastralQuery"] = Relationship(back_populates="parameter")

    names: ClassVar[dict] = {
        "cf_piva": "tax_code",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }


class CadastralLegalEntitySearchEntity(SQLModel, table=True):
    __tablename__ = "legal_entity_search_entities"

    id: str | None = Field(default=None, primary_key=True)
    query_id: str = Field(foreign_key="cadastral_queries.id")
    subject_id: int | None = Field(default=None, foreign_key="cadastral_subjects.id", index=True)
    birth_place_text: str | None = None

    query: Optional["CadastralQuery"] = Relationship(back_populates="entities")
    subject: Optional["CadastralSubject"] = Relationship()
    geographic_summaries: list["CadastralLegalEntitySearchGeoSummary"] = Relationship(back_populates="entity")
    properties: list["CadastralLegalEntitySearchProperty"] = Relationship(back_populates="entity")

    names: ClassVar[dict] = {
        "id_soggetto": "id",
        "id": "query_id",
        "luogo_nascita": "birth_place_text",
    }

    subject_names: ClassVar[dict] = {
        "cognome": "last_name",
        "nome": "first_name",
        "data_nascita": "date_of_birth",
        "sesso": "gender",
        "cf": "fiscal_code",
    }


# ─── Geographic Summary (merged LandRegistry + Municipality) ─────────────────
#
# geo_type: "land_registry" | "municipality"
# land_registry rows carry town + province; municipality rows carry municipality.
# buildings / lands counts are present in both.


class CadastralLegalEntitySearchGeoSummary(SQLModel, table=True):
    __tablename__ = "legal_entity_search_geo_summaries"

    id: int | None = Field(default=None, primary_key=True)
    entity_id: str = Field(foreign_key="legal_entity_search_entities.id", index=True)
    geo_type: str  # "land_registry" | "municipality"
    town: str | None = None  # land_registry rows only
    province: str | None = None  # land_registry rows only
    municipality: str | None = None  # municipality rows only
    buildings: int
    lands: int

    entity: Optional["CadastralLegalEntitySearchEntity"] = Relationship(back_populates="geographic_summaries")

    names: ClassVar[dict] = {
        # land_registry fields
        "citta": "town",
        "provincia": "province",
        # municipality fields
        "comune": "municipality",
        # shared
        "fabbricati": "buildings",
        "terreni": "lands",
        "id_soggetto": "entity_id",
    }


class CadastralLegalEntitySearchProperty(SQLModel, table=True):
    __tablename__ = "legal_entity_search_properties"

    id: int | None = Field(default=None, primary_key=True)
    land_registry: str
    ownership: str
    location: str
    location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    cadastral_code: str
    classification: str
    cadastral_class: str
    consistency: str
    income: str
    property_id: str
    entity_id: str = Field(foreign_key="legal_entity_search_entities.id")

    entity: Optional["CadastralLegalEntitySearchEntity"] = Relationship(back_populates="properties")
    cadastral_location: Optional["CadastralLocation"] = Relationship()

    names: ClassVar[dict] = {
        "catasto": "land_registry",
        "titolarita": "ownership",
        "ubicazione": "location",
        "codice_catastale": "cadastral_code",
        "classamento": "classification",
        "classe": "cadastral_class",
        "consistenza": "consistency",
        "rendita": "income",
        "id_immobile": "property_id",
        "id_soggetto": "entity_id",
    }

    location_names: ClassVar[dict] = {
        "catasto": "cadastre_type",
        "provincia": "province",
        "comune": "municipality",
        "sezione": "section",
        "foglio": "sheet",
        "sezione_urbana": "section",
        "particella": "parcel",
        "subalterno": "subunit",
    }

    @staticmethod
    def get_query_with_related_data(query_id, session: Session):
        statement = (
            select(CadastralQuery)
            .options(
                joinedload(CadastralQuery.parameter),
                joinedload(CadastralQuery.entities).joinedload(
                    CadastralLegalEntitySearchEntity.properties
                ),
            )
            .where(CadastralQuery.id == query_id)
        )
        return session.exec(statement).first()
