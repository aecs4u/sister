"""Read-only queries for territory, courts, and Conservatoria reference data."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_
from sqlmodel import select

from .database import _get_session_factory
from .db_models import (
    ConservatoriaMunicipality,
    ConservatoriaOffice,
    ConservatoriaSection,
    Court,
    CourtMunicipality,
    IstatMunicipality,
    TerritoryProvince,
)


async def list_provinces() -> list[TerritoryProvince]:
    async with _get_session_factory()() as session:
        result = await session.execute(select(TerritoryProvince).order_by(TerritoryProvince.name))
        return list(result.scalars().all())


async def get_province(code: str) -> TerritoryProvince | None:
    async with _get_session_factory()() as session:
        return (
            await session.execute(select(TerritoryProvince).where(TerritoryProvince.code == code.upper()))
        ).scalar_one_or_none()


async def list_municipalities(
    province_code: str,
    *,
    cadastre_type: str = "F",
    municipality: str | None = None,
) -> list[dict[str, Any]]:
    async with _get_session_factory()() as session:
        stmt = select(IstatMunicipality).where(
            func.upper(IstatMunicipality.province_abbreviation) == province_code.upper()
        )
        if municipality:
            municipality_key = municipality.strip().upper()
            stmt = stmt.where(
                (func.upper(IstatMunicipality.italian_name) == municipality_key)
                | (func.upper(IstatMunicipality.name) == municipality_key)
                | (func.upper(IstatMunicipality.cadastral_code) == municipality_key)
            )
        rows = (
            await session.execute(
                stmt.order_by(func.upper(IstatMunicipality.italian_name), IstatMunicipality.numeric_code)
            )
        ).scalars().all()
        if not rows:
            return []

        municipality_codes = [row.numeric_code for row in rows]
        section_rows = await session.execute(
            select(ConservatoriaSection.municipality_code, ConservatoriaSection.section_code)
            .where(
                ConservatoriaSection.municipality_code.in_(municipality_codes),
                ConservatoriaSection.cadastre_type == cadastre_type.upper(),
            )
            .distinct()
            .order_by(ConservatoriaSection.municipality_code, ConservatoriaSection.section_code)
        )
        sections_by_municipality: dict[int, list[str]] = {}
        for code, section in section_rows:
            sections_by_municipality.setdefault(code, []).append(section)

    return [
        {
            "comune": (row.italian_name or row.name).upper(),
            "sezioni": sections_by_municipality.get(row.numeric_code) or None,
            "codice_catastale": row.cadastral_code,
        }
        for row in rows
    ]


async def list_conservatorie() -> list[ConservatoriaOffice]:
    async with _get_session_factory()() as session:
        result = await session.execute(select(ConservatoriaOffice).order_by(ConservatoriaOffice.name, ConservatoriaOffice.id))
        return list(result.scalars().all())


async def get_conservatoria(identifier: str) -> ConservatoriaOffice | None:
    identifier = identifier.strip()
    predicates = [
        ConservatoriaOffice.source_id == identifier,
        ConservatoriaOffice.code == identifier,
        func.upper(ConservatoriaOffice.name) == identifier.upper(),
    ]
    if identifier.isdecimal():
        predicates.append(ConservatoriaOffice.id == int(identifier))
    async with _get_session_factory()() as session:
        # Use OR explicitly so a name, source identifier, local code, or row id can address the same record.
        result = await session.execute(
            select(ConservatoriaOffice).where(or_(*predicates)).order_by(ConservatoriaOffice.id).limit(1)
        )
        return result.scalar_one_or_none()


async def list_conservatoria_municipalities(
    office_id: int,
    *,
    municipality: str | None = None,
) -> list[dict[str, Any]]:
    async with _get_session_factory()() as session:
        stmt = (
            select(IstatMunicipality)
            .join(
                ConservatoriaMunicipality,
                ConservatoriaMunicipality.municipality_code == IstatMunicipality.numeric_code,
            )
            .where(ConservatoriaMunicipality.office_id == office_id)
        )
        if municipality:
            municipality_key = municipality.strip().upper()
            stmt = stmt.where(
                (func.upper(IstatMunicipality.italian_name) == municipality_key)
                | (func.upper(IstatMunicipality.name) == municipality_key)
                | (func.upper(IstatMunicipality.cadastral_code) == municipality_key)
            )
        municipalities = (
            await session.execute(
                stmt.order_by(func.upper(IstatMunicipality.italian_name), IstatMunicipality.numeric_code)
            )
        ).scalars().all()
        return [
            {
                "municipality_code": row.numeric_code,
                "comune": (row.italian_name or row.name).upper(),
                "codice_catastale": row.cadastral_code,
            }
            for row in municipalities
        ]


async def get_conservatoria_sections(
    office_id: int,
    municipality_code: int,
    cadastre_type: str,
) -> list[str]:
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(ConservatoriaSection.section_code)
            .where(
                ConservatoriaSection.office_id == office_id,
                ConservatoriaSection.municipality_code == municipality_code,
                ConservatoriaSection.cadastre_type == cadastre_type.upper(),
            )
            .order_by(ConservatoriaSection.section_code)
        )
        return list(result.scalars().all())


async def list_courts() -> list[Court]:
    async with _get_session_factory()() as session:
        result = await session.execute(select(Court).order_by(Court.name, Court.id))
        return list(result.scalars().all())


async def get_court(identifier: str) -> Court | None:
    identifier = identifier.strip()
    predicates = [Court.source_id == identifier, Court.code == identifier, func.upper(Court.name) == identifier.upper()]
    if identifier.isdecimal():
        predicates.append(Court.id == int(identifier))
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(Court).where(or_(*predicates)).order_by(Court.id).limit(1)
        )
        return result.scalar_one_or_none()


async def list_court_municipalities(court_id: int) -> list[dict[str, Any]]:
    async with _get_session_factory()() as session:
        result = await session.execute(
            select(IstatMunicipality)
            .join(CourtMunicipality, CourtMunicipality.municipality_code == IstatMunicipality.numeric_code)
            .where(CourtMunicipality.court_id == court_id)
            .order_by(func.upper(IstatMunicipality.italian_name), IstatMunicipality.numeric_code)
        )
        return [
            {
                "comune": (row.italian_name or row.name).upper(),
                "codice_catastale": row.cadastral_code,
                "codice_istat": row.numeric_code,
            }
            for row in result.scalars().all()
        ]
