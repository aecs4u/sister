"""SQLModel table definitions for the sister database (flat, normalised layout).

The base tables come from ``alembic/sql/20261024_flat_normalised/02_schema.sql``; later Alembic revisions extend them
as the application evolves (see ``docs/database_design.md``). Columns, primary keys and foreign keys are declared here
for queries; checks, unique keys and indexes are defined by the base DDL and subsequent migrations.

* ``Request`` holds the request and its response in one row (the old ``visura_requests`` + ``visura_responses``).
* Dates are ``date``, amounts ``Decimal``, shares are numerator/denominator, places are ISTAT municipalities + ``CadastralUnit``.
* Raw payloads (response JSON, extraction JSON, document text, calculation results) are in ``sister_archive``.

Note: do NOT use ``from __future__ import annotations`` here, it breaks SQLAlchemy's annotation handling with SQLModel.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from aecs4u_domain.feedback import FeedbackConfig, FeedbackConfigItem, FeedbackUnsubscribe  # noqa: F401
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class IstatMunicipality(SQLModel, table=True):
    """Read-only mapping to the shared ISTAT foreign table.

    PostgreSQL does not permit foreign keys to foreign tables, so the app's
    ``*_comune_id`` columns store ``numeric_code`` and join to this table
    explicitly.
    """

    __tablename__ = "municipalities"
    __table_args__ = {"schema": "istat"}

    numeric_code: int = Field(primary_key=True)
    name: str
    italian_name: str
    other_language_name: Optional[str] = Field(default=None)
    province_abbreviation: Optional[str] = Field(default=None)
    cadastral_code: Optional[str] = Field(default=None)


class TerritoryProvince(SQLModel, table=True):
    """Province names used by the local territory lookup API."""

    __tablename__ = "territory_province"

    code: str = Field(primary_key=True, max_length=2)
    name: str


class _TerritoryOfficeFields(SQLModel):
    """Fields shared by the court and land-registry office catalogs."""

    source_id: Optional[str] = Field(default=None, unique=True)
    code: Optional[str] = Field(default=None, unique=True)
    name: str
    province_code: Optional[str] = Field(default=None)
    municipality_code: Optional[int] = Field(default=None)
    address: Optional[str] = Field(default=None)
    source_url: Optional[str] = Field(default=None)
    source_updated_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"nullable": False, "server_default": sa.func.now()},
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"nullable": False, "server_default": sa.func.now()},
    )


class Court(_TerritoryOfficeFields, table=True):
    """Court catalog entry; municipality competence is stored separately."""

    __tablename__ = "court"
    __table_args__ = (
        sa.ForeignKeyConstraint(["province_code"], ["territory_province.code"], ondelete="SET NULL"),
        sa.Index("ix_court_municipality_code", "municipality_code"),
        sa.Index("ix_court_province_code", "province_code"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    court_type: Optional[str] = Field(default=None)


class CourtMunicipality(SQLModel, table=True):
    """Municipalities in a court's territorial jurisdiction."""

    __tablename__ = "court_municipality"
    __table_args__ = (
        sa.ForeignKeyConstraint(["court_id"], ["court.id"], ondelete="CASCADE"),
        sa.Index("ix_court_municipality_municipality_code", "municipality_code"),
    )

    court_id: int = Field(primary_key=True)
    municipality_code: int = Field(primary_key=True)


class ConservatoriaOffice(_TerritoryOfficeFields, table=True):
    """Conservatoria / land-registry public office catalog entry."""

    __tablename__ = "conservatoria_office"
    __table_args__ = (
        sa.ForeignKeyConstraint(["province_code"], ["territory_province.code"], ondelete="SET NULL"),
        sa.Index("ix_conservatoria_office_municipality_code", "municipality_code"),
        sa.Index("ix_conservatoria_office_province_code", "province_code"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)


class ConservatoriaMunicipality(SQLModel, table=True):
    """Municipalities served by a Conservatoria office."""

    __tablename__ = "conservatoria_municipality"
    __table_args__ = (
        sa.ForeignKeyConstraint(["office_id"], ["conservatoria_office.id"], ondelete="CASCADE"),
        sa.Index("ix_conservatoria_municipality_municipality_code", "municipality_code"),
    )

    office_id: int = Field(primary_key=True)
    municipality_code: int = Field(primary_key=True)


class ConservatoriaSection(SQLModel, table=True):
    """Cadastral sections advertised by a Conservatoria for one municipality."""

    __tablename__ = "conservatoria_section"
    __table_args__ = (
        sa.CheckConstraint("cadastre_type IN ('F', 'T')", name="ck_conservatoria_section_cadastre_type"),
        sa.ForeignKeyConstraint(
            ["office_id", "municipality_code"],
            ["conservatoria_municipality.office_id", "conservatoria_municipality.municipality_code"],
            ondelete="CASCADE",
        ),
    )

    office_id: int = Field(primary_key=True)
    municipality_code: int = Field(primary_key=True)
    cadastre_type: str = Field(primary_key=True, max_length=1)
    section_code: str = Field(primary_key=True)


class CadastralOfficeListing(SQLModel, table=True):
    """Row of the portal's "Elenco uffici" page: a provincial office and the date its cadastral data was updated."""

    __tablename__ = "cadastral_office_listing"
    __table_args__ = (
        sa.ForeignKeyConstraint(["province_code"], ["territory_province.code"], ondelete="SET NULL"),
        sa.Index("ix_cadastral_office_listing_province_code", "province_code"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    province_code: Optional[str] = Field(default=None)
    data_updated_on: Optional[date] = Field(default=None)
    source_url: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )
    updated_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )


class CadastralUnit(SQLModel, table=True):
    __tablename__ = "cadastral_unit"

    id: Optional[int] = Field(default=None, primary_key=True)
    cadastre_type: Optional[str] = Field(default=None)
    comune_id: Optional[int] = Field(default=None)
    section: Optional[str] = Field(default=None)
    sheet: Optional[str] = Field(default=None)
    parcel: Optional[str] = Field(default=None)
    subunit: Optional[str] = Field(default=None)


class CadastralParcelCoordinate(SQLModel, table=True):
    """Persistent point location for a cadastral parcel, with its source recorded."""

    __tablename__ = "cadastral_parcel_coordinate"
    __table_args__ = (
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_cadastral_parcel_coordinate_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_cadastral_parcel_coordinate_longitude"),
        sa.CheckConstraint(
            "location_source IN ('cadastral_geometry', 'anncsu_address', 'osm_address')",
            name="ck_cadastral_parcel_coordinate_source",
        ),
        sa.Index("ix_cadastral_parcel_coordinate_source", "location_source"),
    )

    cadastral_code: str = Field(sa_column=Column(sa.Text, primary_key=True, nullable=False))
    section: str = Field(default="", sa_column=Column(sa.Text, primary_key=True, nullable=False, server_default=""))
    sheet: str = Field(sa_column=Column(sa.Text, primary_key=True, nullable=False))
    parcel: str = Field(sa_column=Column(sa.Text, primary_key=True, nullable=False))
    latitude: float
    longitude: float
    location_source: str = Field(sa_column=Column(sa.Text, nullable=False))
    source_reference: Optional[str] = Field(default=None, sa_column=Column(sa.Text, nullable=True))
    source_method: str = Field(sa_column=Column(sa.Text, nullable=False))
    updated_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )


class Subject(SQLModel, table=True):
    __tablename__ = "subject"

    id: Optional[int] = Field(default=None, primary_key=True)
    subject_type: Optional[str] = Field(default=None)
    fiscal_code: Optional[str] = Field(default=None)
    name: str
    last_name: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None)
    gender: Optional[str] = Field(default=None)
    birth_date: Optional[date] = Field(default=None)
    birth_comune_id: Optional[int] = Field(default=None)
    birth_municipality_text: Optional[str] = Field(default=None)
    birth_province_code: Optional[str] = Field(default=None)


