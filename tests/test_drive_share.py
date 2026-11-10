"""Report sharing through the company Drive: the recipient check, what is uploaded and how it is shared."""

import asyncio

import pytest

from sister import drive_share


def test_recipient_must_be_one_plain_email_address():
    assert drive_share.valid_recipient(" cliente@example.com ") == "cliente@example.com"
    for bad in ("", "no-at-sign", "a@b", "a@b.com, c@d.com", "x y@example.com", "<a@b.com>"):
        with pytest.raises(drive_share.DriveShareError):
            drive_share.valid_recipient(bad)


def test_not_configured_without_a_refresh_token(monkeypatch):
    monkeypatch.delenv("GDRIVE_REFRESH_TOKEN", raising=False)
    assert not drive_share.drive_configured()
    with pytest.raises(drive_share.DriveShareError):
        asyncio.run(drive_share.share_report(b"%PDF-1.7", "r.pdf", "a@example.com"))


def test_report_is_uploaded_then_shared_with_the_recipient_only(monkeypatch):
    monkeypatch.setenv("GDRIVE_REFRESH_TOKEN", "token")
    monkeypatch.setenv("GDRIVE_CLIENT_ID", "id")
    monkeypatch.setenv("GDRIVE_CLIENT_SECRET", "secret")
    calls = {}

    class FakePermissions:
        def create(self, **kwargs):
            calls["permission"] = kwargs
            return self

        def execute(self):
            return {"id": "p1"}

    class FakeService:
        def permissions(self):
            return FakePermissions()

    class FakeStored:
        key = "file123"
        url = "https://drive.google.com/file/d/file123/view"

    class FakeProvider:
        def __init__(self, credentials, folder_name):
            calls["folder"] = folder_name

        async def upload(self, key, data, content_type, filename=None, metadata=None):
            calls["upload"] = (filename, content_type, data)
            return FakeStored()

        def _get_service(self):
            return FakeService()

        async def _run_sync(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    import aecs4u_storage

    monkeypatch.setattr(aecs4u_storage, "GoogleDriveStorageProvider", FakeProvider)

    link = asyncio.run(drive_share.share_report(b"%PDF-1.7", "report.pdf", "Cliente@Example.com", requested_by="me@x.it"))

    assert link == FakeStored.url
    assert calls["upload"] == ("report.pdf", "application/pdf", b"%PDF-1.7")
    permission = calls["permission"]
    assert permission["body"] == {"type": "user", "role": "reader", "emailAddress": "Cliente@Example.com"}
    assert permission["sendNotificationEmail"] is False  # never "anyone with the link"
