"""Rename the cadastral subject birth-location FK to reference geographic places."""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260616_rename_birth_place"
down_revision: Union[str, None] = "20260616_fix_prospect_cardinality"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("cadastral_subjects")}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("cadastral_subjects")}


def _drop_birth_fk(column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys("cadastral_subjects"):
        if foreign_key.get("constrained_columns") == [column] and foreign_key.get("name"):
            op.drop_constraint(foreign_key["name"], "cadastral_subjects", type_="foreignkey")
            return


def _ensure_birth_fk(column: str, target: str, name: str) -> None:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys("cadastral_subjects")
    if any(
        fk.get("constrained_columns") == [column]
        and fk.get("referred_table") == target
        and fk.get("referred_columns") == ["id"]
        for fk in foreign_keys
    ):
        return
    op.create_foreign_key(name, "cadastral_subjects", target, [column], ["id"])


def upgrade() -> None:
    columns = _columns()
    if "birth_location_id" in columns and "birth_place_id" not in columns:
        _drop_birth_fk("birth_location_id")
        indexes = _indexes()
        if "ix_cadastral_subjects_birth_location_id" in indexes:
            op.drop_index("ix_cadastral_subjects_birth_location_id", table_name="cadastral_subjects")
        op.alter_column("cadastral_subjects", "birth_location_id", new_column_name="birth_place_id")
        op.create_index("ix_cadastral_subjects_birth_place_id", "cadastral_subjects", ["birth_place_id"])
    if "birth_place_id" in _columns():
        _ensure_birth_fk(
            "birth_place_id",
            "geographic_places",
            "fk_cadastral_subjects_birth_place_geographic_places",
        )


def downgrade() -> None:
    columns = _columns()
    if "birth_place_id" in columns and "birth_location_id" not in columns:
        _drop_birth_fk("birth_place_id")
        indexes = _indexes()
        if "ix_cadastral_subjects_birth_place_id" in indexes:
            op.drop_index("ix_cadastral_subjects_birth_place_id", table_name="cadastral_subjects")
        op.alter_column("cadastral_subjects", "birth_place_id", new_column_name="birth_location_id")
        op.create_index("ix_cadastral_subjects_birth_location_id", "cadastral_subjects", ["birth_location_id"])
        _ensure_birth_fk(
            "birth_location_id",
            "cadastral_locations",
            "fk_cadastral_subjects_birth_location_cadastral_locations",
        )
