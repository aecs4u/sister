"""Batch engine: submission file, planning, retries, journal and resume (no network, fake client)."""

import asyncio
import json
from pathlib import Path

import pytest

from sister import batch
from sister.client import VisuraAPIError, VisuraClient

ROOT = Path(__file__).resolve().parent.parent
SEARCH = {"provincia": "Trieste", "comune": "TRIESTE", "foglio": 9, "particella": 166}


def make(queries, **extra):
    return batch.BatchFile.model_validate({"queries": queries, **extra})


# --- file format --------------------------------------------------------------------------------------------


def test_example_file_is_valid_and_fully_planned():
    spec = batch.load_batch(ROOT / "docs" / "examples" / "batch_example.json")
    plan = batch.plan_batch(spec)
    assert [(i.id, i.state) for i in plan] == [
        ("trieste-9-166", "ready"),
        ("proprietario", "ready"),
        ("storica-9-166-3", "ready"),
        ("da-fare-dopo", "disabled"),
    ]
    assert plan[0].params["tipo_catasto"] == "F" and plan[0].params["foglio"] == "9"  # defaults merged, numbers → text
    assert "tipo_catasto" not in plan[1].params  # null removes an inherited default


def test_schema_file_is_current():
    committed = json.loads((ROOT / "docs" / "batch_submission.schema.json").read_text())
    assert committed == batch.batch_json_schema(), (
        "regenerate: sister query batch-schema -o docs/batch_submission.schema.json"
    )


def test_unknown_keys_and_empty_queries_are_rejected():
    with pytest.raises(ValueError):
        make([{"params": SEARCH, "colour": "red"}])
    with pytest.raises(ValueError):
        batch.BatchFile.model_validate({"queries": []})
    with pytest.raises(ValueError, match="duplicato"):
        make([{"id": "a", "params": SEARCH}, {"id": "a", "params": SEARCH}])
    with pytest.raises(ValueError):
        make([{"id": "bad id!", "params": SEARCH}])


def test_csv_is_loaded_as_a_batch(tmp_path):
    path = tmp_path / "in.csv"
    path.write_text(
        "# comment\ncommand,provincia,comune,foglio,particella,subalterno,vista,source_files\n"
        "visura-storica,Palermo,PALERMO,11,1073,5,analitica,a.pdf\n"
        "soggetto,,,,,,,\n"
    )
    plan = batch.plan_batch(batch.load_batch(path))
    assert plan[0].command == "visura-storica" and plan[0].state == "ready"
    assert "vista" not in plan[0].params and plan[0].metadata == {"vista": "analitica", "source_files": "a.pdf"}
    assert plan[1].state == "invalid" and "codice_fiscale" in plan[1].error  # soggetto with no parameters


def test_empty_csv_raises(tmp_path):
    path = tmp_path / "in.csv"
    path.write_text("provincia,comune\n")
    with pytest.raises(ValueError):
        batch.load_batch(path)


# --- planning -----------------------------------------------------------------------------------------------


def test_plan_reports_every_problem_without_stopping():
    spec = make(
        [
            {"params": SEARCH},
            {"command": "nope", "params": {}},
            {"params": {**SEARCH, "fogli": 3}},
            {"params": {"provincia": "Roma"}},
            {"params": SEARCH},
        ]
    )
    plan = batch.plan_batch(spec)
    assert [i.state for i in plan] == ["ready", "invalid", "invalid", "invalid", "duplicate"]
    assert "sconosciuto" in plan[1].error and "fogli" in plan[2].error and "obbligatori" in plan[3].error
    assert plan[4].duplicate_of == plan[0].id
    assert [i.id for i in plan] == ["q0001", "q0002", "q0003", "q0004", "q0005"]


def test_aliases_resolve_to_the_canonical_command():
    item = batch.plan_batch(make([{"command": "persona-giuridica", "params": {"identificativo": "01234567890"}}]))[0]
    assert item.command == "azienda"


