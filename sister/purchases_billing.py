"""Paid document and entity access through ``aecs4u-billing`` (Stripe Checkout) and prepaid wallet credits.

The entitlement stays the ``document_purchase`` table (``database.create_document_purchase``); this module only
*fulfils* it from a paid Checkout session and starts such a session:

* ``POST /documents/<id>/purchase`` (signed-in user) builds the Checkout session **on the server**: the price is the
  configured ``SISTER_DOCUMENT_PRICE_ID`` (never a client value), the buyer's account key and the document id go into the
  session metadata. The shared ``create_checkout`` of ``aecs4u_billing`` adds the tenant (``site_id``) and checks
  ``ALLOWED_STRIPE_PRICE_IDS``.
* the signed ``/webhooks/stripe`` route of ``aecs4u_billing`` (signature check, replay de-duplication, ``strict`` dispatch so
  a failed handler makes Stripe retry) calls ``fulfil_checkout_session`` for ``checkout.session.completed``. A
  ``payment_ref`` that was already used never grants twice.

Stripe requires ``STRIPE_API_KEY``, ``STRIPE_WEBHOOK_SECRET``, and a product price ID. Documents use
``SISTER_DOCUMENT_PRICE_ID``; persons and companies use their own ``SISTER_PERSON_PRICE_ID`` and
``SISTER_COMPANY_PRICE_ID``. Add every configured price to ``ALLOWED_STRIPE_PRICE_IDS``. ``*_PRICE_CENTS`` settings label
the price shown to the buyer.

Prepaid credits (``aecs4u_billing.wallet``) are the second way to pay: ``SISTER_DOCUMENT_CREDITS`` is the cost of one
document in credits, an admin loads credits on an account (``grant_credits``) and the buyer pays with ``pay_with_credits``
(debit → entitlement; the debit is refunded when the entitlement cannot be written). The wallet is durable
(by default the package's SQL storage in SISTER's own PostgreSQL schema, tables ``billing_wallets`` / ``billing_transactions``;
``REDIS_URL`` selects Redis, ``SISTER_WALLET_STORAGE=memory`` an in-memory wallet for development and tests).

Buyers reach a document through a signed, expiring link an admin creates (``purchase_link_token``): the catalogue is never
listed to non-admins, so titles of other customers' documents stay private.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
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


def subject_price_id(subject_type: str) -> str | None:
    """The server-configured Stripe price for one kind of entity."""
    if subject_type not in {"person", "company"}:
        return None
    value = os.getenv(f"SISTER_{subject_type.upper()}_PRICE_ID", "").strip()
    return value or None


def subject_billing_configured(subject_type: str) -> bool:
    return bool(subject_price_id(subject_type)) and all(
        os.getenv(name, "").strip() for name in ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET")
    )


def any_billing_configured() -> bool:
    """Whether at least one document or entity Stripe product is ready for fulfilment."""
    return billing_configured() or any(subject_billing_configured(kind) for kind in ("person", "company"))


def subject_price_label(subject_type: str) -> str | None:
    cents = os.getenv(f"SISTER_{subject_type.upper()}_PRICE_CENTS", "").strip()
    if not cents.isdigit():
        return None
    return f"€ {int(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def document_price_label() -> str | None:
    cents = os.getenv("SISTER_DOCUMENT_PRICE_CENTS", "").strip()
    if not cents.isdigit():
        return None
    return f"€ {int(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def subject_credit_cost(subject_type: str) -> Decimal | None:
    if subject_type not in {"person", "company"}:
        return None
    raw = os.getenv(f"SISTER_{subject_type.upper()}_CREDITS", "").strip()
    try:
        cost = Decimal(raw)
    except Exception:  # noqa: BLE001 - missing or malformed setting means disabled
        return None
    return cost if cost.is_finite() and cost > 0 else None


def subject_credits_enabled(subject_type: str) -> bool:
    return subject_credit_cost(subject_type) is not None and wallet_backend() != "none"


def checkout_metadata(user_key: str, document_id: int) -> dict[str, str]:
    """Session metadata that lets the webhook find the buyer and the document (never trusted from the client)."""
    return {"item_type": ITEM_TYPE, "reference_id": str(int(document_id)), KEY_FIELD: user_key}


def subject_checkout_metadata(
    user_key: str, subject_type: str, subject_code: str, user_keys: list[str] | None = None
) -> dict[str, str]:
    """Session metadata for one entity type and code, all selected by the server."""
    if subject_type not in {"person", "company"}:
        raise ValueError("Invalid subject purchase type")
    keys = sorted({str(key).strip() for key in [user_key, *(user_keys or [])] if str(key).strip()})
    return {
        "item_type": subject_type,
        "reference_id": str(subject_code).strip().upper(),
        KEY_FIELD: user_key,
        "sister_user_keys": ",".join(keys),
    }


async def fulfil_checkout_session(session: dict[str, Any]) -> str:
    """Turn a paid Checkout session into a document purchase; return what happened.

    ``created`` · ``replay`` (this payment was already recorded) · ``already_owned`` (the buyer had the document: logged
    for a manual refund) · ``ignored`` (not a document purchase, not paid or malformed). Raises only for conditions where
    a retry can help (database errors), so Stripe re-delivers.
    """
    from .database import PurchaseError, create_document_purchase, record_activity

    metadata = session.get("metadata") or {}
    if metadata.get("item_type") in {"person", "company"}:
        return await fulfil_subject_checkout_session(session)
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
            await record_activity(
                "admin.document_purchase.refund_required",
                actor="Stripe",
                source="stripe_webhook",
                status="review",
                target=f"document:{reference}",
                params={"document_id": int(reference), "payment_ref": payment_ref},
                result={
                    "refund_required": True,
                    "amount_cents": session.get("amount_total"),
                    "currency": str(session.get("currency") or "").upper(),
                },
            )
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


async def start_subject_checkout(
    request: Any,
    user_key: str,
    email: str | None,
    subject_type: str,
    subject_code: str,
    token: str,
    user_keys: list[str] | None = None,
) -> str:
    """Start a Stripe Checkout session using the configured price for this entity kind."""
    from aecs4u_billing.demo.routers.billing import CreateCheckoutRequest, create_checkout

    price_id = subject_price_id(subject_type)
    if not price_id or not subject_billing_configured(subject_type):
        raise RuntimeError("Entity checkout is not configured")
    base = str(request.base_url).rstrip("/")
    return_url = f"{base}/buy/entity/{token}"
    result = await create_checkout(
        request,
        CreateCheckoutRequest(
            price_id=price_id,
            mode="payment",
            quantity=1,
            customer_email=email,
            metadata=subject_checkout_metadata(user_key, subject_type, subject_code, user_keys),
            idempotency_key=f"subject-{subject_type}-{subject_code}-{wallet_user_id(user_key)}"[:255],
            success_url=f"{return_url}?status=paid",
            cancel_url=f"{return_url}?error=cancelled",
        ),
        None,
    )
    return result.url


async def fulfil_subject_checkout_session(session: dict[str, Any]) -> str:
    """Turn a completed Stripe person/company checkout into an audited entitlement."""
    from .database import complete_subject_purchase, record_activity

    metadata = session.get("metadata") or {}
    kind = metadata.get("item_type")
    user_key = str(metadata.get(KEY_FIELD) or "").strip()
    user_keys = [key.strip() for key in str(metadata.get("sister_user_keys") or "").split(",") if key.strip()]
    code = str(metadata.get("reference_id") or "").strip().upper()
    payment_ref = str(session.get("id") or "").strip()
    if kind not in {"person", "company"}:
        return "ignored"
    if session.get("payment_status") not in {"paid", "no_payment_required"}:
        return "ignored"
    if not (user_key and code and payment_ref):
        logger.warning("Subject checkout %s lacks buyer or profile: not fulfilled", payment_ref or "?")
        return "ignored"
    raw_amount = session.get("amount_total")
    amount = Decimal(int(raw_amount)) / Decimal(100) if raw_amount is not None else None
    outcome = await complete_subject_purchase(
        user_keys or [user_key],
        kind,
        code,
        source="stripe",
        payment_ref=payment_ref,
        created_by="stripe",
        amount=amount,
        currency=str(session.get("currency") or "").upper() or None,
        note=f"Pagamento Stripe {payment_ref}",
    )
    if outcome == "already_owned":
        await record_activity(
            "subject.purchase.refund_required",
            actor="Stripe",
            source="stripe_webhook",
            status="review",
            target=f"{kind}:{code}",
            params={"user_id": user_key, "subject_type": kind, "subject_code": code, "payment_ref": payment_ref},
            result={
                "refund_required": True,
                "amount_cents": raw_amount,
                "currency": str(session.get("currency") or "").upper(),
            },
        )
        return outcome
    if outcome == "created":
        await record_activity(
            "subject.purchase.completed",
            actor="Stripe",
            source="stripe_webhook",
            target=f"{kind}:{code}",
            params={"user_id": user_key, "subject_type": kind, "subject_code": code, "payment_ref": payment_ref},
            result={"amount_cents": raw_amount, "currency": str(session.get("currency") or "").upper()},
        )
    return outcome


def mount_billing(app: Any) -> bool:
    """Mount the signed Stripe webhook route and register fulfilment when payments are configured."""
    if not any_billing_configured():
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
    logger.info("Pagamenti documenti/profili attivi (Stripe Checkout via aecs4u-billing)")
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


def subject_purchase_link_token(subject_type: str, subject_code: str) -> str:
    """Sign an expiring purchase link for one archived person or company profile."""
    code = str(subject_code or "").strip().upper()
    pattern = r"\d{11}" if subject_type == "company" else r"[A-Z0-9]{16}"
    import re

    if subject_type not in {"person", "company"} or not re.fullmatch(pattern, code):
        raise ValueError("Invalid subject purchase link")
    return _serializer().dumps({"subject_type": subject_type, "subject_code": code})


def subject_from_purchase_token(token: str) -> tuple[str, str] | None:
    """Decode one signed entity purchase link or return None when invalid/expired."""
    import re

    from itsdangerous import BadSignature

    try:
        payload = _serializer().loads(token, max_age=LINK_TTL_SECONDS)
    except (BadSignature, RuntimeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("subject_type")
    code = str(payload.get("subject_code") or "").strip().upper()
    pattern = r"\d{11}" if kind == "company" else r"[A-Z0-9]{16}"
    return (kind, code) if kind in {"person", "company"} and re.fullmatch(pattern, code) else None


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


def wallet_lookup_key(wallet_user_id_value: str) -> str:
    """Return an accepted user key that resolves to a stored canonical wallet user ID."""
    value = str(wallet_user_id_value or "").strip()
    if value.startswith("sister:"):
        value = value.partition(":")[2]
    return "id:" + value


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


def _gift_code_secret() -> bytes:
    secret = os.getenv("SISTER_GIFT_COUPON_SECRET") or os.getenv("SISTER_LINK_SECRET") or os.getenv("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("SECRET_KEY (or SISTER_GIFT_COUPON_SECRET) is required for gift coupons")
    return secret.encode("utf-8")


def _normalize_gift_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isascii() and ch.isalnum())


def gift_coupon_digest(code: str) -> str:
    normalized = _normalize_gift_code(code)
    if len(normalized) < 12 or len(normalized) > 64:
        raise ValueError("invalid_gift_coupon")
    return hmac.new(_gift_code_secret(), normalized.encode("ascii"), hashlib.sha256).hexdigest()


async def create_gift_coupon(
    amount: Decimal,
    max_redemptions: int,
    created_by: str,
    *,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> str:
    """Create a one-time-display gift code and persist only its keyed digest."""
    from .database import create_wallet_gift_coupon

    coupon = f"SISTER-{secrets.token_hex(8).upper()}"
    await create_wallet_gift_coupon(
        gift_coupon_digest(coupon),
        coupon[-6:],
        amount,
        max_redemptions,
        created_by,
        expires_at=expires_at,
        note=note,
    )
    return coupon


async def redeem_gift_coupon(code: str, user_key: str) -> str:
    """Redeem a gift to wallet credits with a durable reservation and wallet idempotency key."""
    from aecs4u_billing.wallet.models import TransactionType
    from aecs4u_billing.wallet.service import WalletInactiveError, WalletLockedError

    from .database import (
        complete_wallet_gift_coupon_redemption,
        record_activity,
        reserve_wallet_gift_coupon_redemption,
    )

    try:
        digest = gift_coupon_digest(code)
    except (ValueError, RuntimeError):
        return "invalid"
    result = await reserve_wallet_gift_coupon_redemption(digest, wallet_user_id(user_key))
    status = result["status"]
    if status != "pending":
        return status
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    redemption_id = int(result["redemption_id"])
    try:
        tx = await service.credit(
            wallet.id,
            Decimal(str(result["amount"])),
            wallet.default_currency,
            description=f"Gift coupon {digest[:8]}",
            reference_id=f"gift_coupon_{redemption_id}",
            idempotency_key=f"gift-coupon-{redemption_id}",
            metadata={"gift_coupon_redemption_id": redemption_id},
            type=TransactionType.BONUS,
        )
    except (WalletInactiveError, WalletLockedError):
        return "wallet_locked"
    await complete_wallet_gift_coupon_redemption(redemption_id, str(tx.id))
    try:
        await record_activity(
            "wallet.gift_coupon.redeemed",
            actor=user_key,
            source="gift_coupon",
            target=f"wallet-redemption:{redemption_id}",
            params={"user_id": wallet_user_id(user_key), "amount": str(result["amount"])},
            result={"wallet_tx_id": str(tx.id)},
        )
    except Exception as exc:  # noqa: BLE001 - the durable coupon/redemption and wallet ledger are authoritative
        logger.warning("Gift coupon redemption activity was not recorded: %s", type(exc).__name__)
    return "redeemed"


async def reverse_gift_coupon_redemption(redemption_id: int, actor: str) -> str:
    """Reverse an unspent gift credit as a new wallet debit; preserve both ledger entries."""
    from aecs4u_billing.wallet.models import TransactionType
    from aecs4u_billing.wallet.service import InsufficientBalanceError, WalletInactiveError, WalletLockedError

    from .database import (
        get_wallet_gift_coupon_redemption,
        mark_wallet_gift_coupon_redemption_reversed,
        record_activity,
    )

    redemption = await get_wallet_gift_coupon_redemption(redemption_id)
    if redemption is None:
        return "not_found"
    if redemption["status"] == "reversed":
        return "already_reversed"
    if redemption["status"] != "completed":
        return "not_reversible"
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(redemption["user_id"])
    try:
        tx = await service.debit(
            wallet.id,
            Decimal(str(redemption["amount"])),
            wallet.default_currency,
            description=f"Undo gift coupon {redemption['code_hint']}",
            reference_id=f"gift_coupon_undo_{redemption_id}",
            idempotency_key=f"gift-coupon-undo-{redemption_id}",
            metadata={"gift_coupon_redemption_id": redemption_id, "actor": actor},
            type=TransactionType.DEBIT,
        )
    except InsufficientBalanceError:
        return "insufficient_balance"
    except (WalletInactiveError, WalletLockedError):
        return "wallet_locked"
    await mark_wallet_gift_coupon_redemption_reversed(redemption_id, str(tx.id))
    try:
        await record_activity(
            "wallet.gift_coupon.reversed",
            actor=actor,
            source="admin",
            target=f"wallet-redemption:{redemption_id}",
            params={"user_id": redemption["user_id"], "amount": str(redemption["amount"])},
            result={"wallet_tx_id": str(tx.id)},
        )
    except Exception as exc:  # noqa: BLE001 - preserve the completed reversal
        logger.warning("Gift coupon reversal activity was not recorded: %s", type(exc).__name__)
    return "reversed"


async def wallet_admin_snapshot(
    user_key: str,
    *,
    transaction_page: int = 1,
    page_size: int = 50,
) -> dict[str, Any] | None:
    """Read one existing wallet and a page of its ledger without creating a wallet on page load."""
    service = _wallet_service()
    wallet = await service.storage.get_wallet_by_user(wallet_user_id(user_key), service.config.site_id)
    if wallet is None:
        return None
    balance = await service.get_balance(wallet.id, wallet.default_currency)
    page_size = max(1, min(int(page_size), 100))
    transaction_page = max(1, int(transaction_page))
    transactions = await service.get_transactions(
        wallet.id,
        limit=page_size + 1,
        offset=(transaction_page - 1) * page_size,
    )
    has_more = len(transactions) > page_size
    transactions = transactions[:page_size]
    return {
        "id": wallet.id,
        "user_id": wallet.user_id,
        "currency": wallet.default_currency,
        "balance": balance.available,
        "locked": wallet.is_locked,
        "active": wallet.is_active,
        "transaction_page": transaction_page,
        "transaction_page_size": page_size,
        "transaction_has_more": has_more,
        "transactions": [
            {
                "id": tx.id,
                "type": tx.type.value,
                "status": tx.status.value,
                "amount": tx.amount,
                "balance_after": tx.balance_after,
                "description": tx.description,
                "reference_id": tx.reference_id or "",
                "created_at": tx.created_at,
            }
            for tx in transactions
        ],
    }


def wallet_admin_accounts(
    query: str = "",
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List every SQL wallet for this SISTER tenant, including its current balance and ledger activity count."""
    from sqlalchemy import func, select

    from aecs4u_billing.wallet.sqlmodel_storage import TransactionDB, WalletDB

    service = _wallet_service()
    session = _sql_session()
    page_size = max(1, min(int(page_size), 100))
    query = str(query or "").strip()[:320]
    if ":" in query:
        prefix, _, identifier = query.partition(":")
        if prefix.casefold() in {"id", "email", "username"}:
            query = identifier.strip()
    filters = [WalletDB.site_id == service.config.site_id]
    if query:
        filters.append(WalletDB.user_id.ilike(f"%{query}%"))

    total = int(
        session.exec(select(func.count()).select_from(WalletDB).where(*filters)).one()[0]
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), total_pages)
    grouped_columns = (
        WalletDB.id,
        WalletDB.user_id,
        WalletDB.default_currency,
        WalletDB.balance_available,
        WalletDB.is_active,
        WalletDB.is_locked,
        WalletDB.updated_at,
    )
    rows = session.exec(
        select(
            *grouped_columns,
            func.count(TransactionDB.id),
            func.max(TransactionDB.created_at),
        )
        .select_from(WalletDB)
        .outerjoin(TransactionDB, TransactionDB.wallet_id == WalletDB.id)
        .where(*filters)
        .group_by(*grouped_columns)
        .order_by(func.lower(WalletDB.user_id), WalletDB.user_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "rows": [
            {
                "id": row[0],
                "user_id": row[1],
                "lookup_key": wallet_lookup_key(row[1]),
                "currency": row[2],
                "balance": row[3],
                "active": row[4],
                "locked": row[5],
                "updated_at": row[6],
                "transaction_count": int(row[7] or 0),
                "last_transaction_at": row[8],
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def wallet_admin_transactions(
    query: str = "",
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Search and paginate billing transactions across every wallet in this SISTER tenant."""
    from sqlalchemy import func, or_, select

    from aecs4u_billing.wallet.sqlmodel_storage import TransactionDB, WalletDB

    service = _wallet_service()
    session = _sql_session()
    page_size = max(1, min(int(page_size), 100))
    query = str(query or "").strip()[:320]
    filters = [WalletDB.site_id == service.config.site_id]
    if query:
        pattern = f"%{query}%"
        filters.append(
            or_(
                WalletDB.user_id.ilike(pattern),
                TransactionDB.type.ilike(pattern),
                TransactionDB.status.ilike(pattern),
                TransactionDB.reference_id.ilike(pattern),
                TransactionDB.description.ilike(pattern),
            )
        )
    join = TransactionDB.wallet_id == WalletDB.id
    total = int(
        session.exec(
            select(func.count(TransactionDB.id))
            .select_from(TransactionDB)
            .join(WalletDB, join)
            .where(*filters)
        ).one()[0]
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), total_pages)
    rows = session.exec(
        select(TransactionDB, WalletDB.user_id)
        .join(WalletDB, join)
        .where(*filters)
        .order_by(TransactionDB.created_at.desc(), TransactionDB.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "rows": [
            {
                "id": tx.id,
                "user_id": user_id,
                "lookup_key": wallet_lookup_key(user_id),
                "type": tx.type,
                "status": tx.status,
                "currency": tx.currency_code,
                "amount": tx.amount,
                "balance_before": tx.balance_before,
                "balance_after": tx.balance_after,
                "reference_id": tx.reference_id or "",
                "description": tx.description,
                "created_at": tx.created_at,
            }
            for tx, user_id in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def billing_admin_invoices(
    query: str = "",
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List invoices stored by aecs4u-billing for this SISTER tenant."""
    from sqlalchemy import String, func, or_, select

    from aecs4u_billing.invoicing.sqlmodel_storage import InvoiceDB

    service = _wallet_service()
    session = _sql_session()
    page_size = max(1, min(int(page_size), 100))
    query = str(query or "").strip()[:320]
    filters = [
        InvoiceDB.invoice_metadata["site_id"].as_string() == service.config.site_id
    ]
    if query:
        pattern = f"%{query}%"
        filters.append(
            or_(
                InvoiceDB.id.ilike(pattern),
                InvoiceDB.number.ilike(pattern),
                InvoiceDB.customer_id.ilike(pattern),
                InvoiceDB.status.ilike(pattern),
                InvoiceDB.invoice_metadata.cast(String).ilike(pattern),
            )
        )

    total = int(
        session.exec(
            select(func.count()).select_from(InvoiceDB).where(*filters)
        ).one()[0]
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), total_pages)
    invoices = session.exec(
        select(InvoiceDB)
        .where(*filters)
        .order_by(InvoiceDB.created_at.desc(), InvoiceDB.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    rows = []
    for invoice in invoices:
        metadata = invoice.invoice_metadata or {}
        user_key = str(
            metadata.get("sister_user_key") or metadata.get("user_id") or ""
        ).strip()
        rows.append(
            {
                "id": invoice.id,
                "number": invoice.number or invoice.id,
                "customer_id": invoice.customer_id,
                "user_id": user_key or invoice.customer_id,
                "lookup_key": wallet_lookup_key(user_key) if user_key else "",
                "status": invoice.status,
                "currency": invoice.currency,
                "total": Decimal(invoice.total or 0) / Decimal(100),
                "amount_due": Decimal(invoice.amount_due or 0) / Decimal(100),
                "amount_paid": Decimal(invoice.amount_paid or 0) / Decimal(100),
                "created_at": invoice.created_at,
                "hosted_invoice_url": invoice.hosted_invoice_url,
                "invoice_pdf": invoice.invoice_pdf,
            }
        )
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def pagopa_admin_history(
    query: str = "",
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Search and paginate the imported pagoPA history without touching wallet records."""
    from sqlalchemy import func, or_, select

    from .db_models import PagoPAPaymentHistory

    session = _sql_session()
    page_size = max(1, min(int(page_size), 100))
    query = str(query or "").strip()[:320]
    filters = []
    if query:
        pattern = f"%{query}%"
        filters.append(
            or_(
                PagoPAPaymentHistory.user_id.ilike(pattern),
                PagoPAPaymentHistory.payer_reference.ilike(pattern),
                PagoPAPaymentHistory.payment_method.ilike(pattern),
                PagoPAPaymentHistory.payment_status.ilike(pattern),
                PagoPAPaymentHistory.office.ilike(pattern),
                PagoPAPaymentHistory.note.ilike(pattern),
                PagoPAPaymentHistory.source_file.ilike(pattern),
            )
        )

    total = int(
        session.exec(
            select(func.count(PagoPAPaymentHistory.id)).where(*filters)
        ).one()[0]
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), total_pages)
    payments = session.execute(
        select(PagoPAPaymentHistory)
        .where(*filters)
        .order_by(PagoPAPaymentHistory.payment_date.desc(), PagoPAPaymentHistory.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return {
        "rows": [
            {
                "id": payment.id,
                "user_id": payment.user_id,
                "lookup_key": wallet_lookup_key(payment.user_id),
                "payer_reference": payment.payer_reference or "",
                "payment_method": payment.payment_method,
                "amount": payment.amount,
                "currency": payment.currency,
                "payment_status": payment.payment_status,
                "payment_date": payment.payment_date,
                "payment_date_source": payment.payment_date_source or "",
                "operation_date_source": payment.operation_date_source or "",
                "validation_date_source": payment.validation_date_source or "",
                "rejection_date_source": payment.rejection_date_source or "",
                "office": payment.office or "",
                "note": payment.note or "",
                "retry_available": payment.retry_available,
                "receipt_available": payment.receipt_available,
                "source_file": payment.source_file,
            }
            for payment in payments
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


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


async def pay_subject_with_credits(
    user_key: str, subject_type: str, subject_code: str, *, user_keys: list[str] | None = None
) -> str:
    """Buy one person/company profile with credits; each entity kind has its own configured cost."""
    from aecs4u_billing.wallet.models import TransactionType
    from aecs4u_billing.wallet.service import InsufficientBalanceError

    from .database import complete_subject_purchase, has_subject_access, record_activity

    cost = subject_credit_cost(subject_type)
    code = str(subject_code or "").strip().upper()
    if cost is None or not subject_credits_enabled(subject_type):
        return "disabled"
    buyer_keys = user_keys or [user_key]
    if await has_subject_access(buyer_keys, subject_type, code):
        return "already_owned"
    service = _wallet_service()
    wallet = await service.get_or_create_wallet(wallet_user_id(user_key))
    if (await service.get_balance(wallet.id, wallet.default_currency)).available < cost:
        return "insufficient"
    try:
        tx = await service.debit(
            wallet.id,
            cost,
            wallet.default_currency,
            description=f"Profilo {subject_type} {code}",
            reference_id=f"subject_{subject_type}_{code}",
            idempotency_key=f"subject-{subject_type}-{code}-{wallet.id}",
            type=TransactionType.USAGE,
        )
    except InsufficientBalanceError:
        return "insufficient"
    try:
        outcome = await complete_subject_purchase(
            buyer_keys,
            subject_type,
            code,
            source="wallet",
            payment_ref=f"wallet:{tx.id}",
            created_by="wallet",
            amount=cost,
            currency="CREDITS",
            wallet_tx_id=str(tx.id),
            note=f"Pagamento con {cost} crediti",
        )
    except Exception:
        await _refund(service, wallet, tx, cost)
        raise
    if outcome == "created":
        await record_activity(
            "subject.purchase.completed",
            actor="wallet",
            source="wallet",
            target=f"{subject_type}:{code}",
            params={"user_id": user_key, "subject_type": subject_type, "subject_code": code, "payment_ref": f"wallet:{tx.id}"},
            result={"amount": str(cost), "currency": "CREDITS"},
        )
        return "created"
    if outcome == "already_owned":
        refunded = await _refund(service, wallet, tx, cost)
        if refunded:
            from .database import mark_subject_purchase_refunded

            await mark_subject_purchase_refunded(f"wallet:{tx.id}")
        await record_activity(
            "subject.purchase.refunded" if refunded else "subject.purchase.refund_required",
            actor="wallet",
            source="wallet",
            status="success" if refunded else "review",
            target=f"{subject_type}:{code}",
            params={"user_id": user_key, "subject_type": subject_type, "subject_code": code, "payment_ref": f"wallet:{tx.id}"},
            result={"refunded": refunded, "amount": str(cost), "currency": "CREDITS"},
        )
        return "already_owned"
    return "already_owned" if await has_subject_access(buyer_keys, subject_type, code) else "replay"


async def _refund(service, wallet, tx, cost: Decimal) -> bool:
    from aecs4u_billing.wallet.models import TransactionType

    try:
        await service.credit(
            wallet.id, cost, wallet.default_currency, description=f"Rimborso {tx.id}",
            reference_id=f"refund_{tx.id}", idempotency_key=f"refund-{tx.id}", type=TransactionType.REFUND,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - leave a trace for a manual refund rather than hide the original error
        logger.error("Refund of wallet transaction %s failed: %s", tx.id, type(exc).__name__)
        return False
