"""v_subject_search_outcome: also count "too many results", pending requests and requests that never got a subject_query."""

from typing import Sequence, Union

from alembic import op

revision: str = "20261105_subject_search_outcome_v2"
down_revision: Union[str, None] = "20261104_request_type_catalog_refinements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None


def _schema() -> str:
    import os

    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def upgrade() -> None:
    schema = _schema()
    # ``request.subject_query`` is filled only from an answer that carries the subject ("soggetto"); a request that ended on SISTER's
    # "Superato il limite massimo di 400 elementi" (the placeholder VAT codes) or that is still queued has none.
    # The code that was asked stays in the job payload (``params.codice_fiscale``, ``fiscal_code`` for the persona fisica routes).
    # ``searched_types`` lists the catasto types searched (T, F, E): a "no match" for T alone says nothing about fabbricati.
    op.execute(
        f"""
        CREATE OR REPLACE VIEW {schema}.v_subject_search_outcome AS
        WITH asked AS (
            SELECT r.id, r.success, r.error, r.responded_at, r.cadastre_type,
                   upper(COALESCE(
                       NULLIF(r.subject_query, ''),
                       j.payload ->'params' ->> 'codice_fiscale',
                       j.payload ->> 'fiscal_code'
                   )) AS code
            FROM {schema}.request r
            LEFT JOIN (
                SELECT request_id,
                       CASE WHEN left(btrim(payload_json), 1) = '{{' THEN payload_json::jsonb END AS payload
                FROM {schema}.sister_request_jobs
            ) j ON j.request_id = r.request_ref
        )
        SELECT a.code AS fiscal_code,
               CASE
                   WHEN bool_or(a.success IS TRUE) THEN 'found'
                   WHEN bool_or(a.error ILIKE '%NESSUNA CORRISPONDENZA%non valido%') THEN 'invalid_code'
                   WHEN bool_or(a.error ILIKE '%NESSUNA CORRISPONDENZA%') THEN 'no_match'
                   WHEN bool_or(a.error ILIKE '%Superato il limite massimo%') THEN 'too_many'
                   WHEN bool_and(a.success IS NULL AND a.error IS NULL) THEN 'pending'
                   ELSE 'error'
               END AS outcome,
               count(*) AS attempts,
               max(a.responded_at) AS last_answer_at,
               max(s.id) AS subject_id,
               max(s.name) AS subject_name,
               string_agg(DISTINCT a.cadastre_type, ',' ORDER BY a.cadastre_type) AS searched_types
        FROM asked a
        LEFT JOIN {schema}.subject s ON s.fiscal_code = a.code
        WHERE a.code IS NOT NULL AND a.code <> ''
        GROUP BY a.code
        """
    )


def downgrade() -> None:
    schema = _schema()
    # back to the first definition (20261102): CREATE OR REPLACE cannot drop a column, so the view is dropped and rebuilt
    op.execute(f"DROP VIEW IF EXISTS {schema}.v_subject_search_outcome")
    op.execute(
        f"""
        CREATE VIEW {schema}.v_subject_search_outcome AS
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
