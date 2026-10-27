"""SQLModel classes for what a visura XML says: buildings, land parcels, groups and ownership acts.

All of these hang off ``Document`` (see ``db_models.py``). The flat/normalised layout replaced the old polymorphic tables:
a building unit is one row (classification, identifier and surface were three 1:1 tables, and ``is_current`` marks the
current state of a single-property document); an ownership mutation always has a ``document_id`` and optionally one
property group or one land parcel. ``DocumentXmlNode`` / ``DocumentXmlAttribute`` are the raw element tree and live in
the ``sister_archive`` schema.
"""

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlmodel import Field, SQLModel


class DocumentSubject(SQLModel, table=True):
    __tablename__ = "document_subject"

    document_id: int = Field(primary_key=True, foreign_key="document.id")
    subject_id: int = Field(primary_key=True, foreign_key="subject.id")


class DocumentAddress(SQLModel, table=True):
    __tablename__ = "document_address"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    address_text: str
    situation: Optional[str] = Field(default=None)


class PropertyGroup(SQLModel, table=True):
    __tablename__ = "property_group"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    group_index: Optional[int] = Field(default=None)
    cadastre_type: Optional[str] = Field(default=None)
    comune_id: Optional[int] = Field(default=None)
    description: Optional[str] = Field(default=None)


class BuildingUnit(SQLModel, table=True):
    __tablename__ = "building_unit"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    property_group_id: Optional[int] = Field(default=None, foreign_key="property_group.id")
    unit_index: Optional[int] = Field(default=None)
    is_current: bool
    address_text: Optional[str] = Field(default=None)
    cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    sequence_id: Optional[str] = Field(default=None)
    census_zone: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None, foreign_key="cadastral_category.code")
    cadastral_class: Optional[str] = Field(default=None)
    cadastral_income_eur: Optional[Decimal] = Field(default=None)
    consistency_value: Optional[Decimal] = Field(default=None)
    consistency_unit: Optional[str] = Field(default=None)
    consistency_sqm: Optional[Decimal] = Field(default=None)
    consistency_rooms: Optional[Decimal] = Field(default=None)
    total_area_sqm: Optional[Decimal] = Field(default=None)
    excluded_area_sqm: Optional[Decimal] = Field(default=None)
    planimetria_filed_on: Optional[date] = Field(default=None)
    planimetria_protocol: Optional[str] = Field(default=None)


class BuildingUnitRelatedParcel(SQLModel, table=True):
    __tablename__ = "building_unit_related_parcel"

    id: Optional[int] = Field(default=None, primary_key=True)
    building_unit_id: int = Field(foreign_key="building_unit.id")
    cadastral_unit_id: int = Field(foreign_key="cadastral_unit.id")
    sequence_id: Optional[str] = Field(default=None)


class LandParcel(SQLModel, table=True):
    __tablename__ = "land_parcel"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    cadastral_unit_id: int = Field(foreign_key="cadastral_unit.id")
    partita: Optional[str] = Field(default=None)
    sequence_id: Optional[str] = Field(default=None)


class LandParcelClassification(SQLModel, table=True):
    __tablename__ = "land_parcel_classification"

    id: Optional[int] = Field(default=None, primary_key=True)
    land_parcel_id: int = Field(foreign_key="land_parcel.id")
    land_quality: Optional[str] = Field(default=None, foreign_key="land_quality.code")
    cadastral_class: Optional[str] = Field(default=None)
    area_sqm: Optional[Decimal] = Field(default=None)
    deduction_symbol: Optional[str] = Field(default=None)
    dominical_income_eur: Optional[Decimal] = Field(default=None)
    agricultural_income_eur: Optional[Decimal] = Field(default=None)
    dominical_income_lire: Optional[Decimal] = Field(default=None)
    agricultural_income_lire: Optional[Decimal] = Field(default=None)


class OwnershipMutation(SQLModel, table=True):
    __tablename__ = "ownership_mutation"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    property_group_id: Optional[int] = Field(default=None, foreign_key="property_group.id")
    land_parcel_id: Optional[int] = Field(default=None, foreign_key="land_parcel.id")
    mutation_index: Optional[int] = Field(default=None)
    source_description: Optional[str] = Field(default=None)
    status: Optional[str] = Field(default=None)
    reference_cadastral_unit_id: Optional[int] = Field(default=None, foreign_key="cadastral_unit.id")
    is_current: bool = Field(default=False)
    mutation_date: Optional[date] = Field(default=None)


class MutationOwner(SQLModel, table=True):
    __tablename__ = "mutation_owner"

    id: Optional[int] = Field(default=None, primary_key=True)
    mutation_id: int = Field(foreign_key="ownership_mutation.id")
    position: Optional[int] = Field(default=None)
    subject_id: int = Field(foreign_key="subject.id")
    right_id: Optional[int] = Field(default=None, foreign_key="ownership_right.id")


class DocumentXmlNode(SQLModel, table=True):
    __tablename__ = "document_xml_node"
    __table_args__ = {"schema": "sister_archive"}

    id: int = Field(primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    parent_id: Optional[int] = Field(default=None, foreign_key="sister_archive.document_xml_node.id")
    ordinal: Optional[int] = Field(default=None)
    tag: Optional[str] = Field(default=None)
    text: Optional[str] = Field(default=None)


class DocumentXmlAttribute(SQLModel, table=True):
    __tablename__ = "document_xml_attribute"
    __table_args__ = {"schema": "sister_archive"}

    id: int = Field(primary_key=True)
    node_id: int = Field(foreign_key="sister_archive.document_xml_node.id")
    name: Optional[str] = Field(default=None)
    value: Optional[str] = Field(default=None)