class SubjectRecord(SQLModel, table=True):
    """One registry record that carries a fiscal code.

    SISTER stores a codice fiscale / partita IVA under several records (denominazione + sede): a region, its comuni and
    its demanio; ANAS has hundreds. They are different subjects of the cadastre with their own properties, while
    ``subject`` is unique per code. Each property found for a code points at the record it was found under
    (``property.subject_record_id``), so the entities that share a code stay distinguishable.
    """

    __tablename__ = "subject_record"
    __table_args__ = (
        sa.UniqueConstraint("fiscal_code", "denomination", "seat", name="uq_subject_record_identity"),
        sa.UniqueConstraint("id", "subject_id", name="uq_subject_record_id_subject"),
        sa.Index("ix_subject_record_subject", "subject_id"),
        sa.Index("ix_subject_record_fiscal_code", "fiscal_code"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
    fiscal_code: str
    denomination: str
    seat: str = Field(default="", description="Sede as SISTER writes it, e.g. 'LECCO (LC)'; empty when unknown")
    seat_municipality: Optional[str] = Field(default=None)
    seat_province_code: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )


class RightKind(SQLModel, table=True):
    __tablename__ = "right_kind"

    id: Optional[int] = Field(default=None, primary_key=True)
    right_type: Optional[str] = Field(default=None)
    right_code: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)


class OwnershipRight(SQLModel, table=True):
    __tablename__ = "ownership_right"

    id: Optional[int] = Field(default=None, primary_key=True)
    right_kind_id: int = Field(foreign_key="right_kind.id")
    share_numerator: Optional[int] = Field(default=None)
    share_denominator: Optional[int] = Field(default=None)
    start_date: Optional[date] = Field(default=None)
    end_date: Optional[date] = Field(default=None)


class RequestType(SQLModel, table=True):
    __tablename__ = "request_type"

    code: str = Field(primary_key=True)
    label: str
    description: str


class RequestTypeTag(SQLModel, table=True):
    __tablename__ = "request_type_tag"
    __table_args__ = (
        sa.ForeignKeyConstraint(["request_type"], ["request_type.code"], ondelete="CASCADE"),
        sa.Index("ix_request_type_tag_tag", "tag"),
    )

    request_type: str = Field(primary_key=True)
    tag: str = Field(primary_key=True)


class RequestTag(SQLModel, table=True):
    """Source or workflow tags assigned to an individual persisted request."""

    __tablename__ = "request_tag"
    __table_args__ = (
        sa.ForeignKeyConstraint(["request_id"], ["request.id"], ondelete="CASCADE"),
        sa.Index("ix_request_tag_tag", "tag"),
    )

    request_id: int = Field(sa_column=Column(sa.BigInteger, primary_key=True))
    tag: str = Field(primary_key=True)


class DocumentType(SQLModel, table=True):
    __tablename__ = "document_type"

    code: str = Field(primary_key=True)


class CadastralCategory(SQLModel, table=True):
    __tablename__ = "cadastral_category"

    code: str = Field(primary_key=True)
    description: Optional[str] = Field(default=None)


class LandQuality(SQLModel, table=True):
    __tablename__ = "land_quality"

    code: str = Field(primary_key=True)


class LandRegistryOffice(SQLModel, table=True):
    __tablename__ = "land_registry_office"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str


class Request(SQLModel, table=True):
    __tablename__ = "request"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_ref: str
    request_type: Optional[str] = Field(default=None, foreign_key="request_type.code")
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    cache_key: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    responded_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    success: Optional[bool] = Field(default=None)
    error: Optional[str] = Field(default=None)
    cadastre_type: Optional[str] = Field(default=None)
    total_results: Optional[int] = Field(default=None)
    total_intestati: Optional[int] = Field(default=None)
    skipped_soppresso: Optional[int] = Field(default=None)
    subject_query: Optional[str] = Field(default=None)
    # hash of the answer's content; an answer identical to the previous one of the same query (cache_key) is not stored
    # again: it points at the request that holds the content (payload, properties, owners)
    content_hash: Optional[str] = Field(default=None, index=True)
    same_as_request_id: Optional[int] = Field(default=None, foreign_key="request.id", index=True)


class RequestProvince(SQLModel, table=True):
    """One row of the "Elenco Province" table SISTER shows for a subject search (provinces where it has assets)."""

    __tablename__ = "request_province"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    province_index: int
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
    fiscal_code: Optional[str] = Field(default=None)
    province_code: Optional[str] = Field(default=None)
    province_name: str
    buildings_count: int = Field(default=0)
    land_parcels_count: int = Field(default=0)


