"""A company found by a persona_giuridica search that has several registry records keeps no ``subject.name`` of its own;
the lists show the first record's denomination instead (cleaned), with the number of distinct ones."""

import json

import pytest
from sqlalchemy import text
from starlette.requests import Request

from sister import database, web

MANY, ONE, OWN, NONE = "02595400587", "02594070365", "01234567890", "09876543210"


class User:
    email = "reader@example.com"


async def _exec(sql: str, **params):
    async with database._get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


async def _company(code: str, name: str | None = None, records: tuple[str, ...] = ()):
    subject_pk = (await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:c, :n) RETURNING id", c=code, n=name or code)).scalar_one()
    await _exec(
        "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) VALUES (:r, now(), now(), true, :c)",
        r=f"pnf_E_{code}", c=code,
    )
    for denomination, seat in records:  # inserted in the order SISTER listed them
        await _exec(
            "INSERT INTO subject_record (subject_id, fiscal_code, denomination, seat) VALUES (:s, :c, :d, :seat)",
            s=subject_pk, c=code, d=denomination, seat=seat,
        )


async def _seed():
    await _company(MANY, records=(
        ('"C SERVI CARITA DON GUANELLA"', "ROMA (RM)"), ("C SERVI CARITA DON GUANELLA", "ROMA (RM)"),  # the same, with quotes
        ("CASA DELL'ANGELO", "GENOVA (GE)"), ("CASA DELL'ANGELO CON SEDE IN GENOVA", "GENOVA (GE)"),  # ... and with the seat
        ("CONGREGAZIONE DEI SERVI DELLA CARITA'", ""),
    ))
    await _company(ONE, records=(("EURO BAGNO S.R.L.", "FIORANO MODENESE (MO)"),))
    await _company(OWN, name="ACME SRL", records=(("ALTRA DENOMINAZIONE", "MILANO (MI)"),))  # a real name wins
    await _company(NONE)  # no records, no name: still unknown


def _index(query: str = ""):
    return Request({"type": "http", "method": "GET", "path": "/companies/index.json", "query_string": query.encode(), "headers": [],
                    "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})


@pytest.mark.usefixtures("fresh_db")
async def test_company_without_a_name_takes_the_first_registry_record():
    await _seed()
    rows, total = await database.query_subject_profiles("company", limit=None)
    by_code = {r["fiscal_code"]: r for r in rows}
    assert total == 4
    many = by_code[MANY]
    assert (many["name"], many["name_inferred"], many["name_records"]) == ("C SERVI CARITA DON GUANELLA", True, 3)  # quotes and "con sede in" folded away
    assert (by_code[ONE]["name"], by_code[ONE]["name_inferred"], by_code[ONE]["name_records"]) == ("EURO BAGNO S.R.L.", True, 1)
    assert (by_code[OWN]["name"], by_code[OWN]["name_inferred"]) == ("ACME SRL", False)  # its own name is never replaced
    assert (by_code[NONE]["name"], by_code[NONE]["name_inferred"], by_code[NONE]["name_records"]) == ("", False, 0)


@pytest.mark.usefixtures("fresh_db")
async def test_name_filter_and_search_see_the_registry_name():
    await _seed()
    missing, total = await database.query_subject_profiles("company", name_status="missing")
    assert total == 1 and [r["fiscal_code"] for r in missing] == [NONE]
    named, total = await database.query_subject_profiles("company", name_status="named")
    assert total == 3
    found, _ = await database.query_subject_profiles("company", q="euro bagno")
    assert [r["fiscal_code"] for r in found] == [ONE]
    one = await database.get_subject_profile(MANY, "company")  # the profile page header uses the same query
    assert one["name"] == "C SERVI CARITA DON GUANELLA"
    payload = json.loads((await web._subject_index_json(_index("name_status=missing"), "company", User())).body)
    assert [r["fiscal_code"] for r in payload["data"]] == [NONE]
