"""Persona giuridica: a code with many registry records is read record by record and appended into one result."""

import asyncio

import types

import pytest

from sister import batch, utils
from tests.test_batch import FakeClient, make, run

IDENT = "80208450587"
RECORDS_PAGE = [
    {"Denominazione": f"ENTE {n}", "Sede": f"SEDE {n} (XX)", "Codice Fiscale": IDENT, "omonimoSelezionato": f"{n}#0#ENTE {n}#SEDE {n} (XX)#{IDENT}"}
    for n in (1, 2, 3, 4)
]


def test_records_are_read_from_the_homonym_table():
    records = utils._omonimi_records([*RECORDS_PAGE, {"Provincia": "ROMA"}, "x"])
    assert [r["denominazione"] for r in records] == ["ENTE 1", "ENTE 2", "ENTE 3", "ENTE 4"]
    assert records[0] == {"denominazione": "ENTE 1", "sede": "SEDE 1 (XX)", "codice_fiscale": IDENT}
    assert utils._omonimi_records([{"Provincia": "ROMA"}]) == []  # a province table is not a records page


@pytest.fixture
def portal(monkeypatch):
    """The two SISTER steps, scripted: the search that stops on the records page, and one record's properties."""
    calls = []
    state = {"search": {"soggetto": IDENT, "immobili": RECORDS_PAGE, "total_results": 4}, "fail": {}}

    async def search(page, identificativo, **kwargs):
        return state["search"]

    async def immobili(page, identificativo, tipo_catasto="E", provincia=None, con_intestati=False, azienda=False, omonimo_index=0):
        calls.append(omonimo_index)
        if omonimo_index in state["fail"]:
            raise Exception(state["fail"][omonimo_index])
        return {
            "soggetto": identificativo,
            "province": [{"provincia": "ROMA", "codice": "058", "fabbricati": omonimo_index + 1, "terreni": 1}],
            "immobili": [{"Foglio": "1", "Particella": str(omonimo_index), "visImmSel": f"v{omonimo_index}"}] * (omonimo_index + 1),
            "total_results": omonimo_index + 1,
            "page_visits": [{"step": f"record{omonimo_index}"}],
        }

    monkeypatch.setattr(utils, "_search_persona_giuridica", search)
    monkeypatch.setattr(utils, "run_soggetto_immobili", immobili)
    state["calls"] = calls
    return state


def go(fields=None):
    async def main():
        with utils.form_context("azienda", fields or {}):
            return await utils.run_visura_persona_giuridica(None, IDENT)

    return asyncio.run(main())


def test_every_record_is_opened_and_appended_into_one_result(portal):
    out = go()
    assert portal["calls"] == [0, 1, 2, 3]
    assert out["omonimi_totale"] == 4 and out["omonimi_elaborati"] == 4 and out["incompleta"] is False
    assert out["omonimo_prossimo"] is None and out["total_results"] == 1 + 2 + 3 + 4
    # each property keeps the record it was found under
    assert {(r["omonimo"], r["omonimo_denominazione"]) for r in out["immobili"]} == {(n, f"ENTE {n}") for n in (1, 2, 3, 4)}
    assert [s["immobili"] for s in out["omonimi"]] == [1, 2, 3, 4]
    assert out["province"] == [{"provincia": "ROMA", "codice": "058", "fabbricati": 10, "terreni": 4}]  # counts summed
    assert len(out["page_visits"]) == 4 and "error" not in out


def test_max_omonimi_and_omonimo_da_split_a_code_into_chunks(portal):
    first = go({"max_omonimi": "2"})
    assert portal["calls"] == [0, 1] and first["incompleta"] is True and first["omonimo_prossimo"] == 3
    portal["calls"].clear()
    rest = go({"omonimo_da": "3", "max_omonimi": "2"})
    assert portal["calls"] == [2, 3] and rest["incompleta"] is False and rest["omonimi"][0]["indice"] == 3


