"""Ownership history: identical answers are not stored twice, changes are appended as new periods."""

import pytest
from sqlalchemy import text

from sister import database
from sister.ownership_history import content_hash, subject_search_is_complete

CF = "RSSMRA80A01H501U"
CO = "BNCNNA70A41H501Z"


def _owner(cf, right="Proprieta'", quota="1/1"):
    return {"Nominativo o denominazione": f"X {cf[:3]} nato a ROMA (RM) il 01/01/1980", "Codice fiscale": cf, "Titolarità": right, "Quota": quota}


def _visura(owners, rendita="Euro: 500,00"):
    """A property search with the full Intestati list of one unit (Ravenna 103/1714 sub 2)."""
    return {
        "immobili": [{"Foglio": "RA/103", "Particella": "1714", "Sub": "2", "Rendita": rendita, "Classamento": "Zona 1 Cat.A/2"}],
        "results": [{"result_index": 1, "immobile": {}, "intestati": owners}],
        "intestati": [],
        "page_visits": [{"step": "x", "timestamp": "2026-11-05T10:00:00"}],
    }


async def _save(request_id, data, cache="k1"):
    await database.save_request(
        request_id=request_id, request_type="visura", tipo_catasto="F", provincia="Ravenna", comune="RAVENNA",
        foglio="103", particella="1714", subalterno="2", cache_key=cache,
    )
    await database.save_response(request_id, True, "F", data=data, export=False)


async def _rows(sql, **params):
    async with database._get_session_factory()() as session:
        return [tuple(r) for r in (await session.execute(text(sql), params)).all()]


def test_the_content_hash_ignores_what_changes_with_every_run():
    a, b = _visura([_owner(CF)]), _visura([_owner(CF)])
    b["page_visits"] = [{"step": "y", "timestamp": "2026-11-06T09:00:00"}]
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(_visura([_owner(CO)]))
    assert content_hash(None) is None


def test_a_subject_search_is_complete_only_when_it_lists_everything():
    base = {"soggetto": CF, "province": [], "immobili": []}
    assert subject_search_is_complete(base, "E")
    assert not subject_search_is_complete(base, "T")
    assert not subject_search_is_complete({**base, "incompleta": True}, "E")
    assert not subject_search_is_complete({**base, "omonimi_totale": 3, "omonimi_elaborati": 2, "omonimi": [{"indice": 1}]}, "E")
    assert subject_search_is_complete({**base, "omonimi_totale": 1, "omonimi_elaborati": 1, "omonimi": [{"indice": 1}]}, "E")
    assert not subject_search_is_complete({"immobili": []}, "E")  # not a subject search


@pytest.mark.usefixtures("fresh_db")
async def test_an_identical_answer_is_not_stored_again_and_extends_the_periods():
    await _save("v1", _visura([_owner(CF)]))
    await _save("v2", _visura([_owner(CF)]))  # same content, another run
    requests = await _rows("SELECT request_ref, same_as_request_id IS NOT NULL, (SELECT count(*) FROM property p WHERE p.request_id = r.id) FROM request r ORDER BY id")
    assert requests == [("v1", False, 1), ("v2", True, 0)]
    history = await _rows("SELECT seen_count, ended_at IS NULL FROM ownership_history")
    assert history == [(2, True)]
    states = await _rows("SELECT seen_count FROM property_state_history")
    assert states == [(2,)]
    # the readers still find the content through the later request
    props = await database.get_db_properties_for_response("v2")
    assert len(props) == 1
    assert (await database.get_response("v2"))["data"]["immobili"][0]["Particella"] == "1714"


@pytest.mark.usefixtures("fresh_db")
async def test_a_change_of_owner_closes_the_old_period_and_opens_a_new_one():
    await _save("v1", _visura([_owner(CF)]))
    await _save("v2", _visura([_owner(CO)]))  # the unit was sold
    await _save("v3", _visura([_owner(CO)]))  # seen again: no new row
    history = await _rows(
        "SELECT s.fiscal_code, h.seen_count, h.ended_at IS NOT NULL FROM ownership_history h JOIN subject s ON s.id = h.subject_id ORDER BY h.id"
    )
    assert history == [(CF, 1, True), (CO, 2, False)]
    stored = await _rows("SELECT count(*) FROM request WHERE same_as_request_id IS NULL")
    assert stored == [(2,)]  # v3 repeated v2: only v1 and v2 hold content


@pytest.mark.usefixtures("fresh_db")
async def test_a_change_of_share_and_of_rendita_are_new_periods():
    await _save("v1", _visura([_owner(CF, quota="1/2"), _owner(CO, quota="1/2")]))
    await _save("v2", _visura([_owner(CF, quota="1/1")], rendita="Euro: 600,00"))
    history = await _rows(
        "SELECT s.fiscal_code, o.share_numerator, o.share_denominator, h.ended_at IS NULL FROM ownership_history h "
        "JOIN subject s ON s.id = h.subject_id JOIN ownership_right o ON o.id = h.right_id ORDER BY h.id"
    )
    assert sorted(history) == sorted([(CF, 1, 2, False), (CO, 1, 2, False), (CF, 1, 1, True)])
    states = await _rows("SELECT income_eur, ended_at IS NULL FROM property_state_history ORDER BY id")
    assert [(str(i), open_) for i, open_ in states] == [("500.00", False), ("600.00", True)]


@pytest.mark.usefixtures("fresh_db")
async def test_a_complete_subject_search_closes_what_the_subject_no_longer_owns():
    def search(units):
        return {
            "soggetto": CF, "ambito": "NAZIONALE", "province": [{"provincia": "RAVENNA", "codice": "RA", "fabbricati": len(units), "terreni": 0}],
            "immobili": [{"Catasto": "F", "Titolarità": "Proprieta' per 1/1", "Foglio": "RA/103", "Particella": p, "Sub": "1",
                          "visImmSel": f"1#1#F#RA/103#{p}#H199##1# #RAVENNA", "provincia": "RA"} for p in units],
        }

    async def save(ref, data, scope="NAZIONALE"):
        await database.save_request(request_id=ref, request_type="soggetto", tipo_catasto="E", provincia="NAZIONALE", comune="-",
                                    foglio="-", particella="-", cache_key="s1")
        await database.save_response(ref, True, "E", data={**data, "ambito": scope}, export=False)

    await save("s1", search(["1", "2"]))
    await save("s2", search(["2"]))  # unit 1 no longer listed: sold
    rows = await _rows("SELECT u.parcel, h.ended_at IS NULL FROM ownership_history h JOIN cadastral_unit u ON u.id = h.cadastral_unit_id ORDER BY u.parcel")
    assert rows == [("1", False), ("2", True)]
    await save("s3", search([]), scope="RA")  # a provincial search proves nothing about the rest
    assert await _rows("SELECT count(*) FROM ownership_history WHERE ended_at IS NULL") == [(1,)]
