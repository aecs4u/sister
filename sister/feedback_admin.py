"""Admin endpoints for configuring and sending feedback invitation emails."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from email.utils import formataddr
from typing import Annotated, Optional

import structlog
from aecs4u_email import send_email
from aecs4u_email.feedback import FeedbackInvitationConfig, render_feedback_invitation
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, EmailStr
from sqlmodel import select

from .database import _get_session_factory
from .db_models import FeedbackConfig, FeedbackUnsubscribe

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/admin/feedback", tags=["feedback-admin"])

_UNSUB_SALT = "feedback-unsubscribe"
_api_key = os.getenv("API_KEY")


def _require_admin(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    if not _api_key:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, _api_key):
        raise HTTPException(status_code=401, detail="API key non valida")


def _secret_key() -> str:
    return os.getenv("SECRET_KEY", "change-me-in-production")


def _base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def _make_unsub_token(email: str) -> str:
    return URLSafeSerializer(_secret_key(), salt=_UNSUB_SALT).dumps(email)


def _verify_unsub_token(token: str) -> str | None:
    try:
        return URLSafeSerializer(_secret_key(), salt=_UNSUB_SALT).loads(token)
    except (BadSignature, ValueError):
        return None


async def _get_config() -> FeedbackConfig:
    session_factory = _get_session_factory()
    async with session_factory() as session:
        row = await session.get(FeedbackConfig, 1)
        if row is None:
            row = FeedbackConfig(id=1)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row


class FeedbackConfigPayload(BaseModel):
    cc_emails: list[EmailStr] = []
    bcc_emails: list[EmailStr] = []
    invitation_subject: str = "Il tuo feedback è importante"
    invitation_intro: str = ""
    invitation_bullets: list[str] = []
    invitation_cta_text: str = "Lascia il tuo feedback →"
    invitation_privacy_note: str = ""
    invitation_signature: str = ""
    invitation_unsub_text: str = "Non vuoi più ricevere queste email?"
    invitation_unsub_link_text: str = "Disiscriviti qui"
    grace_period_days: int = 30


class RecipientModel(BaseModel):
    email: str
    name: str


class SendInvitationsPayload(BaseModel):
    recipients: list[RecipientModel]
    custom_message: str | None = None


_UNSUB_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preferenze email</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
</head><body class="bg-light"><div class="container py-5" style="max-width:520px">
<div class="card shadow-sm"><div class="card-body p-4 text-center">{icon}
<h5 class="fw-semibold mt-3">{title}</h5>
<p class="text-body-secondary">{body}</p>
<a href="/" class="btn btn-outline-primary btn-sm mt-2">Torna alla piattaforma</a>
</div></div></div></body></html>"""


@router.get("/config")
async def get_feedback_config(_: None = None):
    cfg = await _get_config()
    return {
        "cc_emails": cfg.cc_emails or [],
        "bcc_emails": cfg.bcc_emails or [],
        "invitation_subject": cfg.invitation_subject,
        "invitation_intro": cfg.invitation_intro,
        "invitation_bullets": cfg.invitation_bullets or [],
        "invitation_cta_text": cfg.invitation_cta_text,
        "invitation_privacy_note": cfg.invitation_privacy_note,
        "invitation_signature": cfg.invitation_signature,
        "invitation_unsub_text": cfg.invitation_unsub_text,
        "invitation_unsub_link_text": cfg.invitation_unsub_link_text,
        "grace_period_days": cfg.grace_period_days,
    }


@router.put("/config")
async def update_feedback_config(body: FeedbackConfigPayload, _: None = None):
    session_factory = _get_session_factory()
    async with session_factory() as session:
        cfg = await session.get(FeedbackConfig, 1)
        if cfg is None:
            cfg = FeedbackConfig(id=1)
        cfg.cc_emails = [str(e) for e in body.cc_emails]
        cfg.bcc_emails = [str(e) for e in body.bcc_emails]
        cfg.invitation_subject = body.invitation_subject
        cfg.invitation_intro = body.invitation_intro
        cfg.invitation_bullets = body.invitation_bullets
        cfg.invitation_cta_text = body.invitation_cta_text
        cfg.invitation_privacy_note = body.invitation_privacy_note
        cfg.invitation_signature = body.invitation_signature
        cfg.invitation_unsub_text = body.invitation_unsub_text
        cfg.invitation_unsub_link_text = body.invitation_unsub_link_text
        cfg.grace_period_days = max(1, body.grace_period_days)
        session.add(cfg)
        await session.commit()
    return {"message": "Configurazione aggiornata."}


@router.post("/send-invitations")
async def send_feedback_invitations(body: SendInvitationsPayload, _: None = None):
    base = _base_url()
    feedback_url = f"{base}/feedback"
    cfg = await _get_config()

    session_factory = _get_session_factory()
    async with session_factory() as session:
        result = await session.execute(select(FeedbackUnsubscribe.email))
        unsubs = {row for row in result.scalars().all()}

    inv_cfg = FeedbackInvitationConfig(
        subject=cfg.invitation_subject,
        intro=cfg.invitation_intro,
        bullets=list(cfg.invitation_bullets or []),
        cta_text=cfg.invitation_cta_text,
        privacy_note=cfg.invitation_privacy_note,
        signature=cfg.invitation_signature,
        unsub_text=cfg.invitation_unsub_text,
        unsub_link_text=cfg.invitation_unsub_link_text,
    )

    sent = failed = skipped = 0
    failures: list[dict] = []

    for r in body.recipients:
        if r.email.lower() in {e.lower() for e in unsubs}:
            skipped += 1
            continue
        unsub_token = _make_unsub_token(r.email)
        unsub_url = f"{base}/api/v1/admin/feedback/unsubscribe?token={unsub_token}"
        html_body, text_body = render_feedback_invitation(
            recipient_name=r.name,
            feedback_url=feedback_url,
            unsub_url=unsub_url,
            config=inv_cfg,
            custom_message=body.custom_message,
        )
        result = await send_email(
            to=formataddr((r.name, r.email)),
            subject=cfg.invitation_subject,
            html=html_body,
            text=text_body,
            cc=([str(e) for e in cfg.cc_emails] or None),
            bcc=([str(e) for e in cfg.bcc_emails] or None),
            tags=["feedback-invitation"],
            headers={
                "List-Unsubscribe": f"<{unsub_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
        if result.success:
            sent += 1
        else:
            failed += 1
            failures.append({"email": r.email, "name": r.name})

    logger.info("feedback_invitations_sent", sent=sent, failed=failed, skipped=skipped)
    return {"sent": sent, "failed": failed, "skipped": skipped, "failures": failures}


@router.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def feedback_unsubscribe(token: str = ""):
    email = _verify_unsub_token(token) if token else None
    if not email:
        return HTMLResponse(
            _UNSUB_PAGE.format(
                icon='<i class="bi bi-x-circle text-danger fs-1"></i>',
                title="Link non valido",
                body="Il link di disiscrizione non è valido o è scaduto.",
            ),
            status_code=400,
        )
    session_factory = _get_session_factory()
    async with session_factory() as session:
        if not await session.get(FeedbackUnsubscribe, email):
            session.add(FeedbackUnsubscribe(email=email, unsubscribed_at=datetime.now(timezone.utc)))
            await session.commit()
    logger.info("feedback_unsubscribed_via_email", email=email)
    return HTMLResponse(
        _UNSUB_PAGE.format(
            icon='<i class="bi bi-check-circle text-success fs-1"></i>',
            title="Disiscrizione confermata",
            body="Non riceverai più email di invito feedback.",
        )
    )