class RequestResult(SQLModel, table=True):
    __tablename__ = "request_result"
    __table_args__ = (
        sa.UniqueConstraint("request_id", "result_index", name="uq_request_result"),
        sa.UniqueConstraint("id", "request_id", name="uq_request_result_id_request"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_index: int
    visura_present: Optional[bool] = Field(default=None)


class Property(SQLModel, table=True):
    __tablename__ = "property"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["result_id", "request_id"], ["request_result.id", "request_result.request_id"],
            name="fk_property_result_request",
        ),
        sa.ForeignKeyConstraint(
            ["subject_record_id", "subject_id"], ["subject_record.id", "subject_record.subject_id"],
            name="fk_property_subject_record_subject",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_id: Optional[int] = Field(default=None)
    property_type: Optional[str] = Field(default=None)
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
    subject_record_id: Optional[int] = Field(default=None)
    address: Optional[str] = Field(default=None)
    partita: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None, foreign_key="cadastral_category.code")
    cadastral_class: Optional[str] = Field(default=None)
    consistency_value: Optional[Decimal] = Field(default=None)
    consistency_unit: Optional[str] = Field(default=None)
    consistency_sqm: Optional[Decimal] = Field(default=None)
    consistency_rooms: Optional[Decimal] = Field(default=None)
    census_zone: Optional[str] = Field(default=None)
    land_quality: Optional[str] = Field(default=None, foreign_key="land_quality.code")
    area_sqm: Optional[Decimal] = Field(default=None)
    income_eur: Optional[Decimal] = Field(default=None)
    dominical_income_eur: Optional[Decimal] = Field(default=None)
    agricultural_income_eur: Optional[Decimal] = Field(default=None)
    registered_office_comune_id: Optional[int] = Field(default=None)


class ResultOwner(SQLModel, table=True):
    __tablename__ = "result_owner"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["result_id", "request_id"], ["request_result.id", "request_result.request_id"],
            name="fk_result_owner_result_request",
        ),
        sa.ForeignKeyConstraint(
            ["subject_record_id", "subject_id"], ["subject_record.id", "subject_record.subject_id"],
            name="fk_result_owner_subject_record_subject",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_id: Optional[int] = Field(default=None)
    owner_index: Optional[int] = Field(default=None)
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
    subject_record_id: Optional[int] = Field(default=None)
    right_id: Optional[int] = Field(default=None, foreign_key="ownership_right.id")


class OwnershipHistory(SQLModel, table=True):
    """One period in which a subject held a right on a cadastral unit, as the searches saw it.

    Appended only when something changes: an identical observation moves ``last_seen_at``/``last_request_id`` and adds
    to ``seen_count``; a different right or record opens a new period; a search that lists everything (all the owners of
    a unit, or all the properties of a subject) and no longer shows it closes the period (``ended_at``).
    """

    __tablename__ = "ownership_history"
    __table_args__ = (
        sa.Index("ix_ownership_history_unit", "cadastral_unit_id"),
        sa.Index("ix_ownership_history_subject", "subject_id"),
        sa.Index("ix_ownership_history_last_request", "last_request_id"),
        sa.Index(
            "uq_ownership_history_open", "cadastral_unit_id", "subject_id", "right_id", "subject_record_id",
            unique=True, postgresql_where=sa.text("ended_at IS NULL"), postgresql_nulls_not_distinct=True,
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    cadastral_unit_id: int = Field(foreign_key="cadastral_unit.id")
    subject_id: int = Field(foreign_key="subject.id")
    subject_record_id: Optional[int] = Field(default=None, foreign_key="subject_record.id")
    right_id: Optional[int] = Field(default=None, foreign_key="ownership_right.id")
    first_seen_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    first_request_id: int = Field(foreign_key="request.id")
    last_seen_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    last_request_id: int = Field(foreign_key="request.id")
    seen_count: int = Field(default=1)
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    ended_request_id: Optional[int] = Field(default=None, foreign_key="request.id")


class PropertyStateHistory(SQLModel, table=True):
    """One period of a cadastral unit's classamento, consistenza, rendita and address, appended only when one changes."""

    __tablename__ = "property_state_history"
    __table_args__ = (
        sa.Index("ix_property_state_history_unit", "cadastral_unit_id"),
        sa.Index("ix_property_state_history_last_request", "last_request_id"),
        sa.Index("uq_property_state_history_open", "cadastral_unit_id", unique=True, postgresql_where=sa.text("ended_at IS NULL")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    cadastral_unit_id: int = Field(foreign_key="cadastral_unit.id")
    state_hash: str
    property_type: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)
    cadastral_class: Optional[str] = Field(default=None)
    consistency_value: Optional[Decimal] = Field(default=None)
    consistency_unit: Optional[str] = Field(default=None)
    census_zone: Optional[str] = Field(default=None)
    land_quality: Optional[str] = Field(default=None)
    area_sqm: Optional[Decimal] = Field(default=None)
    income_eur: Optional[Decimal] = Field(default=None)
    dominical_income_eur: Optional[Decimal] = Field(default=None)
    agricultural_income_eur: Optional[Decimal] = Field(default=None)
    address: Optional[str] = Field(default=None)
    partita: Optional[str] = Field(default=None)
    first_seen_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    first_request_id: int = Field(foreign_key="request.id")
    last_seen_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    last_request_id: int = Field(foreign_key="request.id")
    seen_count: int = Field(default=1)
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    ended_request_id: Optional[int] = Field(default=None, foreign_key="request.id")


class Document(SQLModel, table=True):
    __tablename__ = "document"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: Optional[int] = Field(default=None, foreign_key="request.id")
    document_type: Optional[str] = Field(default=None, foreign_key="document_type.code")
    file_format: Optional[str] = Field(default=None)
    file_name: str
    file_size: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    subject_text: Optional[str] = Field(default=None)
    requested_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    land_registry_office_id: Optional[int] = Field(default=None, foreign_key="land_registry_office.id")
    view_subtype: Optional[str] = Field(default=None)
    protocol: Optional[str] = Field(default=None)
    protocol_year: Optional[int] = Field(default=None)
    title: Optional[str] = Field(default=None)
    reference_date: Optional[date] = Field(default=None)
    generated_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    liquidation_protocol: Optional[str] = Field(default=None)
    liquidation_year: Optional[int] = Field(default=None)
    registry_view_type: Optional[str] = Field(default=None)
    service_type: Optional[str] = Field(default=None)
    requester: Optional[str] = Field(default=None)
    liquidation_units: Optional[int] = Field(default=None)
    historical_total_area_sqm: Optional[Decimal] = Field(default=None)
    historical_excluded_area_sqm: Optional[Decimal] = Field(default=None)
    subject_search_scope: Optional[str] = Field(default=None)
    subject_search_province: Optional[str] = Field(default=None)


class DocumentFavorite(SQLModel, table=True):
    """A user's saved document in the documents browser."""

    __tablename__ = "document_favorite"
    __table_args__ = (sa.Index("ix_document_favorite_document_id", "document_id"),)

    user_id: str = Field(sa_column=Column(sa.String(), primary_key=True))
    document_id: int = Field(
        sa_column=Column(sa.BigInteger, sa.ForeignKey("document.id", ondelete="CASCADE"), primary_key=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class DocumentPurchase(SQLModel, table=True):
    """A user's recorded purchase and resulting access to one archived document."""

    __tablename__ = "document_purchase"
    __table_args__ = (
        sa.Index("ix_document_purchase_document_id", "document_id"),
        sa.Index(
            "ux_document_purchase_payment_ref",
            "payment_ref",
            unique=True,
            postgresql_where=sa.text("payment_ref IS NOT NULL"),
        ),
    )

    user_id: str = Field(sa_column=Column(sa.String(), primary_key=True))
    document_id: int = Field(
        sa_column=Column(sa.BigInteger, sa.ForeignKey("document.id", ondelete="CASCADE"), primary_key=True)
    )
    purchased_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    created_by: str = Field(default="", sa_column=Column(sa.String(), nullable=False, server_default=""))
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # How the access was obtained: 'admin_grant', or what aecs4u-billing fulfilled ('stripe', 'wallet').
    source: str = Field(default="admin_grant", sa_column=Column(sa.String(32), nullable=False, server_default="admin_grant"))
    amount_cents: Optional[int] = Field(default=None, sa_column=Column(sa.Integer(), nullable=True))
    currency: Optional[str] = Field(default=None, sa_column=Column(sa.String(3), nullable=True))
    payment_ref: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    wallet_tx_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    expires_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    # Revocation is soft: the row stays as the history of what was granted, to whom and why it ended.
    revoked_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    revoked_by: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    revoked_reason: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))


class SubjectAccess(SQLModel, table=True):
    """A grant to view a saved subject search; document contents still require their own purchases."""

    __tablename__ = "subject_access"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_access_type"),
        sa.CheckConstraint(
            "(subject_type = 'company' AND subject_code ~ '^[0-9]{11}$') OR "
            "(subject_type = 'person' AND subject_code ~ '^[A-Z0-9]{16}$')",
            name="ck_subject_access_code",
        ),
        sa.ForeignKeyConstraint(
            ["subject_type", "subject_code"], ["subject_profile.subject_type", "subject_profile.subject_code"],
            name="fk_subject_access_profile",
        ),
    )

    user_id: str = Field(sa_column=Column(sa.String(), primary_key=True))
    subject_type: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    subject_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    created_by: str
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    revoked_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    revoked_by: Optional[str] = Field(default=None)
    revoked_reason: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))


class SubjectPurchase(SQLModel, table=True):
    """Append-only payment records for buying person and company profile access."""

    __tablename__ = "subject_purchase"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_purchase_type"),
        sa.CheckConstraint(
            "(subject_type = 'company' AND subject_code ~ '^[0-9]{11}$') OR "
            "(subject_type = 'person' AND subject_code ~ '^[A-Z0-9]{16}$')",
            name="ck_subject_purchase_code",
        ),
        sa.ForeignKeyConstraint(
            ["subject_type", "subject_code"], ["subject_profile.subject_type", "subject_profile.subject_code"],
            name="fk_subject_purchase_profile",
        ),
        sa.CheckConstraint("status IN ('completed', 'refund_required', 'refunded')", name="ck_subject_purchase_status"),
        sa.Index(
            "uq_subject_purchase_payment_ref",
            "payment_ref",
            unique=True,
            postgresql_where=sa.text("payment_ref IS NOT NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(sa_column=Column(sa.String(), nullable=False, index=True))
    subject_type: str = Field(sa_column=Column(sa.String(16), nullable=False))
    subject_code: str = Field(sa_column=Column(sa.String(16), nullable=False))
    purchased_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    created_by: str = Field(default="stripe", sa_column=Column(sa.String(), nullable=False, server_default="stripe"))
    source: str = Field(default="stripe", sa_column=Column(sa.String(32), nullable=False, server_default="stripe"))
    amount: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(12, 2), nullable=True))
    currency: Optional[str] = Field(default=None, sa_column=Column(sa.String(8), nullable=True))
    payment_ref: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    wallet_tx_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    status: str = Field(default="completed", sa_column=Column(sa.String(24), nullable=False, server_default="completed"))
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))


