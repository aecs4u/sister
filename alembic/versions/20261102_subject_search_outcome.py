"""View with the outcome of the searches of every codice fiscale / partita IVA (found, no match, invalid code, error)."""

from typing import Sequence, Union

from alembic import op

revision: str = "20261102_subject_search_outcome"
down_revision: Union[str, None] = "20261101_request_province"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    # "NESSUNA CORRISPONDENZA" is SISTER's final answer (nobody owns anything under that code), not a failure; a code with a wrong
    # check character gets the same answer with a note. A code with any successful search is "found", whatever came before.
    op.execute(
        f"""
        CREATE OR REPLACE VIEW {schema}.v_subject_search_outcome AS
        SELECT r.subject_query AS fiscal_code,
               CASE
                   WHEN bool_or(r.success IS TRUE) THEN 'found'
                   WHEN bool_or(r.error ILIKE '%NESSUNA CORRISPONDENZA%non valido%') THEN 'invalid_code'
                   WHEN bool_or(r.error ILIKE '%NESSUNA CORRISPONDENZA%') THEN 'no_match'
                   ELSE 'error'
               END AS outcome,
               count(*) AS attempts,
               max(r.responded_at) AS last_answer_at,
               max(s.id) AS subject_id,
               max(s.name) AS subject_name
        FROM {schema}.request r
        LEFT JOIN {schema}.subject s ON s.fiscal_code = r.subject_query
        WHERE r.subject_query IS NOT NULL AND r.subject_query <> ''
        GROUP BY r.subject_query
        """
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_schema()}.v_subject_search_outcome")