def test_query_options_override_batch_options():
    spec = make([{"params": SEARCH, "reuse": "force", "retries": 0}], options={"reuse": "reuse", "retries": 3})
    item = batch.plan_batch(spec)[0]
    assert (item.reuse, item.retries) == ("force", 0)


# --- runner -------------------------------------------------------------------------------------------------


class FakeClient:
    """Stands in for VisuraClient: scripted submit/wait outcomes, records the calls."""

    request_ids = staticmethod(VisuraClient.request_ids)

    def __init__(self, submit=None, wait=None):
        self.submits: list[tuple] = []
        self.waits: list[str] = []
        self._submit = submit
        self._wait = wait
        self.in_flight = self.peak = 0

    async def submit(self, command, params, *, force=False, reuse_previous=False):
        self.submits.append((command, dict(params), force, reuse_previous))
        if self._submit:
            outcome = self._submit(len(self.submits), command, params, force)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"request_id": f"r{len(self.submits)}", "status": "queued"}

    async def wait_for_result(self, request_id, **kwargs):
        self.waits.append(request_id)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        if self._wait:
            return self._wait(request_id)
        return {"request_id": request_id, "status": "completed", "data": {}}


def run(client, spec, *, journal=None, resume=False, confirm=None, events=None, **options):
    opts = batch.BatchOptions.model_validate({**spec.options.model_dump(), "retry_delay": 0, **options})
    runner = batch.BatchRunner(
        client,
        opts,
        journal=batch.BatchJournal(journal) if journal else None,
        resume=resume,
        confirm=confirm,
        emit=(lambda event, item, **info: events.append((event, item.id))) if events is not None else None,
    )
    return asyncio.run(runner.run(batch.plan_batch(spec, opts), spec.name))


def three():
    return make([{"id": f"p{n}", "params": {**SEARCH, "particella": n}} for n in (1, 2, 3)])


def test_submit_only_marks_rows_queued():
    client = FakeClient()
    report = run(client, three())
    assert [e["status"] for e in report.entries] == ["queued"] * 3 and report.failed == 0
    assert client.waits == [] and all(call[3] and not call[2] for call in client.submits)  # reuse_previous, not force


def test_wait_collects_results_and_writes_row_files(tmp_path):
    report = run(FakeClient(), three(), wait=True, output_dir=str(tmp_path))
    assert [e["status"] for e in report.entries] == ["completed"] * 3
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"batch_000{n}_search.json" for n in (1, 2, 3)]
    assert json.loads((tmp_path / "batch_0002_search.json").read_text())["id"] == "p2"
    assert report.to_dict()["counts"] == {"completed": 3}


def test_invalid_rows_are_reported_not_submitted():
    client = FakeClient()
    report = run(client, make([{"params": SEARCH}, {"command": "nope"}]))
    assert [e["status"] for e in report.entries] == ["queued", "invalid"]
    assert len(client.submits) == 1 and report.failed == 1


def test_transient_failure_is_retried_with_force():
    def submit(n, command, params, force):
        return VisuraAPIError(503, "busy") if n == 1 else {"request_id": "ok", "status": "queued"}

    client = FakeClient(submit=submit)
    report = run(client, make([{"params": SEARCH}]), retries=1)
    assert report.entries[0]["status"] == "queued" and report.entries[0]["attempts"] == 2
    assert [c[2] for c in client.submits] == [False, True]


def test_client_error_is_not_retried():
    client = FakeClient(submit=lambda *a: VisuraAPIError(422, "bad"))
    report = run(client, make([{"params": SEARCH}]), retries=3)
    assert report.entries[0]["status"] == "error" and report.entries[0]["attempts"] == 1
    assert "422" in report.entries[0]["error"]


def test_error_result_is_retried_then_reported():
    client = FakeClient(wait=lambda rid: {"status": "error", "error": "portal down"})
    report = run(client, make([{"params": SEARCH}]), wait=True, retries=2)
    entry = report.entries[0]
    assert entry["status"] == "error" and entry["attempts"] == 3 and len(client.submits) == 3
    assert "portal down" in entry["error"] and report.failed == 1


