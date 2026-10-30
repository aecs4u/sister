"""Pay-per-document access through ``aecs4u-billing`` (Stripe Checkout) — dormant until Stripe is configured.

The entitlement stays the ``document_purchase`` table (``database.create_document_purchase``); this module only
*fulfils* it from a paid Checkout session and starts such a session:

* ``POST /documents/<id>/purchase`` (signed-in user) builds the Checkout session **on the server**: the price is the
  configured ``SISTER_DOCUMENT_PRICE_ID`` (never a client value), the buyer's account key and the document id go into the
  session metadata. The shared ``create_checkout`` of ``aecs4u_billing`` adds the tenant (``site_id``) and checks
  ``ALLOWED_STRIPE_PRICE_IDS``.
* the signed ``/webhooks/stripe`` route of ``aecs4u_billing`` (signature check, replay de-duplication, ``strict`` dispatch so
  a failed handler makes Stripe retry) calls ``fulfil_checkout_session`` for ``checkout.session.completed``. A
  ``payment_ref`` that was already used never grants twice.

Settings (all required, otherwise nothing is mounted): ``STRIPE_API_KEY``, ``STRIPE_WEBHOOK_SECRET``,
``SISTER_DOCUMENT_PRICE_ID`` (and ``ALLOWED_STRIPE_PRICE_IDS`` should list it). ``SISTER_DOCUMENT_PRICE_CENTS`` labels the
price shown to the buyer.

Prepaid credits (``aecs4u_billing.wallet``) are the second way to pay: ``SISTER_DOCUMENT_CREDITS`` is the cost of one
document in credits, an admin loads credits on an account (``grant_credits``) and the buyer pays with ``pay_with_credits``
(debit → entitlement; the debit is refunded when the entitlement cannot be written). The wallet is durable
(by default the package's SQL storage in SISTER's own PostgreSQL schema, tables ``billing_wallets`` / ``billing_transactions``;
``REDIS_URL`` selects Redis, ``SISTER_WALLET_STORAGE=memory`` an in-memory wallet for development and tests).

Buyers reach a document through a signed, expiring link an admin creates (``purchase_link_token``): the catalogue is never
listed to non-admins, so titles of other customers' documents stay private.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

ITEM_TYPE = "document"
KEY_FIELD = "sister_user_key"


def billing_configured() -> bool:
    """Whether Stripe payments for documents are switched on (every setting present)."""
    return all(os.getenv(name, "").strip() for name in ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "SISTER_DOCUMENT_PRICE_ID"))


def document_price_label() -> str | None:
    cents = os.getenv("SISTER_DOCUMENT_PRICE_CENTS", "").strip()
    if not cents.isdigit():
        return None
    return f"€ {int(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def checkout_metadata(user_key: str, document_id: int) -> dict[str, str]:
    """Session metadata that lets the webhook find the buyer and the document (never trusted from the client)."""
    return {"item_type": ITEM_TYPE, "reference_id": str(int(document_id)), KEY_FIELD: user_key}


async def fulfil_checkout_session(session: dict[str, Any]) -> str:
    """Turn a paid Checkout session into a document purchase; return what happened.

    ``created`` · ``replay`` (this payment was already recorded) · ``already_owned`` (the buyer had the document: logged
    for a manual refund) · ``ignored`` (not a document purchase, not paid or malformed). Raises only for conditions where
    a retry can help (database errors), so Stripe re-delivers.
    """
    from .database import PurchaseError, create_document_purchase

    metadata = session.get("metadata") or {}
    if metadata.get("item_type") != ITEM_TYPE:
        return "ignored"
    if session.get("payment_status") not in {"paid", "no_payment_required"}:
        return "ignored"
    user_key = str(metadata.get(KEY_FIELD) or "").strip()
    reference = str(metadata.get("reference_id") or "").strip()
    payment_ref = str(session.get("id") or "").strip()
    if not (user_key and reference.isdigit() and payment_ref):
        logger.warning("Document checkout %s lacks buyer or document: not fulfilled", payment_ref or "?")
        return "ignored"
    try:
        await create_document_purchase(
            user_key,
            int(reference),
            datetime.now(timezone.utc),
            "stripe",
            f"Pagamento {payment_ref}",
            source="stripe",
            amount_cents=session.get("amount_total"),
            currency=session.get("currency"),
            payment_ref=payment_ref,
        )
    except PurchaseError as exc:
        if exc.code == "duplicate_payment":
            return "replay"
        if exc.code == "duplicate":
            logger.warning("Document %s paid again by %s (%s): refund it", reference, user_key, payment_ref)
            return "already_owned"
        logger.error("Document checkout %s not fulfilled: %s", payment_ref, exc.code)
        return "ignored"
    return "created"


def register_stripe_handlers() -> None:
    """Register the fulfilment handler once (module import is idempotent)."""
    from aecs4u_billing.webhooks import on_stripe_event
    from aecs4u_billing.webhooks.registry import list_stripe_handlers

    if "fulfil_document_checkout" in list_stripe_handlers().get("checkout.session.completed", []):
        return

    @on_stripe_event("checkout.session.completed")
    async def fulfil_document_checkout(payload: dict[str, Any]) -> None:
        result = await fulfil_checkout_session((payload.get("data") or {}).get("object") or {})
        logger.info("checkout.session.completed → %s", result)


async def start_document_checkout(request: Any, user_key: str, email: str | None, document_id: int) -> str:
    """Create the Stripe Checkout session for one document and return its URL."""
    from aecs4u_billing.demo.routers.billing import CreateCheckoutRequest, create_checkout

    result = await create_checkout(
        request,
        CreateCheckoutRequest(
            price_id=os.environ["SISTER_DOCUMENT_PRICE_ID"].strip(),
            mode="payment",
            quantity=1,
            customer_email=email,
            metadata=checkout_metadata(user_key, document_id),
            idempotency_key=f"doc-{document_id}-{user_key}"[:255],
        ),
        None,
    )
    return result.url


def mount_billing(app: Any) -> bool:
    """Mount the signed Stripe webhook route and register fulfilment when payments are configured."""
    if not billing_configured():
        return False
    from aecs4u_billing import setup_billing

    setup_billing(
        app,
        include_billing_routes=False,
        include_webhook_routes=True,
        register_default_webhook_handlers=False,
        start_usage_tracking=False,
    )
    register_stripe_handlers()
    logger.info("Pagamenti documenti attivi (Stripe Checkout via aecs4u-billing)")
    return True


# ---------------------------------------------------------------------------
# Signed purchase links
# ---------------------------------------------------------------------------

LINK_TTL_SECONDS = int(os.getenv("SISTER_PURCHASE_LINK_DAYS", "30")) * 86400


def _serializer():
    from itsdangerous import URLSafeTimedSerializer

    secret = os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("SECRET_KEY (or SISTER_LINK_SECRET) is required to sign purchase links")
    return URLSafeTimedSerializer(secret, salt="sister-document-purchase-link")


def purchase_link_token(document_id: int) -> str:
    """A signed token for ``/buy/<token>``; it names one document and expires (default 30 days)."""
    return _serializer().dumps({"doc": int(document_id)})


def document_id_from_token(token: str) -> int | None:
    """The document a purchase link names, or None when the token is forged, malformed or expired."""
    from itsdangerous import BadSignature

    try:
        payload = _serializer().loads(token, max_age=LINK_TTL_SECONDS)
    except (BadSignature, RuntimeError):
        return None
    doc = payload.get("doc") if isinstance(payload, dict) else None
    return doc if isinstance(doc, int) and doc > 0 else None


def _query_token_serializer():
    from itsdangerous import URLSafeTimedSerializer

    secret = os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("SECRET_KEY (or SISTER_LINK_SECRET) is required to sign query-result links")
    return URLSafeTimedSerializer(secret, salt="sister-query-result-owner")


def query_result_token(request_id: str, user_key: str) -> str:
    """Sign a short-lived result-poll capability bound to one stable user identity."""
    import hashlib
    import hmac

    owner = hmac.new(
        (os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY") or "").encode(),
        user_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return _query_token_serializer().dumps({"rid": str(request_id), "owner": owner})


def request_id_from_query_token(token: str, user_key: str) -> str | None:
    """Return a request id only when its signed result token belongs to this user and has not expired."""
    import hashlib
    import hmac

    from itsdangerous import BadSignature

    secret = os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY") or ""
    if not secret:
        return None
    owner = hmac.new(secret.encode(), user_key.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        payload = _query_token_serializer().loads(token, max_age=30 * 86400)
    except (BadSignature, RuntimeError):
        return None
    if not isinstance(payload, dict) or not hmac.compare_digest(str(payload.get("owner") or ""), owner):
        return None
    request_id = payload.get("rid")
    return request_id if isinstance(request_id, str) and 0 < len(request_id) <= 512 else None


# ---------------------------------------------------------------------------
# Prepaid credits (aecs4u_billing.wallet)
# ---------------------------------------------------------------------------


def credit_cost() -> Decimal | None:
    raw = os.getenv("SISTER_DOCUMENT_CREDITS", "").strip()
    try:
        cost = Decimal(raw)
    except Exception:  # noqa: BLE001 - empty or malformed setting means "off"
        return None
    return cost if cost > 0 else None


def wallet_backend() -> str:
    """Where wallet balances live: ``sql`` (SISTER's own PostgreSQL, the default), ``redis`` (``REDIS_URL``), ``memory``
    (development only: lost on restart) or ``none``. ``SISTER_WALLET_STORAGE`` forces one; ``SISTER_WALLET_ALLOW_VOLATILE=true``
    is the older switch for ``memory``."""
    forced = os.getenv("SISTER_WALLET_STORAGE", "").strip().lower()
    if forced in {"sql", "redis", "memory"}:
        return forced
    if os.getenv("SISTER_WALLET_ALLOW_VOLATILE", "").strip().lower() in {"1", "true", "yes"}:
        return "memory"
    if os.getenv("REDIS_URL", "").strip():
        return "redis"
    from . import database

    return "sql" if str(getattr(database, "DATABASE_DSN", "") or "").startswith("postgresql") else "none"


def credits_enabled() -> bool:
    """Credits are on when a cost is set and there is somewhere durable (or an explicit dev) wallet to keep the balances."""
    return credit_cost() is not None and wallet_backend() != "none"


def normalize_query_price_path(value: Any) -> str:
    """Normalize an OpenData price endpoint, including values like ``POST /visura/soggetto``."""
    endpoint = str(value or "").strip().split("?", 1)[0]
    if " " in endpoint:
        endpoint = endpoint.rsplit(" ", 1)[-1]
    return "/" + endpoint.strip("/").lower() if endpoint.strip("/") else "/"


def parse_query_price_catalog(payload: Any) -> dict[str, Decimal]:
    """Read active SISTER query prices from OpenData's ``/api/pricing`` response.

    ``customer_credits`` is preferred when supplied. The current OpenData pricing API exposes ``amount`` in EUR, so
    that value is converted with the same integer truncation and 100 credits per euro used by OpenData's customer price editor.
    Ambiguous duplicate endpoints are omitted so a price is never chosen arbitrarily.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("pricing"), list):
        return {}

    prices: dict[str, Decimal] = {}
    ambiguous: set[str] = set()
    for item in payload["pricing"]:
        if not isinstance(item, dict):
            continue
        active = item.get("is_active", item.get("active", True))
        if isinstance(active, bool):
            is_active = active
        elif isinstance(active, (int, Decimal)):
            is_active = active == 1
        else:
            is_active = str(active).strip().lower() in {"1", "true", "yes", "on", "active"}
        if not is_active:
            continue
        raw_endpoint = str(item.get("endpoint") or "").strip()
        if " " in raw_endpoint and raw_endpoint.split(None, 1)[0].upper() != "POST":
            continue
        path = normalize_query_price_path(raw_endpoint)
        if path == "/":
            continue
        raw_credits = item.get("customer_credits")
        try:
            if raw_credits is not None:
                cost = Decimal(str(raw_credits))
            else:
                currency = str(item.get("currency") or payload.get("currency") or "EUR").upper()
                if currency != "EUR":
                    continue
                raw_eur = item.get("amount", item.get("customer_cost_eur"))
                cost = (Decimal(str(raw_eur)) * 100).to_integral_value(rounding=ROUND_DOWN)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not cost.is_finite() or cost <= 0:
            continue
        if path in prices and prices[path] != cost:
            ambiguous.add(path)
        else:
            prices[path] = cost
    for path in ambiguous:
        prices.pop(path, None)
    return prices