class WalletGiftCoupon(SQLModel, table=True):
    """Hashed wallet-credit gift codes; plaintext codes are shown only when first created."""

    __tablename__ = "wallet_gift_coupon"
    __table_args__ = (
        sa.UniqueConstraint("code_hash", name="uq_wallet_gift_coupon_code_hash"),
        sa.CheckConstraint("amount > 0", name="ck_wallet_gift_coupon_amount"),
        sa.CheckConstraint("max_redemptions > 0", name="ck_wallet_gift_coupon_max_redemptions"),
        sa.CheckConstraint("redeemed_count >= 0 AND redeemed_count <= max_redemptions", name="ck_wallet_gift_coupon_redemptions"),
        sa.CheckConstraint("status IN ('active', 'frozen', 'revoked')", name="ck_wallet_gift_coupon_status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    code_hash: str = Field(sa_column=Column(sa.String(64), nullable=False))
    code_hint: str = Field(sa_column=Column(sa.String(8), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    max_redemptions: int = Field(default=1, nullable=False)
    redeemed_count: int = Field(default=0, nullable=False)
    status: str = Field(default="active", sa_column=Column(sa.String(16), nullable=False, server_default="active"))
    created_by: str = Field(sa_column=Column(sa.String(), nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    expires_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))


class WalletGiftCouponRedemption(SQLModel, table=True):
    """Append-only redemption lifecycle for one user per wallet gift coupon."""

    __tablename__ = "wallet_gift_coupon_redemption"
    __table_args__ = (
        sa.UniqueConstraint("coupon_id", "user_id", name="uq_wallet_gift_coupon_redemption_user"),
        sa.ForeignKeyConstraint(
            ["coupon_id"], ["wallet_gift_coupon.id"],
            name="fk_wallet_gift_coupon_redemption_coupon_id_wallet_gift_coupon", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("status IN ('pending', 'completed', 'reversed', 'reversal_required')", name="ck_wallet_gift_coupon_redemption_status"),
        sa.Index("ix_wallet_gift_coupon_redemption_coupon_id", "coupon_id"),
        sa.Index("ix_wallet_gift_coupon_redemption_user_id", "user_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    coupon_id: int = Field(nullable=False)
    user_id: str = Field(sa_column=Column(sa.String(), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    status: str = Field(default="pending", sa_column=Column(sa.String(24), nullable=False, server_default="pending"))
    wallet_tx_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    reversed_tx_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    redeemed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    completed_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))


class PagoPAPaymentHistory(SQLModel, table=True):
    """Imported pagoPA source rows, kept separate from wallet balances and billing transactions."""

    __tablename__ = "pagopa_payment_history"
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "source_file", "source_row", name="uq_pagopa_history_user_source_row"
        ),
        sa.CheckConstraint(
            "payment_status IN ('validated', 'rejected', 'unconfirmed')",
            name="ck_pagopa_history_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_pagopa_history_amount"),
        sa.Index("ix_pagopa_payment_history_user_id", "user_id"),
        sa.Index("ix_pagopa_payment_history_payment_date", "payment_date"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(sa_column=Column(sa.String(), nullable=False))
    payer_reference: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    payment_method: str = Field(sa_column=Column(sa.String(32), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    amount_source: str = Field(sa_column=Column(sa.String(32), nullable=False))
    currency: str = Field(default="EUR", sa_column=Column(sa.String(8), nullable=False, server_default="EUR"))
    payment_status: str = Field(sa_column=Column(sa.String(16), nullable=False))
    payment_date: Optional[date] = Field(default=None, sa_column=Column(sa.Date(), nullable=True))
    payment_date_source: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    operation_date: Optional[date] = Field(default=None, sa_column=Column(sa.Date(), nullable=True))
    operation_date_source: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    validation_date: Optional[date] = Field(default=None, sa_column=Column(sa.Date(), nullable=True))
    validation_date_source: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    rejection_date: Optional[date] = Field(default=None, sa_column=Column(sa.Date(), nullable=True))
    rejection_date_source: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    office: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    note: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    retry_available: bool = Field(default=False, sa_column=Column(sa.Boolean(), nullable=False, server_default=sa.false()))
    receipt_available: bool = Field(default=False, sa_column=Column(sa.Boolean(), nullable=False, server_default=sa.false()))
    source_file: str = Field(sa_column=Column(sa.String(255), nullable=False))
    source_sha256: str = Field(sa_column=Column(sa.String(64), nullable=False))
    source_row: int = Field(sa_column=Column(sa.Integer(), nullable=False))
    imported_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class SubjectFavorite(SQLModel, table=True):
    """A user's saved person or company profile, identified independently of its documents."""

    __tablename__ = "subject_favorite"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_favorite_type"),
        sa.ForeignKeyConstraint(
            ["subject_type", "subject_code"], ["subject_profile.subject_type", "subject_profile.subject_code"],
            name="fk_subject_favorite_profile",
        ),
    )

    user_id: str = Field(sa_column=Column(sa.String(), primary_key=True))
    subject_type: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    subject_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class SubjectProfile(SQLModel, table=True):
    """Canonical identity for profile metadata that exists independently of stored SISTER documents."""

    __tablename__ = "subject_profile"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_profile_type"),
    )

    subject_type: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    subject_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class SubjectHolding(SQLModel, table=True):
    """What a codice fiscale / partita IVA holds, computed from its latest subject search (``/persons`` ranking).

    Derived data: ``database.refresh_subject_holdings`` rewrites the row when a subject search is saved and
    ``sister db backfill holdings`` rebuilds them all. Valori catastali are the IMU base (``cadastral_values``), not
    market values; ``cadastral_value_share`` scales each unit by the subject's quota (a missing quota counts as 1/1).
    """

    __tablename__ = "subject_holding"

    fiscal_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    request_id: Optional[int] = Field(
        default=None, sa_column=Column(sa.BigInteger, sa.ForeignKey("request.id", ondelete="SET NULL"), nullable=True)
    )
    buildings: Optional[int] = Field(default=None)
    land_parcels: Optional[int] = Field(default=None)
    properties: Optional[int] = Field(default=None)
    full_ownership: Optional[int] = Field(default=None)
    unvalued: Optional[int] = Field(default=None)
    unknown_share: Optional[int] = Field(default=None)
    searched_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    cadastral_income_eur: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(16, 2), nullable=True))
    dominical_income_eur: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(16, 2), nullable=True))
    cadastral_value_eur: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(18, 2), nullable=True))
    cadastral_value_share_eur: Optional[Decimal] = Field(
        default=None, sa_column=Column(sa.Numeric(18, 2), nullable=True)
    )
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class SubjectHoldingProvince(SQLModel, table=True):
    """One province in which the latest successful subject search found property."""

    __tablename__ = "subject_holding_province"
    __table_args__ = (
        sa.ForeignKeyConstraint(["fiscal_code"], ["subject_holding.fiscal_code"], ondelete="CASCADE"),
    )

    fiscal_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    province: str = Field(sa_column=Column(sa.String(), primary_key=True))


class SubjectNote(SQLModel, table=True):
    """A private note a user keeps on one person or company profile."""

    __tablename__ = "subject_note"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_note_type"),
        sa.ForeignKeyConstraint(
            ["subject_type", "subject_code"], ["subject_profile.subject_type", "subject_profile.subject_code"],
            name="fk_subject_note_profile",
        ),
    )

    user_id: str = Field(sa_column=Column(sa.String(), primary_key=True))
    subject_type: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    subject_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    note: str = Field(sa_column=Column(sa.Text(), nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class SubjectTag(SQLModel, table=True):
    """A shared label that groups person and company profiles (any signed-in user sees and edits the same tags).

    ``tag_key`` is the casefolded tag, so "Politici" and "politici" are one tag; ``tag`` keeps the spelling of the
    first user who wrote it. Several tags per subject, several subjects per tag.
    """

    __tablename__ = "subject_tag"
    __table_args__ = (
        sa.CheckConstraint("subject_type IN ('company', 'person')", name="ck_subject_tag_type"),
        sa.ForeignKeyConstraint(
            ["subject_type", "subject_code"], ["subject_profile.subject_type", "subject_profile.subject_code"],
            name="fk_subject_tag_profile",
        ),
        sa.Index("ix_subject_tag_type_key", "subject_type", "tag_key"),
    )

    subject_type: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    subject_code: str = Field(sa_column=Column(sa.String(16), primary_key=True))
    tag_key: str = Field(sa_column=Column(sa.String(40), primary_key=True))
    tag: str = Field(sa_column=Column(sa.String(40), nullable=False))
    created_by: Optional[str] = Field(default=None, sa_column=Column(sa.String(), nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class ManualPropertyValuation(SQLModel, table=True):
    """A user's manually transcribed market estimate from an external provider."""

    __tablename__ = "manual_property_valuation"
    __table_args__ = (
        sa.CheckConstraint(
            "provider IN ('immobiliare', 'idealista', 'other')", name="ck_manual_property_valuation_provider"
        ),
        sa.CheckConstraint(
            "value_min > 0 AND (value_max IS NULL OR value_max >= value_min)",
            name="ck_manual_property_valuation_range",
        ),
        sa.Index("ix_manual_property_valuation_asset", "asset_key"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_manual_property_valuation_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    provider: str = Field(primary_key=True)
    value_min: Decimal = Field(sa_column=Column(sa.Numeric(14, 2), nullable=False))
    value_max: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(14, 2), nullable=True))
    source_date: Optional[date] = Field(default=None)
    source_url: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyValuationPreference(SQLModel, table=True):
    """One user's preferred source for one cadastral property."""

    __tablename__ = "property_valuation_preference"
    __table_args__ = (
        sa.Index("ix_property_valuation_preference_asset", "asset_key"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_property_valuation_preference_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    preferred_source: str
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyAffrancationAmount(SQLModel, table=True):
    """A user's editable affrancazione amount for one cadastral property."""

    __tablename__ = "property_affrancation_amount"
    __table_args__ = (
        sa.CheckConstraint("amount >= 0", name="ck_property_affrancation_amount_nonnegative"),
        sa.Index("ix_property_affrancation_amount_asset", "asset_key"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_property_affrancation_amount_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    amount: Decimal = Field(sa_column=Column(sa.Numeric(14, 2), nullable=False))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyOmiOverride(SQLModel, table=True):
    """A user's narrower, property-specific OMI value range."""

    __tablename__ = "property_omi_override"
    __table_args__ = (
        sa.CheckConstraint(
            "value_min > 0 AND value_max >= value_min", name="ck_property_omi_override_range"
        ),
        sa.Index("ix_property_omi_override_asset", "asset_key"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_property_omi_override_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    value_min: Decimal = Field(sa_column=Column(sa.Numeric(14, 2), nullable=False))
    value_max: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(14, 2), nullable=True))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyOmiSelection(SQLModel, table=True):
    """A user's selected OMI zone and typology for one cadastral property."""

    __tablename__ = "property_omi_selection"
    __table_args__ = (
        sa.CheckConstraint("typology_code IS NULL OR typology_code > 0", name="ck_property_omi_selection_typology"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_property_omi_selection_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    zone_code: Optional[str] = Field(default=None, sa_column=Column(sa.String(24), nullable=True))
    typology_code: Optional[int] = Field(default=None)
    is_manual: Optional[bool] = Field(default=None)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyActualSize(SQLModel, table=True):
    """A user's effective square-metre size for one cadastral property."""

    __tablename__ = "property_actual_size"
    __table_args__ = (
        sa.CheckConstraint("actual_size_sqm > 0", name="ck_property_actual_size_positive"),
        sa.Index("ix_property_actual_size_asset", "asset_key"),
        sa.ForeignKeyConstraint(["asset_key"], ["property_asset.asset_key"], name="fk_property_actual_size_asset"),
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    actual_size_sqm: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PropertyAsset(SQLModel, table=True):
    """Canonical registry for cadastral identities referenced by per-user settings."""

    __tablename__ = "property_asset"
    __table_args__ = (
        sa.CheckConstraint("asset_key ~ '^[a-f0-9]{64}$'", name="ck_property_asset_key_sha256"),
        sa.CheckConstraint(
            "(identity_version = 0 AND cadastre_type IS NULL AND municipality_code IS NULL AND municipality IS NULL "
            "AND province IS NULL AND section IS NULL AND sheet IS NULL AND parcel IS NULL AND subunit IS NULL) OR "
            "(identity_version = 1 AND cadastre_type IS NOT NULL AND sheet IS NOT NULL AND parcel IS NOT NULL "
            "AND (municipality_code IS NOT NULL OR municipality IS NOT NULL))",
            name="ck_property_asset_identity_version",
        ),
    )

    asset_key: str = Field(sa_column=Column(sa.String(64), primary_key=True))
    identity_version: int = Field(default=0, sa_column=Column(sa.SmallInteger, nullable=False, server_default="0"))
    cadastre_type: Optional[str] = Field(default=None)
    municipality_code: Optional[str] = Field(default=None)
    municipality: Optional[str] = Field(default=None)
    province: Optional[str] = Field(default=None)
    section: Optional[str] = Field(default=None)
    sheet: Optional[str] = Field(default=None)
    parcel: Optional[str] = Field(default=None)
    subunit: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class CalculationSnapshot(SQLModel, table=True):
    """Latest persisted result for a deterministic calculation and its source scope."""

    __tablename__ = "calculation_snapshot"
    __table_args__ = (
        sa.CheckConstraint("scope_type IN ('document', 'request')", name="ck_calculation_snapshot_scope_type"),
        sa.Index("ix_calculation_snapshot_calculated_at", "calculated_at"),
    )

    scope_type: str = Field(sa_column=Column(sa.String(24), primary_key=True))
    scope_id: str = Field(sa_column=Column(sa.Text, primary_key=True))
    viewer_key: str = Field(sa_column=Column(sa.Text, primary_key=True))
    calculation_type: str = Field(sa_column=Column(sa.String(64), primary_key=True))
    input_hash: str = Field(sa_column=Column(sa.String(64), nullable=False))
    algorithm_version: str = Field(sa_column=Column(sa.String(40), nullable=False))
    calculated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class PageVisit(SQLModel, table=True):
    __tablename__ = "page_visit"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    step: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    screenshot_url: Optional[str] = Field(default=None)
    visited_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))


class PageVisitFormElement(SQLModel, table=True):
    __tablename__ = "page_visit_form_element"

    id: Optional[int] = Field(default=None, primary_key=True)
    page_visit_id: int = Field(foreign_key="page_visit.id")
    element_index: Optional[int] = Field(default=None)
    tag: Optional[str] = Field(default=None)
    element_type: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    label: Optional[str] = Field(default=None)
    value: Optional[str] = Field(default=None)


class PageVisitError(SQLModel, table=True):
    __tablename__ = "page_visit_error"

    id: Optional[int] = Field(default=None, primary_key=True)
    page_visit_id: int = Field(foreign_key="page_visit.id")
    error_index: Optional[int] = Field(default=None)
    message: Optional[str] = Field(default=None)


class ActivityAction(SQLModel, table=True):
    __tablename__ = "activity_action"

    code: str = Field(primary_key=True)


class ActivityLog(SQLModel, table=True):
    __tablename__ = "activity_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    duration_ms: Optional[int] = Field(default=None)
    actor: str = Field(default="system")
    source: str = Field(default="web")
    action: str = Field(foreign_key="activity_action.code")
    status: str = Field(default="success")
    target: Optional[str] = Field(default=None)
    error: Optional[str] = Field(default=None)
    client_ip: Optional[str] = Field(default=None)


class ActivityLogAttribute(SQLModel, table=True):
    __tablename__ = "activity_log_attribute"

    activity_id: int = Field(primary_key=True, foreign_key="activity_log.id")
    section: str = Field(primary_key=True)
    path: str = Field(primary_key=True)
    value: Optional[str] = Field(default=None)


class DocumentExtraction(SQLModel, table=True):
    __tablename__ = "document_extraction"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    provider: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)
    schema_name: str
    schema_version: Optional[str] = Field(default=None)
    run_id: str
    workflow_id: Optional[str] = Field(default=None)
    output_path: Optional[str] = Field(default=None)
    extracted_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), sa_column=Column(sa.DateTime(timezone=True), nullable=False))


class MortgageLienType(SQLModel, table=True):
    __tablename__ = "mortgage_lien_type"

    code: str = Field(primary_key=True)


class MortgageTitleType(SQLModel, table=True):
    __tablename__ = "mortgage_title_type"

    code: str = Field(primary_key=True)


class MortgageNoteType(SQLModel, table=True):
    __tablename__ = "mortgage_note_type"

    code: str = Field(primary_key=True)


class MortgagePartyQuality(SQLModel, table=True):
    __tablename__ = "mortgage_party_quality"

    code: str = Field(primary_key=True)


class MortgageInspection(SQLModel, table=True):
    __tablename__ = "mortgage_inspection"

    extraction_id: int = Field(primary_key=True, foreign_key="document_extraction.id")
    inspection_date: Optional[date] = Field(default=None)
    inspection_time: Optional[time] = Field(default=None)
    inspection_number: Optional[str] = Field(default=None)
    inspection_number_date: Optional[date] = Field(default=None)
    inspection_started_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=False), nullable=True))  # portal local time
    requester: Optional[str] = Field(default=None)
    tax_paid_eur: Optional[int] = Field(default=None)
    page_count: Optional[int] = Field(default=None)
    office: Optional[str] = Field(default=None)
    service: Optional[str] = Field(default=None)
    note_type: Optional[str] = Field(default=None, foreign_key="mortgage_note_type.code")
    note_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    registro_generale: Optional[int] = Field(default=None)
    registro_particolare: Optional[int] = Field(default=None)
    presentazione_numero: Optional[int] = Field(default=None)
    presentazione_data: Optional[date] = Field(default=None)
    section_a_other_data: Optional[str] = Field(default=None)
    section_d_text: Optional[str] = Field(default=None)
    lien_type: Optional[str] = Field(default=None, foreign_key="mortgage_lien_type.code")
    lien_derived_from: Optional[str] = Field(default=None)
    lien_derived_from_code: Optional[str] = Field(default=None)
    lien_derived_from_description: Optional[str] = Field(default=None)
    lien_principal_eur: Optional[int] = Field(default=None)
    lien_annual_interest_rate_pct: Optional[Decimal] = Field(default=None)
    lien_total_eur: Optional[int] = Field(default=None)
    lien_variable_amounts: Optional[bool] = Field(default=None)
    lien_automatic_increase: Optional[bool] = Field(default=None)
    lien_resolutive_condition: Optional[bool] = Field(default=None)
    lien_duration_years: Optional[int] = Field(default=None)
    lien_duration_description: Optional[str] = Field(default=None)
    lien_mortgage_rank: Optional[int] = Field(default=None)
    title_type: Optional[str] = Field(default=None, foreign_key="mortgage_title_type.code")
    title_description: Optional[str] = Field(default=None)
    title_date: Optional[date] = Field(default=None)
    title_repertory_number: Optional[str] = Field(default=None)
    title_notary_name: Optional[str] = Field(default=None)
    title_notary_fiscal_code: Optional[str] = Field(default=None)
    title_notary_location: Optional[str] = Field(default=None)
    lien_semiannual_interest_rate: Optional[str] = Field(default=None)
    lien_interest_eur: Optional[int] = Field(default=None)
    lien_expenses_eur: Optional[int] = Field(default=None)
    lien_foreign_currency: Optional[str] = Field(default=None)


class MortgageInspectionParty(SQLModel, table=True):
    __tablename__ = "mortgage_inspection_party"

    id: Optional[int] = Field(default=None, primary_key=True)
    inspection_id: int = Field(foreign_key="mortgage_inspection.extraction_id")
    role: str
    ordinal: int
    subject_id: int = Field(foreign_key="subject.id")
    subject_number: Optional[int] = Field(default=None)
    quality: Optional[str] = Field(default=None, foreign_key="mortgage_party_quality.code")
    registered_office: Optional[str] = Field(default=None)
    mortgage_domicile: Optional[str] = Field(default=None)
    negotiation_unit_reference: Optional[int] = Field(default=None)
    right_type: Optional[str] = Field(default=None)
    share_numerator: Optional[int] = Field(default=None)
    share_denominator: Optional[int] = Field(default=None)


class MortgageInspectionProperty(SQLModel, table=True):
    __tablename__ = "mortgage_inspection_property"

    id: Optional[int] = Field(default=None, primary_key=True)
    inspection_id: int = Field(foreign_key="mortgage_inspection.extraction_id")
    unit_number: Optional[int] = Field(default=None)
    property_number: Optional[int] = Field(default=None)
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    nature: Optional[str] = Field(default=None)
    nature_description: Optional[str] = Field(default=None)
    room_count: Optional[int] = Field(default=None)
    floor: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)
    civic_number: Optional[str] = Field(default=None)
    area_sqm: Optional[Decimal] = Field(default=None)


class MortgageSearch(SQLModel, table=True):
    """One accepted persona-fisica search in the SISTER mortgage registers."""

    __tablename__ = "mortgage_search"
    __table_args__ = (
        sa.UniqueConstraint("request_id", name="uq_mortgage_search_request"),
        sa.UniqueConstraint("inspection_number", "inspection_date", name="uq_mortgage_search_number_date"),
        sa.CheckConstraint("scope IN ('office', 'national')", name="ck_mortgage_search_scope"),
        sa.Index("ix_mortgage_search_subject_date", "subject_fiscal_code", "inspection_date"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    request_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("request.id", name="fk_mortgage_search_request"), nullable=False
        )
    )
    inspection_number: str = Field(sa_column=Column(sa.String(64), nullable=False))
    inspection_date: date
    office: str = Field(sa_column=Column(sa.String(255), nullable=False))
    scope: str = Field(sa_column=Column(sa.String(16), nullable=False))
    subject_fiscal_code: str = Field(sa_column=Column(sa.String(32), nullable=False))
    requester_fiscal_code: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    motivation: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    convention_request_code: Optional[str] = Field(default=None, sa_column=Column(sa.String(128), nullable=True))
    formalities_count: int = Field(default=0, nullable=False)
    annotations_count: int = Field(default=0, nullable=False)


class MortgageSearchFormality(SQLModel, table=True):
    """Typed *Elenco sintetico delle formalità* row, linked to its archive search."""

    __tablename__ = "mortgage_search_formality"
    __table_args__ = (
        sa.UniqueConstraint("search_id", "ordinal", name="uq_mortgage_search_formality_ordinal"),
        sa.Index("ix_mortgage_search_formality_register", "registro_particolare", "registro_generale"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    search_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search.id", name="fk_mortgage_search_formality_search"),
            nullable=False,
        )
    )
    ordinal: int = Field(nullable=False)
    kind: str = Field(sa_column=Column(sa.String(24), nullable=False))
    direction: Optional[str] = Field(default=None, sa_column=Column(sa.String(24), nullable=True))
    formality_date: Optional[date] = Field(default=None)
    registro_particolare: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    registro_generale: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    public_official: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    repertory: Optional[str] = Field(default=None, sa_column=Column(sa.String(128), nullable=True))
    repertory_date: Optional[date] = Field(default=None)
    act_description: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    location: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    electronic_note: bool = Field(default=False, nullable=False)
    electronic_title: bool = Field(default=False, nullable=False)
    portal_selection_value: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    note_document_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("document.id", name="fk_mortgage_search_formality_note_document"),
            nullable=True,
        ),
    )
    title_document_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("document.id", name="fk_mortgage_search_formality_title_document"),
            nullable=True,
        ),
    )


