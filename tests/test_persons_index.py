"""/persons archive table: stored holdings (counts, rendite, valori catastali) and names for code-only subjects."""

from decimal import Decimal

import pytest
from sqlalchemy import text

from sister import database
from sister.subject_names import names_from_markdown, names_in_text

CF_A = "RSSMRA80A01H501U"
CF_B = "VRDLGU75B12F205X"


def test_names_in_text_reads_code_and_name_columns():
    text_md = (
        "# Lista\n\n<!-- sister-batch-table {\"command\": \"soggetto\"} -->\n"
        "| id | codice_fiscale | intestatario | fonte |\n|---|---|---|---|\n"
        f"| pf-{CF_A} | {CF_A} | ROSSI MARIO | pvp |\n"
        f"| pf-{CF_B} | `{CF_B}` |  | opendata |\n"
        "| pf-X | NOTACODE | SOMEONE | x |\n\n"
        "| id | identificativo | meta_sede | fonte |\n|---|---|---|---|\n| pi-1 | 01234567890 | MILANO | x |\n"
    )
    assert list(names_in_text(text_md)) == [(CF_A, "ROSSI MARIO")]


def test_names_from_markdown_keeps_the_most_frequent_name(tmp_path):
    row = "| id | codice_fiscale | intestatario |\n|---|---|---|\n| a | {cf} | {name} |\n"
    (tmp_path / "a.md").write_text(row.format(cf=CF_A, name="ROSSI MARIO"), encoding="utf-8")
    (tmp_path / "b.md").write_text(row.format(cf=CF_A, name="ROSSI MARIO"), encoding="utf-8")
    (tmp_path / "c.md").write_text(row.format(cf=CF_A, name="ROSSI M."), encoding="utf-8")
    assert names_from_markdown(sorted(tmp_path.glob("*.md"))) == {CF_A: "ROSSI MARIO"}


async def _exec(sql: str, **params):
    async with database._get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


@pytest.mark.usefixtures("fresh_db")
async def test_fill_subject_names_only_fills_code_only_subjects():
    await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:a, :a), (:b, 'BIANCHI LUCA')", a=CF_A, b=CF_B)
    assert await database.nameless_subject_codes() == [CF_A]
    updated = await database.fill_subject_names({CF_A: ("ROSSI MARIO", "ROSSI", "MARIO"), CF_B: "OVERWRITE"})
    assert updated == 1
    rows = (await _exec("SELECT fiscal_code, name, last_name FROM subject ORDER BY fiscal_code")).all()
    assert [tuple(r) for r in rows] == [(CF_A, "ROSSI MARIO", "ROSSI"), (CF_B, "BIANCHI LUCA", None)]


