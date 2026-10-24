"""Tie the durable request queue to the request it carries.

``sister_request_jobs`` (one row per queued/running request) was the only request-side table without a foreign key:
``request_id`` holds ``request.request_ref`` and ``request_type`` a ``request_type.code``. The service always saves the
``request`` row (and its type) before it enqueues the job, so both links can be enforced. Deleting a request removes its
job. Jobs without a matching request (none on live) are deleted first, since the payload could not be tied to anything.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261030_request_jobs_fk"
down_revision: Union[str, None] = "20261029_sezioni_jobs_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on = None

TABLE = "sister_request_jobs"
REQUEST_FK = "sister_request_jobs_request_id_fkey"
TYPE_FK = "sister_request_jobs_request_type_fkey"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    existing = {fk["name"] for fk in inspector.get_foreign_keys(TABLE)}
    if REQUEST_FK not in existing:
        op.execute(f"DELETE FROM {TABLE} j WHERE NOT EXISTS (SELECT 1 FROM request r WHERE r.request_ref = j.request_id)")
        op.create_foreign_key(REQUEST_FK, TABLE, "request", ["request_id"], ["request_ref"], ondelete="CASCADE")
    if TYPE_FK not in existing:
        op.execute(
            f"INSERT INTO request_type (code) SELECT DISTINCT j.request_type FROM {TABLE} j "
            "WHERE NOT EXISTS (SELECT 1 FROM request_type t WHERE t.code = j.request_type)"
        )
        op.create_foreign_key(TYPE_FK, TABLE, "request_type", ["request_type"], ["code"])
    op.create_index("ix_sister_request_jobs_request_type", TABLE, ["request_type"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_sister_request_jobs_request_type", table_name=TABLE, if_exists=True)
    op.drop_constraint(TYPE_FK, TABLE, type_="foreignkey")
    op.drop_constraint(REQUEST_FK, TABLE, type_="foreignkey")