class MortgageSearchAnnotation(SQLModel, table=True):
    """Annotation subsequent to a formality, including cancellation provenance."""

    __tablename__ = "mortgage_search_annotation"
    __table_args__ = (
        sa.UniqueConstraint("formality_id", "ordinal", name="uq_mortgage_search_annotation_ordinal"),
        sa.Index("ix_mortgage_search_annotation_cancellation", "is_cancellation"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    formality_id: int = Field(
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey("mortgage_search_formality.id", name="fk_mortgage_search_annotation_formality"),
            nullable=False,
        )
    )
    ordinal: int = Field(nullable=False)
    number: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    annotation_date: Optional[date] = Field(default=None)
    description: Optional[str] = Field(default=None, sa_column=Column(sa.Text(), nullable=True))
    is_cancellation: bool = Field(default=False, nullable=False)


class MortgageSearchFlow(SQLModel, table=True):
    """Durable state for a persona-fisica flow, including its pre-accept preview."""

    __tablename__ = "mortgage_search_flow"
    __table_args__ = (
        sa.UniqueConstraint("request_id", name="uq_mortgage_search_flow_request"),
        sa.CheckConstraint("scope IN ('office', 'national')", name="ck_mortgage_search_flow_scope"),
        sa.CheckConstraint(
            "state IN ('preview', 'accepted', 'running', 'complete', 'uncertain', 'failed', 'cancelled')",
            name="ck_mortgage_search_flow_state",
        ),
        sa.Index("ix_mortgage_search_flow_subject_state", "subject_fiscal_code", "state"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    request_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("request.id", name="fk_mortgage_search_flow_request"), nullable=False
        )
    )
    search_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search.id", name="fk_mortgage_search_flow_search"),
            nullable=True,
        ),
    )
    subject_fiscal_code: str = Field(sa_column=Column(sa.String(32), nullable=False))
    office: str = Field(sa_column=Column(sa.String(255), nullable=False))
    scope: str = Field(sa_column=Column(sa.String(16), nullable=False))
    state: str = Field(default="preview", sa_column=Column(sa.String(24), nullable=False))
    current_step: str = Field(default="preview", sa_column=Column(sa.String(32), nullable=False))
    state_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    started_by: str = Field(sa_column=Column(sa.String(255), nullable=False))
    started_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))