def query_credits_enabled(price_available: bool = True) -> bool:
    """Whether a supplied product price can be charged to the configured wallet."""
    return price_available and wallet_backend() != "none"


def credit_grants_enabled() -> bool:
    """Whether the admin may load credits on an available wallet backend."""
    return wallet_backend() != "none"


def wallet_user_id(user_key: str) -> str:
    """One wallet per person whatever key form was typed (``email:a@b`` and ``id:A@B`` share it)."""
    from .database import _identity_part

    return "sister:" + _identity_part(user_key)


_SQL_SESSION = None


def _sql_session():
    """One sync SQLModel session on SISTER's schema for the wallet storage (each storage call commits; none awaits)."""
    global _SQL_SESSION
    if _SQL_SESSION is None:
        from sqlalchemy.engine import make_url
        from sqlmodel import Session, create_engine

        from . import database

        url = make_url(database.DATABASE_DSN).set(drivername="postgresql+psycopg")
        query = dict(url.query)
        query.setdefault("options", f"-csearch_path={database.DATABASE_SCHEMA}")
        _SQL_SESSION = Session(create_engine(url.set(query=query), pool_pre_ping=True))
    return _SQL_SESSION


def _wallet_service():
    """The package's wallet service, built on the configured backend the first time it is needed.

    ``WalletService()`` alone would pick the package's default Redis URL and write balances into a Redis shared with other
    applications, so the backend is always chosen explicitly here (see ``wallet_backend``).
    """
    from aecs4u_billing.wallet import WalletService, get_wallet_service, set_wallet_service
    from aecs4u_billing.wallet import service as wallet_module

    if wallet_module._wallet_service is None:
        backend = wallet_backend()
        if backend == "sql":
            from aecs4u_billing.wallet.sqlmodel_storage import SQLModelWalletStorage

            set_wallet_service(WalletService(storage=SQLModelWalletStorage(_sql_session())))
        elif backend == "memory":
            from aecs4u_billing.wallet.storage import InMemoryWalletStorage

            set_wallet_service(WalletService(storage=InMemoryWalletStorage()))
        # redis: the package default (it connects to REDIS_URL)
    return get_wallet_service()


