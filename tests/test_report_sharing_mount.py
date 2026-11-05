"""The e-mail report-sharing add-on must never stop the service from starting, whatever version of aecs4u-email is installed."""

import sys
import types

import pytest
from fastapi import APIRouter, FastAPI

from sister import main


def _install(monkeypatch, factory):
    module = types.ModuleType("aecs4u_email")
    module.create_report_sharing_router = factory
    monkeypatch.setitem(sys.modules, "aecs4u_email", module)


def _router():
    router = APIRouter()

    @router.post("/api/ui/report-email")
    async def _post():
        return {}

    return router


def _dependency():
    return None


def test_the_current_package_gets_a_public_url_an_authorizer_and_the_shareable_pages(monkeypatch):
    seen = {}

    def factory(*, auth_dependency, public_base_url, authorize_report_path, app_name="X", allowed_path_prefixes=("/reports/",)):
        seen.update(locals())
        return _router()

    _install(monkeypatch, factory)
    app = FastAPI()
    assert main._mount_report_sharing(app, _dependency) is True
    assert seen["auth_dependency"] is _dependency and seen["app_name"] == "SISTER"
    assert seen["allowed_path_prefixes"] == main.SHAREABLE_PREFIXES
    assert callable(seen["public_base_url"]) and seen["authorize_report_path"] is main._authorize_report_path
    assert "/api/ui/report-email" in app.openapi()["paths"]


def test_an_older_package_without_the_new_arguments_still_mounts(monkeypatch):
    def old(*, auth_dependency, app_name="X"):
        return _router()

    _install(monkeypatch, old)
    assert main._mount_report_sharing(FastAPI(), _dependency) is True


def test_a_package_that_rejects_the_call_does_not_stop_the_service(monkeypatch):
    def broken(*, auth_dependency, public_base_url, authorize_report_path, brand_new_required):
        return _router()

    _install(monkeypatch, broken)
    assert main._mount_report_sharing(FastAPI(), _dependency) is False  # logged, not raised


def test_a_missing_package_is_not_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "aecs4u_email", None)  # import raises ImportError
    assert main._mount_report_sharing(FastAPI(), _dependency) is False


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("/documents/12", True), ("/documents/12/pdf", True), ("/persons/RSSMRA80A01H501U", True), ("/companies/00360090393", True),
        ("/results/abc", True), ("/omi/valuation", True), ("/valuation", True),
        ("/admin/commands", False), ("/api/v1/visura/x", False), ("/documentsx", False), ("/", False), ("/settings", False),
    ],
)
def test_only_report_pages_can_be_shared(path, allowed):
    assert main._authorize_report_path(None, path) is allowed


def test_the_public_url_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.setenv("SISTER_PUBLIC_URL", "https://sister.example.com ")
    assert main._public_base_url() == "https://sister.example.com"
    monkeypatch.delenv("SISTER_PUBLIC_URL")
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.com")
    assert main._public_base_url() == "https://app.example.com"
