"""Which batch rows count as already answered (sister.answered) and how run-session prunes a plan with them."""

import json
import sys
from pathlib import Path

import pytest

from sister.answered import NATIONAL, Answered, parse_vs_name, query_kind

CF = "RSSMRA80A01H501U"
OTHER = "02471840997"


def _cf(code=CF, **params):
    return {"codice_fiscale": code, **params}


def test_an_elenco_answer_does_not_answer_a_document_request():
    answered = Answered.build(searches=[CF])
    assert answered.covers("soggetto-immobili", _cf())
    assert answered.covers("soggetto", _cf())
    assert not answered.covers("soggetto-documento", _cf())


def test_a_stored_document_does_not_answer_a_search():
    # a vs_ document is the visura of one office, not the national list of properties
    answered = Answered.build(files=[f"vs_att_{CF}_RM_CE_2026-10-06.p7m"])
    assert not answered.covers("soggetto-immobili", _cf())
    assert answered.covers("soggetto-documento", _cf())


def test_document_rows_match_view_and_province():
    answered = Answered.build(documents=[(CF, "analitica", ["MI", "NA"])])
    assert answered.covers("soggetto-documento", _cf())  # national row: any document of that view
    assert answered.covers("soggetto-documento", _cf(provincia="Milano"))
    assert answered.covers("soggetto-documento", _cf(provincia="NA"))
    assert not answered.covers("soggetto-documento", _cf(provincia="Roma"))
    assert not answered.covers("soggetto-documento", _cf(vista="sintetica"))


def test_storica_rows_are_never_answered():
    answered = Answered.build(searches=[CF], documents=[(CF, "analitica", [])])
    assert not answered.covers("soggetto-immobili", _cf(tipo_richiesta="storica"))
    assert not answered.covers("soggetto-documento", _cf(tipo_richiesta="storica"))
    assert answered.covers("soggetto-immobili", _cf(tipo_richiesta="attualita"))


def test_no_match_answers_documents_and_unknown_commands_are_kept():
    answered = Answered.build(searches=[CF], no_match=[CF])
    assert answered.covers("soggetto-documento", _cf())
    assert not answered.covers("search", _cf())
    assert not answered.covers(None, _cf())


def test_aliases_and_legacy_membership():
    assert query_kind("persona-giuridica") == "search"
    assert Answered.build(searches=["02471840997"]).covers("persona-giuridica", {"identificativo": "02471840997"})
    assert CF.lower() in Answered.build(searches=[CF])  # legacy codici lists: `code in answered`


@pytest.mark.parametrize(
    "name, expected",
    [
        (f"vs_att_{CF}_RA_H199_CF_2026-10-06.p7m", (CF, "analitica", "RA")),
        (f"vs_sin_{CF}_CE_2026-06-04.pdf", (CF, "sintetica", NATIONAL)),
        (f"vs_sto_{CF}_CT_CE_2026-06-04_al2022-06-04.pdf", (CF, "storica", "CT")),  # Catania, then the category
        (f"vs_att_{CF}_CT_2026-06-04_T265733_2026.p7m", (CF, "analitica", NATIONAL)),  # only the category (Terreni)
        ("vs_naz_X_CE_2026-06-04.pdf", None),
        ("el_imm_RM_1_2.pdf", None),
    ],
)
def test_parse_vs_name(name, expected):
    assert parse_vs_name(name) == expected


@pytest.fixture
def run_in_session():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sister" / "command_tools"))
    from sister.command_tools import run_in_session

    return run_in_session


def _plan(tmp_path, queries, defaults=None):
    (tmp_path / "batch.json").write_text(json.dumps({"defaults": defaults or {}, "queries": queries}))


def _ids(tmp_path):
    return [q["id"] for q in json.loads((tmp_path / "batch.json").read_text())["queries"]]


def test_prune_plan_keeps_document_rows_of_searched_codes_and_backs_up(run_in_session, tmp_path):
    _plan(
        tmp_path,
        [
            {"id": "s", "command": "soggetto-immobili", "params": {"codice_fiscale": CF}},
            {"id": "d", "command": "soggetto-documento", "params": {"codice_fiscale": CF}},
            {"id": "o", "command": "soggetto-immobili", "params": {"codice_fiscale": OTHER}},
        ],
    )
    assert run_in_session.prune_plan(tmp_path, Answered.build(searches=[CF])) == (2, 1, 0)
    assert _ids(tmp_path) == ["d", "o"]
    backups = list((tmp_path / "backups").glob("batch.*.json"))
    assert len(backups) == 1 and len(json.loads(backups[0].read_text())["queries"]) == 3


def test_prune_plan_reads_defaults_and_dry_run_writes_nothing(run_in_session, tmp_path):
    defaults = {"command": "soggetto-immobili", "params": {"tipo_richiesta": "storica"}}
    _plan(tmp_path, [{"id": "a", "params": {"codice_fiscale": CF}}], defaults)
    before = (tmp_path / "batch.json").read_text()

    assert run_in_session.prune_plan(tmp_path, Answered.build(searches=[CF]), write=False) == (1, 0, 0)
    assert (tmp_path / "batch.json").read_text() == before and not (tmp_path / "backups").exists()

    _plan(tmp_path, [{"id": "a", "params": {"codice_fiscale": CF}}], {"command": "soggetto-immobili"})
    assert run_in_session.prune_plan(tmp_path, Answered.build(searches=[CF]), write=False) == (0, 1, 0)
