"""Saved results must not be deleted by the cache TTL (the 6 h default used to purge the database every minute)."""

import pytest

from sister import database, services


async def _run_one_cleanup_cycle(monkeypatch, service):
    calls = []

    async def fake_cleanup(ttl_seconds):
        calls.append(ttl_seconds)
        return 0

    async def stop_after_first_sleep(_seconds):
        service.processing = False

    monkeypatch.setattr(services, "cleanup_old_responses", fake_cleanup)
    monkeypatch.setattr(database, "is_db_writable", lambda: True)
    monkeypatch.setattr(services.asyncio, "sleep", stop_after_first_sleep)
    service.processing = True
    await service._periodic_cleanup()
    return calls


@pytest.mark.asyncio
async def test_default_keeps_database_rows_forever(monkeypatch):
    monkeypatch.delenv("DB_RETENTION_SECONDS", raising=False)
    service = services.VisuraService()
    assert service.db_retention_seconds == 0
    assert service.response_ttl_seconds == 6 * 3600  # the cache TTL is untouched
    assert await _run_one_cleanup_cycle(monkeypatch, service) == []


@pytest.mark.asyncio
async def test_explicit_retention_deletes_with_that_value_not_the_cache_ttl(monkeypatch):
    monkeypatch.setenv("DB_RETENTION_SECONDS", "86400")
    service = services.VisuraService()
    assert service.db_retention_seconds == 86400
    assert await _run_one_cleanup_cycle(monkeypatch, service) == [86400]


@pytest.mark.parametrize("raw", ["abc", "-5", "", "0"])
def test_invalid_or_non_positive_retention_means_keep_forever(monkeypatch, raw):
    monkeypatch.setenv("DB_RETENTION_SECONDS", raw)
    assert services.VisuraService().db_retention_seconds == 0
