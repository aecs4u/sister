"""Reference data of the flat schema: comune, cadastral unit, subject, right, lookups (``sister/reference.py``).

Run against the throwaway PostgreSQL schema of ``tests/pg_isolation.py`` (built from the migration's own DDL).
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlmodel import select

from sister import database, reference
from sister.database import _flatten_json
from sister.db_models import IstatMunicipality, Subject

pytestmark = pytest.mark.usefixtures("fresh_db")


async def _session():
    return database._get_session_factory()()


async def _rows(session, sql: str, **params) -> list[tuple]:
    return [tuple(row) for row in (await session.execute(text(sql), params)).all()]


# --- pure helpers -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Ravenna", "RAVENNA"), ("  nato in Brasile ", "BRASILE"), ("NATA A Roma", "ROMA"), ("", None), (None, None)],
)
def test_place_name(value, expected):
    assert reference.place_name(value) == expected


def test_belfiore_code_and_fiscal_code_key():
    assert reference.belfiore_code("h199") == "H199"
    assert reference.belfiore_code("HH199") is None and reference.belfiore_code("") is None
    assert reference.fiscal_code_key(" rss mra 70a01 h501z ") == "RSSMRA70A01H501Z"
    assert reference.fiscal_code_key("") is None
    assert reference.cadastre_type_code("fabbricati") == "F" and reference.cadastre_type_code("x") is None


# --- ISTAT municipality -----------------------------------------------------------------------------------


async def test_comune_resolves_the_shared_istat_numeric_code_by_name_and_province():
    async with await _session() as session:
        first = await reference.get_or_create_comune(session, "Ravenna", "RA")
        again = await reference.get_or_create_comune(session, "RAVENNA", "Ravenna")  # province given as a name
        assert first == again
        municipality = await session.get(IstatMunicipality, first)
        assert municipality is not None
        assert municipality.numeric_code == 39014
        assert municipality.province_abbreviation == "RA"
        assert municipality.italian_name == "Ravenna"


async def test_comune_without_province_resolves_only_a_globally_unique_name():
    async with await _session() as session:
        known = await reference.get_or_create_comune(session, "Palermo", "PA")
        assert await reference.get_or_create_comune(session, "PALERMO", None) == known


async def test_comune_homonyms_in_different_provinces_stay_apart():
    async with await _session() as session:
        a = await reference.get_or_create_comune(session, "Samone", "TN")
        b = await reference.get_or_create_comune(session, "Samone", "TO")
        assert a != b
        # ISTAT has the same name in both provinces, so a blank province is ambiguous.
        unknown = await reference.get_or_create_comune(session, "Samone", None)
        assert unknown not in (a, b)
        assert unknown is None


async def test_belfiore_code_resolves_the_istat_numeric_code_even_when_the_name_differs():
    async with await _session() as session:
        bare = await reference.get_or_create_comune(session, "Roma", "RM")
        coded = await reference.get_or_create_comune(session, "Roma", "RM", "H501")
        assert coded == bare
        municipality = await session.get(IstatMunicipality, bare)
        assert municipality.cadastral_code == "H501"
        # The code alone finds the canonical ISTAT row, regardless of the supplied name.
        assert await reference.get_or_create_comune(session, None, None, "H501") == bare
        assert await reference.get_or_create_comune(session, "ROMA CAPITALE", None, "H501") == bare
        assert await reference.get_or_create_comune(session, None, None, None) is None


async def test_foreign_birthplace_prefix_is_dropped():
    async with await _session() as session:
        comune = await reference.get_or_create_comune(session, "NATO IN BRASILE", "EE")
        assert comune is None  # foreign birthplaces have no ISTAT municipality
        sid = await reference.get_or_create_subject(session, display_name="TEST PERSON", birth_municipality="NATO IN BRASILE", birth_province="EE")
        subject = await session.get(Subject, sid)
        assert subject.birth_comune_id is None
        assert subject.birth_municipality_text == "BRASILE"
        assert subject.birth_province_code == "EE"


# --- cadastral unit ---------------------------------------------------------------------------------------


async def test_cadastral_unit_is_created_once_and_absent_parts_are_null():
    async with await _session() as session:
        args = dict(cadastre_type="F", province="Ravenna", municipality="Ravenna", municipality_code="H199",
                    section="ra", sheet="103", parcel="1714", subunit="")
        first = await reference.get_or_create_cadastral_unit(session, **args)
        assert await reference.get_or_create_cadastral_unit(session, **args) == first
        assert await _rows(session, "SELECT cadastre_type, section, sheet, parcel, subunit FROM cadastral_unit") == [
            ("F", "RA", "103", "1714", None)
        ]
        other = await reference.get_or_create_cadastral_unit(session, **{**args, "subunit": "3"})
        assert other != first


async def test_cadastral_unit_with_nothing_but_a_type_is_still_one_row():
    async with await _session() as session:
        a = await reference.get_or_create_cadastral_unit(session, cadastre_type="E")
        assert await reference.get_or_create_cadastral_unit(session, cadastre_type="e") == a
        assert await reference.get_or_create_cadastral_unit(session, cadastre_type="bogus") != a  # type dropped, not E


# --- subject ----------------------------------------------------------------------------------------------


async def test_subject_by_fiscal_code_is_completed_but_never_overwritten():
    async with await _session() as session:
        bare = await reference.get_or_create_subject(session, fiscal_code="rssmra70a01h501z")
        rich = await reference.get_or_create_subject(
            session, fiscal_code="RSSMRA70A01H501Z", display_name="ROSSI MARIO", last_name="ROSSI", first_name="MARIO",
            gender="m", date_of_birth="01/01/1970", birth_municipality="Roma", birth_province="RM", subject_type="person",
        )
        assert rich == bare
        later = await reference.get_or_create_subject(session, fiscal_code="RSSMRA70A01H501Z", display_name="SOMEONE ELSE")
        assert later == bare
        row = (
            await session.execute(
                select(
                    Subject.name,
                    Subject.gender,
                    Subject.birth_date,
                    IstatMunicipality.italian_name,
                    Subject.subject_type,
                )
                .join(IstatMunicipality, Subject.birth_comune_id == IstatMunicipality.numeric_code)
            )
        ).one()
        assert tuple(row) == ("ROSSI MARIO", "M", date(1970, 1, 1), "Roma", "person")


async def test_subject_without_fiscal_code_is_matched_on_name_and_birth_date():
    async with await _session() as session:
        a = await reference.get_or_create_subject(session, display_name="Bianchi Anna", date_of_birth="02/03/1950")
        assert await reference.get_or_create_subject(session, display_name="BIANCHI ANNA", date_of_birth="02/03/1950") == a
        assert await reference.get_or_create_subject(session, display_name="BIANCHI ANNA", date_of_birth="03/03/1950") != a
        assert await reference.get_or_create_subject(session, display_name="BIANCHI ANNA") != a  # no birth date: another row


async def test_subject_junk_values_become_null_not_errors():
    async with await _session() as session:
        sid = await reference.get_or_create_subject(session, fiscal_code="  ", display_name="X", gender="?", subject_type="alien",
                                                    date_of_birth="31/02/1970")
        assert (await _rows(session, "SELECT fiscal_code, gender, subject_type, birth_date FROM subject WHERE id = :i", i=sid)) == [
            (None, None, None, None)
        ]


# --- rights -----------------------------------------------------------------------------------------------


async def test_equal_shares_are_one_right():
    async with await _session() as session:
        a = await reference.get_or_create_right(session, right_type="Proprieta'", right_code="1", ownership_share="500/1000")
        b = await reference.get_or_create_right(session, right_type="Proprieta'", right_code="1", ownership_share="1/2")
        assert a == b
        assert await _rows(session, "SELECT share_numerator, share_denominator FROM ownership_right") == [(1, 2)]
        c = await reference.get_or_create_right(session, right_type="Proprieta'", right_code="1", ownership_share="1/3")
        assert c != a
        assert (await _rows(session, "SELECT count(*) FROM right_kind"))[0][0] == 1  # one kind, two shares


async def test_right_period_is_part_of_the_right_and_a_reversed_one_is_kept():
    async with await _session() as session:
        open_ended = await reference.get_or_create_right(session, right_type="Usufrutto", ownership_share="1/1", start_date="01/01/2000")
        closed = await reference.get_or_create_right(
            session, right_type="Usufrutto", ownership_share="1/1", start_date="01/01/2000", end_date="31/12/2010"
        )
        assert open_ended != closed
        reversed_period = await reference.get_or_create_right(
            session, right_type="Usufrutto", ownership_share="1/1", start_date="01/01/2010", end_date="01/01/2000"
        )
        assert reversed_period not in (open_ended, closed)  # portal data is kept, not refused


async def test_right_with_nothing_known_is_still_a_right():
    async with await _session() as session:
        a = await reference.get_or_create_right(session)
        assert await reference.get_or_create_right(session, ownership_share="not a share") == a  # unparsable share = no share


# --- lookups and offices ----------------------------------------------------------------------------------


async def test_lookup_rows_are_created_once_and_descriptions_complete_later():
    async with await _session() as session:
        assert await reference.ensure_lookup(session, "cadastral_category", "A/2") == "A/2"
        assert await reference.ensure_lookup(session, "cadastral_category", "A/2", "ABITAZIONI DI TIPO CIVILE") == "A/2"
        assert await reference.ensure_lookup(session, "cadastral_category", "A/2", "something else") == "A/2"
        assert await _rows(session, "SELECT code, description FROM cadastral_category") == [("A/2", "ABITAZIONI DI TIPO CIVILE")]
        assert await reference.ensure_lookup(session, "land_quality", "  ") is None
        with pytest.raises(ValueError):
            await reference.ensure_lookup(session, "municipalities; DROP TABLE municipalities", "x")


async def test_office_is_created_once():
    async with await _session() as session:
        a = await reference.get_or_create_office(session, "Direzione Provinciale di Ravenna")
        assert await reference.get_or_create_office(session, " Direzione Provinciale di Ravenna ") == a
        assert await reference.get_or_create_office(session, "") is None


# --- activity log flattening ------------------------------------------------------------------------------


def test_flatten_json_gives_one_row_per_leaf():
    assert _flatten_json({"a": 1, "b": {"c": [True, None, "x"]}, "d": {}, "e": []}) == [
        ("a", "1"),
        ("b.c.0", "true"),
        ("b.c.1", None),
        ("b.c.2", "x"),
        ("d", "{}"),
        ("e", "[]"),
    ]
    assert _flatten_json("bare") == [("$", "bare")]


async def test_record_activity_stores_leaves_and_the_archived_payload():
    activity_id = await database.record_activity(
        "documents.import", actor="tester", params={"force": True, "files": ["a", "b"]}, result={"indexed": 3}
    )
    assert activity_id is not None
    async with await _session() as session:
        leaves = await _rows(
            session, "SELECT section, path, value FROM activity_log_attribute WHERE activity_id = :i ORDER BY section, path", i=activity_id
        )
        assert leaves == [("params", "files.0", "a"), ("params", "files.1", "b"), ("params", "force", "true"), ("result", "indexed", "3")]
    (recent,) = await database.get_recent_activity(limit=1)
    assert recent["params"] == {"force": True, "files": ["a", "b"]} and recent["result"] == {"indexed": 3}
    assert recent["action"] == "documents.import"
