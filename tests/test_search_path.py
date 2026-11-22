"""The engine puts the SISTER schema first on every transaction when the DSN carries no ``options``."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.usefixtures("fresh_db")


async def _search_path(database) -> str:
    async with database._get_session_factory()() as session:
        return (await session.execute(text("select current_setting('search_path')"))).scalar_one()


async def test_dsn_without_options_gets_the_schema_on_each_transaction(monkeypatch):
    import sister.database as database

    url = make_url(database.DATABASE_DSN)
    assert "options" in url.query  # the fixture DSN selects the throwaway schema this way
    monkeypatch.setattr(database, "DATABASE_DSN", url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False))
    monkeypatch.setattr(database, "_engine", None)
    try:
        first = await _search_path(database)
        second = await _search_path(database)  # a new transaction: SET LOCAL must be applied again
        assert first.split(",")[0].strip().strip('"') == database.DATABASE_SCHEMA
        assert second == first
    finally:
        if database._engine is not None:
            await database._engine.dispose()
            database._engine = None


async def test_dsn_with_options_is_left_alone():
    import sister.database as database

    assert "options" in make_url(database.DATABASE_DSN).query
    assert (await _search_path(database)).split(",")[0].strip().strip('"') == database.DATABASE_SCHEMA
