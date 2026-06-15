from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Column
from sqlalchemy.orm import Session, joinedload
from sqlmodel import Field, Relationship, SQLModel, select


class CadastralQuery(SQLModel, table=True):
    __tablename__ = "cadastral_query"

    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    timestamp: datetime
    owner: str
    datetime: datetime

    names: ClassVar[dict] = {
        "Id": "id",
        "Endpoint": "endpoint",
        "Stato": "status",
        "Timestamp": "timestamp",
        "Owner": "owner",
        "Datetime": "datetime",
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

    names: ClassVar[dict] = {
        "Entita": "entity",
        "Callback": "callback",
        "TipoVisura": "inspection_type",
        "Richiedente": "requester",
        "Documento": "document",
        "Esito": "outcome",
    }


class CadastralInspectionParameter(SQLModel, table=True):
    __tablename__ = "cadastral_inspection_parameters"

    id: str | None = Field(default=None, primary_key=True)
    property_id: str
    cadastre_type: str
    sheet: int
    parcel: int
    subunit: int
    city: str
    section: str
    urban_section: str
    province: str

    names: ClassVar[dict] = {
        "idImmobile": "property_id",
        "tipoCatasto": "cadastre_type",
        "foglio": "sheet",
        "particella": "parcel",
        "subalterno": "subunit",
        "comune": "city",
        "sezione": "section",
        "sezioneUrbana": "urban_section",
        "provincia": "province",
    }


class CadastralProperty(SQLModel, table=True):
    __tablename__ = "properties"

    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    callback: bool
    result: dict[str, Any] = Field(default={}, sa_column=Column(JSON))
    outcome: str
    timestamp: int
    owner: str

    names: ClassVar[dict] = {
        "id": "id",
        "endpoint": "endpoint",
        "stato": "status",
        "callback": "callback",
        "risultato": "result",
        "esito": "outcome",
        "timestamp": "timestamp",
        "owner": "owner",
    }


class CadastralPropertyParameter(SQLModel, table=True):
    __tablename__ = "property_parameters"

    id: str | None = Field(default=None, primary_key=True)
    cadastre_type: str
    province: str
    city: str
    section: str
    urban_section: str
    sheet: int
    parcel: int

    names: ClassVar[dict] = {
        "tipoCatasto": "cadastre_type",
        "provincia": "province",
        "comune": "city",
        "sezione": "section",
        "sezioneUrbana": "urban_section",
        "foglio": "sheet",
        "particella": "parcel",
    }


class CadastralPropertyProperty(SQLModel, table=True):
    __tablename__ = "property_properties"

    id: str | None = Field(default=None, primary_key=True)


class CadastralProspect(SQLModel, table=True):
    __tablename__ = "cadastral_prospects"

    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    callback: bool
    result: dict[str, Any] = Field(default={}, sa_column=Column(JSON))
    outcome: str
    timestamp: int
    owner: str

    names: ClassVar[dict] = {
        "id": "id",
        "endpoint": "endpoint",
        "stato": "status",
        "callback": "callback",
        "risultato": "result",
        "esito": "outcome",
        "timestamp": "timestamp",
        "owner": "owner",
    }


class CadastralProspectParameter(SQLModel, table=True):
    __tablename__ = "cadastral_prospect_parameters"

    id: str | None = Field(default=None, primary_key=True)
    cadastre_type: str
    province: str
    city: str
    section: str
    urban_section: str
    sheet: int
    parcel: int
    subunit: int

    names: ClassVar[dict] = {
        "tipoCatasto": "cadastre_type",
        "provincia": "province",
        "comune": "city",
        "sezione": "section",
        "sezioneUrbana": "urban_section",
        "foglio": "sheet",
        "particella": "parcel",
        "subalterno": "subunit",
    }


class CadastralProspectProperty(SQLModel, table=True):
    __tablename__ = "cadastral_prospect_properties"

    id: str | None = Field(default=None, primary_key=True)


class CadastralProspectOwner(SQLModel, table=True):
    __tablename__ = "cadastral_prospect_owners"

    id: str | None = Field(default=None, primary_key=True)


class CadastralNationalLegalEntitySearchQuery(SQLModel, table=True):
    __tablename__ = "national_legal_entity_search_queries"

    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    callback: bool
    outcome: str
    timestamp: datetime
    owner: str

    # Relationships
    parameter = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchParameter",
        back_populates="query",
    )
    legal_entity = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchLegalEntity",
        back_populates="query",
    )

    names: ClassVar[dict] = {
        "stato": "status",
        "esito": "outcome",
    }


class CadastralNationalLegalEntitySearchParameter(SQLModel, table=True):
    __tablename__ = "national_legal_entity_search_parameters"

    id: str = Field(
        foreign_key="national_legal_entity_search_queries.id", primary_key=True
    )
    tax_code: str
    cadastre_type: str
    province: str

    # Relationships
    query = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchQuery",
        back_populates="parameter",
    )

    names: ClassVar[dict] = {
        "cf_piva": "tax_code",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }


class CadastralNationalLegalEntitySearchLegalEntity(SQLModel, table=True):
    __tablename__ = "national_legal_entity_search_legal_entities"

    id: str | None = Field(default=None, primary_key=True)
    query_id: str = Field(foreign_key="national_legal_entity_search_queries.id")
    last_name: str
    first_name: str
    date_of_birth: datetime
    place_of_birth: str
    gender: str = Field(max_length=1)
    fiscal_code: str

    # Relationships
    query = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchQuery",
        back_populates="legal_entity",
    )
    land_registries = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchLandRegistry",
        back_populates="legal_entity",
    )
    municipalities = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchMunicipality",
        back_populates="legal_entity",
    )

    names: ClassVar[dict] = {
        "id_soggetto": "id",
        "id": "query_id",
        "cognome": "last_name",
        "nome": "first_name",
        "data_nascita": "date_of_birth",
        "luogo_nascita": "place_of_birth",
        "sesso": "gender",
        "cf": "fiscal_code",
    }


