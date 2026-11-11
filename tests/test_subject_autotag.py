"""Tags derived from PVP roles, auctioned properties and the list of politicians (sister/subject_autotag.py)."""

import pytest
from sqlalchemy import text

from sister import database
from sister import subject_autotag as auto

ROSSI, VERDI, BIANCHI = "RSSMRA80A01H501U", "VRDLGU75B12F205X", "BNCLRA90C41H501Z"
ACME, BETA = "01234567890", "09876543210"


def test_kind_of_code():
    assert auto.kind_of(ROSSI) == "person" and auto.kind_of(ACME) == "company"
    assert auto.kind_of("1234567890A") is None and auto.kind_of("short") is None


def test_pvp_dsn_uses_selected_sister_connection(monkeypatch):
    from sqlalchemy.engine import make_url

    monkeypatch.delenv("PVP_DATABASE_DSN", raising=False)
    monkeypatch.setenv("DATABASE_DSN", "postgresql://old.invalid/wrong")
    monkeypatch.setattr(database, "DATABASE_DSN", "postgresql+psycopg://user@localhost/sister?options=-csearch_path%3Dsister")
    target = make_url(auto.pvp_dsn())
    assert target.host == "localhost" and target.username == "user"
    assert target.database == "pvp" and not target.query


def test_pvp_dsn_preserves_explicit_override(monkeypatch):
    monkeypatch.setenv("PVP_DATABASE_DSN", "postgresql://override.invalid/pvp")
    monkeypatch.setattr(database, "DATABASE_DSN", None)
    assert auto.pvp_dsn() == "postgresql://override.invalid/pvp"


def test_pvp_tags_add_one_tag_per_role_and_group_the_rare_ones():
    assert auto.pvp_tags(["DELEGATO"]) == {"PVP", "PVP: delegato"}
    assert auto.pvp_tags(["delegato", "CUSTODE", "CURATOR"]) == {"PVP", "PVP: delegato", "PVP: custode", "PVP: curatore"}
    assert auto.pvp_tags(["LIQUGIU", "LIQUIDAT", "COMMLIQUID", "GESTLIQU"]) == {"PVP", "PVP: liquidatore"}
    assert auto.pvp_tags(["COMMGIU", "COMISIO"]) == {"PVP", "PVP: commissario"}
    assert auto.pvp_tags(["SOGGSPV", "IVG", "", None]) == {"PVP", "PVP: altro"}
    assert all(len(tag) <= database.SUBJECT_TAG_MAX_LENGTH for tag in {*auto.PVP_ROLE_TAGS.values(), auto.TAG_PVP_OTHER})


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("INGALA ELISA MARIA", "Ingala Elisa Maria Lucia", True),  # a middle name more
        ("LOMBARDO GIUSEPPE GEREMIA", "Lombardo Giuseppe", True),  # ... or less
        ("IMPELLIZZERI ANTONIO", "Impellizzieri Antonio", True),  # one letter of spelling
        ("RUSSO D'AMICO MARIO", "Russo Damico Mario", True),
        ("ÈNEL MARIO", "ENEL MARIO", True),
        ("SEVERO ROSALIA", "Saverino Ersilia", False),  # two people with the same computed code
        ("ROSSI MARIO", "Verdi Luigi", False),
        ("", "Rossi Mario", None),
        ("ROSSI MARIO", "   ", None),
    ],
)
def test_names_agree(first, second, expected):
    assert auto.names_agree(first, second) is expected


POLITICIANS = f"""# Politici

## Regione Siciliana (ARS e Giunta)

<!-- sister-batch-table {{"command": "soggetto"}} -->
| id | codice_fiscale | intestatario | carica |
| --- | --- | --- | --- |
| ps-1 | {ROSSI} | Rossi Mario | Assessore |
| ps-2 | `{VERDI}` | Verdi Luigi | Consigliere |

## Comuni

<!-- sister-batch-table {{"command": "soggetto"}} -->
| id | codice_fiscale | intestatario | carica | meta_lista |
| --- | --- | --- | --- | --- |
| ps-3 | {ROSSI} | Rossi Mario | Sindaco | LISTA |
| ps-4 | NOTACODE | Nessuno | Sindaco | LISTA |

## Citta' metropolitane

| id | codice_fiscale | intestatario |
| --- | --- | --- |
| ps-5 | {BIANCHI} | Bianchi Laura |

## Persone senza codice fiscale calcolabile (luogo di nascita non riconosciuto)

| id | codice_fiscale | intestatario |
| --- | --- | --- |
| ps-6 | AAAAAA00A00A000A | Senza Luogo |
"""


def test_politicians_from_markdown_reads_levels_and_names():
    found = auto.politicians_from_markdown(POLITICIANS)
    assert set(found) == {ROSSI, VERDI, BIANCHI}  # not the bad code, not the section of people without a code
    assert found[ROSSI] == ("Rossi Mario", {"Politici", "Politici: Regione", "Politici: Comune"})  # listed twice
    assert found[VERDI][1] == {"Politici", "Politici: Regione"}
    assert found[BIANCHI] == ("Bianchi Laura", {"Politici", "Politici: Città metropolitana"})


