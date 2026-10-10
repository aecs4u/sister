"""Add the cadastral link columns of the typed visura XML tables.

``sister/visura_xml_models.py`` defines these columns (a location, a subject or a right for each parsed row) but no
migration created them, so every typed insert from ``sister.xml_ingest`` failed on a migrated database and only the raw
``document_xml_nodes`` tree was kept. All columns are nullable; the migration is idempotent (a column that exists is
left alone).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_xml_typed_columns"
down_revision: Union[str, None] = "20261009_ocular_structured"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

# (table, column, referenced table)
COLUMNS = (
    ("document_subjects", "subject_id", "cadastral_subjects"),
    ("building_identifiers", "location_id", "cadastral_locations"),
    ("related_parcels", "location_id", "cadastral_locations"),
    ("land_parcels", "location_id", "cadastral_locations"),
    ("ownership_mutations", "reference_location_id", "cadastral_locations"),
    ("property_owners", "subject_id", "cadastral_subjects"),
    ("property_owners", "right_id", "ownership_rights"),
)


def _existing(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column, target in COLUMNS:
        if column in _existing(table):
            continue
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_{table}_{column}", table, target, [column], ["id"])
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table, column, _target in reversed(COLUMNS):
        if column not in _existing(table):
            continue
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
        op.drop_column(table, column)
