"""
SQLModel ORM classes for structured data parsed from SISTER XML cadastral documents.

All tables are child extensions of visura_documents (see db_models.py).
The parent VisuraDocument already stores the XML header fields (TitoloVisura,
DatiLiquidazione, request section) after being merged in from the former
VisuraDocument/VisuraRequest tables.

Each class carries a `names` ClassVar that maps Italian XML attribute/element
names to English Python field names, following the same convention as cadastral.py.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Optional

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from .db_models import CadastralLocation, CadastralSubject, OwnershipRight


class DocumentSubject(SQLModel, table=True):
    """Parsed subject (Soggetto/SoggettoPF) section — soggetto_attuale documents only."""

    __tablename__ = "document_subjects"

    id: int = Field(foreign_key="visura_documents.id", primary_key=True)
    subject_id: int | None = Field(default=None, foreign_key="cadastral_subjects.id", index=True)

    subject: Optional["CadastralSubject"] = Relationship()

    subject_names: ClassVar[dict] = {
        "CodiceFiscale": "fiscal_code",
        "Cognome": "last_name",
        "Nome": "first_name",
        "Sesso": "gender",
        "DataNascita": "date_of_birth",
        "CodiceComune": "birth_municipality_code",
    }

    birth_location_names: ClassVar[dict] = {
        "Provincia": "province",
        "ComuneNascita": "municipality",
    }


class PropertyGroup(SQLModel, table=True):
    """Group of building units (GruppoUnitaImmobiliari) from a soggetto_attuale document."""

    __tablename__ = "property_groups"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="visura_documents.id")
    group_index: str | None = None
    cadastre_type: str | None = None
    municipality_code: str | None = None
    description: str | None = None

    units: list["BuildingUnit"] = Relationship(back_populates="group")
    ownership_mutations: list["OwnershipMutation"] = Relationship(back_populates="property_group")

    names: ClassVar[dict] = {
        "IndiceGruppo": "group_index",
        "TipoCatasto": "cadastre_type",
        "CodiceComune": "municipality_code",
        "Descrizione": "description",
    }


class BuildingUnit(SQLModel, table=True):
    """Single building unit (ImmobileFabbricatiS) within a property group."""

    __tablename__ = "building_units"

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="property_groups.id")
    unit_index: str | None = None

    group: Optional["PropertyGroup"] = Relationship(back_populates="units")
    identifier: Optional["BuildingIdentifier"] = Relationship(back_populates="building_unit")
    classification: Optional["BuildingClassification"] = Relationship(back_populates="building_unit")
    surface: Optional["BuildingSurface"] = Relationship(back_populates="building_unit")
    related_parcels: list["RelatedParcel"] = Relationship(back_populates="building_unit")

    names: ClassVar[dict] = {
        "IndiceImmobile": "unit_index",
    }


class BuildingCurrentState(SQLModel, table=True):
    """Current-state snapshot (SituazioneAttualeFabbricati) — fabbricati_storica only."""

    __tablename__ = "building_current_states"

    id: int = Field(foreign_key="visura_documents.id", primary_key=True)
    address_text: str | None = None  # from IndirizzoImm element content

    identifier: Optional["BuildingIdentifier"] = Relationship(back_populates="current_state")
    classification: Optional["BuildingClassification"] = Relationship(back_populates="current_state")
    surface: Optional["BuildingSurface"] = Relationship(back_populates="current_state")
    related_parcels: list["RelatedParcel"] = Relationship(back_populates="current_state")

    names: ClassVar[dict] = {
        "IndirizzoImm": "address_text",
    }


class BuildingIdentifier(SQLModel, table=True):
    """Cadastral identifier (IdentificativoDefinitivo) for a building.

    Exactly one of the three nullable FKs is set per row, depending on context:
      - building_unit_id    → soggetto_attuale (unit within group)
      - current_state_id    → fabbricati_storica (current-state snapshot)
      - history_document_id → fabbricati_storica (StoriaImmobileFabbricati historical record)
    """

    __tablename__ = "building_identifiers"

    id: int | None = Field(default=None, primary_key=True)
    building_unit_id: int | None = Field(default=None, foreign_key="building_units.id")
    current_state_id: int | None = Field(default=None, foreign_key="building_current_states.id")
    history_document_id: int | None = Field(default=None, foreign_key="visura_documents.id")
    location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    municipality_code: str | None = None
    sequence_id: str | None = None

    building_unit: Optional["BuildingUnit"] = Relationship(back_populates="identifier")
    current_state: Optional["BuildingCurrentState"] = Relationship(back_populates="identifier")
    location: Optional["CadastralLocation"] = Relationship()

    __table_args__ = (
        sa.CheckConstraint(
            "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) + (history_document_id IS NOT NULL) = 1",
            name="ck_building_identifier_one_parent",
        ),
        sa.Index(
            "uq_building_identifier_building_unit_id",
            "building_unit_id",
            unique=True,
            sqlite_where=sa.text("building_unit_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_building_identifier_current_state_id",
            "current_state_id",
            unique=True,
            sqlite_where=sa.text("current_state_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_building_identifier_history_document_id",
            "history_document_id",
            unique=True,
            sqlite_where=sa.text("history_document_id IS NOT NULL"),
        ),
    )

    names: ClassVar[dict] = {
        "CodiceComune": "municipality_code",
        "ProgrId": "sequence_id",
    }

    location_names: ClassVar[dict] = {
        "Provincia": "province",
        "Comune": "municipality",
        "Foglio": "sheet",
        "ParticellaNum": "parcel",
        "Subalterno": "subunit",
        "SezUrbana": "section",
        "TipoCatasto": "cadastre_type",
    }


class BuildingClassification(SQLModel, table=True):
    """Cadastral classification (DatiClassamentoF) for a building.

    Same polymorphic FK pattern as BuildingIdentifier.
    ConsistenzaType child (Valore/Unita) is inlined.
    """

    __tablename__ = "building_classifications"

    id: int | None = Field(default=None, primary_key=True)
    building_unit_id: int | None = Field(default=None, foreign_key="building_units.id")
    current_state_id: int | None = Field(default=None, foreign_key="building_current_states.id")
    history_document_id: int | None = Field(default=None, foreign_key="visura_documents.id")
    census_zone: str | None = None
    category: str | None = None  # CategoriaFabbricatiType A1–F7
    cadastral_class: str | None = None
    cadastral_income: Decimal | None = None  # RenditaEuro in EUR
    consistency_value: Decimal | None = None  # Consistenza.Valore
    consistency_unit: str | None = None  # Consistenza.Unita: vani | mq | mc

    building_unit: Optional["BuildingUnit"] = Relationship(back_populates="classification")
    current_state: Optional["BuildingCurrentState"] = Relationship(back_populates="classification")

    __table_args__ = (
        sa.CheckConstraint(
            "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) + (history_document_id IS NOT NULL) = 1",
            name="ck_building_classification_one_parent",
        ),
        sa.Index(
            "uq_building_classification_building_unit_id",
            "building_unit_id",
            unique=True,
            sqlite_where=sa.text("building_unit_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_building_classification_current_state_id",
            "current_state_id",
            unique=True,
            sqlite_where=sa.text("current_state_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_building_classification_history_document_id",
            "history_document_id",
            unique=True,
            sqlite_where=sa.text("history_document_id IS NOT NULL"),
        ),
    )

    names: ClassVar[dict] = {
        "ZonaCensuaria": "census_zone",
        "Categoria": "category",
        "Classe": "cadastral_class",
        "RenditaEuro": "cadastral_income",
        # Consistenza child element attributes
        "Valore": "consistency_value",
        "Unita": "consistency_unit",
    }


class BuildingSurface(SQLModel, table=True):
    """Surface area record (SuperficieF) for a building unit or current-state record.

    Fabbricati storica adds TotaleE (excluded area); soggetto attuale only has Totale.
    Planimetria.Descrizione is inlined as planimetria (soggetto only).
    """

    __tablename__ = "building_surfaces"

    id: int | None = Field(default=None, primary_key=True)
    building_unit_id: int | None = Field(default=None, foreign_key="building_units.id")
    current_state_id: int | None = Field(default=None, foreign_key="building_current_states.id")
    total_area: Decimal | None = None
    excluded_area: Decimal | None = None  # TotaleE (fabbricati storica only)
    planimetria: str | None = None  # Planimetria.Descrizione (soggetto only)

    building_unit: Optional["BuildingUnit"] = Relationship(back_populates="surface")
    current_state: Optional["BuildingCurrentState"] = Relationship(back_populates="surface")

    __table_args__ = (
        sa.CheckConstraint(
            "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) = 1",
            name="ck_building_surface_one_parent",
        ),
        sa.Index(
            "uq_building_surface_building_unit_id",
            "building_unit_id",
            unique=True,
            sqlite_where=sa.text("building_unit_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_building_surface_current_state_id",
            "current_state_id",
            unique=True,
            sqlite_where=sa.text("current_state_id IS NOT NULL"),
        ),
    )

    names: ClassVar[dict] = {
        "Totale": "total_area",
        "TotaleE": "excluded_area",
        "Descrizione": "planimetria",  # from Planimetria child element
    }


class BuildingAddress(SQLModel, table=True):
    """Historical address record (DatiIndirizzo) from StoriaImmobileFabbricati."""

    __tablename__ = "building_addresses"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="visura_documents.id")
    address_text: str | None = None  # IndirizzoImm element content
    situation: str | None = None  # Situazione element content

    names: ClassVar[dict] = {
        "IndirizzoImm": "address_text",
        "Situazione": "situation",
    }


class RelatedParcel(SQLModel, table=True):
    """Related land parcel (IdentificativoCorrelato / MappaliCorrelati) for a building unit.

    FK is building_unit_id (soggetto_attuale) or current_state_id (fabbricati_storica).
    """

    __tablename__ = "related_parcels"

    id: int | None = Field(default=None, primary_key=True)
    building_unit_id: int | None = Field(default=None, foreign_key="building_units.id")
    current_state_id: int | None = Field(default=None, foreign_key="building_current_states.id")
    location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    sequence_id: str | None = None

    building_unit: Optional["BuildingUnit"] = Relationship(back_populates="related_parcels")
    current_state: Optional["BuildingCurrentState"] = Relationship(back_populates="related_parcels")
    location: Optional["CadastralLocation"] = Relationship()

    __table_args__ = (
        sa.CheckConstraint(
            "(building_unit_id IS NOT NULL) + (current_state_id IS NOT NULL) = 1",
            name="ck_related_parcel_one_parent",
        ),
    )

    names: ClassVar[dict] = {
        "ProgrId": "sequence_id",
    }

    location_names: ClassVar[dict] = {
        "SezCensuaria": "section",
        "Foglio": "sheet",
        "ParticellaNum": "parcel",
        "TipoCatasto": "cadastre_type",
    }


class LandParcel(SQLModel, table=True):
    """Land parcel (ImmobileTerreni / IdentificativoDefinitivo) from terreni_attuale documents."""

    __tablename__ = "land_parcels"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="visura_documents.id")
    location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    municipality_code: str | None = None
    sequence_id: str | None = None
    partita: str | None = None  # Partita child element content

    location: Optional["CadastralLocation"] = Relationship()
    classifications: list["LandClassification"] = Relationship(back_populates="parcel")
    ownership_mutations: list["OwnershipMutation"] = Relationship(back_populates="land_parcel")

    names: ClassVar[dict] = {
        "CodiceComune": "municipality_code",
        "ProgrId": "sequence_id",
        "Partita": "partita",
    }

    location_names: ClassVar[dict] = {
        "Provincia": "province",
        "Comune": "municipality",
        "SezCensuaria": "section",
        "Foglio": "sheet",
        "ParticellaNum": "parcel",
        "TipoCatasto": "cadastre_type",
    }


class LandClassification(SQLModel, table=True):
    """Land quality/income classification row (ClassamentoT) for a land parcel."""

    __tablename__ = "land_classifications"

    id: int | None = Field(default=None, primary_key=True)
    parcel_id: int = Field(foreign_key="land_parcels.id")
    quality: str | None = None  # QualitaTerreniType
    cadastral_class: str | None = None
    area: Decimal | None = None  # SuperficieMQ in m²
    deduction_symbol: str | None = None  # SimboloDeduzione
    dominical_income: Decimal | None = None  # RedditoDominicaleEuro
    agricultural_income: Decimal | None = None  # RedditoAgrarioEuro
    dominical_income_lire: str | None = None  # RedditoDominicaleLire (legacy archival)
    agricultural_income_lire: str | None = None  # RedditoAgrarioLire (legacy archival)

    parcel: Optional["LandParcel"] = Relationship(back_populates="classifications")

    names: ClassVar[dict] = {
        "Qualita": "quality",
        "Classe": "cadastral_class",
        "SuperficieMQ": "area",
        "SimboloDeduzione": "deduction_symbol",
        "RedditoDominicaleEuro": "dominical_income",
        "RedditoAgrarioEuro": "agricultural_income",
        "RedditoDominicaleLire": "dominical_income_lire",
        "RedditoAgrarioLire": "agricultural_income_lire",
    }


class OwnershipMutation(SQLModel, table=True):
    """Ownership mutation event (MutazioneSoggettiva).

    Polymorphic: exactly one of the three nullable FKs is set per row:
      - document_id       → fabbricati_storica (StoriaIntestazione, direct doc link)
      - property_group_id → soggetto_attuale (IntestazioneGruppo)
      - land_parcel_id    → terreni_attuale (ImmobileTerreni)
    mutation_index="current" marks IntestazioneAttuale rows (fabbricati_storica).

    IdentificativoDefinitivoRiferimento cross-reference fields are inlined (ref_*).
    """

    __tablename__ = "ownership_mutations"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int | None = Field(default=None, foreign_key="visura_documents.id")
    property_group_id: int | None = Field(default=None, foreign_key="property_groups.id")
    land_parcel_id: int | None = Field(default=None, foreign_key="land_parcels.id")
    mutation_index: str | None = None  # IndiceMutazione; "current" = IntestazioneAttuale
    mutation_date: str | None = None  # DataMutazione YYYYMMDD
    source_description: str | None = None  # DatiDerivantiDaMutazSogg element content
    reference_location_id: int | None = Field(default=None, foreign_key="cadastral_locations.id", index=True)
    ref_municipality_code: str | None = None
    ref_property_status: str | None = None  # StatoImmobile: A=active | S=suppressed

    property_group: Optional["PropertyGroup"] = Relationship(back_populates="ownership_mutations")
    land_parcel: Optional["LandParcel"] = Relationship(back_populates="ownership_mutations")
    reference_location: Optional["CadastralLocation"] = Relationship()
    owners: list["PropertyOwner"] = Relationship(back_populates="mutation")

    __table_args__ = (
        sa.CheckConstraint(
            "(document_id IS NOT NULL) + (property_group_id IS NOT NULL) + (land_parcel_id IS NOT NULL) = 1",
            name="ck_ownership_mutation_one_parent",
        ),
    )

    names: ClassVar[dict] = {
        "IndiceMutazione": "mutation_index",
        "DataMutazione": "mutation_date",
        "DatiDerivantiDaMutazSogg": "source_description",
        "CodiceComune": "ref_municipality_code",
        "StatoImmobile": "ref_property_status",
    }

    reference_location_names: ClassVar[dict] = {
        "Provincia": "province",
        "Comune": "municipality",
        "Foglio": "sheet",
        "ParticellaNum": "parcel",
        "SezUrbana": "section",
        "TipoCatasto": "cadastre_type",
    }


class PropertyOwner(SQLModel, table=True):
    """Individual owner (Intestato) within an ownership mutation.

    DirittiReali child attributes are inlined (right_*).
    Nominativo and CF are element content, inlined as display_name and fiscal_code.
    """

    __tablename__ = "property_owners"

    id: int | None = Field(default=None, primary_key=True)
    mutation_id: int = Field(foreign_key="ownership_mutations.id")
    owner_index: str | None = None  # IndiceIntestato
    subject_id: int | None = Field(default=None, foreign_key="cadastral_subjects.id", index=True)
    right_id: int | None = Field(default=None, foreign_key="ownership_rights.id", index=True)

    mutation: Optional["OwnershipMutation"] = Relationship(back_populates="owners")
    subject: Optional["CadastralSubject"] = Relationship()
    right: Optional["OwnershipRight"] = Relationship()

    names: ClassVar[dict] = {
        "IndiceIntestato": "owner_index",
    }

    subject_names: ClassVar[dict] = {
        "Nominativo": "display_name",
        "CF": "fiscal_code",
    }

    right_names: ClassVar[dict] = {
        "CodiceDiritto": "right_code",
        "Descrizione": "right_description",
        "Quota": "ownership_share",
        "InizioTitolo": "start_date",
        "FineDiritto": "end_date",
    }
