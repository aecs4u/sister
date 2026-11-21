"""Move SISTER local accounts into the canonical aecs4u-auth users table."""

import os

import sqlalchemy as sa
from alembic import op

revision = "20261210_aecs4u_users"
down_revision = "20261209_local_users"
branch_labels = None
depends_on = None


def _schema() -> str:
    return op.get_context().opts.get("sister_schema") or os.getenv("DATABASE_SCHEMA", "sister")


def _canonical_table():
    from aecs4u_auth.users.models import User, UserRole

    metadata = sa.MetaData()
    table = User.__table__.to_metadata(metadata, schema=_schema())
    roles = UserRole.__table__.to_metadata(metadata, schema=_schema())
    return User, metadata, table, roles


def _new_user_values(User, user_table, email, password_hash, is_active, is_superuser):
    user = User.from_local_credentials(
        email=email,
        hashed_password=password_hash,
        is_active=is_active,
        is_superuser=is_superuser,
    )
    return {column.name: getattr(user, column.name) for column in user_table.columns}


def _ensure_role(bind, roles_table, user_id, role):
    exists = bind.execute(
        sa.select(roles_table.c.user_id).where(
            roles_table.c.user_id == user_id,
            roles_table.c.role == role,
        )
    ).first()
    if not exists:
        bind.execute(sa.insert(roles_table).values(user_id=user_id, role=role))


def _merge_legacy_user(
    bind,
    User,
    user_table,
    roles_table,
    email,
    password_hash,
    is_active,
    is_superuser,
    created_at,
    updated_at,
    last_login_at,
):
    email_key = email.strip().casefold()
    if "@" not in email_key or email_key.startswith("@") or email_key.endswith("@"):
        raise RuntimeError("Cannot migrate a local account whose username is not an email address")
    if not password_hash.startswith("pbkdf2_sha256$"):
        raise RuntimeError("Cannot migrate a local account with an unsupported password hash")

    existing = bind.execute(
        sa.select(user_table).where(sa.func.lower(user_table.c.email) == email_key)
    ).mappings().all()
    if len(existing) > 1:
        raise RuntimeError("Cannot migrate a local account with duplicate canonical email rows")

    if not existing:
        values = _new_user_values(User, user_table, email_key, password_hash, is_active, is_superuser)
        if created_at is not None:
            values["created_at"] = created_at
        if updated_at is not None:
            values["updated_at"] = updated_at
        values["last_sign_in_at"] = last_login_at
        bind.execute(sa.insert(user_table).values(**values))
        user_id = values["id"]
        _ensure_role(bind, roles_table, user_id, "admin" if is_superuser else "user")
        return

    current = existing[0]
    current_hash = current["hashed_password"]
    if current_hash not in (None, password_hash):
        raise RuntimeError("Cannot migrate a local account with a conflicting canonical password hash")
    metadata = dict(current["public_metadata"] or {})
    if is_superuser:
        metadata["is_superuser"] = True
    values = {
        "hashed_password": password_hash,
        "password_enabled": True,
        "is_active": bool(current["is_active"] and is_active),
        "public_metadata": metadata,
        "last_sign_in_at": last_login_at or current["last_sign_in_at"],
        "updated_at": updated_at or current["updated_at"],
    }
    bind.execute(sa.update(user_table).where(user_table.c.id == current["id"]).values(**values))
    _ensure_role(
        bind,
        roles_table,
        current["id"],
        "admin" if metadata.get("is_superuser") else "user",
    )


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema()
    User, metadata, user_table, roles_table = _canonical_table()
    metadata.create_all(bind, checkfirst=True)

    if sa.inspect(bind).has_table("local_user", schema=schema):
        legacy = sa.Table("local_user", sa.MetaData(), schema=schema, autoload_with=bind)
        rows = bind.execute(sa.select(legacy)).mappings().all()
        for row in rows:
            _merge_legacy_user(
                bind,
                User,
                user_table,
                roles_table,
                row["username"],
                row["password_hash"],
                bool(row["is_active"]),
                row["role"] == "admin",
                row["created_at"],
                row["updated_at"],
                row["last_login_at"],
            )
        op.drop_table("local_user", schema=schema)


def downgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("local_user", schema=schema):
        op.create_table(
            "local_user",
            sa.Column("username_key", sa.String(255), nullable=False),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("password_hash", sa.String(), nullable=False),
            sa.Column("role", sa.String(16), server_default="user", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("role IN ('admin', 'user')", name="ck_local_user_role"),
            sa.CheckConstraint("substr(password_hash, 1, 14) = 'pbkdf2_sha256$'", name="ck_local_user_hashed"),
            sa.PrimaryKeyConstraint("username_key", name="pk_local_user"),
            schema=schema,
        )

    from aecs4u_auth.users.models import User, UserRole

    metadata = sa.MetaData()
    User.__table__.to_metadata(metadata, schema=schema)
    UserRole.__table__.to_metadata(metadata, schema=schema)
    legacy = sa.Table("local_user", metadata, schema=schema, autoload_with=bind)
    if sa.inspect(bind).has_table("users", schema=schema):
        users = sa.Table("users", metadata, schema=schema, autoload_with=bind)
        local_rows = bind.execute(
            sa.select(users).where(users.c.clerk_id.like("local_%"), users.c.hashed_password.is_not(None))
        ).mappings()
        for user in local_rows:
            email = str(user["email"]).strip()
            username_key = email.casefold()
            exists = bind.execute(
                sa.select(legacy.c.username_key).where(legacy.c.username_key == username_key)
            ).first()
            if exists:
                continue
            metadata_value = user["public_metadata"] or {}
            bind.execute(
                sa.insert(legacy)
                .values(
                    username_key=email.casefold(),
                    username=email,
                    password_hash=user["hashed_password"],
                    role="admin" if metadata_value.get("is_superuser") else "user",
                    is_active=user["is_active"],
                    created_at=user["created_at"],
                    updated_at=user["updated_at"],
                    last_login_at=user["last_sign_in_at"],
                )
            )
