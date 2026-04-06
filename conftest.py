import logging
import sys
import types

import pytest


def _install_test_stubs() -> None:
    if "bs4" not in sys.modules:
        bs4_mod = types.ModuleType("bs4")

        class BeautifulSoup:
            def __init__(self, html: str, parser: str):
                self.html = html
                self.parser = parser

            def find_all(self, _tag):
                return []

        bs4_mod.BeautifulSoup = BeautifulSoup
        sys.modules["bs4"] = bs4_mod

    if "playwright.async_api" not in sys.modules:
        playwright_pkg = types.ModuleType("playwright")
        async_api_mod = types.ModuleType("playwright.async_api")

        class Page:
            pass

        async_api_mod.Page = Page
        sys.modules["playwright"] = playwright_pkg
        sys.modules["playwright.async_api"] = async_api_mod

    if "rich.logging" not in sys.modules:
        rich_pkg = types.ModuleType("rich")
        rich_logging_mod = types.ModuleType("rich.logging")

        class RichHandler(logging.Handler):
            def __init__(self, **kwargs):
                super().__init__()

            def emit(self, record):
                pass

        rich_logging_mod.RichHandler = RichHandler
        sys.modules.setdefault("rich", rich_pkg)
        sys.modules["rich.logging"] = rich_logging_mod

    if "aecs4u_auth.browser" not in sys.modules:
        auth_pkg = types.ModuleType("aecs4u_auth")
        browser_mod = types.ModuleType("aecs4u_auth.browser")

        class BrowserConfig:
            pass

        class PageLogger:
            def __init__(self, _name: str = "test"):
                pass

            @staticmethod
            def reset_session():
                return None

            async def log(self, _page, _label: str):
                return None

        class _Session:
            is_valid = True
            page = object()

        class BrowserManager:
            def __init__(self, _config):
                self.is_authenticated = True
                self.session = _Session()

            async def initialize(self):
                return None

            async def login(self, service: str):
                return None

            async def start_keepalive(self):
                return None

            async def stop_keepalive(self):
                return None

            async def ensure_authenticated(self):
                return None

            async def close(self):
                return None

            async def graceful_shutdown(self):
                return None

        browser_mod.BrowserConfig = BrowserConfig
        browser_mod.PageLogger = PageLogger
        browser_mod.BrowserManager = BrowserManager

        sys.modules["aecs4u_auth"] = auth_pkg
        sys.modules["aecs4u_auth.browser"] = browser_mod


_install_test_stubs()


@pytest.fixture()
def main_module():
    import importlib

    return importlib.import_module("main")
