"""Import hashed legacy local accounts into the canonical aecs4u-auth ``users`` table."""

from __future__ import annotations

from pathlib import Path

from ..local_users import parse_users_file
from ..passwords import is_hashed_password


async def run(path: Path, admins: set[str], apply: bool) -> dict[str, int | bool]:
    """Preview or apply an idempotent import without returning emails or credential material."""
    from sqlalchemy import func
    from sqlmodel import select

    from aecs4u_auth.users.models import User, UserRole

    from .. import database

    entries = parse_users_file(path)
    admin_keys = {name.strip().casefold() for name in admins if name.strip()}
    result: dict[str, int | bool] = {
        "applied": apply,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_plaintext": 0,
        "invalid_email": 0,
        "conflicts": 0,
    }

    async with database._get_session_factory()() as session:
        for key, (email, stored_hash) in entries.items():
            if "@" not in email or email.startswith("@") or email.endswith("@"):
                result["invalid_email"] += 1
                continue
            if not is_hashed_password(stored_hash):
                result["skipped_plaintext"] += 1
                continue

            matches = (
                await session.execute(select(User).where(func.lower(User.email) == key).limit(2))
            ).scalars().all()
            if len(matches) > 1:
                result["conflicts"] += 1
                continue

            is_admin = key in admin_keys
            if not matches:
                result["created"] += 1
                if apply:
                    user = User.from_local_credentials(
                        email=email,
                        hashed_password=stored_hash,
                        is_superuser=is_admin,
                    )
                    session.add(user)
                    session.add(UserRole(user_id=user.id, role="admin" if is_admin else "user"))
                continue

            user = matches[0]
            changed = False
            if user.hashed_password is None:
                user.hashed_password = stored_hash
                user.password_enabled = True
                changed = True
            elif user.hashed_password != stored_hash:
                result["conflicts"] += 1
                continue
            metadata = dict(user.public_metadata or {})
            if is_admin and not metadata.get("is_superuser"):
                metadata["is_superuser"] = True
                user.public_metadata = metadata
                changed = True
            role = "admin" if (is_admin or metadata.get("is_superuser")) else "user"
            has_role = await session.get(UserRole, (user.id, role))
            if has_role is None:
                session.add(UserRole(user_id=user.id, role=role))
                changed = True
            if changed:
                result["updated"] += 1
            else:
                result["unchanged"] += 1

        if apply:
            await session.commit()

    return result