def test_politicians_from_markdown_reads_a_file(tmp_path):
    path = tmp_path / "politici.md"
    path.write_text(POLITICIANS, encoding="utf-8")
    assert set(auto.politicians_from_markdown(path)) == {ROSSI, VERDI, BIANCHI}


def test_unknown_companies_have_a_search_but_no_name_and_no_property():
    companies = [
        {"fiscal_code": "1", "name": "", "properties": None, "searches": 2},  # SISTER does not know it
        {"fiscal_code": "2", "name": "ACME", "properties": None, "searches": 1},  # named by a registry record
        {"fiscal_code": "3", "name": "", "properties": 5, "searches": 1},  # not named yet, but it has properties
        {"fiscal_code": "4", "name": "", "properties": 0, "searches": 0},  # never searched
    ]
    assert auto.unknown_company_codes(companies) == {"1"}


async def _exec(sql: str, **params):
    async with database._get_session_factory()() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


async def _seed():
    for code, name in ((ROSSI, "ROSSI MARIO"), (VERDI, "VERDI LUIGI"), (BIANCHI, "BIANCHI LAURA"), (ACME, "ACME SRL"), (BETA, "")):
        await _exec("INSERT INTO subject (fiscal_code, name) VALUES (:c, :n)", c=code, n=name or code)
        await _exec(
            "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) VALUES (:r, now(), now(), true, :c)",
            r=f"soggetto_E_{code}", c=code,
        )


@pytest.mark.usefixtures("fresh_db")
async def test_auction_owners_are_the_owners_of_pvp_tagged_requests():
    await _seed()
    await _exec("INSERT INTO request_type (code, label, description) VALUES ('visura_storica', 'V', 'V') ON CONFLICT DO NOTHING")
    for ref, tag, owner in (("pvp_1", "PVP", VERDI), ("other_1", "ALTRO", ROSSI)):
        request_pk = (await _exec(
            "INSERT INTO request (request_ref, request_type, created_at, responded_at, success) VALUES (:r, 'visura_storica', now(), now(), true) RETURNING id", r=ref
        )).scalar_one()
        await _exec("INSERT INTO request_tag (request_id, tag) VALUES (:r, :t)", r=request_pk, t=tag)
        result_pk = (await _exec("INSERT INTO request_result (request_id, result_index) VALUES (:r, 0) RETURNING id", r=request_pk)).scalar_one()
        subject_pk = (await _exec("SELECT id FROM subject WHERE fiscal_code = :c", c=owner)).scalar_one()
        await _exec("INSERT INTO result_owner (request_id, result_id, owner_index, subject_id) VALUES (:r, :res, 0, :s)", r=request_pk, res=result_pk, s=subject_pk)
    assert await auto.auction_owner_codes([ROSSI, VERDI, BIANCHI]) == {VERDI}
    assert await auto.auction_owner_codes([ROSSI, BIANCHI]) == set()


@pytest.mark.usefixtures("fresh_db")
async def test_plan_apply_and_revert_only_touch_what_the_script_added(tmp_path, monkeypatch):
    await _seed()
    path = tmp_path / "politici.md"
    path.write_text(POLITICIANS.replace("Bianchi Laura", "Totalmente Diverso"), encoding="utf-8")  # a name that disagrees

    async def no_pvp(_codes):
        return {ROSSI: {"DELEGATO", "CUSTODE"}}

    monkeypatch.setattr(auto, "pvp_roles", no_pvp)
    plan, skipped = await auto.build_plan(path, only=("pvp", "politici", "catasto"))
    assert skipped == [{"code": BIANCHI, "archive": "BIANCHI LAURA", "list": "Totalmente Diverso"}]
    assert plan[("person", ROSSI)] == {"PVP", "PVP: delegato", "PVP: custode", "Politici", "Politici: Regione", "Politici: Comune"}
    assert plan[("person", VERDI)] == {"Politici", "Politici: Regione"}
    assert ("person", BIANCHI) not in plan
    assert plan[("company", BETA)] == {auto.TAG_NOT_IN_CADASTRE}  # no name, no property, one search
    assert ("company", ACME) not in plan  # named

    await database.set_subject_tags("person", VERDI, ["politici", "Mio tag"], "id:someone")  # already there, written by a user
    rows = await auto.new_rows(plan)
    assert {"kind": "person", "code": VERDI, "tag": "Politici"} not in rows  # the user's "politici" counts as present
    assert {"kind": "person", "code": VERDI, "tag": "Politici: Regione"} in rows

    added = await auto.apply_rows(rows)
    assert added == len(rows)
    assert await database.get_subject_tags("person", VERDI) == ["Mio tag", "politici", "Politici: Regione"]
    assert await auto.apply_rows(rows) == 0 and await auto.new_rows(plan) == []  # idempotent

    removed = await auto.revert_rows(rows)
    assert removed == len(rows)
    assert await database.get_subject_tags("person", VERDI) == ["Mio tag", "politici"]  # the user's tags stay
    assert await database.get_subject_tags("person", ROSSI) == []