def test_a_lost_session_returns_what_was_read_and_where_to_continue(portal):
    portal["fail"][2] = "Sessione SISTER scaduta o non valida"
    out = go()
    assert out["incompleta"] is True and out["omonimo_prossimo"] == 3 and out["omonimi_elaborati"] == 2
    assert "scaduta" in out["interrotta"] and out["total_results"] == 3


def test_a_lost_session_on_the_first_record_is_an_error(portal):
    portal["fail"][0] = "Sessione SISTER scaduta o non valida"
    with pytest.raises(Exception, match="scaduta"):
        go()


def test_one_failing_record_does_not_lose_the_others(portal):
    portal["fail"][1] = "Timeout 15000ms exceeded"
    out = go()
    assert out["omonimi_elaborati"] == 4 and out["incompleta"] is False
    assert out["omonimi"][1]["errore"].startswith("Timeout") and out["total_results"] == 1 + 3 + 4


def test_no_match_is_returned_as_it_is(portal):
    portal["search"] = {"soggetto": IDENT, "immobili": [], "total_results": 0, "error": "NESSUNA CORRISPONDENZA TROVATA"}
    assert go()["error"] == "NESSUNA CORRISPONDENZA TROVATA"


def test_a_code_without_homonym_step_is_one_record(portal):
    portal["search"] = {"soggetto": IDENT, "immobili": [{"Provincia": "ROMA"}], "total_results": 1}
    out = go()
    assert portal["calls"] == [0] and out["omonimi_totale"] == 1 and out["total_results"] == 1


# --- run-list: the runner continues an incomplete answer -------------------------------------------------------


def _answers(*chunks):
    """Wait results: each request answers the next chunk ({'next': n} = stopped before record n)."""
    seq = list(chunks)

    def wait(request_id):
        chunk = seq.pop(0)
        if isinstance(chunk, str):
            return {"request_id": request_id, "status": "error", "error": chunk}
        data = {"immobili": [], "omonimi_totale": 6, "incompleta": bool(chunk), "omonimo_prossimo": chunk or None}
        return {"request_id": request_id, "status": "completed", "data": data}

    return wait


def azienda():
    return make([{"id": "anas", "command": "azienda", "params": {"identificativo": IDENT, "tipo_catasto": "E"}}])


def test_runner_appends_the_chunks_of_one_code_into_one_row():
    client = FakeClient(wait=_answers(3, 5, 0))
    report = run(client, azienda(), wait=True)
    entry = report.entries[0]
    assert entry["status"] == batch.COMPLETED and "omonimo_prossimo" not in entry
    assert [s[1].get("omonimo_da") for s in client.submits] == [None, "3", "5"]
    assert all(s[2] for s in client.submits[1:])  # follow-ups are forced: they are not the same request
    assert entry["request_ids"] == ["r1", "r2", "r3"] and len(entry["results"]) == 3


def test_a_lost_session_leaves_the_row_incomplete_and_resume_carries_on(tmp_path):
    journal = tmp_path / "j.jsonl"
    client = FakeClient(wait=_answers(3, "Sessione SISTER scaduta o non valida"))
    entry = run(client, azienda(), journal=journal, wait=True).entries[0]
    assert entry["status"] == batch.INCOMPLETE and entry["omonimo_prossimo"] == 3 and "scaduta" in entry["error"]

    again = FakeClient(wait=_answers(0))
    entry = run(again, azienda(), journal=journal, resume=True, wait=True).entries[0]
    assert entry["status"] == batch.COMPLETED
    # only the missing records are requested: no new first search, the follow-up starts at record 3
    assert [s[1].get("omonimo_da") for s in again.submits] == ["3"]
    assert entry["request_ids"][:2] == ["r1", "r2"]


def test_a_complete_answer_needs_no_follow_up():
    client = FakeClient(wait=_answers(0))
    entry = run(client, azienda(), wait=True).entries[0]
    assert entry["status"] == batch.COMPLETED and len(client.submits) == 1


