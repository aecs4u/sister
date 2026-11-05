"""Shared tags that group persons and companies: normalisation, storage, the table filter and the web handlers."""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from sister import database, web

ROSSI, VERDI, BIANCHI = "RSSMRA80A01H501U", "VRDLGU75B12F205X", "BNCLRA90C41H501Z"
ACME, BETA = "01234567890", "09876543210"


class User:
    email = "editor@example.com"


def test_normalize_tag_squeezes_whitespace_and_ignores_case():
    assert database.normalize_tag("  Politici   Siciliani ") == ("politici siciliani", "Politici Siciliani")
    assert database.normalize_tag("ÈNEL")[0] == database.normalize_tag("ènel")[0]


@pytest.mark.parametrize("bad", ["", "   ", None, "-", "!!!", "a" * 41, "bell\x07", "tab​"])
def test_normalize_tag_rejects_unusable_tags(bad):
    with pytest.raises(ValueError):
        database.normalize_tag(bad)


async def _exec(sql: str, **params):
    async with database._get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


async def _seed():
    """Three persons and two companies, each with one successful search (so they are in the archive)."""
    for code, name in ((ROSSI, "ROSSI MARIO"), (VERDI, "VERDI LUIGI"), (BIANCHI, "BIANCHI LAURA"), (ACME, "ACME SRL"), (BETA, "BETA SPA")):
        await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:c, :n)", c=code, n=name)
        await _exec(
            "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
            "VALUES (:r, now(), now(), true, :c)", r=f"soggetto_E_{code}", c=code,
        )


