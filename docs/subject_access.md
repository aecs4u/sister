# Access to archived person and company searches

Apply `alembic upgrade head` before running the app. Revision
`20261128_subject_access` adds `subject_access`, keyed by user identity, subject
type (`person` or `company`), and fiscal code. The `/persons` and `/companies`
tables, summaries, tags, search, paging, and CSV export are scoped to that
account's active grants. It allows the owner to open a saved subject search even
when no visura document exists.

`database.grant_subject_access(user_id, subject_type, subject_code, created_by,
note)` creates a grant or restores a revoked grant. Existing active grants keep
their original attribution and note. Use normalized account keys such as
`email:name@example.com`; `has_subject_access` matches the signed-in principal's
ID, username, and email case-insensitively. Future-dated and revoked grants do
not authorize access. Revocation retains the row and its attribution.

The grant covers the saved profile, its PDF report, and its parcel map. Map
requests verify the subject grant and derive the cache key from that subject's
authorized profile. An arbitrary cached map key cannot select another subject.
Documents use separate active `document_purchase` grants; owning a subject
record does not grant document access, and purchasing a document does not add a
person or company to the owner's table. Favorites do not grant access, and these
grants do not confer an administrator role.

Operational callers must record changes with `database.record_activity`, using
`admin.subject_access.create` for subject grants and the existing
`admin.document_purchase.create` action for related document grants. Include the
target account and subject code or document ID, together with the grant reason.
