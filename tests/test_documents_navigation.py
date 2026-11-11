"""Catalogue category filtering and navigation across pagination boundaries."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from starlette.requests import Request

import sister.web as web
from sister import database


@pytest.fixture
def catalog(monkeypatch):
    documents = [
        {"id": i, "document_type": kind, "filename": f"document-{i}.pdf", "file_format": "PDF",
         "file_size": 1, "created_at": "2026-10-01T00:00:00", "oggetto": "Ravenna"}
        for i, kind in enumerate(["visura_fabbricati", "visura_terreni", "visura_storica", "epa",
                                  "elaborato_planimetrico", "unclassified"] + ["richieste"] * 205, 1)
    ]

    async def rows():
        for document in documents:
            yield dict(document)

    async def visible_ids(user):
        # The unpurchased third visura must stay hidden for buyers.
        return None if user.role == "admin" else {d["id"] for d in documents if d["id"] != 3}

    async def by_ids(ids):
        return [dict(d) for d in documents if d["id"] in ids]

    async def favorite_ids(_key):
        return {1, 2, 3, 4}

    async def owners(*_args, **_kwargs):
        return {}

    async def subject_ids(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(web, "iter_all_documents", rows)
    monkeypatch.setattr(web, "_visible_document_ids", visible_ids)
    monkeypatch.setattr(web, "get_documents_by_ids", by_ids)
    monkeypatch.setattr(web, "get_document_group_siblings", by_ids)
    monkeypatch.setattr(web, "_favorite_user_key", lambda _user: "id:catalog-test")
    monkeypatch.setattr(database, "get_document_favorite_ids", favorite_ids)
    monkeypatch.setattr(web, "get_document_owner_fiscal_codes", owners)
    monkeypatch.setattr(web, "get_subject_document_ids_for_fiscal_codes", subject_ids)
    return documents


@pytest.mark.asyncio
@pytest.mark.parametrize("role, expected_ids", [("admin", {1, 2, 3}), ("user", {1, 2})])
@pytest.mark.parametrize("favorites", [False, True])
async def test_category_finds_older_documents_and_preserves_access(catalog, role, expected_ids, favorites):
    class CaptureTheme:
        def render(self, _template, _request, **context):
            return context

    request = Request({"type": "http", "method": "GET", "scheme": "http", "server": ("testserver", 80),
                       "path": "/documents", "query_string": b"type=visura_immobile&page=3",
                       "app": SimpleNamespace(state=SimpleNamespace(theme_setup=CaptureTheme()))})
    context = await web.web_documents(request, page=3, favorites=favorites, q="Ravenna",
                                      user=SimpleNamespace(role=role))
    assert {d["id"] for d in context["files"]} == expected_ids
    assert context["total"] == len(expected_ids)
    assert context["page"] == context["page_count"] == 1
    assert [g["key"] for g in context["tree"]] == ["visura_immobile"]


@pytest.mark.asyncio
async def test_category_menu_preserves_search_view_favorites_and_resets_page(main_module, monkeypatch, catalog):
    from fastapi.testclient import TestClient

    user = SimpleNamespace(id="catalog-test", role="admin", is_superuser=True)
    monkeypatch.setitem(main_module.app.dependency_overrides, web._require_auth, lambda: user)
    client = TestClient(main_module.app)
    response = client.get("/documents?type=visura_immobile&page=3&q=Ravenna&view=files&favorites=true")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    nav = soup.find("nav", attrs={"aria-label": "Categorie dei documenti"})
    assert "Visura per Immobile" in nav.find("button").get_text()
    active = nav.select_one('a[aria-current="page"]')
    assert parse_qs(urlsplit(active["href"]).query)["type"] == ["visura_immobile"]
    for link in nav.find_all("a"):
        url = urlsplit(link["href"])
        params = parse_qs(url.query)
        assert url.path == "/documents" and not url.fragment
        assert params["q"] == ["Ravenna"]
        assert params["view"] == ["files"]
        assert params["favorites"] == ["true"]
        assert "page" not in params
    epa_link = next(a for a in nav.find_all("a") if "Elaborato Planimetrico" in a.get_text())
    epa_response = client.get(epa_link["href"])
    assert epa_response.status_code == 200
    assert "document-4.pdf" in epa_response.text and "document-5.pdf" not in epa_response.text
    # Save the rendered production template for a browser layout check.
    from pathlib import Path
    Path("/tmp/sister-documents-navigation.html").write_text(response.text)


@pytest.mark.parametrize("category, kind, expected", [
    ("elaborato_planimetrico", "epa", True),
    ("elenco_immobili", "intestati", True),
    ("nota_trascrizione_iscrizione", "nota_annotazione", True),
    ("ispezione_ipotecaria", "visura_ipotecaria", True),
    ("estratto_mappa", "mappa", True),
    ("altro", "new_import_type", True),
    ("altro", "visura_storica", False),
    ("nota_iscrizione", "nota_trascrizione", False),
])
def test_category_includes_aliases_and_keeps_raw_type_filters(category, kind, expected):
    assert web._document_matches_type_filter({"document_type": kind}, category) is expected