def test_resume_reads_again_an_answer_that_was_only_the_list_of_records(tmp_path):
    """The old flow journaled 'completed' for a code after listing its records; that is not an answer."""
    journal = tmp_path / "j.jsonl"
    spec = azienda()
    plan = batch.plan_batch(spec, batch.BatchOptions.model_validate({**spec.options.model_dump(), "retry_delay": 0}))
    batch.BatchJournal(journal).append(
        {"id": "anas", "status": "completed", "fingerprint": plan[0].fingerprint, "request_ids": ["old"]}
    )
    client = FakeClient(wait=_answers(0))
    entry = run(client, spec, journal=journal, resume=True, wait=True).entries[0]
    assert entry["status"] == batch.COMPLETED and len(client.submits) == 1
    assert client.submits[0][2] is True  # forced: the cached list-only answer is not reused
    # an answer of the new flow carries the number of records and is skipped on the next resume
    again = FakeClient(wait=_answers(0))
    run(again, spec, journal=journal, resume=True, wait=True)
    assert again.submits == []


# --- more than 400 results: SISTER refuses the search; a name is split by the province of the seat --------------

TOO_MANY = "Superato il limite massimo di 400 elementi. Aggiungere altri parametri per la ricerca"


def by_name(name="OPERA PIA"):
    return make([{"id": "opera", "command": "azienda", "params": {"identificativo": name, "tipo_catasto": "E"}}])


def provinces_client(answers: dict, calls: list):
    """submit remembers the params of every request; wait answers by province (None = the plain search)."""
    sent: dict[str, dict] = {}

    def submit(n, command, params, force):
        sent[f"r{n}"] = dict(params)
        calls.append(dict(params))
        return {"request_id": f"r{n}", "status": "queued"}

    def wait(rid):
        province = sent[rid].get("provincia_sede")
        outcome = answers.get(province, "NESSUNA CORRISPONDENZA TROVATA")
        if outcome == "ok":
            return {"request_id": rid, "status": "completed", "data": {"omonimi_totale": 3, "immobili": []}}
        return {"request_id": rid, "status": "error", "error": outcome}

    return FakeClient(submit=submit, wait=wait)


def test_a_name_over_400_is_split_by_province_of_the_seat():
    calls: list = []
    client = provinces_client({None: TOO_MANY, "PA": "ok", "RM": "ok", "MI": TOO_MANY}, calls)
    entry = run(client, by_name(), wait=True).entries[0]
    assert calls[0] == {"identificativo": "OPERA PIA", "tipo_catasto": "E"}  # the plain search first
    assert [c["provincia_sede"] for c in calls[1:]] == batch.SEDE_PROVINCES  # then one per province, all with the name
    assert entry["status"] == batch.COMPLETED and entry["split"] is True
    assert (entry["split_found"], entry["split_too_many"]) == (2, ["MI"])
    assert entry["split_empty"] == len(batch.SEDE_PROVINCES) - 3 and entry["split_pending"] == []


def test_a_final_answer_is_not_retried():
    client = provinces_client({None: "NESSUNA CORRISPONDENZA TROVATA"}, [])
    entry = run(client, by_name(), wait=True, retries=3).entries[0]
    assert len(client.submits) == 1 and entry["status"] == batch.ERROR


def test_a_code_over_400_is_reported_not_split():
    spec = make([{"id": "ph", "command": "azienda", "params": {"identificativo": "00000000059", "tipo_catasto": "E"}}])
    calls: list = []
    client = provinces_client({None: TOO_MANY}, calls)
    entry = run(client, spec, wait=True, retries=2).entries[0]
    assert len(calls) == 1 and entry["status"] == batch.ERROR and "split" not in entry  # nothing to narrow a code with