class MortgagePaidAction(SQLModel, table=True):
    """Durable consent and outcome record for a single potentially chargeable portal action."""

    __tablename__ = "mortgage_paid_action"
    __table_args__ = (
        sa.UniqueConstraint("token_digest", name="uq_mortgage_paid_action_token_digest"),
        sa.CheckConstraint(
            "state IN ('intended', 'clicked', 'confirmed', 'uncertain', 'not_executed')",
            name="ck_mortgage_paid_action_state",
        ),
        sa.Index(
            "uq_mortgage_paid_action_open_step",
            "flow_id",
            "step",
            unique=True,
            postgresql_where=sa.text("state IN ('intended', 'clicked', 'uncertain')"),
        ),
        sa.Index("ix_mortgage_paid_action_flow_state", "flow_id", "state"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    flow_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search_flow.id", name="fk_mortgage_paid_action_flow"),
            nullable=False,
        )
    )
    search_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search.id", name="fk_mortgage_paid_action_search"),
            nullable=True,
        ),
    )
    step: str = Field(sa_column=Column(sa.String(32), nullable=False))
    quote: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    confirmed_by: str = Field(sa_column=Column(sa.String(255), nullable=False))
    confirmed_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    state: str = Field(default="intended", sa_column=Column(sa.String(24), nullable=False))
    token_digest: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    token_expires_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    clicked_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    portal_request_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    result: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    reconciled_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))