async def credit_balance(user_key: str) -> Decimal:
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    return (await service.get_balance(wallet.id, wallet.default_currency)).available


async def grant_credits(user_key: str, amount: Decimal, reason: str, actor: str) -> Decimal:
    """Load credits on an account (admin); returns the new balance. Idempotent per (actor, reason, minute) is not needed:
    each grant is a ledger line of its own."""
    from aecs4u_billing.wallet.models import TransactionType

    if amount <= 0:
        raise ValueError("amount must be positive")
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    await service.credit(
        wallet.id, amount, wallet.default_currency,
        description=f"Ricarica crediti ({reason or 'nessun motivo'}) da {actor}",
        reference_id=f"grant_{actor}", type=TransactionType.CREDIT,  # (ADJUSTMENT is not a credit-class type in the package: it would subtract)
    )
    return await credit_balance(user_key)


@dataclass
class QueryCreditCharge:
    """A wallet debit reserved for one portal form action."""

    service: Any
    wallet: Any
    transaction: Any
    unit_cost: Decimal
    units: int


async def charge_query_credits(
    user_key: str,
    units: int,
    reference: str,
    unit_cost: Decimal | None = None,
) -> tuple[str, QueryCreditCharge | None, Decimal]:
    """Debit a query action up front; callers refund rejected rows/steps using ``refund_query_credits``.

    Returns ``(created, charge, balance_before)`` or ``(insufficient, None, balance)``. Configuration errors raise so
    the route can fail closed without reporting a misleading insufficient-balance response.
    """
    from aecs4u_billing.wallet.models import TransactionType
    from aecs4u_billing.wallet.service import InsufficientBalanceError

    cost = unit_cost
    if units <= 0 or cost is None or not cost.is_finite() or cost <= 0 or wallet_backend() == "none":
        raise RuntimeError("Paid query credits are not configured")
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    balance = (await service.get_balance(wallet.id, wallet.default_currency)).available
    amount = cost * units
    if balance < amount:
        return "insufficient", None, balance
    try:
        transaction = await service.debit(
            wallet.id,
            amount,
            wallet.default_currency,
            description=f"Query portale ({units} unita)",
            reference_id=f"portal_query_{reference}",
            idempotency_key=f"portal-query-{wallet.id}-{reference}",
            type=TransactionType.USAGE,
        )
    except InsufficientBalanceError:
        # The available balance may have changed between the read and debit.
        return "insufficient", None, (await service.get_balance(wallet.id, wallet.default_currency)).available
    return "created", QueryCreditCharge(service, wallet, transaction, cost, units), balance