def _request(query: str = "", path: str = "/persons/tags/bulk"):
    return Request({"type": "http", "method": "POST", "path": path, "query_string": query.encode(), "headers": [],
                    "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})


def _with_body(request, payload):
    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    request._receive = receive
    return request


@pytest.mark.usefixtures("fresh_db")
async def test_set_subject_tags_replaces_the_list_and_keeps_the_first_spelling():
    await _seed()
    assert await database.set_subject_tags("person", ROSSI, ["Politici", "politici", " Sicilia "], "id:a") == ["Politici", "Sicilia"]
    # a later edit keeps the stored spelling and drops what is no longer listed
    assert await database.set_subject_tags("person", ROSSI, ["POLITICI", "Milano"], "id:b") == ["Milano", "Politici"]
    assert await database.get_subject_tags("person", ROSSI) == ["Milano", "Politici"]
    assert await database.set_subject_tags("person", ROSSI, []) == []
    with pytest.raises(ValueError):
        await database.set_subject_tags("person", ROSSI, [f"t{i}" for i in range(database.SUBJECT_TAGS_PER_SUBJECT + 1)])
    with pytest.raises(ValueError):
        await database.set_subject_tags("organisation", ROSSI, ["x"])


@pytest.mark.usefixtures("fresh_db")
async def test_tags_belong_to_the_kind_of_subject():
    await _seed()
    await database.set_subject_tags("person", ROSSI, ["gruppo"])
    await database.set_subject_tags("company", ACME, ["gruppo", "grandi"])
    assert await database.list_subject_tags("person") == [{"tag": "gruppo", "count": 1}]
    assert await database.list_subject_tags("company") == [{"tag": "grandi", "count": 1}, {"tag": "gruppo", "count": 1}]  # ties: A-Z
    rows, _ = await database.query_subject_profiles("company", tag="gruppo")
    assert [r["fiscal_code"] for r in rows] == [ACME] and rows[0]["tags"] == ["grandi", "gruppo"]


@pytest.mark.usefixtures("fresh_db")
async def test_table_filters_by_tag_without_tag_and_searches_tags():
    await _seed()
    await database.set_subject_tags("person", ROSSI, ["Politici", "Sicilia"])
    await database.set_subject_tags("person", VERDI, ["politici"])

    rows, total = await database.query_subject_profiles("person", tag="POLITICI")
    assert total == 2 and {r["fiscal_code"] for r in rows} == {ROSSI, VERDI}
    rows, _ = await database.query_subject_profiles("person", tag="sicilia")
    assert [r["fiscal_code"] for r in rows] == [ROSSI]
    rows, _ = await database.query_subject_profiles("person", tag=database.SUBJECT_NO_TAG)
    assert [r["fiscal_code"] for r in rows] == [BIANCHI]
    rows, _ = await database.query_subject_profiles("person", tag="nessuno")
    assert rows == []
    rows, _ = await database.query_subject_profiles("person", tag="!!!")  # an impossible tag matches nothing
    assert rows == []
    rows, _ = await database.query_subject_profiles("person", q="sicilia")  # the search box looks into tags too
    assert [r["fiscal_code"] for r in rows] == [ROSSI]
    rows, _ = await database.query_subject_profiles("person", tag="politici", q="verdi")
    assert [r["fiscal_code"] for r in rows] == [VERDI]
    profile = await database.get_subject_profile(ROSSI, "person")
    assert profile["tags"] == ["Politici", "Sicilia"]
    assert (await database.get_subject_profile(BIANCHI, "person"))["tags"] == []


@pytest.mark.usefixtures("fresh_db")
async def test_tag_and_untag_many_subjects():
    await _seed()
    assert await database.tag_subjects("person", [ROSSI, VERDI, ROSSI.lower()], "Gruppo A", "id:a") == 2
    assert await database.tag_subjects("person", [ROSSI, BIANCHI], "gruppo a") == 1  # Rossi already had it
    assert await database.untag_subjects("person", [ROSSI, ACME], "GRUPPO A") == 1  # the company has no such tag
    assert await database.list_subject_tags("person") == [{"tag": "Gruppo A", "count": 2}]
    assert await database.tag_subjects("person", [], "x") == 0
    with pytest.raises(ValueError):
        await database.tag_subjects("person", [ROSSI], "  ")


@pytest.mark.usefixtures("fresh_db")
async def test_replace_endpoint_validates_the_code_and_the_body():
    await _seed()
    response = await web._subject_tags_replace(_with_body(_request(), {"tags": ["Gruppo", "gruppo", "Altro"]}), "person", ROSSI.lower(), User)
    assert json.loads(response.body) == {"subject_type": "person", "subject_code": ROSSI, "tags": ["Altro", "Gruppo"]}
    for code, kind in (("short", "person"), (ROSSI, "company")):
        with pytest.raises(HTTPException) as error:
            await web._subject_tags_replace(_with_body(_request(), {"tags": []}), kind, code, User)
        assert error.value.status_code == 404
    for payload in ({"tags": "gruppo"}, {"tags": [1]}, {"nope": 1}):
        with pytest.raises(HTTPException) as error:
            await web._subject_tags_replace(_with_body(_request(), payload), "person", ROSSI, User)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await web._subject_tags_replace(_with_body(_request(), {"tags": ["x" * 41]}), "person", ROSSI, User)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:  # nobody signed in (no key) may not write
        await web._subject_tags_replace(_with_body(_request(), {"tags": ["x"]}), "person", ROSSI, None)
    assert error.value.status_code == 401


@pytest.mark.usefixtures("fresh_db")
async def test_bulk_tags_exactly_the_rows_the_table_shows():
    await _seed()
    await database.set_subject_tags("person", ROSSI, ["vip"])
    query = "q=laura"  # one match
    ok = await web._subject_tags_bulk(_with_body(_request(query), {"action": "add", "tag": "Nuovo", "expected": 1}), "person", User)
    body = json.loads(ok.body)
    assert (body["matched"], body["changed"]) == (1, 1)
    assert await database.get_subject_tags("person", BIANCHI) == ["Nuovo"]
    assert await database.get_subject_tags("person", VERDI) == []

    # every person (no filter): the count the user saw must still be right
    with pytest.raises(HTTPException) as stale:
        await web._subject_tags_bulk(_with_body(_request(""), {"action": "add", "tag": "tutti", "expected": 2}), "person", User)
    assert stale.value.status_code == 409
    assert await database.get_subject_tags("person", ROSSI) == ["vip"]  # nothing was written

    done = await web._subject_tags_bulk(_with_body(_request(""), {"action": "add", "tag": "tutti", "expected": 3}), "person", User)
    assert json.loads(done.body)["changed"] == 3
    removed = await web._subject_tags_bulk(
        _with_body(_request("tag=nuovo"), {"action": "remove", "tag": "tutti", "expected": 1}), "person", User
    )
    assert json.loads(removed.body)["changed"] == 1 and await database.get_subject_tags("person", BIANCHI) == ["Nuovo"]
    assert await database.get_subject_tags("company", ACME) == []  # the other kind is never touched

    for payload in ({"action": "drop", "tag": "x", "expected": 1}, {"action": "add", "tag": 5, "expected": 1},
                    {"action": "add", "tag": "x"}, {"action": "add", "tag": "x", "expected": "1"}):
        with pytest.raises(HTTPException) as error:
            await web._subject_tags_bulk(_with_body(_request(""), payload), "person", User)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await web._subject_tags_bulk(_with_body(_request("q=laura"), {"action": "add", "tag": "  ", "expected": 1}), "person", User)
    assert error.value.status_code == 422


@pytest.mark.usefixtures("fresh_db")
async def test_index_json_and_csv_carry_the_tags_and_follow_the_tag_filter():
    await _seed()
    await database.set_subject_tags("person", ROSSI, ["Politici", "Sicilia"])
    await database.set_subject_tags("person", VERDI, ["Altro"])

    def index(query: str):
        return Request({"type": "http", "method": "GET", "path": "/persons/index.json", "query_string": query.encode(),
                        "headers": [], "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})

    payload = json.loads((await web._subject_index_json(index("tag=politici"), "person", User)).body)
    assert [(r["fiscal_code"], r["tags"]) for r in payload["data"]] == [(ROSSI, ["Politici", "Sicilia"])]
    payload = json.loads((await web._subject_index_json(index("tag=-"), "person", User)).body)
    assert [r["fiscal_code"] for r in payload["data"]] == [BIANCHI] and payload["data"][0]["tags"] == []

    csv_text = (await web._subject_index_csv(index("tag=politici"), "person", User)).body.decode("utf-8").lstrip("﻿")
    import csv
    import io

    header, row = list(csv.reader(io.StringIO(csv_text), delimiter=";"))
    assert header[-1] == "Tag" and row[-1] == "Politici | Sicilia"


def test_the_migration_creates_the_table_the_model_describes():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261127_subject_tags.py").read_text()
    assert 'down_revision: Union[str, None] = "20261126_ownership_history"' in source
    assert database.DATABASE_REVISION == "20261127_subject_tags"
    columns = {c.name for c in database.SubjectTag.__table__.columns}
    assert columns == {"subject_type", "subject_code", "tag_key", "tag", "created_by", "created_at"}
    for column in columns:
        assert f'"{column}"' in source


def _render_tags_partial(**context):
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = Path(__file__).resolve().parent.parent / "sister" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape(["html"]))
    return environment.get_template("catasto/_subject_tags.html").render(csp_nonce="N0NCE", **context)


def test_profile_strip_links_each_tag_to_the_filtered_list_and_escapes_it():
    html = _render_tags_partial(
        subject_fiscal_code=ACME, subject_route_prefix="/companies", subject_tags=["Grandi & ricchi", "<b>x</b>"],
        subject_favorite_enabled=True,
    )
    assert 'href="/companies/?tag=Grandi%20%26%20ricchi"' in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert 'data-tags-url="/companies/01234567890/tags"' in html
    assert 'data-tags-edit' in html and '<script nonce="N0NCE">' in html
    assert "hidden" in html.split("data-tags-empty")[1].split(">")[0]  # the "nessun tag" note is hidden when there are tags


def test_profile_strip_is_read_only_without_a_signed_in_key_and_says_when_there_are_no_tags():
    html = _render_tags_partial(
        subject_fiscal_code=ROSSI, subject_route_prefix="/persons", subject_tags=[], subject_favorite_enabled=False
    )
    assert "nessun tag" in html and "data-tags-edit" not in html and "<script" not in html
    assert "hidden" not in html.split("data-tags-empty")[1].split(">")[0]


@pytest.mark.usefixtures("fresh_db")
async def test_companies_can_be_filtered_by_name_status_too():
    await _seed()
    await _exec("UPDATE subject SET name = fiscal_code WHERE fiscal_code = :c", c=BETA)  # a name that only repeats the code

    def index(query: str):
        return Request({"type": "http", "method": "GET", "path": "/companies/index.json", "query_string": query.encode(),
                        "headers": [], "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})

    async def codes(query: str):
        return [r["fiscal_code"] for r in json.loads((await web._subject_index_json(index(query), "company", User)).body)["data"]]

    assert sorted(await codes("")) == [ACME, BETA]
    assert await codes("name_status=named") == [ACME]
    assert await codes("name_status=missing") == [BETA]
    assert sorted(await codes("name_status=bogus")) == [ACME, BETA]  # an unknown value does not filter