@pytest.mark.usefixtures("fresh_db")
async def test_refresh_subject_holdings_persists_counts_and_values():
    request_type = (await _exec("SELECT code FROM request_type WHERE code = 'soggetto'")).scalar_one_or_none()
    if request_type is None:
        await _exec("INSERT INTO request_type (code, label, description) VALUES ('soggetto', 'Soggetto', 'Soggetto')")
    await _exec("INSERT INTO cadastral_category (code) VALUES ('A/2') ON CONFLICT DO NOTHING")
    request_pk = (
        await _exec(
            "INSERT INTO request (request_ref, request_type, created_at, responded_at, success, subject_query) "
            "VALUES ('soggetto_E_1', 'soggetto', now(), now(), true, :cf) RETURNING id",
            cf=CF_A,
        )
    ).scalar_one()
    subject_pk = (await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:cf, :cf) RETURNING id", cf=CF_A)).scalar_one()
    await _exec(
        "INSERT INTO request_province (request_id, province_index, fiscal_code, province_code, province_name, "
        "buildings_count, land_parcels_count) VALUES (:r, 0, :cf, 'RM', 'ROMA', 1, 1)",
        r=request_pk,
        cf=CF_A,
    )
    kind_pk = (await _exec("INSERT INTO right_kind (right_type) VALUES (:t) RETURNING id", t="Proprieta'")).scalar_one()
    # A/2 with rendita 1000 (x 1.05 x 160 = 168000) held for 1/2, land with reddito dominicale 100 (x 1.25 x 135).
    for index, (kind, category, income, dominical, num, den) in enumerate(
        [("building", "A/2", 1000, None, 1, 2), ("land", None, None, 100, 1, 1)]
    ):
        result_pk = (
            await _exec(
                "INSERT INTO request_result (request_id, result_index) VALUES (:r, :i) RETURNING id", r=request_pk, i=index
            )
        ).scalar_one()
        await _exec(
            "INSERT INTO property (request_id, result_id, property_type, category, income_eur, dominical_income_eur) "
            "VALUES (:r, :res, :t, :c, :inc, :dom)",
            r=request_pk, res=result_pk, t=kind, c=category, inc=income, dom=dominical,
        )
        right_pk = (
            await _exec(
                "INSERT INTO ownership_right (right_kind_id, share_numerator, share_denominator) VALUES (:k, :n, :d) RETURNING id",
                k=kind_pk, n=num, d=den,
            )
        ).scalar_one()
        await _exec(
            "INSERT INTO result_owner (request_id, result_id, owner_index, subject_id, right_id) "
            "VALUES (:r, :res, 0, :s, :right)",
            r=request_pk, res=result_pk, s=subject_pk, right=right_pk,
        )

    third_result = (
        await _exec("INSERT INTO request_result (request_id, result_index) VALUES (:r, 2) RETURNING id", r=request_pk)
    ).scalar_one()
    await _exec(
        "INSERT INTO property (request_id, result_id, property_type) VALUES (:r, :res, 'building')",
        r=request_pk, res=third_result,
    )

    assert await database.refresh_subject_holdings([CF_A]) == 1
    row = (await _exec("SELECT * FROM subject_holding WHERE fiscal_code = :cf", cf=CF_A)).mappings().one()
    assert (row["properties"], row["buildings"], row["land_parcels"], row["provinces"]) == (3, 1, 1, "RM")
    assert (row["unvalued"], row["unknown_share"]) == (1, 1)  # the third has no value and no recorded quota
    assert row["searched_at"] is not None
    assert row["full_ownership"] == 1  # the land, 1/1; the building is held for 1/2
    assert row["cadastral_income_eur"] == Decimal("1000.00") and row["dominical_income_eur"] == Decimal("100.00")
    assert row["cadastral_value_eur"] == Decimal("168000.00") + Decimal("16875.00")
    assert row["cadastral_value_share_eur"] == Decimal("84000.00") + Decimal("16875.00")

    profiles = await database.list_person_profiles()
    assert [(p["fiscal_code"], p["name"], p["cadastral_value_share"]) for p in profiles] == [(CF_A, "", 100875.0)]
    assert (profiles[0]["unvalued"], profiles[0]["unknown_share"]) == (1, 1)
    assert profiles[0]["searched_at"]
    assert await database.list_subject_profiles("company") == []


PIVA = "01234567890"


@pytest.mark.usefixtures("fresh_db")
async def test_list_subject_profiles_company_has_no_birth_fields_and_rejects_unknown_kind():
    await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:p, 'ACME SRL'), (:cf, 'ROSSI MARIO')", p=PIVA, cf=CF_A)
    for code in (PIVA, CF_A):
        await _exec(
            "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
            "VALUES (:ref, now(), now(), true, :code)",
            ref=f"soggetto_E_{code}", code=code,
        )
    companies = await database.list_subject_profiles("company")
    assert [(c["fiscal_code"], c["name"]) for c in companies] == [(PIVA, "ACME SRL")]
    assert "birth_date" not in companies[0] and "gender" not in companies[0]
    persons = await database.list_subject_profiles("person")
    assert [(p["fiscal_code"], p["name"]) for p in persons] == [(CF_A, "ROSSI MARIO")]
    assert "birth_date" in persons[0]
    with pytest.raises(ValueError):
        await database.list_subject_profiles("organisation")


