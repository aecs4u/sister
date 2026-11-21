"""Local credential import and lookup through the canonical aecs4u-auth users table."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .passwords import hash_password, is_hashed_password, verify_local_password

logger = logging.getLogger("sister")


def parse_users_file(path: Path) -> dict[str, tuple[str, str]]:
    """Read legacy ``email password-hash`` entries without logging their contents."""
    users: dict[str, tuple[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.error("Could not read local users file %s (%s)", path, type(exc).__name__)
        return users
    for line_number, line in enumerate(lines, start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        fields = entry.split(maxsplit=1)
        if len(fields) != 2 or not fields[0] or not fields[1]:
            logger.warning("Ignoring malformed local user entry on line %d", line_number)
            continue
        email, password_hash = fields
        key = email.strip().casefold()
        if key in users:
            logger.warning("Ignoring duplicate local user entry on line %d", line_number)
            continue
        users[key] = (email.strip(), password_hash)
    return users


class LocalUserDirectory:
    """Resolve and verify local accounts using ``aecs4u_auth.users.models.User`` rows."""

    def __init__(self, admin_usernames: set[str] | None = None, cache_seconds: float = 30.0) -> None:
        self.admin_usernames = {name.strip().casefold() for name in (admin_usernames or set()) if name.strip()}
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, object | None]] = {}
        self._dummy_hash: str | None = None

    @staticmethod
    def _key(identifier: object) -> str:
        return str(identifier or "").strip().casefold()

    def _apply_admin_override(self, user):
        if user.email and user.email.casefold() in self.admin_usernames and not user.is_superuser:
            user.public_metadata = {**(user.public_metadata or {}), "is_superuser": True}
        return user

    async def _find(self, identifier: str, *, local_only: bool = False):
        from sqlalchemy import func, or_
        from sqlmodel import select

        from aecs4u_auth.users.models import User

        from . import database

        key = self._key(identifier)
        if not key:
            return None
        stmt = select(User).where(
            or_(
                User.id == identifier.strip(),
                User.clerk_id == identifier.strip(),
                func.lower(User.email) == key,
                func.lower(User.username) == key,
            ),
            User.is_active.is_(True),
            User.is_deleted.is_(False),
        )
        if local_only:
            stmt = stmt.where(User.hashed_password.is_not(None))
        async with database._get_session_factory()() as session:
            matches = (await session.execute(stmt.limit(2))).scalars().all()
        if len(matches) != 1:
            if len(matches) > 1:
                logger.error("User lookup matched multiple canonical accounts")
            return None
        return self._apply_admin_override(matches[0])

    async def get(self, identifier: str):
        key = self._key(identifier)
        if not key:
            return None
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self.cache_seconds:
            return cached[1]
        user = await self._find(identifier)
        self._cache[key] = (now, user)
        return user

    async def usernames(self) -> set[str]:
        from sqlmodel import select

        from aecs4u_auth.users.models import User

        from . import database

        async with database._get_session_factory()() as session:
            emails = (
                await session.execute(
                    select(User.email).where(
                        User.hashed_password.is_not(None),
                        User.is_active.is_(True),
                        User.is_deleted.is_(False),
                    )
                )
            ).scalars()
            return {str(email).strip().casefold() for email in emails if email}

    @staticmethod
    def _verify_stored_password(password: str, stored: str) -> bool:
        if is_hashed_password(stored):
            return verify_local_password(password, stored)
        try:
            from aecs4u_auth.password import verify_password

            return bool(verify_password(password, stored))
        except (ImportError, TypeError, ValueError):
            return False

    async def verify(self, identifier: str, password: str):
        key = self._key(identifier)
        user = await self._find(identifier, local_only=True) if key else None
        if user is None or not user.hashed_password:
            if self._dummy_hash is None:
                self._dummy_hash = hash_password("sister-invalid-user-check")
            await asyncio.to_thread(verify_local_password, str(password), self._dummy_hash)
            return None

        valid = await asyncio.to_thread(
            self._verify_stored_password, str(password), str(user.hashed_password)
        )
        if not valid:
            return None

        await self._touch_last_login(user.id)
        self._cache.pop(key, None)
        return self._apply_admin_override(user)

    @staticmethod
    async def _touch_last_login(user_id: str) -> None:
        try:
            import sqlalchemy as sa

            from aecs4u_auth.users.models import User

            from . import database

            now = datetime.now(timezone.utc)
            async with database._get_session_factory()() as session:
                await session.execute(
                    sa.update(User)
                    .where(User.id == user_id)
                    .values(last_sign_in_at=now, updated_at=now)
                )
                await session.commit()
        except Exception as exc:
            logger.debug("Canonical user last-sign-in timestamp not updated (%s)", type(exc).__name__)