def test_a_lost_session_stops_the_split_and_resume_finishes_the_other_provinces(tmp_path):
    journal = tmp_path / "j.jsonl"
    calls: list = []
    lost = "Sessione SISTER scaduta o non valida"
    client = provinces_client({None: TOO_MANY, "AG": "ok", "BG": "ok", "BI": lost}, calls)
    entry = run(client, by_name(), journal=journal, wait=True, retries=0).entries[0]
    assert entry["status"] == batch.INCOMPLETE and "da completare" in entry["error"]
    assert entry["split_pending"][0] == "BI" and len(calls) < len(batch.SEDE_PROVINCES)  # it stopped at the lost session

    again: list = []
    client = provinces_client({None: TOO_MANY, "BI": "ok", "BL": "ok"}, again)
    entry = run(client, by_name(), journal=journal, resume=True, wait=True, retries=0).entries[0]
    sent = [c.get("provincia_sede") for c in again]
    assert entry["status"] == batch.COMPLETED and "AG" not in sent and "BG" not in sent  # answered provinces are not asked again
    assert sent[0] == "BI"


def test_the_400_refusal_is_recognised_on_the_search_form():
    page = "Ricerca in ambito nazionale Superato il limite massimo di 400 elementi. Aggiungere altri parametri per la ricerca Denominazione:"
    assert utils._too_many(page) and not utils._too_many("NESSUNA CORRISPONDENZA TROVATA") and not utils._too_many(None)
    assert utils.TOO_MANY_ERROR.startswith(utils.TOO_MANY_MARKER) and batch.TOO_MANY in utils.TOO_MANY_ERROR


# --- long lists: a subject with thousands of immobili (one owner: 5,190) must not take minutes to read ---------------


class CountingPage:
    """A page that counts the calls made to the browser."""

    def __init__(self, values):
        self.values, self.calls = values, 0

    async def eval_on_selector_all(self, selector, script):
        self.calls += 1
        return list(self.values)


def test_all_radio_values_are_read_in_one_browser_call():
    page = CountingPage([f"H501#{n}#F#1#2" for n in range(5190)] + [None])
    values = asyncio.run(utils._radio_values(page, "input[type='radio'][name='visImmSel']"))
    assert page.calls == 1 and len(values) == 5191 and values[0] == "H501#0#F#1#2" and values[-1] == ""


def test_the_immobili_rows_are_classified_with_one_call():
    page = CountingPage(["a", "x Soppresso", "Bene comune non censibile", "b"])
    active, bene_comune, soppressi = asyncio.run(utils._classify_immobili_radios(page, 4))
    assert (active, bene_comune, soppressi) == ([0, 2, 3], {2}, 1) and page.calls == 1


def test_a_long_table_is_parsed_into_the_same_rows_as_before():
    html = "<table><tr><th></th><th>Foglio</th><th>Particella</th></tr>" + "".join(
        f"<tr><td><input type='radio' name='visImmSel' value='v{n}'/></td><td>{n}</td><td>{n + 1}</td></tr>" for n in range(3000)
    ) + "</table>"
    rows = utils._extract_result_tables(html)
    assert len(rows) == 3000 and rows[0] == {"Foglio": "0", "Particella": "1", "visImmSel": "v0"}
    assert rows == utils.parse_table(html)  # the page is parsed once, the table is not parsed again from a string


# --- reuse: force keeps a row that is repeated on purpose -------------------------------------------------------


def test_a_forced_row_is_never_pruned_as_already_answered(tmp_path):
    import json
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "sister" / "command_tools"))
    from sister.command_tools.prune_code_list import available_lines
    from sister.command_tools.run_in_session import prune_plan

    code, other = "RSSMRA80A01H501U", "BNCNNA70A41H501Z"
    answered = {code, other}
    table = (
        '<!-- sister-batch-table {"command": "soggetto", "params": {"tipo_catasto": "E"}} -->\n'
        "| id | codice_fiscale | reuse |\n| --- | --- | --- |\n"
        f"| a | {code} | force |\n| b | {other} |  |\n"
    )
    drop, counts = available_lines(table, answered, set())
    assert len(drop) == 1 and counts["subject"] == 2  # only the row without reuse=force goes

    plan = {"queries": [
        {"id": "a", "command": "soggetto", "reuse": "force", "params": {"codice_fiscale": code}},
        {"id": "b", "command": "soggetto", "params": {"codice_fiscale": other}},
    ]}
    (tmp_path / "batch.json").write_text(json.dumps(plan))
    kept, done, invalid = prune_plan(tmp_path, answered)
    assert (kept, done, invalid) == (1, 1, 0)
    assert [q["id"] for q in json.loads((tmp_path / "batch.json").read_text())["queries"]] == ["a"]