def test_needs_human_is_final_and_not_retried():
    client = FakeClient(wait=lambda rid: {"status": "needs_human"})
    report = run(client, make([{"params": SEARCH}]), wait=True, retries=2)
    assert report.entries[0]["status"] == "needs_human" and len(client.submits) == 1


def test_poll_timeout_keeps_request_ids():
    class Slow(FakeClient):
        async def wait_for_result(self, request_id, **kwargs):
            raise TimeoutError("too slow")

    report = run(Slow(), make([{"params": SEARCH}]), wait=True)
    assert report.entries[0]["status"] == "timeout" and report.entries[0]["request_ids"] == ["r1"]


def test_on_error_stop_leaves_the_rest_pending():
    client = FakeClient(submit=lambda n, *a: VisuraAPIError(422, "bad"))
    report = run(client, three(), on_error="stop")
    assert [e["status"] for e in report.entries] == ["error", "skipped", "skipped"]
    assert len(client.submits) == 1


def test_max_in_flight_bounds_concurrency():
    client = FakeClient()
    run(client, make([{"params": {**SEARCH, "particella": n}} for n in range(6)]), wait=True, max_in_flight=1)
    assert client.peak == 1
    client = FakeClient()
    run(client, make([{"params": {**SEARCH, "particella": n}} for n in range(6)]), wait=True, max_in_flight=3)
    assert 1 < client.peak <= 3


def test_stale_answer_asks_before_downloading_again():
    stale = {"request_id": "old", "status": "completed", "stale": True}

    def submit(n, command, params, force):
        return {"request_id": "new"} if force else stale

    client = FakeClient(submit=submit)
    asked = []
    run(client, make([{"params": SEARCH}]), confirm=lambda r: asked.append(r) or False)
    assert len(asked) == 1 and len(client.submits) == 1  # said no: the earlier answer is reused

    client = FakeClient(submit=submit)
    report = run(client, make([{"params": SEARCH}]), confirm=lambda r: True)
    assert report.entries[0]["request_ids"] == ["new"] and len(client.submits) == 2

    client = FakeClient(submit=submit)
    run(client, make([{"params": SEARCH, "reuse": "reuse"}]), confirm=lambda r: pytest.fail("must not ask"))
    run(client, make([{"params": SEARCH, "reuse": "force"}]))
    assert client.submits[-1][2] is True  # force skips the cache


# --- journal and resume -------------------------------------------------------------------------------------


def test_resume_skips_completed_repolls_sent_and_reruns_failed(tmp_path):
    journal = tmp_path / batch.JOURNAL_NAME
    spec = three()

    def wait(rid):
        return {"status": "error", "error": "boom"} if rid == "r2" else {"status": "completed"}

    first = FakeClient(wait=wait)
    run(first, spec, journal=journal, wait=True, retries=0)
    # p1 completed, p2 failed; simulate a crash before p3 finished: it was sent (queued) but never waited for
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [rec["status"] for rec in lines if rec["id"] == "p3"][-1] == "completed"
    journal.write_text(
        "\n".join(json.dumps(rec) for rec in lines if not (rec["id"] == "p3" and rec["status"] == "completed"))
        + '\n{"id": "p9", "sta'
    )  # also a half-written last line

    second = FakeClient()
    events = []
    report = run(second, spec, journal=journal, resume=True, wait=True, events=events)
    assert [e["status"] for e in report.entries] == ["completed", "completed", "completed"]
    assert second.waits == ["r1", "r3"]  # p2 resubmitted (new id r1), p3 re-polled with its journaled id; p1 untouched
    assert [(c[1]["particella"], c[2]) for c in second.submits] == [("2", True)]  # only p2 sent, as a new request
    assert ("skip", "p1") in events


def test_resume_submit_only_does_not_resend_queued_rows(tmp_path):
    journal = tmp_path / batch.JOURNAL_NAME
    run(FakeClient(), three(), journal=journal)
    second = FakeClient()
    report = run(second, three(), journal=journal, resume=True)
    assert second.submits == [] and [e["status"] for e in report.entries] == ["queued"] * 3
    assert [e["request_ids"] for e in report.entries] == [["r1"], ["r2"], ["r3"]]


