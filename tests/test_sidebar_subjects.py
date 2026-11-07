"""The left sidebar (``_base.html``, shown on ``/`` and every page that extends it) links the Persone and Società archives."""

import re
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def sidebar():
    import sister.main as main

    template = main.app.state.theme_setup.templates.env.get_template("_base.html")

    def render(path: str) -> str:
        request = SimpleNamespace(url=SimpleNamespace(path=path))
        context = template.new_context(
            {"request": request, "user": None, "can_admin": lambda user: True, "csp_nonce": "n", "csrf_token": lambda r: "t"}
        )
        return "".join(template.blocks["sidebar_content"](context))

    return render


def _link(html: str, href: str) -> tuple[str, bool]:
    match = re.search(rf'<a class="nav-link( active)?" href="{href}"[^>]*>\s*<i class="[^"]+"></i>\s*<span>([^<]+)</span>', html)
    assert match, f"no sidebar link to {href}"
    return match.group(2), bool(match.group(1))


def test_root_sidebar_links_persons_and_companies(sidebar):
    html = sidebar("/")
    assert _link(html, "/persons") == ("Persone", False)
    assert _link(html, "/companies") == ("Società", False)


@pytest.mark.parametrize(
    ("path", "active"),
    [("/persons", "/persons"), ("/persons/RSSMRA80A01H501U", "/persons"), ("/companies", "/companies"), ("/companies/01234567890", "/companies")],
)
def test_only_the_current_archive_is_highlighted(sidebar, path, active):
    html = sidebar(path)
    for href in ("/persons", "/companies"):
        assert _link(html, href)[1] is (href == active)