@pytest.mark.usefixtures("fresh_db")
async def test_index_json_is_revalidated_with_an_etag():
    import json

    from starlette.requests import Request

    from sister import web

    await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:cf, 'ROSSI MARIO')", cf=CF_A)
    await _exec(
        "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
        "VALUES ('soggetto_E_1', now(), now(), true, :cf)",
        cf=CF_A,
    )

    def request(headers=()):
        return Request({"type": "http", "method": "GET", "path": "/persons/index.json", "query_string": b"",
                        "headers": list(headers), "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})

    class User:
        email = "reader@example.com"

    first = await web._subject_index_json(request(), "person", User())
    payload = json.loads(first.body)
    assert (payload["total"], payload["last_page"]) == (1, 1)
    assert [(r["fiscal_code"], r["name"], r["favorite"]) for r in payload["data"]] == [(CF_A, "ROSSI MARIO", False)]
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache"

    again = await web._subject_index_json(request([(b"if-none-match", etag.encode())]), "person", User())
    assert again.status_code == 304 and again.body == b""
    changed = await web._subject_index_json(request([(b"if-none-match", b'"stale"')]), "person", User())
    assert changed.status_code == 200


def test_short_list_cuts_long_province_lists():
    from sister import web

    assert web._short_list("MI, RM") == "MI, RM"
    assert web._short_list("") == ""
    many = ", ".join(f"P{i:02d}" for i in range(20))
    assert web._short_list(many, limit=3) == "P00, P01, P02 … (+17)"


@pytest.mark.usefixtures("fresh_db")
async def test_subject_facts_for_the_profile_header():
    from datetime import date

    from sister import web

    born = date(1980, 1, 1)
    await _exec(
        "INSERT INTO subject (fiscal_code, name, last_name, first_name, gender, birth_date, birth_municipality_text, "
        "birth_province_code) VALUES (:cf, 'ROSSI MARIO', 'ROSSI', 'MARIO', 'M', :b, 'ROMA', 'RM')",
        cf=CF_A, b=born,
    )
    await _exec(
        "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
        "VALUES ('soggetto_E_1', now(), now(), true, :cf)",
        cf=CF_A,
    )
    facts = await web._subject_facts(CF_A, "person")
    today = date.today()
    assert facts["is_person"] and facts["name"] == "ROSSI MARIO"
    assert facts["birth_date"] == "01/01/1980" and facts["birth_place"] == "ROMA" and facts["birth_province"] == "RM"
    assert facts["age"] == today.year - 1980 - ((today.month, today.day) < (1, 1))
    assert facts["gender"] == "Maschio"
    assert facts["properties"] is None and facts["searches"] == 1
    assert await web._subject_facts("ZZZZZZ00Z00Z000Z", "person") is None


def _sql_request(query: str):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/persons/index.json", "query_string": query.encode(),
                    "headers": [], "scheme": "http", "server": ("localhost", 8025), "client": ("127.0.0.1", 1)})


async def _seed_people():
    """Three people with different holdings (subject_holding is written directly: the point here is paging/filtering)."""
    people = [
        ("RSSMRA80A01H501U", "ROSSI MARIO", "M", "1980-01-01", "ROMA", "RM", 10, 100000, "RM, MI"),
        ("VRDLGU75B12F205X", "VERDI LUIGI", "M", "1975-02-12", "MILANO", "MI", 3, 900000, "MI"),
        ("BNCLRA90C41H501Z", "BIANCHI LAURA", "F", "1990-03-01", "ROMA", "RM", None, None, None),
    ]
    for code, name, gender, born, place, prov, properties, value, provinces in people:
        await _exec(
            "INSERT INTO subject (fiscal_code, name, gender, birth_date, birth_municipality_text, birth_province_code) "
            "VALUES (:c, :n, :g, CAST(:b AS date), :pl, :pr)", c=code, n=name, g=gender, b=born, pl=place, pr=prov,
        )
        await _exec(
            "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
            "VALUES (:r, now(), now(), true, :c)", r=f"soggetto_E_{code}", c=code,
        )
        if properties is not None:
            await _exec(
                "INSERT INTO subject_holding (fiscal_code, properties, cadastral_value_share_eur, provinces) "
                "VALUES (:c, :p, :v, :pv)", c=code, p=properties, v=value, pv=provinces,
            )
    return [p[0] for p in people]


