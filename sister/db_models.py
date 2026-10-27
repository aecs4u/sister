"""SQLModel table definitions for the sister database (flat, normalised layout).

The tables are the ones created by ``alembic/sql/20261024_flat_normalised/02_schema.sql`` (design and mapping from the old
layout in ``docs/database_design.md``). Columns, primary keys and foreign keys are declared here for queries; the DDL
(checks, unique keys, indexes) is the SQL file, which the test schema is built from, so the two cannot drift.

* ``Request`` holds the request and its response in one row (the old ``visura_requests`` + ``visura_responses``).
* Dates are ``date``, amounts ``Decimal``, shares are numerator/denominator, places are ISTAT municipalities + ``CadastralUnit``.
* Raw payloads (response JSON, extraction JSON, document text) are in the ``sister_archive`` schema.

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


class CadastralUnit(SQLModel, table=True):
    __tablename__ = "cadastral_unit"

    id: Optional[int] = Field(default=None, primary_key=True)
    cadastre_type: Optional[str] = Field(default=None)
    comune_id: Optional[int] = Field(default=None)
    section: Optional[str] = Field(default=None)
    sheet: Optional[str] = Field(default=None)
    parcel: Optional[str] = Field(default=None)
    subunit: Optional[str] = Field(default=None)


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

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_index: int
    visura_present: Optional[bool] = Field(default=None)


class Property(SQLModel, table=True):
    __tablename__ = "property"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_id: Optional[int] = Field(default=None, foreign_key="request_result.id")
    property_type: Optional[str] = Field(default=None)
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
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

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="request.id")
    result_id: Optional[int] = Field(default=None, foreign_key="request_result.id")
    owner_index: Optional[int] = Field(default=None)
    subject_id: Optional[int] = Field(default=None, foreign_key="subject.id")
    right_id: Optional[int] = Field(default=None, foreign_key="ownership_right.id")


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
    __table_args__ = (sa.Index("ix_property_valuation_preference_asset", "asset_key"),)

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
    )

    user_id: str = Field(primary_key=True)
    asset_key: str = Field(primary_key=True)
    actual_size_sqm: Decimal = Field(sa_column=Column(sa.Numeric(12, 2), nullable=False))
    updated_at: datetime = Field(
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
    result: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
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
