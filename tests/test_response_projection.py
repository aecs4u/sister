"""Response JSON → structured tables: every owner stays tied to its own property (via ``result_id``)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from sister import database

FIXTURES = Path(__file__).parent / "fixtures" / "single_step"


def _data(query: str, outcome: str = "success") -> dict:
    return json.loads((FIXTURES / query / f"{outcome}.json").read_text(encoding="utf-8"))["response"]["data"]


def _two_property_visura() -> dict:
    owner = lambda name, cf, quota: {  # noqa: E731
        "Nominativo o denominazione": f"{name} a ROMA (RM) il 01/01/1970",
        "Codice fiscale": cf,
        "Titolarità": "Proprieta'",
        "Quota": quota,
    }
    return {
        "immobili": [
            {"Foglio": "RA/103", "Particella": "1714", "Sub": "2", "Rendita": "R.Euro:557,77"},
            {"Foglio": "RA/103", "Particella": "1714", "Sub": "3", "Rendita": "Euro: 100,00"},
        ],
        "results": [
            {"result_index": 1, "immobile": {}, "intestati": [owner("ROSSI MARIO", "RSSMRA70A01H501Z", "1/2")]},
            {"result_index": 2, "immobile": {}, "intestati": [owner("BIANCHI ANNA", "BNCNNA70A41H501Z", "1/1")]},
        ],
        "intestati": [],  # run_visura also returns the flat concatenation: it must not be stored twice
    }


# --- pure projection --------------------------------------------------------------------------------------


def test_project_response_ties_each_owner_to_its_property():
    data = _two_property_visura()
    data["intestati"] = [o for r in data["results"] for o in r["intestati"]]

    properties, unlinked, results = database._project_response("r1", "F", data)

    assert [p.result_index for p in properties] == [1, 2]
    assert [[s["fiscal_code"] for s, _ in p.owners] for p in properties] == [["RSSMRA70A01H501Z"], ["BNCNNA70A41H501Z"]]
    assert unlinked == []
    assert [r["result_index"] for r in results] == [1, 2]


def test_project_response_splits_foglio_and_cleans_amounts():
    (first, _), _, _ = database._project_response("r1", "F", _two_property_visura())

    assert (first.location["section"], first.location["sheet"], first.location["subunit"]) == ("RA", "103", "2")
    assert first.prop["income"] == "557,77"


def test_project_response_of_single_property_adopts_top_level_owners():
    data = _data("intestati")
    data.pop("results")
    (only,), unlinked, results = database._project_response("r1", "F", data)

    assert len(only.owners) == 2 and unlinked == []
    assert only.result_index == 1 and results == [{"result_index": 1, "visura_present": False}]


def test_project_response_keeps_top_level_owners_unlinked_for_several_properties():
    data = {"immobili": [{"Foglio": "1", "Particella": "1"}, {"Foglio": "1", "Particella": "2"}],
            "intestati": [{"Nominativo": "ROSSI", "Codice fiscale": "RSSMRA70A01H501Z"}]}
    properties, unlinked, results = database._project_response("r1", "F", data)

    assert all(not p.owners for p in properties) and len(unlinked) == 1 and results == []


def test_project_response_of_subject_query_makes_the_subject_the_owner_of_each_row():
    properties, _, results = database._project_response("s1", "E", _data("soggetto"))

    (row,) = properties
    assert [(s["fiscal_code"], r["right_type"], r["ownership_share"]) for s, r in row.owners] == [
        ("TSTUSR00A01H501X", "Proprieta'", "1/2")
    ]
    # the combined cells of the subject list are split and the radio supplies what the table omits
    assert (row.location["section"], row.location["sheet"], row.location["subunit"]) == ("RA", "103", "2")
    assert row.location["municipality"] == "RAVENNA" and row.location["province"] == "RAVENNA"
    assert row.prop["address"] == "VIA EL ALAMEIN n. 2 Piano T-1"
    assert (row.prop["census_zone"], row.prop["category"], row.prop["income"]) == ("1", "A/4", "557,77")
    assert results == [{"result_index": 1, "visura_present": False}]


def test_project_response_ignores_owner_junk_and_duplicates():
    owner = {"Nominativo": "ROSSI", "Codice fiscale": "RSSMRA70A01H501Z", "Quota": "1/1"}
    data = {"immobili": [{"Foglio": "1", "Particella": "1"}],
            "results": [{"result_index": 1, "intestati": [owner, dict(owner), "junk"]}]}
    (only,), _, _ = database._project_response("r1", "F", data)

    assert len(only.owners) == 1


# --- database --------------------------------------------------------------------------------------------


async def _save(request_id: str, data: dict, tipo: str = "F") -> None:
    await database.save_request(
        request_id=request_id, request_type="visura", tipo_catasto=tipo, provincia="Ravenna", comune="RAVENNA",
        foglio="103", particella="1714",
    )
    await database.save_response(request_id, True, tipo, data=data)


@pytest.mark.usefixtures("fresh_db")
async def test_owners_and_properties_share_a_result_id():
    await _save("v1", _two_property_visura())

    properties = await database.get_db_properties_for_response("v1")
    owners = await database.get_db_owners_for_response("v1")

    by_result = {o["result_id"]: o for o in owners}
    assert len(properties) == 2 and len(owners) == 2 and None not in by_result
    assert {p["result_id"] for p in properties} == set(by_result)
    sub3 = next(p for p in properties if p["subunit"] == "3")
    assert by_result[sub3["result_id"]]["fiscal_code"] == "BNCNNA70A41H501Z"
    assert by_result[sub3["result_id"]]["gender"] == "F"
    assert {o["owner_index"] for o in owners} == {1}


@pytest.mark.usefixtures("fresh_db")
async def test_owner_property_view_input_is_exact_for_a_visura_response():
    """``v_sister_owner_property_links`` joins on result_id first: owners must not meet every property."""
    await _save("v1", _two_property_visura())

    async with database._get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM visura_owners o JOIN visura_properties p ON p.result_id = o.result_id "
                    "WHERE o.response_id = 'v1'"
                )
            )
        ).scalar_one()
    assert rows == 2  # not 2 owners x 2 properties


@pytest.mark.usefixtures("fresh_db")
async def test_subject_gets_birth_data_and_a_later_bare_row_does_not_erase_it():
    await _save("v1", _two_property_visura())
    await _save("v2", {"immobili": [{"Foglio": "9", "Particella": "1", "Codice fiscale": "RSSMRA70A01H501Z"}]})

    async with database._get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT s.display_name, s.date_of_birth, s.gender, g.municipality FROM cadastral_subjects s "
                    "LEFT JOIN geographic_places g ON g.id = s.birth_place_id WHERE s.fiscal_code = 'RSSMRA70A01H501Z'"
                )
            )
        ).one()
    assert tuple(row) == ("ROSSI MARIO", "01/01/1970", "M", "ROMA")


@pytest.mark.usefixtures("fresh_db")
async def test_subject_query_links_the_queried_subject_to_each_property():
    await _save("s1", _data("soggetto"), tipo="E")

    (owner,) = await database.get_db_owners_for_response("s1")
    (prop,) = await database.get_db_properties_for_response("s1")

    assert owner["fiscal_code"] == "TSTUSR00A01H501X" and owner["ownership_share"] == "1/2"
    assert owner["result_id"] == prop["result_id"] is not None
    assert (prop["section"], prop["sheet"], prop["municipality"]) == ("RA", "103", "RAVENNA")


@pytest.mark.usefixtures("fresh_db")
async def test_backfill_relinks_old_rows_and_ties_documents_to_their_response():
    from sister.db_models import VisuraDocument

    await _save("v1", {**_two_property_visura(), "downloaded_pdfs": [{"filename": "vi_att_RA_FG103_PT1714_SUB2.p7m"}]})
    async with database._get_session_factory()() as session:
        session.add(VisuraDocument(document_type="visura", file_format="P7M", filename="vi_att_RA_FG103_PT1714_SUB2.p7m"))
        # the state before the fix: owners and properties without a result
        await session.execute(text("UPDATE visura_owners SET result_id = NULL, owner_index = NULL"))
        await session.execute(text("UPDATE visura_properties SET result_id = NULL"))
        await session.commit()

    stats = await database.backfill_projections()

    assert stats["responses"] == 1
    owners = await database.get_db_owners_for_response("v1")
    assert all(o["result_id"] is not None and o["owner_index"] == 1 for o in owners)
    async with database._get_session_factory()() as session:
        linked = (await session.execute(text("SELECT response_id FROM visura_documents"))).scalar_one()
    assert linked == "v1"


def test_terreni_row_uses_the_portal_columns_for_area_and_incomes():
    row = {"Foglio": "101", "Particella": "2", "Qualità": "ENTE URBANO", "Classe": "", "ha": "1", "are": "0", "ca": "24",
           "Reddito dominicale": "Euro: 12,34", "Reddito agrario": "9,87"}

    prop, loc, _ = database._property_row("r1", "T", row)

    assert prop["area"] == "10024"  # 1 ha + 0 are + 24 ca, in square metres
    assert (prop["dominical_income"], prop["agricultural_income"]) == ("12,34", "9,87")
    assert prop["quality"] == "ENTE URBANO" and prop["property_type"] == "land"


def test_each_row_of_a_mixed_response_keeps_its_own_catasto():
    data = {"immobili": [{"Foglio": "1", "Particella": "1", "_tipo_catasto": "F"},
                         {"Foglio": "1", "Particella": "2", "_tipo_catasto": "T"},
                         {"Foglio": "1", "Particella": "3", "Catasto": "F"}]}

    props, _, _ = database._project_response("r1", "E", data)

    assert [(p.prop["property_type"], p.location["cadastre_type"]) for p in props] == [
        ("building", "F"), ("land", "T"), ("building", "F")]