@pytest.mark.usefixtures("fresh_db")
async def test_query_subject_profiles_pages_sorts_and_filters_in_sql():
    rossi, verdi, bianchi = await _seed_people()

    rows, total = await database.query_subject_profiles("person", limit=2, offset=0, sort="cadastral_value_share")
    assert total == 3 and [r["fiscal_code"] for r in rows] == [verdi, rossi]  # richest first, no-holdings row last
    rows, total = await database.query_subject_profiles("person", limit=2, offset=2, sort="cadastral_value_share")
    assert total == 3 and [r["fiscal_code"] for r in rows] == [bianchi]
    rows, _ = await database.query_subject_profiles("person", sort="cadastral_value_share", direction="asc")
    assert [r["fiscal_code"] for r in rows] == [rossi, verdi, bianchi]  # empty values stay last when ascending too

    rows, total = await database.query_subject_profiles("person", q="roma")  # birthplace, every word must match
    assert {r["fiscal_code"] for r in rows} == {rossi, bianchi} and total == 2
    rows, _ = await database.query_subject_profiles("person", q="roma laura")
    assert [r["fiscal_code"] for r in rows] == [bianchi]
    rows, _ = await database.query_subject_profiles("person", filters={"properties": ">5"})
    assert [r["fiscal_code"] for r in rows] == [rossi]
    rows, _ = await database.query_subject_profiles("person", filters={"properties": "1-5", "provinces": "mi"})
    assert [r["fiscal_code"] for r in rows] == [verdi]
    rows, _ = await database.query_subject_profiles("person", filters={"gender": "F"})
    assert [r["fiscal_code"] for r in rows] == [bianchi]
    rows, _ = await database.query_subject_profiles("person", filters={"birth_date": "1975"})
    assert [r["fiscal_code"] for r in rows] == [verdi]
    rows, _ = await database.query_subject_profiles("person", filters={"birth_date": "01/03/1990"})
    assert [r["fiscal_code"] for r in rows] == [bianchi]
    rows, _ = await database.query_subject_profiles("person", filters={"properties": "not a number"})
    assert len(rows) == 3  # an unparsable numeric filter does not filter
    rows, _ = await database.query_subject_profiles("person", filters={"name": "100%"})  # wildcard characters are literal
    assert rows == []

    rows, _ = await database.query_subject_profiles("person", favorites=[verdi])
    assert [r["favorite"] for r in rows if r["fiscal_code"] == verdi] == [True]
    rows, _ = await database.query_subject_profiles("person", filters={"favorite_label": "si"}, favorites=[verdi])
    assert [r["fiscal_code"] for r in rows] == [verdi]
    rows, _ = await database.query_subject_profiles("person", filters={"favorite_label": "no"}, favorites=[verdi])
    assert verdi not in [r["fiscal_code"] for r in rows] and len(rows) == 2
    rows, _ = await database.query_subject_profiles("person", sort="favorite_label", favorites=[bianchi])
    assert rows[0]["fiscal_code"] == bianchi
    rows, _ = await database.query_subject_profiles("person", sort="definitely; DROP TABLE subject")
    assert len(rows) == 3  # an unknown sort field falls back to the default


@pytest.mark.usefixtures("fresh_db")
async def test_subject_overview_has_totals_widths_and_maxima():
    await _seed_people()
    overview = await database.subject_overview("person")
    assert (overview["subjects"], overview["with_visura"]) == (3, 0)
    assert overview["value_share"] == 1000000.0
    assert overview["widest"]["name"] == "BIANCHI LAURA" and overview["widest"]["provinces"] == "RM, MI"
    assert overview["widest"]["birth_place"] == "MILANO (MI)"
    assert overview["max"]["properties"] == 10 and overview["max"]["cadastral_value_share"] == 900000.0
    assert (await database.subject_overview("company"))["subjects"] == 0


