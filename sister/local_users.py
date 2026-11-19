"""Local sign-in accounts: the ``local_user`` table, with ``local_users.txt`` as an optional fallback.

``SISTER_LOCAL_USERS_SOURCE`` selects where accounts come from:

* ``auto`` (default): the table first; the file only for users the table does not know, or when the table is
  unreachable (e.g. the migration has not run yet). A user that exists in the table is never overridden by the file.
* ``db``: the table only.
* ``file``: the file only (the behaviour before the table existed).

Passwords are verified in constant time, and a unknown user costs the same hash computation as a known one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .passwords import hash_password, is_hashed_password, verify_local_password

logger = logging.getLogger("sister")

SOURCES = ("auto", "db", "file")


@dataclass(frozen=True)
class LocalAccount:
    id: str
    email: str
    username: str
    full_name: str | None = None
    role: str = "user"
    is_active: bool = True
    is_superuser: bool = False


def parse_users_file(path: Path) -> dict[str, tuple[str, str]]:
    """Read ``username password`` lines into ``{casefolded username: (username, stored password)}``."""
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
        username, password = fields
        key = username.casefold()
        if key in users:
            logger.warning("Ignoring duplicate local user entry on line %d", line_number)
            continue
        users[key] = (username, password)
    return users


def normalize_source(value: str | None) -> str:
    source = (value or "auto").strip().casefold()
    if source not in SOURCES:
        logger.warning("Unknown SISTER_LOCAL_USERS_SOURCE %r; using 'auto'", value)
        return "auto"
    return source


def _key(username: object) -> str:
    return str(username).strip().casefold()


class LocalUserDirectory:
    def __init__(
        self,
        file_users: dict[str, tuple[str, str]] | None = None,
        admin_usernames: set[str] | None = None,
        source: str = "auto",
        cache_seconds: float = 30.0,
    ) -> None:
        self.file_users = file_users or {}
        self.admin_usernames = {name.casefold() for name in (admin_usernames or set())}
        self.source = normalize_source(source)
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, LocalAccount | None]] = {}
        self._dummy_hash: str | None = None
        self._db_warned = False

    # -- lookup ------------------------------------------------------------------------------------------------

    async def _db_row(self, key: str):
        from sqlmodel import select

        from . import database
        from .db_models import LocalUser

        async with database._get_session_factory()() as session:
            return (await session.execute(select(LocalUser).where(LocalUser.username_key == key))).scalars().first()

    async def _find(self, key: str) -> tuple[str, str, str, bool] | None:
        """Return ``(username, stored hash/password, role, is_active)`` or None."""
        if self.source != "file":
            try:
                row = await self._db_row(key)
            except Exception as exc:  # table missing, DB down: never lock everybody out in auto mode
                if not self._db_warned:
                    self._db_warned = True
                    logger.warning("local_user table unreachable (%s); source=%s", type(exc).__name__, self.source)
                row = None
            else:
                self._db_warned = False
            if row is not None:
                return row.username, row.password_hash, row.role, bool(row.is_active)
            if self.source == "db":
                return None
        entry = self.file_users.get(key)
        if entry is None:
            return None
        return entry[0], entry[1], "user", True

    def _account(self, username: str, role: str, is_active: bool) -> LocalAccount:
        is_admin = role == "admin" or username.casefold() in self.admin_usernames
        return LocalAccount(
            id=username.casefold(),
            email=username if "@" in username else "",
            username=username,
            role="admin" if is_admin else "user",
            is_active=is_active,
            is_superuser=is_admin,
        )

    async def get(self, username: str) -> LocalAccount | None:
        key = _key(username)
        if not key:
            return None
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.cache_seconds:
            return cached[1]
        found = await self._find(key)
        account = self._account(found[0], found[2], found[3]) if found and found[3] else None
        self._cache[key] = (now, account)
        return account

    async def usernames(self) -> set[str]:
        names = {name for name, _ in self.file_users.values()} if self.source != "db" else set()
        if self.source != "file":
            try:
                from sqlmodel import select

                from . import database
                from .db_models import LocalUser

                async with database._get_session_factory()() as session:
                    rows = (await session.execute(select(LocalUser.username).where(LocalUser.is_active))).scalars()
                    names |= set(rows)
            except Exception as exc:
                logger.warning("local_user table unreachable (%s)", type(exc).__name__)
        return names

    # -- verification ------------------------------------------------------------------------------------------

    async def verify(self, username: str, password: str) -> LocalAccount | None:
        key = _key(username)
        found = await self._find(key) if key else None
        if found is None:
            # Same cost as a real check so timing does not reveal which usernames exist.
            if self._dummy_hash is None:
                self._dummy_hash = hash_password(os.urandom(8).hex())
            await asyncio.to_thread(verify_local_password, str(password), self._dummy_hash)
            return None
        name, stored, role, is_active = found
        ok = await asyncio.to_thread(verify_local_password, str(password), stored)
        if not (ok and is_active):
            return None
        self._cache.pop(key, None)
        if self.source != "file" and is_hashed_password(stored):
            await self._touch_last_login(key)
        return self._account(name, role, is_active)

    async def _touch_last_login(self, key: str) -> None:
        try:
            import sqlalchemy as sa

            from . import database
            from .db_models import LocalUser

            async with database._get_session_factory()() as session:
                await session.execute(
                    sa.update(LocalUser).where(LocalUser.username_key == key).values(last_login_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception as exc:
            logger.debug("last_login_at not updated (%s)", type(exc).__name__)