class CadastralNationalLegalEntitySearchLandRegistry(SQLModel, table=True):
    __tablename__ = "national_legal_entity_search_land_registries"

    id: int | None = Field(default=None, primary_key=True)
    town: str
    buildings: int
    lands: int
    province: str
    legal_entity_id: str = Field(
        foreign_key="national_legal_entity_search_legal_entities.id"
    )

    # Relationships
    legal_entity = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchLegalEntity",
        back_populates="land_registries",
    )

    names: ClassVar[dict] = {
        "citta": "town",
        "fabbricati": "buildings",
        "terreni": "lands",
        "provincia": "province",
        "id_soggetto": "legal_entity_id",
    }


class CadastralNationalLegalEntitySearchMunicipality(SQLModel, table=True):
    __tablename__ = "national_legal_entity_search_municipalities"

    id: int | None = Field(default=None, primary_key=True)
    municipality: str
    buildings: int
    lands: int
    legal_entity_id: str = Field(
        foreign_key="national_legal_entity_search_legal_entities.id"
    )

    # Relationships
    legal_entity = Relationship(
        sa_relationship="CadastralNationalLegalEntitySearchLegalEntity",
        back_populates="municipalities",
    )

    names: ClassVar[dict] = {
        "comune": "municipality",
        "fabbricati": "buildings",
        "terreni": "lands",
        "id_soggetto": "legal_entity_id",
    }


class CadastralLegalEntitySearchQuery(SQLModel, table=True):
    __tablename__ = "legal_entity_search_queries"

    id: str | None = Field(default=None, primary_key=True)
    endpoint: str
    status: str
    callback: bool
    outcome: str
    timestamp: datetime
    owner: str

    # Relationships
    parameter = Relationship(
        sa_relationship="CadastralLegalEntitySearchParameter", back_populates="query"
    )
    legal_entity = Relationship(
        sa_relationship="CadastralLegalEntitySearchLegalEntity", back_populates="query"
    )

    names: ClassVar[dict] = {
        "stato": "status",
        "esito": "outcome",
    }


class CadastralLegalEntitySearchParameter(SQLModel, table=True):
    __tablename__ = "legal_entity_searche_parameters"

    id: str = Field(foreign_key="legal_entity_search_queries.id", primary_key=True)
    tax_code: str
    cadastre_type: str
    province: str
    # query_id: str = Field(foreign_key='legal_entity_search_queries.id')

    # Relationships
    query = Relationship(
        sa_relationship="CadastralLegalEntitySearchQuery", back_populates="parameter"
    )

    names: ClassVar[dict] = {
        "cf_piva": "tax_code",
        "tipo_catasto": "cadastre_type",
        "provincia": "province",
    }


class CadastralLegalEntitySearchLegalEntity(SQLModel, table=True):
    __tablename__ = "legal_entity_search_legal_entities"

    id: str | None = Field(default=None, primary_key=True)
    query_id: str = Field(foreign_key="legal_entity_search_queries.id")
    last_name: str
    first_name: str
    date_of_birth: datetime
    place_of_birth: str
    gender: str = Field(max_length=1)
    fiscal_code: str

    # Relationships
    query = Relationship(
        sa_relationship="CadastralLegalEntitySearchQuery", back_populates="legal_entity"
    )
    properties = Relationship(
        sa_relationship="CadastralLegalEntitySearchProperty",
        back_populates="legal_entity",
    )

    names: ClassVar[dict] = {
        "id_soggetto": "id",
        "id": "query_id",
        "cognome": "last_name",
        "nome": "first_name",
        "data_nascita": "date_of_birth",
        "luogo_nascita": "place_of_birth",
        "sesso": "gender",
        "cf": "fiscal_code",
    }


class CadastralLegalEntitySearchProperty(SQLModel, table=True):
    __tablename__ = "legal_entity_search_properties"

    id: int | None = Field(default=None, primary_key=True)
    land_registry: str
    ownership: str
    location: str
    province: str
    municipality: str
    cadastral_code: str
    section: str
    sheet: str
    urban_section: str
    particle: str
    subaltern: str
    classification: str
    cadastral_class: str
    consistency: str
    income: str
    property_id: str
    legal_entity_id: str = Field(foreign_key="legal_entity_search_legal_entities.id")

    # Relationships
    legal_entity = Relationship(
        sa_relationship="CadastralLegalEntitySearchLegalEntity",
        back_populates="properties",
    )

    names: ClassVar[dict] = {
        "catasto": "land_registry",
        "titolarita": "ownership",
        "ubicazione": "location",
        "provincia": "province",
        "comune": "municipality",
        "codice_catastale": "cadastral_code",
        "sezione": "section",
        "foglio": "sheet",
        "sezione_urbana": "urban_section",
        "particella": "particle",
        "subalterno": "subaltern",
        "classamento": "classification",
        "classe": "cadastral_class",
        "consistenza": "consistency",
        "rendita": "income",
        "id_immobile": "property_id",
        "id_soggetto": "legal_entity_id",
    }

    @staticmethod
    def get_query_with_related_data(query_id, session: Session):
        statement = (
            select(CadastralLegalEntitySearchQuery)
            .options(
                joinedload(CadastralLegalEntitySearchQuery.parameter),
                joinedload(CadastralLegalEntitySearchQuery.legal_entity),
                joinedload(CadastralLegalEntitySearchQuery.properties),
            )
            .where(CadastralLegalEntitySearchQuery.id == query_id)
        )

        result = session.exec(statement).first()

        return result