class MortgageSearchRequest(SQLModel, table=True):
    """One note/title request as it moves from intent to availability and archival."""

    __tablename__ = "mortgage_search_request"
    __table_args__ = (
        sa.UniqueConstraint("portal_request_id", name="uq_mortgage_search_request_portal_id"),
        sa.CheckConstraint("kind IN ('note', 'title')", name="ck_mortgage_search_request_kind"),
        sa.CheckConstraint(
            "status IN ('intended', 'requested', 'available', 'downloaded', 'failed', 'uncertain')",
            name="ck_mortgage_search_request_status",
        ),
        sa.Index("ix_mortgage_search_request_flow", "search_id", "kind", "status"),
        sa.Index("ix_mortgage_search_request_formality", "formality_id", "kind", "status"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    search_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search.id", name="fk_mortgage_search_request_search"),
            nullable=False,
        )
    )
    formality_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search_formality.id", name="fk_mortgage_search_request_formality"),
            nullable=False,
        )
    )
    kind: str = Field(sa_column=Column(sa.String(16), nullable=False))
    format: Optional[str] = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    portal_request_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    status: str = Field(default="intended", sa_column=Column(sa.String(24), nullable=False))
    requested_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    document_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("document.id", name="fk_mortgage_search_request_document"), nullable=True
        ),
    )
    paid_action_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_paid_action.id", name="fk_mortgage_search_request_paid_action"),
            nullable=True,
        ),
    )


class MortgageSearchCharge(SQLModel, table=True):
    """An inspection cost line only when the evidence ties the charge to this search."""

    __tablename__ = "mortgage_search_charge"
    __table_args__ = (
        sa.CheckConstraint("kind IN ('inspection', 'note', 'title')", name="ck_mortgage_search_charge_kind"),
        sa.CheckConstraint("basis IN ('cost_page', 'receipt', 'statement_detail', 'tariff')", name="ck_mortgage_search_charge_basis"),
        sa.CheckConstraint("quantity > 0 AND unit_amount >= 0 AND amount >= 0", name="ck_mortgage_search_charge_amounts"),
        sa.UniqueConstraint("allocation_id", name="uq_mortgage_search_charge_allocation"),
        sa.ForeignKeyConstraint(
            ["allocation_id"], ["ade_charge_allocation.id"],
            name="fk_mortgage_search_charge_allocation_id_ade_charge_allocation", ondelete="RESTRICT",
        ),
        sa.Index("ix_mortgage_search_charge_search_kind", "search_id", "kind"),
        sa.Index("ix_mortgage_search_charge_portal_request", "portal_request_id"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    search_id: int = Field(
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("mortgage_search.id", name="fk_mortgage_search_charge_search"),
            nullable=False,
        )
    )
    kind: str = Field(sa_column=Column(sa.String(16), nullable=False))
    quantity: int = Field(nullable=False)
    unit_amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    currency: str = Field(default="EUR", sa_column=Column(sa.String(3), nullable=False, server_default="EUR"))
    charged_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    portal_request_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    basis: str = Field(sa_column=Column(sa.String(24), nullable=False))
    receipt_id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), nullable=True))
    wallet_tx_id: Optional[str] = Field(default=None, sa_column=Column(sa.String(128), nullable=True))
    allocation_id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), nullable=True))


class AdeStatementImport(SQLModel, table=True):
    """One immutable source file import of the AdE account statement."""

    __tablename__ = "ade_statement_import"
    __table_args__ = (
        sa.UniqueConstraint("source_sha256", name="uq_ade_statement_import_sha256"),
        sa.CheckConstraint("period_end >= period_start", name="ck_ade_statement_import_period"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    period_start: date
    period_end: date
    total_credit: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    total_debit: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    closing_balance: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(12, 2), nullable=True))
    source_filename: str = Field(sa_column=Column(sa.String(512), nullable=False))
    source_sha256: str = Field(sa_column=Column(sa.String(64), nullable=False))
    summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    imported_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class AdeStatementRow(SQLModel, table=True):
    """One posted credit or charge, keyed by the radio value from *Estratto conto consultazioni*."""

    __tablename__ = "ade_statement_row"
    __table_args__ = (
        sa.CheckConstraint("currency = 'EUR'", name="ck_ade_statement_row_currency"),
        sa.CheckConstraint("credit_amount IS NULL OR credit_amount >= 0", name="ck_ade_statement_row_credit"),
        sa.CheckConstraint("debit_amount IS NULL OR debit_amount >= 0", name="ck_ade_statement_row_debit"),
        sa.CheckConstraint(
            "num_nonnulls(credit_amount, debit_amount) = 1", name="ck_ade_statement_row_one_side"
        ),
        sa.Index("ix_ade_statement_row_date", "posted_on"),
    )

    statement_key: str = Field(sa_column=Column(sa.String(256), primary_key=True))
    import_id: int = Field(
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey(
                "ade_statement_import.id", name="fk_ade_statement_row_import_id_ade_statement_import",
                ondelete="RESTRICT",
            ),
            nullable=False,
        )
    )
    posted_on: date
    office: Optional[str] = Field(default=None, sa_column=Column(sa.String(32), nullable=True))
    description: str = Field(sa_column=Column(sa.Text(), nullable=False))
    credit_amount: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(12, 2), nullable=True))
    debit_amount: Optional[Decimal] = Field(default=None, sa_column=Column(sa.Numeric(12, 2), nullable=True))
    currency: str = Field(default="EUR", sa_column=Column(sa.String(3), nullable=False, server_default="EUR"))
    row_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    detail_data: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    detail_checked_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))


