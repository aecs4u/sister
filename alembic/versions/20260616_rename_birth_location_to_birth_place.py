"""Rename cadastral_subjects.birth_location_id → birth_place_id (FK geographic_places)

The ORM model was updated to reference geographic_places instead of
cadastral_locations for the birth place FK, but no migration was created.
No data has been written to birth_location_id so the rename is lossless.

Revision ID: 20260616_rename_birth_place
Revises: 20260616_fix_prospect_cardinality
Create Date: 2026-06-16
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260616_rename_birth_place"
down_revision: Union[str, None] = "20260616_fix_prospect_cardinality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Recreate cadastral_subjects with birth_place_id in place of birth_location_id.
    # SQLite doesn't support ADD FOREIGN KEY; we recreate via raw DDL.
    # Preserve existing columns and data (birth_location_id has no data in practice).
    conn.execute(sa.text("""
        CREATE TABLE cadastral_subjects_new (
            id INTEGER NOT NULL PRIMARY KEY,
            fiscal_code VARCHAR,
            display_name VARCHAR,
            last_name VARCHAR,
            first_name VARCHAR,
            gender VARCHAR(1),
            date_of_birth VARCHAR,
            birth_place_id INTEGER REFERENCES geographic_places(id),
            birth_municipality_code VARCHAR,
            subject_type VARCHAR
        )
    """))
    conn.execute(sa.text("""
        INSERT INTO cadastral_subjects_new
            (id, fiscal_code, display_name, last_name, first_name, gender,
             date_of_birth, birth_municipality_code, subject_type)
        SELECT id, fiscal_code, display_name, last_name, first_name, gender,
               date_of_birth, birth_municipality_code, subject_type
        FROM cadastral_subjects
    """))
    conn.execute(sa.text("DROP TABLE cadastral_subjects"))
    conn.execute(sa.text("ALTER TABLE cadastral_subjects_new RENAME TO cadastral_subjects"))
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX uq_subject_fiscal_code ON cadastral_subjects (fiscal_code) WHERE fiscal_code IS NOT NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX ix_cadastral_subjects_birth_place_id ON cadastral_subjects (birth_place_id)"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        CREATE TABLE cadastral_subjects_old (
            id INTEGER NOT NULL PRIMARY KEY,
            fiscal_code VARCHAR,
            display_name VARCHAR,
            last_name VARCHAR,
            first_name VARCHAR,
            gender VARCHAR(1),
            date_of_birth VARCHAR,
            birth_location_id INTEGER REFERENCES cadastral_locations(id),
            birth_municipality_code VARCHAR,
            subject_type VARCHAR
        )
    """))
    conn.execute(sa.text("""
        INSERT INTO cadastral_subjects_old
            (id, fiscal_code, display_name, last_name, first_name, gender,
             date_of_birth, birth_municipality_code, subject_type)
        SELECT id, fiscal_code, display_name, last_name, first_name, gender,
               date_of_birth, birth_municipality_code, subject_type
        FROM cadastral_subjects
    """))
    conn.execute(sa.text("DROP TABLE cadastral_subjects"))
    conn.execute(sa.text("ALTER TABLE cadastral_subjects_old RENAME TO cadastral_subjects"))
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX uq_subject_fiscal_code ON cadastral_subjects (fiscal_code) WHERE fiscal_code IS NOT NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX ix_cadastral_subjects_birth_location_id ON cadastral_subjects (birth_location_id)"
    ))