@pytest.mark.usefixtures("fresh_db")
async def test_index_json_pages_and_csv_follows_the_same_query():
    import json

    from sister import web

    rossi, verdi, bianchi = await _seed_people()

    class User:
        email = "reader@example.com"

    first = await web._subject_index_json(_sql_request("page=1&size=2&sort=cadastral_value_share&dir=desc"), "person", User())
    payload = json.loads(first.body)
    assert (payload["total"], payload["last_page"], len(payload["data"])) == (3, 2, 2)
    second = await web._subject_index_json(_sql_request("page=2&size=2&sort=cadastral_value_share&dir=desc"), "person", User())
    assert [r["fiscal_code"] for r in json.loads(second.body)["data"]] == [bianchi]
    filtered = await web._subject_index_json(_sql_request("f_properties=%3E5&q=rossi&f_nonsense=1&size=9999"), "person", User())
    assert [r["fiscal_code"] for r in json.loads(filtered.body)["data"]] == [rossi]

    csv_response = await web._subject_index_csv(_sql_request("sort=cadastral_value_share&dir=desc&f_gender=M"), "person", User())
    text_csv = csv_response.body.decode("utf-8")
    lines = text_csv.lstrip("\ufeff").strip().splitlines()
    assert lines[0].startswith("Codice;Nome;Sesso")
    assert [line.split(";")[0] for line in lines[1:]] == [verdi, rossi]  # all matches, not one page
    assert csv_response.headers["content-disposition"].endswith('filename="persone.csv"')
    company_csv = await web._subject_index_csv(_sql_request(""), "company", User())
    assert "Data di nascita" not in company_csv.body.decode("utf-8")


@pytest.mark.usefixtures("fresh_db")
async def test_holdings_count_a_unit_listed_under_two_quotas_once_but_add_both_quotas():
    await _exec("INSERT INTO cadastral_category (code) VALUES ('A/2') ON CONFLICT DO NOTHING")
    await _exec("INSERT INTO request_type (code, label, description) VALUES ('soggetto', 'Soggetto', 'Soggetto') ON CONFLICT DO NOTHING")
    request_pk = (
        await _exec(
            "INSERT INTO request (request_ref, request_type, created_at, responded_at, success, subject_query) "
            "VALUES ('soggetto_E_1', 'soggetto', now(), now(), true, :cf) RETURNING id", cf=CF_A,
        )
    ).scalar_one()
    subject_pk = (await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:cf, 'ROSSI MARIO') RETURNING id", cf=CF_A)).scalar_one()
    kind_pk = (await _exec("INSERT INTO right_kind (right_type) VALUES (:t) RETURNING id", t="Proprieta'")).scalar_one()
    unit_pk = (await _exec("INSERT INTO cadastral_unit (cadastre_type, sheet, parcel, subunit) VALUES ('F', '7', '758', '1') RETURNING id")).scalar_one()
    # the same unit twice (an ownership entry each, 1/2 and 1/4); A/2 rendita 1000 -> 168.000 for the whole unit
    for index, (num, den) in enumerate([(1, 2), (1, 4)]):
        result_pk = (
            await _exec("INSERT INTO request_result (request_id, result_index) VALUES (:r, :i) RETURNING id", r=request_pk, i=index)
        ).scalar_one()
        await _exec(
            "INSERT INTO property (request_id, result_id, cadastral_unit_id, property_type, category, income_eur) "
            "VALUES (:r, :res, :u, 'building', 'A/2', 1000)", r=request_pk, res=result_pk, u=unit_pk,
        )
        right_pk = (
            await _exec(
                "INSERT INTO ownership_right (right_kind_id, share_numerator, share_denominator) VALUES (:k, :n, :d) RETURNING id",
                k=kind_pk, n=num, d=den,
            )
        ).scalar_one()
        await _exec(
            "INSERT INTO result_owner (request_id, result_id, owner_index, subject_id, right_id) VALUES (:r, :res, 0, :s, :right)",
            r=request_pk, res=result_pk, s=subject_pk, right=right_pk,
        )

    await database.refresh_subject_holdings([CF_A])
    row = (await _exec("SELECT * FROM subject_holding WHERE fiscal_code = :cf", cf=CF_A)).mappings().one()
    assert row["properties"] == 1  # one unit, two rows
    assert row["cadastral_income_eur"] == Decimal("1000.00")
    assert row["cadastral_value_eur"] == Decimal("168000.00")  # not 336.000
    assert row["cadastral_value_share_eur"] == Decimal("126000.00")  # 1/2 + 1/4 of the unit: both quotas count
