"""Save a report PDF to the company Google Drive (``aecs4u-storage``) and share it with one recipient.

The report holds personal and cadastral data, so the file is never public: it is shared by e-mail address, as a reader,
with the Drive notification switched off (the sender mails the link from their own Gmail, see ``document_share.js``).

Credentials of the company Drive account (one-time OAuth consent with the ``drive.file`` scope; the refresh token is
kept in ``.env``): ``GDRIVE_CLIENT_ID``, ``GDRIVE_CLIENT_SECRET`` (default ``GOOGLE_CLIENT_ID`` / ``_SECRET``) and
``GDRIVE_REFRESH_TOKEN``. ``GDRIVE_FOLDER_NAME`` names the folder (default ``SISTER report``), created on first use.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("sister")

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_EMAIL = re.compile(r"^[^@\s,;<>]{1,64}@[^@\s,;<>]{1,190}\.[A-Za-z]{2,}$")


class DriveShareError(Exception):
    """The report could not be saved or shared; the message is safe to show to the user."""


def valid_recipient(value: str) -> str:
    email = (value or "").strip()
    if len(email) > 254 or not _EMAIL.match(email):
        raise DriveShareError("Indirizzo email del destinatario non valido")
    return email


def _credentials():
    client_id = os.getenv("GDRIVE_CLIENT_ID", "").strip() or os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GDRIVE_CLIENT_SECRET", "").strip() or os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    refresh_token = os.getenv("GDRIVE_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        return None
    from google.oauth2.credentials import Credentials

    return Credentials(
        None, refresh_token=refresh_token, token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id, client_secret=client_secret, scopes=[DRIVE_SCOPE],
    )


def drive_configured() -> bool:
    return bool(os.getenv("GDRIVE_REFRESH_TOKEN", "").strip())


async def share_report(pdf: bytes, filename: str, recipient: str, *, requested_by: str = "") -> str:
    """Upload ``pdf`` to the company Drive, share it with ``recipient`` only and return the Drive link."""
    recipient = valid_recipient(recipient)
    credentials = _credentials()
    if credentials is None:
        raise DriveShareError("Google Drive non configurato (GDRIVE_REFRESH_TOKEN)")
    try:
        from aecs4u_storage import GoogleDriveStorageProvider

        provider = GoogleDriveStorageProvider(
            credentials, folder_name=os.getenv("GDRIVE_FOLDER_NAME", "").strip() or "SISTER report"
        )
        stored = await provider.upload(
            filename, pdf, "application/pdf", filename=filename,
            metadata={"source": "sister", "requested_by": requested_by[:100]},
        )

        def grant():  # only this address, as a reader, and no notification from Drive
            provider._get_service().permissions().create(
                fileId=stored.key, body={"type": "user", "role": "reader", "emailAddress": recipient},
                sendNotificationEmail=False, fields="id",
            ).execute()

        await provider._run_sync(grant)
    except DriveShareError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak provider details to the page
        logger.warning("Drive share failed: %s", type(exc).__name__)
        raise DriveShareError("Salvataggio su Google Drive non riuscito") from exc
    logger.info("Report %s saved to Drive and shared (%s)", filename, requested_by or "unknown user")
    return stored.url or f"https://drive.google.com/file/d/{stored.key}/view"
