"""Copy the hashed accounts of ``local_users.txt`` into the ``local_user`` table. Dry run unless ``apply``."""

from __future__ import annotations

from pathlib import Path

from ..local_users import parse_users_file
from ..passwords import is_hashed_password


async def run(path: Path, admins: set[str], apply: bool) -> dict:
    """Return counts and usernames; passwords and hashes are never part of the result."""
    from sqlmodel import select

    from .. import database
    from ..db_models import LocalUser

    entries = parse_users_file(path)
    plan = {"create": [], "update": [], "skipped_plaintext": [], "unchanged": []}
    admin_keys = {name.casefold() for name in admins}
    async with database._get_session_factory()() as session:
        existing = {
            row.username_key: row for row in (await session.execute(select(LocalUser))).scalars()
        }
        for key, (username, stored) in entries.items():
            if not is_hashed_password(stored):
                plan["skipped_plaintext"].append(username)
                continue
            role = "admin" if key in admin_keys else "user"
            row = existing.get(key)
            if row is None:
                plan["create"].append(username)
                if apply:
                    session.add(LocalUser(username_key=key, username=username, password_hash=stored, role=role))
            elif row.password_hash != stored or row.role != role:
                plan["update"].append(username)
                if apply:
                    row.password_hash, row.role, row.username = stored, role, username
                    session.add(row)
            else:
                plan["unchanged"].append(username)
        if apply:
            await session.commit()
    return {"applied": apply, **plan}