def test_edited_query_is_a_new_request_on_resume(tmp_path):
    journal = tmp_path / batch.JOURNAL_NAME
    run(FakeClient(), make([{"id": "a", "params": SEARCH}]), journal=journal, wait=True)
    client = FakeClient()
    run(client, make([{"id": "a", "params": {**SEARCH, "particella": 167}}]), journal=journal, resume=True, wait=True)
    assert len(client.submits) == 1


def test_without_resume_the_journal_is_ignored(tmp_path):
    journal = tmp_path / batch.JOURNAL_NAME
    run(FakeClient(), three(), journal=journal, wait=True)
    client = FakeClient()
    run(client, three(), journal=journal, wait=True)
    assert len(client.submits) == 3


# --- CLI ----------------------------------------------------------------------------------------------------


def test_cli_invalid_rows_abort_before_sending(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from sister.cli import app

    sent = []

    async def fake_submit(self, command, params, **kwargs):
        sent.append(command)
        return {"request_id": "r"}

    monkeypatch.setattr(VisuraClient, "submit", fake_submit)
    file = tmp_path / "b.json"
    file.write_text(json.dumps({"queries": [{"params": SEARCH}, {"command": "soggetto", "params": {}}]}))
    runner = CliRunner()
    result = runner.invoke(app, ["query", "batch", "-I", str(file)])
    assert result.exit_code == 2 and sent == []
    result = runner.invoke(app, ["query", "batch", "-I", str(file), "--skip-invalid"])
    assert sent == ["search"] and result.exit_code == 1  # ran the valid row; the invalid one still counts as an error


def test_cli_json_resume_and_report(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from sister.cli import app

    calls = []

    async def fake_submit(self, command, params, **kwargs):
        calls.append(params["particella"])
        return {"request_id": f"r{len(calls)}"}

    async def fake_wait(self, request_id, **kwargs):
        return {"request_id": request_id, "status": "completed"}

    monkeypatch.setattr(VisuraClient, "submit", fake_submit)
    monkeypatch.setattr(VisuraClient, "wait_for_result", fake_wait)
    file = tmp_path / "b.json"
    file.write_text(json.dumps({"name": "t", "options": {"wait": True}, "queries": [{"params": SEARCH}]}))
    out, report = tmp_path / "out", tmp_path / "report.json"
    runner = CliRunner()
    args = ["query", "batch", "-I", str(file), "-O", str(out), "-o", str(report)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, [*args, "--resume"]).exit_code == 0
    assert calls == ["166"]  # the second run found it completed in the journal
    data = json.loads(report.read_text())
    assert data["name"] == "t" and data["counts"] == {"completed": 1}
    assert runner.invoke(app, ["query", "batch", "-I", str(file), "--resume"]).exit_code == 2  # no journal location


def test_cli_batch_schema_command():
    from typer.testing import CliRunner

    from sister.cli import app

    result = CliRunner().invoke(app, ["query", "batch-schema"])
    assert result.exit_code == 0 and json.loads(result.output)["title"] == "SISTER batch submission"


def test_resume_does_not_resend_a_final_no_match_answer(tmp_path):
    journal = tmp_path / batch.JOURNAL_NAME
    spec = three()

    def wait(rid):
        return {"status": "error", "error": "NESSUNA CORRISPONDENZA TROVATA"} if rid == "r2" else {"status": "completed"}

    run(FakeClient(wait=wait), spec, journal=journal, wait=True, retries=0)

    second = FakeClient()
    report = run(second, spec, journal=journal, resume=True, wait=True)
    assert second.submits == []  # SISTER's answer does not change: nothing is sent again, not even the "failed" row
    assert [e["status"] for e in report.entries] == ["completed", "error", "completed"]
    assert "NESSUNA CORRISPONDENZA" in report.entries[1]["error"]
