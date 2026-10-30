"""Document purchases: identity matching, validation, soft revocation, expiry and idempotent fulfilment."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import sister.web as web
from sister import database
from sister.database import PurchaseError
from sister.db_models import Document

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure helpers (no database)
# ---------------------------------------------------------------------------


def test_principal_keys_cover_every_key_form():
    user = SimpleNamespace(id="Demo@AECS4U.com", username="Demo@AECS4U.com", email="demo@aecs4u.com")
    assert web._principal_keys(user) == ["id:Demo@AECS4U.com", "username:demo@aecs4u.com", "email:demo@aecs4u.com"]
    assert web._principal_keys(SimpleNamespace()) == []


@pytest.mark.parametrize("raw", ["demo", "id:", "phone:123", "email:a b@c.it", "", "id:" + "x" * 400])
def test_purchase_identity_rejects_malformed_keys(raw):
    with pytest.raises(PurchaseError) as error:
        web._purchase_identity(raw)
    assert error.value.code in {"bad_key", "key_too_long"}


def test_purchase_identity_normalises_prefix_and_mail_case():
    assert web._purchase_identity(" EMAIL:Demo@AECS4U.com ") == "email:demo@aecs4u.com"
    assert web._purchase_identity("ID:user_2AbC") == "id:user_2AbC"  # ids keep their case (Clerk ids are case-sensitive)


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "", "99999999999999999999", "１２"])
def test_purchase_document_id_must_be_a_bigint_sized_positive_integer(raw):
    with pytest.raises(PurchaseError) as error:
        web._purchase_document_id(raw)
    assert error.value.code == "bad_document"
    assert web._purchase_document_id("1886") == 1886


def test_purchase_datetime_rejects_garbage_and_the_future():
    with pytest.raises(PurchaseError) as bad:
        web._purchase_datetime("banana")
    assert bad.value.code == "bad_date"
    with pytest.raises(PurchaseError) as future:
        web._purchase_datetime("2099-01-01T00:00")
    assert future.value.code == "future_date"
    assert web._purchase_datetime("2026-01-05T10:00").tzinfo is not None
    assert web._purchase_datetime("2099-06-01T12:00", allow_future=True).year == 2099


def test_every_error_code_has_an_italian_message():
    for code in ("bad_key", "bad_document", "document_not_found", "bad_date", "future_date", "duplicate", "not_found"):
        assert web.PURCHASE_MESSAGES[code]
    assert "Python" not in " ".join(web.PURCHASE_MESSAGES.values())


# ---------------------------------------------------------------------------
# Persistence (throwaway PostgreSQL schema)
# ---------------------------------------------------------------------------


@pytest.fixture
async def doc_id(fresh_db):
    await database.init_db()
    async with database._get_session_factory()() as session:
        row = Document(file_format="P7M", file_name="vi_sto_test.p7m")
        session.add(row)
        await session.commit()
        return row.id


async def test_grant_matches_any_key_form_case_insensitively(doc_id):
    await database.create_document_purchase("email:demo@aecs4u.com", doc_id, NOW - timedelta(minutes=1), "admin")
    assert await database.has_document_purchase(["id:other", "EMAIL:Demo@aecs4u.com"], doc_id)
    assert await database.get_document_purchase_ids(["email:demo@aecs4u.com"]) == {doc_id}
    assert not await database.has_document_purchase(["id:someone-else"], doc_id)
    assert not await database.has_document_purchase([], doc_id)


async def test_duplicate_grant_is_refused_and_a_revoked_one_is_reactivated(doc_id):
    key = "id:demo@aecs4u.com"
    await database.create_document_purchase(key, doc_id, NOW - timedelta(hours=1), "admin", "first")
    with pytest.raises(PurchaseError) as duplicate:
        await database.create_document_purchase(key, doc_id, NOW, "admin")
    assert duplicate.value.code == "duplicate"

    assert await database.revoke_document_purchase(key, doc_id, "admin", "rimborsato") is True
    assert await database.revoke_document_purchase(key, doc_id, "admin", "again") is False  # already revoked
    assert not await database.has_document_purchase([key], doc_id)
    history = await database.list_document_purchases(status="revoked")
    assert [(p["user_id"], p["state"], p["revoked_reason"]) for p in history] == [(key, "revoked", "rimborsato")]
    assert await database.list_document_purchases(status="active") == []

    await database.create_document_purchase(key, doc_id, NOW - timedelta(minutes=1), "admin", "again")
    assert await database.has_document_purchase([key], doc_id)
    (row,) = await database.list_document_purchases(status="all")
    assert row["state"] == "active" and row["revoked_at"] is None and row["revoked_reason"] == ""


async def test_expired_and_scheduled_purchases_give_no_access(doc_id):
    await database.create_document_purchase(
        "id:expired", doc_id, NOW - timedelta(days=2), "admin", expires_at=NOW - timedelta(days=1)
    )
    await database.create_document_purchase("id:scheduled", doc_id, NOW + timedelta(days=1), "admin")
    await database.create_document_purchase("id:valid", doc_id, NOW - timedelta(days=1), "admin", expires_at=NOW + timedelta(days=1))
    assert not await database.has_document_purchase(["id:expired"], doc_id)
    assert not await database.has_document_purchase(["id:scheduled"], doc_id)
    assert await database.has_document_purchase(["id:valid"], doc_id)
    states = {p["user_id"]: p["state"] for p in await database.list_document_purchases(status="all")}
    assert states == {"id:expired": "expired", "id:scheduled": "scheduled", "id:valid": "active"}


async def test_a_payment_reference_fulfils_only_once(doc_id):
    await database.create_document_purchase(
        "id:buyer", doc_id, NOW - timedelta(seconds=5), "stripe", source="stripe", amount_cents=1290,
        currency="eur", payment_ref="cs_test_123",
    )
    with pytest.raises(PurchaseError) as replay:
        await database.create_document_purchase(
            "id:other", doc_id, NOW, "stripe", source="stripe", payment_ref="cs_test_123"
        )
    assert replay.value.code == "duplicate_payment"
    (row,) = await database.list_document_purchases()
    assert (row["source"], row["amount_cents"], row["currency"], row["payment_ref"]) == ("stripe", 1290, "EUR", "cs_test_123")


async def test_unknown_document_and_update_conflicts_use_error_codes(doc_id):
    with pytest.raises(PurchaseError) as missing:
        await database.create_document_purchase("id:a", doc_id + 999, NOW, "admin")
    assert missing.value.code == "document_not_found"
    await database.create_document_purchase("id:a", doc_id, NOW - timedelta(minutes=1), "admin")
    await database.create_document_purchase("id:b", doc_id, NOW - timedelta(minutes=1), "admin")
    with pytest.raises(PurchaseError) as clash:
        await database.update_document_purchase("id:a", doc_id, "id:b", doc_id, None, "x")
    assert clash.value.code == "duplicate"
    with pytest.raises(PurchaseError) as vanished:
        await database.update_document_purchase("id:zzz", doc_id, "id:zzz", doc_id, None, "x")
    assert vanished.value.code == "not_found"


async def test_update_keeps_the_stored_instant_when_the_time_is_not_posted(doc_id):
    stored = (NOW - timedelta(hours=3)).replace(microsecond=123456)
    await database.create_document_purchase("id:a", doc_id, stored, "admin")
    await database.update_document_purchase("id:a", doc_id, "id:a", doc_id, None, "nota")
    (row,) = await database.list_document_purchases()
    assert row["purchased_at"] == stored and row["note"] == "nota"


async def test_the_same_person_under_another_key_form_is_a_duplicate(doc_id):
    await database.create_document_purchase("email:demo@aecs4u.com", doc_id, NOW - timedelta(minutes=1), "admin")
    for key in ("id:Demo@AECS4U.com", "username:demo@aecs4u.com"):
        with pytest.raises(PurchaseError) as duplicate:
            await database.create_document_purchase(key, doc_id, NOW, "admin")
        assert duplicate.value.code == "duplicate"
    await database.create_document_purchase("id:someone-else", doc_id, NOW - timedelta(minutes=1), "admin")
    with pytest.raises(PurchaseError):
        await database.update_document_purchase("id:someone-else", doc_id, "id:DEMO@aecs4u.com", doc_id, None, "x")
    # after a revoke the person can be granted again under any form
    assert await database.revoke_document_purchase("email:demo@aecs4u.com", doc_id, "admin", "")
    await database.create_document_purchase("id:demo@aecs4u.com", doc_id, NOW - timedelta(minutes=1), "admin")
    assert await database.has_document_purchase(["email:demo@aecs4u.com"], doc_id) is False  # that row is still revoked
    assert await database.has_document_purchase(["id:demo@aecs4u.com"], doc_id)


# ---------------------------------------------------------------------------
# Stripe fulfilment (aecs4u-billing); no Stripe calls: sessions are plain dicts
# ---------------------------------------------------------------------------


def _session(doc_id, **overrides):
    from sister import purchases_billing as pb

    base = {
        "id": "cs_test_abc", "payment_status": "paid", "amount_total": 1290, "currency": "eur",
        "metadata": pb.checkout_metadata("id:buyer@example.it", doc_id),
    }
    return {**base, **overrides}


async def test_a_paid_session_creates_the_access_once(doc_id):
    from sister import purchases_billing as pb

    assert await pb.fulfil_checkout_session(_session(doc_id)) == "created"
    assert await pb.fulfil_checkout_session(_session(doc_id)) == "replay"  # Stripe re-delivers: nothing changes
    (row,) = await database.list_document_purchases()
    assert (row["user_id"], row["source"], row["amount_cents"], row["currency"], row["payment_ref"]) == (
        "id:buyer@example.it", "stripe", 1290, "EUR", "cs_test_abc")
    assert await database.has_document_purchase(["id:buyer@example.it"], doc_id)


async def test_a_second_payment_for_an_owned_document_is_flagged_not_duplicated(doc_id):
    from sister import purchases_billing as pb

    await pb.fulfil_checkout_session(_session(doc_id))
    assert await pb.fulfil_checkout_session(_session(doc_id, id="cs_test_other")) == "already_owned"
    assert len(await database.list_document_purchases()) == 1


@pytest.mark.parametrize(
    "change",
    [{"payment_status": "unpaid"}, {"metadata": {"item_type": "subscription"}}, {"metadata": {"item_type": "document"}}, {"id": ""}],
)
async def test_sessions_that_are_not_a_paid_document_purchase_are_ignored(doc_id, change):
    from sister import purchases_billing as pb

    assert await pb.fulfil_checkout_session(_session(doc_id, **change)) == "ignored"
    assert await database.list_document_purchases() == []


def test_billing_stays_off_without_stripe_settings(monkeypatch):
    from sister import purchases_billing as pb

    for name in ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "SISTER_DOCUMENT_PRICE_ID"):
        monkeypatch.delenv(name, raising=False)
    assert pb.billing_configured() is False and pb.mount_billing(object()) is False
    monkeypatch.setenv("SISTER_DOCUMENT_PRICE_CENTS", "129000")
    assert pb.document_price_label() == "€ 1.290,00"


# ---------------------------------------------------------------------------
# Signed purchase links and prepaid credits (in-memory wallet, never a real Stripe/Redis)
# ---------------------------------------------------------------------------


def test_purchase_links_are_signed_and_expire(monkeypatch):
    from sister import purchases_billing as pb

    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    token = pb.purchase_link_token(1886)
    assert pb.document_id_from_token(token) == 1886
    assert pb.document_id_from_token(token[:-2] + "xx") is None  # tampered
    assert pb.document_id_from_token("garbage") is None
    monkeypatch.setattr(pb, "LINK_TTL_SECONDS", -1)
    assert pb.document_id_from_token(token) is None  # expired
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.delenv("SISTER_LINK_SECRET", raising=False)
    assert pb.document_id_from_token(token) is None  # no secret: nothing validates


def test_query_result_tokens_are_signed_and_bound_to_the_user(monkeypatch):
    from sister import purchases_billing as pb

    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    token = pb.query_result_token("visura_123", "id:buyer@example.it")
    assert pb.request_id_from_query_token(token, "id:buyer@example.it") == "visura_123"
    assert pb.request_id_from_query_token(token, "id:someone-else") is None
    assert pb.request_id_from_query_token(token[:-2] + "xx", "id:buyer@example.it") is None
    assert pb.request_id_from_query_token("not-a-token", "id:buyer@example.it") is None


def test_query_price_catalog_prefers_credit_products_and_converts_euro_prices():
    from sister import purchases_billing as pb

    prices = pb.parse_query_price_catalog(
        {
            "pricing": [
                {"endpoint": "POST /visura/soggetto", "customer_credits": 37, "amount": 0.25},
                {"endpoint": "/visura/query", "amount": 0.42},
                {"endpoint": "/visura/inactive", "amount": 9, "is_active": False},
                {"endpoint": "/visura/wrong-currency", "amount": 9, "currency": "USD"},
            ]
        }
    )
    assert prices == {"/visura/soggetto": Decimal("37"), "/visura/query": Decimal("42")}


def test_query_price_catalog_omits_conflicting_duplicate_products():
    from sister import purchases_billing as pb

    prices = pb.parse_query_price_catalog(
        {"pricing": [
            {"endpoint": "POST /visura/soggetto", "customer_credits": 30},
            {"endpoint": "/visura/soggetto", "customer_credits": 35},
        ]}
    )
    assert prices == {}


async def test_query_credit_debit_and_failure_refund(monkeypatch, wallet):
    pb = wallet
    assert await pb.grant_credits("id:buyer@example.it", Decimal("10"), "test", "admin") == Decimal("10")

    outcome, charge, before = await pb.charge_query_credits("id:buyer@example.it", 3, "action-1", Decimal("2.5"))
    assert outcome == "created" and charge is not None and before == Decimal("10")
    assert await pb.credit_balance("id:buyer@example.it") == Decimal("2.5")
    await pb.refund_query_credits(charge, 2, "failed-steps")
    assert await pb.credit_balance("id:buyer@example.it") == Decimal("7.5")

    outcome, charge, available = await pb.charge_query_credits("id:buyer@example.it", 4, "action-2", Decimal("2.5"))
    assert outcome == "insufficient" and charge is None and available == Decimal("7.5")


@pytest.fixture
def wallet(monkeypatch):
    from aecs4u_billing.wallet import WalletService, set_wallet_service
    from aecs4u_billing.wallet.storage import InMemoryWalletStorage

    from sister import purchases_billing as pb

    # explicit in-memory storage: WalletService() would pick the Redis of the developer's REDIS_URL and write test wallets there
    service = WalletService(storage=InMemoryWalletStorage())
    set_wallet_service(service)
    monkeypatch.setenv("SISTER_DOCUMENT_CREDITS", "10")
    monkeypatch.setenv("SISTER_WALLET_ALLOW_VOLATILE", "true")
    yield pb
    set_wallet_service(None)


async def test_credits_are_off_without_a_cost_or_a_durable_wallet(monkeypatch):
    from sister import purchases_billing as pb

    for name in ("SISTER_DOCUMENT_CREDITS", "REDIS_URL", "SISTER_WALLET_ALLOW_VOLATILE", "SISTER_WALLET_STORAGE"):
        monkeypatch.delenv(name, raising=False)
    assert pb.credits_enabled() is False and await pb.pay_with_credits("id:a", 1) == "disabled"  # no cost set
    monkeypatch.setenv("SISTER_DOCUMENT_CREDITS", "10")
    assert pb.wallet_backend() == "sql" and pb.credits_enabled() is True  # SISTER's own PostgreSQL is the default wallet
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert pb.wallet_backend() == "redis"
    monkeypatch.setenv("SISTER_WALLET_ALLOW_VOLATILE", "true")
    assert pb.wallet_backend() == "memory"
    monkeypatch.setenv("SISTER_WALLET_STORAGE", "sql")
    assert pb.wallet_backend() == "sql"  # an explicit choice wins
    monkeypatch.setattr(database, "DATABASE_DSN", "sqlite:///x.db")
    for name in ("SISTER_WALLET_STORAGE", "SISTER_WALLET_ALLOW_VOLATILE", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    assert pb.wallet_backend() == "none" and pb.credits_enabled() is False  # nowhere to keep balances


async def test_paying_with_credits_debits_once_and_grants_access(doc_id, wallet):
    pb = wallet
    assert await pb.pay_with_credits("id:buyer@example.it", doc_id) == "insufficient"
    assert await pb.grant_credits("email:Buyer@Example.it", Decimal("25"), "test", "admin") == Decimal("25")  # same person
    assert await pb.pay_with_credits("id:buyer@example.it", doc_id) == "created"
    assert await pb.credit_balance("id:buyer@example.it") == Decimal("15")
    assert await database.has_document_purchase(["id:buyer@example.it"], doc_id)
    assert await pb.pay_with_credits("username:buyer@example.it", doc_id) == "already_owned"  # no second charge
    assert await pb.credit_balance("id:buyer@example.it") == Decimal("15")
    (row,) = await database.list_document_purchases()
    assert row["source"] == "wallet" and row["payment_ref"].startswith("wallet:")


async def test_a_failed_entitlement_refunds_the_debit(doc_id, wallet, monkeypatch):
    pb = wallet
    await pb.grant_credits("id:buyer@example.it", Decimal("10"), "test", "admin")

    async def boom(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(database, "create_document_purchase", boom)
    with pytest.raises(RuntimeError):
        await pb.pay_with_credits("id:buyer@example.it", doc_id)
    assert await pb.credit_balance("id:buyer@example.it") == Decimal("10")  # refunded


async def test_the_sql_wallet_keeps_balances_in_postgres(doc_id, monkeypatch):
    """Default backend: balances are rows of ``billing_wallets`` / ``billing_transactions`` in SISTER's schema."""
    import sister.purchases_billing as pb
    from aecs4u_billing.wallet import set_wallet_service

    set_wallet_service(None)
    monkeypatch.setattr(pb, "_SQL_SESSION", None)
    monkeypatch.setenv("SISTER_DOCUMENT_CREDITS", "10")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("SISTER_WALLET_ALLOW_VOLATILE", raising=False)
    monkeypatch.delenv("SISTER_WALLET_STORAGE", raising=False)
    try:
        assert pb.wallet_backend() == "sql"
        assert await pb.grant_credits("email:Buyer@Example.it", Decimal("30"), "test", "admin") == Decimal("30")
        assert await pb.pay_with_credits("id:buyer@example.it", doc_id) == "created"
        # a second process (a restart) sees the same balance: it is in the database, not in memory
        set_wallet_service(None)
        assert await pb.credit_balance("username:buyer@example.it") == Decimal("20")
        from sqlalchemy import text

        with pb._sql_session().connection() as conn:
            rows = conn.execute(text("select count(*) from billing_transactions")).scalar_one()
        assert rows == 2  # the grant and the debit
    finally:
        pb._sql_session().close()
        set_wallet_service(None)
        monkeypatch.setattr(pb, "_SQL_SESSION", None)