async def refund_query_credits(charge: QueryCreditCharge, units: int, reference: str = "rejected") -> None:
    """Refund failed rows/steps from a reservation, once per caller-supplied outcome reference."""
    from aecs4u_billing.wallet.models import TransactionType

    units = min(max(int(units), 0), charge.units)
    amount = charge.unit_cost * units
    if amount <= 0:
        return
    try:
        await charge.service.credit(
            charge.wallet.id,
            amount,
            charge.wallet.default_currency,
            description=f"Rimborso query ({units} unita)",
            reference_id=f"query_refund_{charge.transaction.id}_{reference}",
            idempotency_key=f"query-refund-{charge.transaction.id}-{reference}",
            type=TransactionType.REFUND,
        )
    except Exception as exc:  # noqa: BLE001 - record for operator follow-up without hiding the query failure
        logger.error("Refund of portal query transaction %s failed: %s", charge.transaction.id, type(exc).__name__)


async def pay_with_credits(user_key: str, document_id: int) -> str:
    """Buy a document with prepaid credits: ``created`` · ``already_owned`` · ``insufficient`` · ``disabled``."""
    from aecs4u_billing.wallet.models import TransactionType
    from aecs4u_billing.wallet.service import InsufficientBalanceError

    from .database import PurchaseError, create_document_purchase, has_document_purchase

    cost = credit_cost()
    if cost is None or not credits_enabled():
        return "disabled"
    if await has_document_purchase([user_key], document_id):
        return "already_owned"  # never charge for what is already owned
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    if (await service.get_balance(wallet.id, wallet.default_currency)).available < cost:
        return "insufficient"
    try:
        tx = await service.debit(
            wallet.id, cost, wallet.default_currency,
            description=f"Documento {document_id}",
            reference_id=f"{ITEM_TYPE}_{document_id}",
            idempotency_key=f"doc-{document_id}-{wallet.id}",  # a double click is one charge
            type=TransactionType.USAGE,
        )
    except InsufficientBalanceError:
        return "insufficient"
    try:
        await create_document_purchase(
            user_key, int(document_id), datetime.now(timezone.utc), "wallet", f"{cost} crediti",
            source="wallet", wallet_tx_id=str(tx.id), payment_ref=f"wallet:{tx.id}",
        )
    except PurchaseError as exc:
        if exc.code in {"duplicate", "duplicate_payment"}:
            return "already_owned"  # the same click twice: one debit (idempotency key), one entitlement
        await _refund(service, wallet, tx, cost)
        raise
    except Exception:
        await _refund(service, wallet, tx, cost)
        raise
    return "created"


async def _refund(service, wallet, tx, cost: Decimal) -> None:
    from aecs4u_billing.wallet.models import TransactionType

    try:
        await service.credit(
            wallet.id, cost, wallet.default_currency, description=f"Rimborso {tx.id}",
            reference_id=f"refund_{tx.id}", idempotency_key=f"refund-{tx.id}", type=TransactionType.REFUND,
        )
    except Exception as exc:  # noqa: BLE001 - leave a trace for a manual refund rather than hide the original error
        logger.error("Refund of wallet transaction %s failed: %s", tx.id, type(exc).__name__)