# --- Genova: PON/4 is an urban section, not a census section ---------------------------------------------------


@pytest.mark.parametrize(
    "selected, sezione, urban, expected",
    [
        (None, "PON", None, "PON"),  # no such census section in the comune: it is the sezione urbana
        (None, "pon", None, "PON"),
        ("RA", "RA", None, None),  # Ravenna: the sezione exists and was selected, nothing to add
        (None, "PON", "SPA", "SPA"),  # an explicit sezione urbana always wins
        (None, None, None, None),
        (None, "SEZIONE LUNGA", None, None),  # not a code: leave it
    ],
)
def test_a_section_the_comune_has_no_option_for_becomes_the_urban_section(selected, sezione, urban, expected):
    assert utils._urban_section_fallback(selected, sezione, urban) == expected


# --- a property search that stays on its form is not a result ---------------------------------------------------------


class FormPage:
    """A page with scripted element counts and body text."""

    def __init__(self, counts, text=""):
        self.counts, self.text = counts, text

    def locator(self, selector):
        async def count():
            return next((n for key, n in self.counts.items() if key in selector), 0)

        return types.SimpleNamespace(count=count)

    async def inner_text(self, _selector):
        return self.text


def test_a_property_search_that_stayed_on_its_form_is_an_error_not_a_success():
    page = FormPage({"particella1": 1, "foglio": 1}, "Dati della ricerca Catasto Foglio Particella")
    with pytest.raises(Exception, match="modulo di ricerca"):
        asyncio.run(utils._raise_if_still_on_search_form(page))


def test_the_portals_own_message_is_reported_when_the_form_comes_back():
    page = FormPage({"particella1": 1, "foglio": 1}, "Il campo Sezione urbana è obbligatorio.")
    with pytest.raises(Exception, match="SISTER ha rifiutato il modulo"):
        asyncio.run(utils._raise_if_still_on_search_form(page))


def test_a_list_page_or_a_confirmation_page_passes():
    asyncio.run(utils._raise_if_still_on_search_form(FormPage({"visImmSel": 5, "particella1": 1, "foglio": 1})))
    asyncio.run(utils._raise_if_still_on_search_form(FormPage({"confAssSub": 1, "particella1": 1, "foglio": 1})))
    asyncio.run(utils._raise_if_still_on_search_form(FormPage({})))  # not the form at all


# --- run-list: a finished batch is not resumed (so reuse: force rows are sent again) ----------------------------------


def _batch(tmp_path, statuses):
    import json

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "batch.json").write_text(json.dumps({"queries": [{"id": i} for i in statuses]}))
    (tmp_path / "batch_journal.jsonl").write_text(
        "".join(json.dumps({"id": i, "status": s, "error": e, "updated_at": "2026-11-05T14:00:00+00:00"}) + "\n" for i, (s, e) in statuses.items())
    )
    return tmp_path


def test_only_a_batch_with_nothing_left_counts_as_finished(tmp_path):
    from sister.command_tools.batch_progress import is_finished

    done = {"a": ("completed", None), "b": ("error", "NESSUNA CORRISPONDENZA TROVATA")}
    assert is_finished(_batch(tmp_path / "done", done))
    assert not is_finished(_batch(tmp_path / "error", {**done, "c": ("error", "HTTP 422: subalterno")}))
    assert not is_finished(_batch(tmp_path / "pending", {**done, "c": ("queued", None)}))
    assert not is_finished(_batch(tmp_path / "expired", {**done, "c": ("incomplete", "Sessione SISTER scaduta")}))
    assert not is_finished(tmp_path / "missing")