class AdeLedgerEntry(SQLModel, table=True):
    """Dedicated AdE account movement; never a SISTER app-credit transaction."""

    __tablename__ = "ade_ledger_entry"
    __table_args__ = (
        sa.UniqueConstraint("statement_key", name="uq_ade_ledger_entry_statement_key"),
        sa.UniqueConstraint("pagopa_payment_id", name="uq_ade_ledger_entry_pagopa_payment"),
        sa.CheckConstraint("entry_type IN ('topup', 'usage')", name="ck_ade_ledger_entry_type"),
        sa.CheckConstraint("amount > 0", name="ck_ade_ledger_entry_amount"),
        sa.Index("ix_ade_ledger_entry_date", "posted_on", "sequence_no"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    statement_key: str = Field(
        sa_column=Column(
            sa.String(256),
            sa.ForeignKey(
                "ade_statement_row.statement_key", name="fk_ade_ledger_entry_statement_key_ade_statement_row",
                ondelete="RESTRICT",
            ),
            nullable=False,
        )
    )
    posted_on: date
    sequence_no: int = Field(nullable=False)
    entry_type: str = Field(sa_column=Column(sa.String(16), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    balance_after: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    pagopa_payment_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey(
                "pagopa_payment_history.id", name="fk_ade_ledger_entry_pagopa_payment_id_pagopa_payment_history",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    linked_by: Optional[str] = Field(default=None, sa_column=Column(sa.String(255), nullable=True))
    linked_at: Optional[datetime] = Field(default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class AdeReceipt(SQLModel, table=True):
    """An imported AdE quietanza; receipt evidence remains separate from SISTER credit wallets."""

    __tablename__ = "ade_receipt"
    __table_args__ = (
        sa.UniqueConstraint("receipt_key", name="uq_ade_receipt_key"),
        sa.UniqueConstraint("source_sha256", name="uq_ade_receipt_sha256"),
        sa.CheckConstraint("amount >= 0", name="ck_ade_receipt_amount"),
        sa.CheckConstraint("currency = 'EUR'", name="ck_ade_receipt_currency"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    receipt_key: str = Field(sa_column=Column(sa.String(256), nullable=False))
    receipt_date: date
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    currency: str = Field(default="EUR", sa_column=Column(sa.String(3), nullable=False, server_default="EUR"))
    payment_method: Optional[str] = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    source_filename: str = Field(sa_column=Column(sa.String(512), nullable=False))
    source_sha256: str = Field(sa_column=Column(sa.String(64), nullable=False))
    document_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(), sa.ForeignKey("document.id", name="fk_ade_receipt_document"), nullable=True
        ),
    )
    receipt_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    imported_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class AdeReceiptLine(SQLModel, table=True):
    """Cost breakdown line from one imported quietanza."""

    __tablename__ = "ade_receipt_line"
    __table_args__ = (
        sa.UniqueConstraint("receipt_id", "ordinal", name="uq_ade_receipt_line_ordinal"),
        sa.CheckConstraint("quantity > 0 AND unit_amount >= 0 AND amount >= 0", name="ck_ade_receipt_line_amounts"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    receipt_id: int = Field(
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey("ade_receipt.id", name="fk_ade_receipt_line_receipt_id_ade_receipt", ondelete="CASCADE"),
            nullable=False,
        )
    )
    ordinal: int = Field(nullable=False)
    element: str = Field(sa_column=Column(sa.Text(), nullable=False))
    quantity: int = Field(default=1, nullable=False)
    unit_amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))


class AdeChargeAllocation(SQLModel, table=True):
    """Evidence-backed assignment of an AdE statement debit to an inspection, or an explicit unattributed amount."""

    __tablename__ = "ade_charge_allocation"
    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_ade_charge_allocation_amount"),
        sa.CheckConstraint(
            "evidence IN ('statement_detail', 'receipt', 'single_inspection_amount', 'unattributed')",
            name="ck_ade_charge_allocation_evidence",
        ),
        sa.UniqueConstraint("statement_key", "evidence_ref", name="uq_ade_charge_allocation_evidence"),
        sa.UniqueConstraint("receipt_line_id", name="uq_ade_charge_allocation_receipt_line"),
        sa.Index("ix_ade_charge_allocation_search", "search_id"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(sa.BigInteger(), primary_key=True))
    statement_key: str = Field(
        sa_column=Column(
            sa.String(256),
            sa.ForeignKey(
                "ade_statement_row.statement_key", name="fk_ade_charge_allocation_statement_key_ade_statement_row",
                ondelete="RESTRICT",
            ),
            nullable=False,
        )
    )
    search_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey(
                "mortgage_search.id", name="fk_ade_charge_allocation_search_id_mortgage_search", ondelete="RESTRICT"
            ),
            nullable=True,
        ),
    )
    receipt_line_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            sa.BigInteger(),
            sa.ForeignKey(
                "ade_receipt_line.id", name="fk_ade_charge_allocation_receipt_line_id_ade_receipt_line",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
    )
    amount: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    evidence: str = Field(sa_column=Column(sa.String(32), nullable=False))
    evidence_ref: str = Field(sa_column=Column(sa.String(256), nullable=False))
    created_by: str = Field(sa_column=Column(sa.String(255), nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


# ---------------------------------------------------------------------------
# Archive (schema sister_archive): raw payloads kept out of the working tables
# ---------------------------------------------------------------------------


class RequestPayload(SQLModel, table=True):
    __tablename__ = "request_payload"
    __table_args__ = {"schema": "sister_archive"}

    request_id: int = Field(primary_key=True, foreign_key="request.id")
    data: str


class DocumentContent(SQLModel, table=True):
    __tablename__ = "document_content"
    __table_args__ = {"schema": "sister_archive"}

    document_id: int = Field(primary_key=True, foreign_key="document.id")
    content: str


class DocumentExtractionPayload(SQLModel, table=True):
    __tablename__ = "document_extraction_payload"
    __table_args__ = {"schema": "sister_archive"}

    extraction_id: int = Field(primary_key=True, foreign_key="document_extraction.id")
    structured_data: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))


class ActivityLogPayload(SQLModel, table=True):
    __tablename__ = "activity_log_payload"
    __table_args__ = {"schema": "sister_archive"}

    activity_id: int = Field(primary_key=True, foreign_key="activity_log.id")
    params: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))
    result: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))


class CalculationSnapshotPayload(SQLModel, table=True):
    """Structured calculation result kept outside the typed working schema."""

    __tablename__ = "calculation_snapshot_payload"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["scope_type", "scope_id", "viewer_key", "calculation_type"],
            ["calculation_snapshot.scope_type", "calculation_snapshot.scope_id", "calculation_snapshot.viewer_key",
             "calculation_snapshot.calculation_type"],
            ondelete="CASCADE",
        ),
        {"schema": "sister_archive"},
    )

    scope_type: str = Field(primary_key=True)
    scope_id: str = Field(primary_key=True)
    viewer_key: str = Field(primary_key=True)
    calculation_type: str = Field(primary_key=True)
    result: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))


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
    "Zona": "census_zone",
    "Zona cens": "census_zone",
    "Zona censuaria": "census_zone",
    # Terreni-specific
    "Qualita": "quality",
    "Qualità": "quality",
    "Superficie": "area",
    "Reddito Dominicale": "dominical_income",
    "Reddito dominicale": "dominical_income",  # the portal's own spelling
    "Reddito Agrario": "agricultural_income",
    "Reddito agrario": "agricultural_income",
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

# HTML table headers that map to a cadastral unit (not to the property row)
PROPERTY_LOCATION_FIELD_MAP = {
    "Foglio": "sheet",
    "Particella": "parcel",
    "Sub": "subunit",
    "Subalterno": "subunit",
}

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
