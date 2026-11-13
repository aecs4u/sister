"""Tracked wallet gift coupons and their redemption lifecycle."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sister import database, purchases_billing, web


@pytest.mark.usefixtures("fresh_db")
async def test_gift_coupon_reservation_is_single_use_replay_safe_and_freezable(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "wallet-gift-test-secret")
    code = await purchases_billing.create_gift_coupon(
        Decimal("12.50"), 1, "admin@example.test", note="welcome gift"
    )
    digest = purchases_billing.gift_coupon_digest(code)

    first = await database.reserve_wallet_gift_coupon_redemption(digest, "sister:buyer@example.test")
    assert first["status"] == "pending"
    retry = await database.reserve_wallet_gift_coupon_redemption(digest, "sister:buyer@example.test")
    assert retry == first
    assert await database.complete_wallet_gift_coupon_redemption(first["redemption_id"], "wallet-tx-gift-1")
    assert await database.reserve_wallet_gift_coupon_redemption(digest, "sister:buyer@example.test") == {
        "status": "already_redeemed"
    }
    assert await database.reserve_wallet_gift_coupon_redemption(digest, "sister:another@example.test") == {
        "status": "exhausted"
    }

    (coupon,) = await database.list_wallet_gift_coupons()
    assert coupon["code_hint"] == code[-6:]
    assert coupon["amount"] == Decimal("12.50")
    assert coupon["redeemed_count"] == 1
    assert "code_hash" not in coupon
    (redemption,) = await database.list_wallet_gift_coupon_redemptions()
    assert redemption["wallet_tx_id"] == "wallet-tx-gift-1"
    assert redemption["status"] == "completed"

    assert await database.set_wallet_gift_coupon_status(coupon["id"], "frozen")
    assert (await database.reserve_wallet_gift_coupon_redemption(digest, "sister:third@example.test"))["status"] == "frozen"
    assert await database.set_wallet_gift_coupon_status(coupon["id"], "active")


@pytest.mark.usefixtures("fresh_db")
async def test_coupon_redemption_credits_wallet_and_admin_undo_posts_reversal(monkeypatch):
    from aecs4u_billing.wallet import WalletService, set_wallet_service
    from aecs4u_billing.wallet.storage import InMemoryWalletStorage

    monkeypatch.setenv("SECRET_KEY", "wallet-gift-test-secret")
    set_wallet_service(WalletService(storage=InMemoryWalletStorage()))
    try:
        code = await purchases_billing.create_gift_coupon(Decimal("5"), 1, "admin@example.test")
        assert await purchases_billing.redeem_gift_coupon(code, "email:buyer@example.test") == "redeemed"
        assert await purchases_billing.credit_balance("id:buyer@example.test") == Decimal("5")
        assert await purchases_billing.redeem_gift_coupon(code, "id:buyer@example.test") == "already_redeemed"
        (redemption,) = await database.list_wallet_gift_coupon_redemptions()
        assert await purchases_billing.reverse_gift_coupon_redemption(redemption["id"], "admin@example.test") == "reversed"
        assert await purchases_billing.credit_balance("email:buyer@example.test") == Decimal("0")
        (reversed_redemption,) = await database.list_wallet_gift_coupon_redemptions()
        assert reversed_redemption["status"] == "reversed"
        assert reversed_redemption["reversed_tx_id"]
    finally:
        set_wallet_service(None)


def test_admin_wallet_template_shows_balance_ledger_and_gift_management(main_module, monkeypatch):
    from fastapi.testclient import TestClient

    admin = SimpleNamespace(id="wallet-admin", email="admin@example.test", role="admin", is_superuser=False)
    monkeypatch.setitem(main_module.app.dependency_overrides, web._require_admin, lambda: admin)
    monkeypatch.setattr(database, "list_document_purchases", AsyncMock(return_value=[]))
    monkeypatch.setattr(database, "list_subject_purchases", AsyncMock(return_value=[]))
    monkeypatch.setattr(database, "list_subject_access", AsyncMock(return_value=[]))
    monkeypatch.setattr(database, "list_wallet_gift_coupons", AsyncMock(return_value=[]))
    monkeypatch.setattr(database, "list_wallet_gift_coupon_redemptions", AsyncMock(return_value=[]))
    monkeypatch.setattr(database, "count_wallet_gift_coupons", AsyncMock(return_value=0))
    monkeypatch.setattr(database, "count_wallet_gift_coupon_redemptions", AsyncMock(return_value=0))
    monkeypatch.setattr(purchases_billing, "credit_grants_enabled", lambda: True)
    monkeypatch.setattr(
        purchases_billing,
        "wallet_admin_snapshot",
        AsyncMock(return_value={"id": "wal_test", "balance": Decimal("9"), "currency": "credits", "locked": False, "active": True, "transactions": []}),
    )

    response = TestClient(main_module.app).get("/admin/wallets?q=email%3Abuyer%40example.test")

    assert response.status_code == 200
    assert "Portafogli e buoni regalo" in response.text
    assert "9,00 credits" in response.text
    assert "Crea un buono regalo" in response.text
    assert "/admin/wallet-gifts/create" in response.text

    # The one-time code is shown once and never copied into links or hidden fields; other filters are kept.
    follow = TestClient(main_module.app).get(
        "/admin/wallets?gift_code=ONE-TIME-SECRET&status=gift_created&error=bad_amount&pagopa_search=rossi&billing_page=2"
    )
    assert follow.status_code == 200
    assert follow.text.count("ONE-TIME-SECRET") == 1
    assert 'name="pagopa_search" value="rossi"' in follow.text
    assert 'name="status"' not in follow.text and 'name="error"' not in follow.text


@pytest.mark.usefixtures("fresh_db")
async def test_wallet_gift_coupon_migration_matches_model():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlmodel import SQLModel

    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261130_wallet_gift_coupons.py"
    spec = importlib.util.spec_from_file_location("wallet_gift_coupon_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def round_trip(connection):
        context = MigrationContext.configure(connection, opts={"sister_schema": database.DATABASE_SCHEMA})
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
        names = {"wallet_gift_coupon", "wallet_gift_coupon_redemption"}
        context = MigrationContext.configure(
            connection,
            opts={"include_object": lambda obj, name, type_, reflected, compare_to: name in names if type_ == "table" else True},
        )
        assert compare_metadata(context, SQLModel.metadata) == []

    async with database._get_engine().begin() as connection:
        await connection.run_sync(round_trip)
