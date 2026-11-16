import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, text, update
from sqlmodel import select

from sister.db_models import (
    Document,
    DocumentType,
    ManualPropertyValuation,
    MortgagePaidAction,
    MortgageSearch,
    MortgageSearchAnnotation,
    MortgageSearchFlow,
    MortgageSearchFormality,
    MortgageSearchRequest,
    RequestPayload,
)


async def test_mortgage_projection_is_idempotent_and_keeps_paid_actions(fresh_db):
    from sister import database

    payload = {
        "ambito": "PROVINCIALE",
        "portal_capture": {
            "inspection": {
                "number": "T 12345",
                "date": "15/11/2026",
                "office": "RAVENNA (RA)",
                "subject_tax_code": "RSSMRA70A01H501Z",
                "requester_tax_code": "RSSMRA70A01H501Z",
                "formalities_count": 4,
                "annotations_count": 1,
                "cost": {"text": "Costo: € 6,40", "value": 6.4},
            },
            "costs": {
                "inspection_cost": "6,40",
                "notes_cost": "28,00",
                "titles_cost": "8,00",
                "unattributed_cost": "32,00",
            },
            "pdf_note_request": {"accepted": True, "cost": "28,00", "unit_cost": "4,00", "selected_count": 7},
        },
        "formalities": [
            {
                "numero": 1,
                "tipo": "ISCRIZIONE",
                "verso": "A FAVORE",
                "data_formalita": "2017-02-01",
                "registro_particolare": "11",
                "registro_generale": "22",
                "causale": "COMPRAVENDITA",
                "nota_disponibile_elettronica": True,
                "presenza_titolo_telematico": False,
                "documenti_correlati": [
                    {
                        "tipo": "ISCRIZIONE",
                        "numero": "55",
                        "data": "2020-04-03",
                        "descrizione": "ANNOTAZIONE DI CANCELLAZIONE",
                    }
                ],
            },
            {
                "numero": 2,
                "tipo": "ISCRIZIONE",
                "registro_particolare": "12",
                "registro_generale": "23",
                "causale": "IPOTECA VOLONTARIA",
            },
            {
                "numero": 3,
                "tipo": "TRASCRIZIONE",
                "data_formalita": "2022-05-01",
                "causale": "DECRETO DI TRASFERIMENTO",
            },
            {
                "numero": 4,
                "tipo": "TRASCRIZIONE",
                "data_formalita": "01/02/2026",
                "causale": "DECRETO DI TRASFERIMENTO",
            },
        ],
    }

    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(
            session, "mortgage-projection-test", "ipotecaria_persona_fisica", None, None
        )
        session.add(RequestPayload(request_id=request_pk, data=json.dumps(payload)))
        flow = MortgageSearchFlow(
            request_id=request_pk,
            subject_fiscal_code="RSSMRA70A01H501Z",
            office="RAVENNA (RA)",
            scope="office",
            state="preview",
            current_step="preview",
            started_by="admin@example.test",
            started_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(flow)
        await session.commit()
        flow_id = flow.id

    first_backfill = await database.backfill_mortgage_searches(["mortgage-projection-test"])
    assert first_backfill["responses"] == 1
    async with database._get_session_factory()() as session:
        search_id = (await session.execute(select(MortgageSearch.id))).scalar_one()
        session.add(
            MortgagePaidAction(
                flow_id=flow_id,
                search_id=search_id,
                step="accept",
                quote={"amount": "6.40"},
                confirmed_by="admin@example.test",
                confirmed_at=datetime.now(timezone.utc),
                state="confirmed",
            )
        )
        await session.commit()

    second_backfill = await database.backfill_mortgage_searches(["mortgage-projection-test"])
    assert second_backfill["responses"] == 1
    async with database._get_session_factory()() as session:
        assert (await session.execute(select(func.count()).select_from(MortgageSearch))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(MortgageSearchFormality))).scalar_one() == 4
        assert (await session.execute(select(func.count()).select_from(MortgageSearchAnnotation))).scalar_one() == 1
        action = (await session.execute(select(MortgagePaidAction))).scalar_one()
        assert action.state == "confirmed"
        flow = (await session.execute(select(MortgageSearchFlow))).scalar_one()
        assert flow.search_id == search_id and flow.state == "accepted"
        search = (await session.execute(select(MortgageSearch))).scalar_one()
        assert search.scope == "office"
        assert search.inspection_number == "T 12345"

    typed_render = await database.get_subject_mortgage_formalities("RSSMRA70A01H501Z")
    assert len(typed_render) == 1
    assert typed_render[0]["formalities"][0]["registro_particolare"] == "11"
    assert typed_render[0]["formalities"][0]["has_cancellation_annotation"] is True
    assert typed_render[0]["risk_summary"]["live_mortgages"] == 1  # canceled mortgage excluded, active one counted
    assert typed_render[0]["risk_summary"]["latest_transfer_date"] == "2026-02-01"
    assert "non attribuite" not in typed_render[0]["cost_summary"]

    async with database._get_session_factory()() as session:
        await session.execute(
            text("UPDATE mortgage_search SET subject_fiscal_code = :other_code"),
            {"other_code": "RSSMRA71A01H501Z"},
        )
        await session.commit()
    json_fallback_render = await database.get_subject_mortgage_formalities("RSSMRA70A01H501Z")
    assert len(json_fallback_render) == 1
    assert json_fallback_render[0]["formalities"][0]["registro_particolare"] == typed_render[0]["formalities"][0]["registro_particolare"]
    assert json_fallback_render[0]["cost_summary"] == ""


def test_two_normalized_inspections_render_in_the_subject_formalities_panel():
    inspections = (
        ("T 91777", date(2026, 11, 11), "PALERMO (PA)", "8036", "11046", "TRASCRIZIONE", "COMPRAVENDITA"),
        ("T 79390", date(2026, 11, 12), "RAVENNA (RA)", "14089", "21041", "ISCRIZIONE", "IPOTECA VOLONTARIA"),
    )

    import aecs4u_theme
    from jinja2 import ChoiceLoader, Environment, FileSystemLoader

    templates = Path(__file__).resolve().parents[1] / "sister" / "templates"
    theme_templates = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader([FileSystemLoader(str(templates)), FileSystemLoader(str(theme_templates))]),
        autoescape=True,
    )
    env.globals["csrf_input"] = lambda: ""
    panel_template = env.from_string(
        '{% from "components/subject_formalities.html" import subject_formalities_panel %}'
        "{{ subject_formalities_panel(f) }}"
    )
    normalized_inspections = [
        {
            "inspection": {"number": number, "date": inspected_on.isoformat(), "office": office},
            "formalities": [
                {
                    "numero": 1,
                    "tipo": kind,
                    "verso": "CONTRO",
                    "data_formalita": inspected_on.isoformat(),
                    "registro_particolare": rp,
                    "registro_generale": rg,
                    "causale": description,
                    "immobili": office,
                    "nota_disponibile_elettronica": True,
                }
            ],
            "risk_summary": {"live_mortgages": int(kind == "ISCRIZIONE"), "live_pignoramenti": 0},
            "note_request": {},
        }
        for number, inspected_on, office, rp, rg, kind, description in inspections
    ]
    html = panel_template.render(f=normalized_inspections)

    assert "Ispezione T 91777" in html and "Ispezione T 79390" in html
    assert "RP 8036 · RG 11046" in html and "RP 14089 · RG 21041" in html
    assert html.count("Ultimo decreto di trasferimento") == 2


@pytest.mark.usefixtures("fresh_db")
async def test_typed_and_legacy_two_inspections_render_in_the_subject_formalities_panel():
    from sister import database

    code = "RSSMRA70A01H501Z"
    inspections = (
        ("T 91777", date(2026, 11, 11), "PALERMO (PA)", "8036", "11046", "TRASCRIZIONE", "COMPRAVENDITA"),
        ("T 79390", date(2026, 11, 12), "RAVENNA (RA)", "14089", "21041", "ISCRIZIONE", "IPOTECA VOLONTARIA"),
    )
    async with database._get_session_factory()() as session:
        for index, (number, inspected_on, office, rp, rg, kind, description) in enumerate(inspections):
            request_pk = await database._upsert_request(
                session, f"mortgage-panel-{index}", "ipotecaria_persona_fisica", None, None
            )
            session.add(
                RequestPayload(
                    request_id=request_pk,
                    data=json.dumps(
                        {
                            "portal_capture": {
                                "inspection": {
                                    "number": number,
                                    "date": inspected_on.isoformat(),
                                    "office": office,
                                    "scope": "office",
                                    "subject_tax_code": code,
                                }
                            },
                            "formalities": [
                                {
                                    "numero": 1,
                                    "tipo": kind,
                                    "verso": "CONTRO",
                                    "data_formalita": inspected_on.isoformat(),
                                    "registro_particolare": rp,
                                    "registro_generale": rg,
                                    "causale": description,
                                    "immobili": office,
                                    "nota_disponibile_elettronica": True,
                                }
                            ],
                        }
                    ),
                )
            )
            search = MortgageSearch(
                request_id=request_pk,
                inspection_number=number,
                inspection_date=inspected_on,
                office=office,
                scope="office",
                subject_fiscal_code=code,
                formalities_count=1,
            )
            session.add(search)
            await session.flush()
            session.add(
                MortgageSearchFormality(
                    search_id=search.id,
                    ordinal=1,
                    kind=kind.lower(),
                    direction="contro",
                    formality_date=inspected_on,
                    registro_particolare=rp,
                    registro_generale=rg,
                    act_description=description,
                    location=office,
                    electronic_note=True,
                )
            )
        await session.commit()

    import aecs4u_theme
    from jinja2 import ChoiceLoader, Environment, FileSystemLoader

    templates = Path(__file__).resolve().parents[1] / "sister" / "templates"
    theme_templates = Path(aecs4u_theme.__file__).parent / "templates"
    env = Environment(
        loader=ChoiceLoader([FileSystemLoader(str(templates)), FileSystemLoader(str(theme_templates))]),
        autoescape=True,
    )
    env.globals["csrf_input"] = lambda: ""
    panel_template = env.from_string(
        '{% from "components/subject_formalities.html" import subject_formalities_panel %}'
        "{{ subject_formalities_panel(f) }}"
    )

    typed_formalities = await database.get_subject_mortgage_formalities(code)
    assert {item["inspection"]["number"] for item in typed_formalities} == {"T 91777", "T 79390"}
    typed_html = panel_template.render(f=typed_formalities)

    async with database._get_session_factory()() as session:
        await session.execute(
            text("UPDATE mortgage_search SET subject_fiscal_code = :other_code"),
            {"other_code": "RSSMRA71A01H501Z"},
        )
        await session.commit()
    legacy_formalities = await database.get_subject_mortgage_formalities(code)
    assert {item["inspection"]["number"] for item in legacy_formalities} == {"T 91777", "T 79390"}
    legacy_html = panel_template.render(f=legacy_formalities)

    for html in (typed_html, legacy_html):
        assert "Ispezione T 91777" in html and "Ispezione T 79390" in html
        assert "RP 8036 · RG 11046" in html and "RP 14089 · RG 21041" in html
        assert html.count("Ultimo decreto di trasferimento") == 2


async def test_paid_quotes_apply_ownership_format_and_staleness_rules(fresh_db):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from sister import ade_tariff, database

    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "mortgage-quote-test", "ipotecaria_persona_fisica", None, None)
        search = MortgageSearch(
            request_id=request_pk,
            inspection_number="T 23456",
            inspection_date=date(2026, 11, 15),
            office="RAVENNA (RA)",
            scope="office",
            subject_fiscal_code="RSSMRA70A01H501Z",
            formalities_count=2,
            annotations_count=0,
        )
        session.add(search)
        await session.flush()
        first = MortgageSearchFormality(
            search_id=search.id,
            ordinal=1,
            kind="trascrizione",
            formality_date=date(2017, 2, 1),
            registro_particolare="11",
            registro_generale="22",
            electronic_note=True,
            electronic_title=True,
        )
        second = MortgageSearchFormality(
            search_id=search.id,
            ordinal=2,
            kind="iscrizione",
            formality_date=date(2020, 4, 3),
            registro_particolare="33",
            registro_generale="44",
            electronic_note=True,
            electronic_title=False,
        )
        session.add_all([first, second])
        await session.flush()
        await session.execute(
            pg_insert(DocumentType).values(code="ispezione_ipotecaria").on_conflict_do_nothing()
        )
        document = Document(document_type="ispezione_ipotecaria", file_name="owned-note.pdf")
        session.add(document)
        await session.flush()
        first.note_document_id = document.id
        await session.commit()
        search_id = search.id

    quote = await ade_tariff.quote_note_request(search_id, [1, 2], "PDF", as_of=date(2026, 11, 15))
    assert [row["ordinal"] for row in quote.already_owned] == [1]
    assert quote.already_owned[0]["file_name"] == "owned-note.pdf"
    assert quote.new_cost == Decimal("4.00")
    assert quote.public_dict(as_of=date(2026, 11, 15))["estimate"] is True

    stale = await ade_tariff.quote_note_request(search_id, [1, 2], "PDF", as_of=date(2027, 1, 1))
    assert stale.new_cost is None
    assert {row["reason"] for row in stale.blocked} == {"tariff_stale"}
    xml = await ade_tariff.quote_note_request(search_id, [2], "XML", as_of=date(2026, 11, 15))
    assert xml.blocked[0]["reason"] == "xml_not_supported"
    title = await ade_tariff.quote_title_request(search_id, 2, "PDF", as_of=date(2026, 11, 15))
    assert title.blocked[0]["reason"] == "no_electronic_title"


def test_mortgage_search_migration_round_trips(fresh_db, pg_test_schema):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    schema, _async_dsn, sync_url = pg_test_schema
    migrations_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"

    def load_migration(filename: str, module_name: str):
        migration_path = migrations_dir / filename
        spec = importlib.util.spec_from_file_location(module_name, migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    mortgage_migration = load_migration("20261205_mortgage_search_tables.py", "mortgage_search_migration")
    ade_migration = load_migration("20261206_ade_statement_ledger.py", "ade_statement_migration")
    charge_projection = load_migration("20261207_mortgage_charge_allocation.py", "mortgage_charge_allocation_migration")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection, opts={"sister_schema": schema})
            with Operations.context(context):
                charge_projection.downgrade()
                ade_migration.downgrade()
                mortgage_migration.downgrade()
                mortgage_migration.upgrade()
                ade_migration.upgrade()
                charge_projection.upgrade()
                charge_projection.downgrade()
                ade_migration.downgrade()
                ade_migration.upgrade()
                charge_projection.upgrade()
        inspector = __import__("sqlalchemy").inspect(engine)
        assert inspector.has_table("mortgage_search", schema=schema)
        assert inspector.has_table("mortgage_search_flow", schema=schema)
        assert inspector.has_table("mortgage_paid_action", schema=schema)
        assert inspector.has_table("mortgage_search_request", schema=schema)
    finally:
        engine.dispose()


async def test_paid_flow_token_expiry_and_uncertain_action_reconciliation(fresh_db, monkeypatch):
    from sister import database, mortgage_flow

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "mortgage-flow-test", "ipotecaria_persona_fisica", None, None)
        await session.commit()

    flow = await mortgage_flow.create_flow_record(
        request_id=request_pk,
        subject_fiscal_code="RSSMRA70A01H501Z",
        office="RAVENNA (RA)",
        scope="office",
        started_by="admin@example.test",
    )
    quote = {"step": "accept", "amount": "6.40", "source": "cost_page"}
    issued = mortgage_flow.issue_confirmation_token(flow["id"], "accept", quote)
    token_payload = mortgage_flow.verify_confirmation_token(issued["token"], flow_id=flow["id"])
    assert token_payload["quote_hash"] == issued["quote_hash"]

    expired = mortgage_flow.issue_confirmation_token(
        flow["id"], "accept", quote, now=datetime(2026, 11, 15, tzinfo=timezone.utc)
    )
    try:
        mortgage_flow.verify_confirmation_token(
            expired["token"], flow_id=flow["id"], now=datetime(2026, 11, 15, 0, 6, tzinfo=timezone.utc)
        )
    except mortgage_flow.MortgageFlowError as exc:
        assert str(exc) == "confirmation_token_expired"
    else:
        raise AssertionError("expired confirmation token was accepted")

    action = await mortgage_flow.record_paid_action_intent(flow["id"], issued["token"], "admin@example.test")
    assert action["state"] == "intended"
    active_status = await mortgage_flow.flow_status(flow["id"])
    assert (active_status["state"], active_status["current_step"]) == ("running", "accept")
    try:
        await mortgage_flow.record_paid_action_intent(flow["id"], issued["token"], "admin@example.test")
    except mortgage_flow.MortgageFlowError as exc:
        assert str(exc) == "confirmation_token_already_used"
    else:
        raise AssertionError("confirmation token was reused")

    await mortgage_flow.mark_paid_action_uncertain(action["id"], "browser closed")
    next_token = mortgage_flow.issue_confirmation_token(flow["id"], "accept", quote)
    try:
        await mortgage_flow.record_paid_action_intent(flow["id"], next_token["token"], "admin@example.test")
    except mortgage_flow.MortgageFlowError as exc:
        assert str(exc) == "paid_action_requires_reconciliation"
    else:
        raise AssertionError("uncertain action did not block retry")

    await mortgage_flow.reconcile_paid_action(
        action["id"],
        resolution="not_executed",
        reconciled_by="admin@example.test",
        evidence={"richieste_checked": True, "statement_checked": True},
    )
    retried = await mortgage_flow.record_paid_action_intent(flow["id"], next_token["token"], "admin@example.test")
    assert retried["state"] == "intended"
    status = await mortgage_flow.flow_status(flow["id"])
    assert [row["state"] for row in status["actions"]] == ["not_executed", "intended"]


async def test_stale_open_paid_action_becomes_uncertain_and_stays_blocking(fresh_db, monkeypatch):
    from sister import database, mortgage_flow

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "mortgage-stale-action-test", "ipotecaria_persona_fisica", None, None)
        await session.commit()
    flow = await mortgage_flow.create_flow_record(
        request_id=request_pk,
        subject_fiscal_code="RSSMRA70A01H501Z",
        office="RAVENNA (RA)",
        scope="office",
        started_by="admin@example.test",
    )
    quote = {"step": "accept", "amount": "6.40", "source": "cost_page"}
    token = mortgage_flow.issue_confirmation_token(flow["id"], "accept", quote)["token"]
    action = await mortgage_flow.record_paid_action_intent(flow["id"], token, "admin@example.test")
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=mortgage_flow._PAID_ACTION_RECOVERY_TIMEOUT_SECONDS + 1
    )
    async with database._get_session_factory()() as session:
        await session.execute(update(MortgagePaidAction).where(MortgagePaidAction.id == action["id"]).values(confirmed_at=stale_at))
        await session.commit()

    status = await mortgage_flow.flow_status(flow["id"])

    assert (status["state"], status["current_step"]) == ("uncertain", "accept")
    assert status["actions"][0]["state"] == "uncertain"
    assert status["actions"][0]["result"]["recovery"]["reason"] == "stale_open_action"
    retry_token = mortgage_flow.issue_confirmation_token(flow["id"], "accept", quote)["token"]
    try:
        await mortgage_flow.record_paid_action_intent(flow["id"], retry_token, "admin@example.test")
    except mortgage_flow.MortgageFlowError as exc:
        assert str(exc) == "paid_action_requires_reconciliation"
    else:
        raise AssertionError("stale paid action did not block retry")


async def test_note_paid_action_persists_intent_then_attaches_exact_portal_request(fresh_db, monkeypatch):
    from sister import database, mortgage_flow

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "mortgage-note-action-test", "ipotecaria_persona_fisica", None, None)
        search = MortgageSearch(
            request_id=request_pk,
            inspection_number="T 77777",
            inspection_date=date(2026, 11, 16),
            office="RAVENNA (RA)",
            scope="office",
            subject_fiscal_code="RSSMRA70A01H501Z",
        )
        session.add(search)
        await session.flush()
        formality = MortgageSearchFormality(
            search_id=search.id,
            ordinal=1,
            kind="trascrizione",
            registro_particolare="11",
            registro_generale="22",
            formality_date=date(2017, 2, 1),
            electronic_note=True,
            portal_selection_value="row-11-22",
        )
        session.add(formality)
        await session.commit()
        search_id = int(search.id)

    flow = await mortgage_flow.create_flow_record(
        request_id=request_pk,
        subject_fiscal_code="RSSMRA70A01H501Z",
        office="RAVENNA (RA)",
        scope="office",
        started_by="admin@example.test",
    )
    async with database._get_session_factory()() as session:
        await session.execute(
            update(MortgageSearchFlow)
            .where(MortgageSearchFlow.id == flow["id"])
            .values(search_id=search_id, state="accepted", current_step="formalita")
        )
        await session.commit()

    quote = {
        "step": "notes",
        "amount": "4.00",
        "format": "PDF",
        "portal_items": [
            {
                "ordinal": 1,
                "selection_value": "row-11-22",
                "registro_particolare": "11",
                "registro_generale": "22",
            }
        ],
    }
    token = mortgage_flow.issue_confirmation_token(flow["id"], "notes", quote)["token"]
    action = await mortgage_flow.record_paid_action_intent(flow["id"], token, "admin@example.test")

    async with database._get_session_factory()() as session:
        request_row = (await session.execute(select(MortgageSearchRequest))).scalar_one()
        assert (request_row.kind, request_row.status, request_row.paid_action_id) == ("note", "intended", action["id"])

    await mortgage_flow.mark_paid_action_clicked(action["id"])
    await mortgage_flow.attach_paid_action_requests(
        action["id"],
        [{"ordinal": 1, "portal_request_id": "REQ-777", "status": "available"}],
    )
    await mortgage_flow.mark_paid_action_confirmed(action["id"], {"confirmed": True})

    async with database._get_session_factory()() as session:
        request_row = (await session.execute(select(MortgageSearchRequest))).scalar_one()
        stored_action = await session.get(MortgagePaidAction, action["id"])
        assert (request_row.portal_request_id, request_row.status) == ("REQ-777", "available")
        assert stored_action.portal_request_ids == ["REQ-777"]


async def test_paid_richieste_save_quotes_exact_row_and_links_saved_document(fresh_db, monkeypatch):
    from sister import ade_tariff, database, mortgage_flow

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(
            session, "mortgage-save-action-test", "ipotecaria_persona_fisica", None, None
        )
        search = MortgageSearch(
            request_id=request_pk,
            inspection_number="T 88888",
            inspection_date=date(2026, 11, 16),
            office="RAVENNA (RA)",
            scope="office",
            subject_fiscal_code="RSSMRA70A01H501Z",
        )
        session.add(search)
        await session.flush()
        formality = MortgageSearchFormality(
            search_id=search.id,
            ordinal=3,
            kind="trascrizione",
            registro_particolare="31",
            registro_generale="42",
            formality_date=date(2020, 3, 4),
        )
        session.add(formality)
        await session.flush()
        request_row = MortgageSearchRequest(
            search_id=search.id,
            formality_id=formality.id,
            kind="note",
            format="PDF",
            portal_request_id="REQ-888",
            status="available",
        )
        session.add(request_row)
        await session.commit()
        search_id = int(search.id)

    portal_row = {
        "id_richiesta": "REQ-888",
        "salva_href": "/Visure/Salva.do?idRichiesta=REQ-888",
        "status": "available",
        "formato": "PDF",
        "costo": "4,00 €",
        "oggetto": "Nota RP 31 RG 42",
        "richiesta_del": "16/11/2026 10:30",
    }
    quote = await ade_tariff.quote_richieste_save(search_id, "REQ-888", portal_row)
    assert quote["portal_request_id"] == "REQ-888"
    assert quote["amount"] == "4.00"
    assert quote["source"] == "richieste_row"
    assert quote["ordinal"] == 3

    with pytest.raises(ValueError, match="portal_request_row_mismatch"):
        await ade_tariff.quote_richieste_save(search_id, "REQ-888", {**portal_row, "id_richiesta": "REQ-other"})
    with pytest.raises(ValueError, match="richieste_row_cost_unparseable"):
        await ade_tariff.quote_richieste_save(search_id, "REQ-888", {**portal_row, "costo": "non disponibile"})
    estimated_quote = await ade_tariff.quote_richieste_save(search_id, "REQ-888", {**portal_row, "costo": ""})
    assert estimated_quote["estimate"] is True
    assert estimated_quote["source"] == ade_tariff.RATES["note"].source

    flow = await mortgage_flow.create_flow_record(
        request_id=request_pk,
        subject_fiscal_code="RSSMRA70A01H501Z",
        office="RAVENNA (RA)",
        scope="office",
        started_by="admin@example.test",
    )
    async with database._get_session_factory()() as session:
        await session.execute(
            update(MortgageSearchFlow)
            .where(MortgageSearchFlow.id == flow["id"])
            .values(search_id=search_id, state="running", current_step="richieste_list")
        )
        await session.commit()

    token = mortgage_flow.issue_confirmation_token(flow["id"], "richieste_save", quote)["token"]
    action = await mortgage_flow.record_paid_action_intent(flow["id"], token, "admin@example.test")
    await mortgage_flow.mark_paid_action_clicked(action["id"])
    async with database._get_session_factory()() as session:
        document = Document(file_name="note-REQ-888.pdf", file_size=1024)
        session.add(document)
        await session.commit()
        document_id = int(document.id)
    await mortgage_flow.complete_paid_request_save(action["id"], "REQ-888", document_id)
    await mortgage_flow.mark_paid_action_confirmed(action["id"], {"confirmed": True})

    async with database._get_session_factory()() as session:
        stored_request = (await session.execute(select(MortgageSearchRequest))).scalar_one()
        stored_action = await session.get(MortgagePaidAction, action["id"])
        assert (stored_request.status, stored_request.document_id, stored_request.paid_action_id) == (
            "downloaded",
            document_id,
            action["id"],
        )
        assert stored_action.portal_request_ids == ["REQ-888"]

    async with database._get_session_factory()() as session:
        recovered_formality = MortgageSearchFormality(
            search_id=search_id,
            ordinal=4,
            kind="trascrizione",
            registro_particolare="32",
            registro_generale="43",
            formality_date=date(2020, 3, 5),
        )
        session.add(recovered_formality)
        await session.flush()
        recovered_request = MortgageSearchRequest(
            search_id=search_id,
            formality_id=recovered_formality.id,
            kind="note",
            format="PDF",
            portal_request_id="REQ-889",
            status="uncertain",
        )
        recovered_action = MortgagePaidAction(
            flow_id=flow["id"],
            search_id=search_id,
            step="richieste_save",
            quote={"step": "richieste_save", "portal_request_id": "REQ-889", "amount": "4.00"},
            confirmed_by="admin@example.test",
            confirmed_at=datetime.now(timezone.utc),
            state="uncertain",
        )
        recovered_document = Document(file_name="note-REQ-889.pdf", file_size=2048)
        session.add_all([recovered_request, recovered_action, recovered_document])
        await session.flush()
        recovered_request.paid_action_id = recovered_action.id
        await session.commit()
        recovered_action_id = int(recovered_action.id)
        recovered_document_id = int(recovered_document.id)

    with pytest.raises(mortgage_flow.MortgageFlowError, match="paid_save_reconciliation_requires_document"):
        await mortgage_flow.reconcile_paid_action(
            recovered_action_id,
            resolution="confirmed",
            reconciled_by="admin@example.test",
            evidence={},
        )
    await mortgage_flow.reconcile_paid_action(
        recovered_action_id,
        resolution="confirmed",
        reconciled_by="admin@example.test",
        evidence={"document_id": recovered_document_id},
    )
    async with database._get_session_factory()() as session:
        recovered_request = (
            await session.execute(
                select(MortgageSearchRequest).where(MortgageSearchRequest.portal_request_id == "REQ-889")
            )
        ).scalar_one()
        assert (recovered_request.status, recovered_request.document_id) == ("downloaded", recovered_document_id)


def test_mortgage_search_flow_template_renders_paid_save_quote_and_optional_reconcile_document_id():
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

    templates = Path(__file__).resolve().parents[1] / "sister" / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"_base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(str(templates)),
            ]
        ),
        autoescape=True,
    )
    env.globals["csrf_input"] = lambda: '<input type="hidden" name="csrf_token" value="test">'
    html = env.get_template("mortgage_search_flow.html").render(
        flow={
            "id": 18,
            "office": "RAVENNA (RA)",
            "subject_fiscal_code": "RSSMRA70A01H501Z",
            "state": "uncertain",
            "current_step": "richieste_save",
            "search_id": 71,
            "state_payload": {},
            "actions": [
                {
                    "id": 29,
                    "step": "richieste_save",
                    "state": "uncertain",
                    "quote": {"amount": "4.00"},
                    "confirmed_by": "admin@example.test",
                    "result": {},
                }
            ],
            "pending_requests": [
                {"kind": "note", "status": "available", "portal_request_id": "REQ-888"}
            ],
        },
        preview=None,
        quote=None,
        confirmation_token=None,
        admin_quote={
            "token": "signed-save-token",
            "quote": {
                "step": "richieste_save",
                "amount": "4.00",
                "new_cost": "4.00",
                "estimate": False,
                "source": "richieste_row",
                "lines": [
                    {"quantity": 1, "kind": "note", "ordinals": [3], "amount": "4.00"}
                ],
                "already_owned": [],
                "blocked": [],
            },
        },
        user=None,
    )

    assert "Prezzo mostrato" in html
    assert 'name="token" value="signed-save-token"' in html
    assert 'name="portal_request_id" value="REQ-888"' in html
    document_input = html.split('id="document-id-29"', 1)[1].split(">", 1)[0]
    assert "required" not in document_input


async def test_api_flow_starts_preview_only_and_returns_a_bound_accept_quote(monkeypatch):
    from types import SimpleNamespace

    from sister import main, mortgage_flow
    from sister.models import IspezioneIpotecariaInput

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")

    class FakeService:
        async def add_mortgage_flow_request(self, request, *, started_by):
            assert request.search_type == "persona_fisica"
            assert request.auto_confirm is False
            assert started_by == "service-api"
            return {"id": 7, "state": "preview", "current_step": "preview", "queue_position": 1}

        async def get_response(self, request_ref):
            assert request_ref == "mortgage_flow_test"
            return SimpleNamespace(
                success=True,
                request_id=request_ref,
                data={"confirmed": False, "cost": {"text": "Costo: € 6,40", "value": 6.4}},
                error=None,
                timestamp=datetime(2026, 11, 15, tzinfo=timezone.utc),
            )

    started = await main._start_mortgage_search(
        IspezioneIpotecariaInput(search_type="persona_fisica", province="Ravenna", fiscal_code="RSSMRA70A01H501Z"),
        FakeService(),
    )
    assert started["flow_id"] == 7
    assert started["message"].startswith("Free preview")

    async def fake_flow_status(flow_id):
        assert flow_id == 7
        return {
            "id": 7,
            "state": "preview",
            "subject_fiscal_code": "RSSMRA70A01H501Z",
            "office": "Ravenna",
            "request_ref": "mortgage_flow_test",
        }

    monkeypatch.setattr(mortgage_flow, "flow_status", fake_flow_status)
    quoted = await main._mortgage_search_quote(7, {"step": "accept"}, FakeService())
    assert quoted["quote"] == {
        "step": "accept",
        "amount": "6.40",
        "currency": "EUR",
        "source": "cost_page",
        "display": "Costo: € 6,40",
    }
    assert mortgage_flow.verify_confirmation_token(quoted["token"], flow_id=7)["quote"] == quoted["quote"]


async def test_api_note_and_title_quotes_issue_bound_consent_tokens(monkeypatch):
    from types import SimpleNamespace

    from sister import ade_tariff, main, mortgage_flow

    async def fake_flow_status(flow_id):
        assert flow_id == 9
        return {"id": 9, "state": "running", "current_step": "formalita", "search_id": 41}

    async def fake_note_quote(search_id, ordinals, fmt):
        assert (search_id, ordinals, fmt) == (41, [2, 4], "PDF")
        return SimpleNamespace(
            public_dict=lambda: {
                "step": "note",
                "selected": [2, 4],
                "already_owned": [{"ordinal": 2}],
                "blocked": [],
                "new_cost": "4.00",
                "estimate": True,
                "measured_on": "2026-11-11",
            },
            lines=[SimpleNamespace(ordinals=(4,))],
            blocked=(),
            new_cost=Decimal("4.00"),
        )

    monkeypatch.setattr(mortgage_flow, "flow_status", fake_flow_status)
    monkeypatch.setattr(ade_tariff, "quote_note_request", fake_note_quote)

    async def fake_portal_items(search_id, ordinals):
        assert (search_id, ordinals) == (41, [4])
        return [{"ordinal": 4, "selection_value": "row-4", "registro_particolare": "44", "registro_generale": "55"}]

    monkeypatch.setattr(ade_tariff, "get_formality_portal_items", fake_portal_items)
    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")
    quoted = await main._mortgage_search_quote(9, {"step": "notes", "ordinals": [2, 4]}, object())
    assert quoted["quote"]["already_owned"] == [{"ordinal": 2}]
    assert quoted["executor_available"] is True
    assert quoted["confirmable"] is True
    assert mortgage_flow.verify_confirmation_token(quoted["token"], flow_id=9)["quote"]["portal_items"][0]["ordinal"] == 4

    async def fake_title_quote(search_id, ordinal, fmt):
        assert (search_id, ordinal, fmt) == (41, 3, "PDF")
        return SimpleNamespace(
            public_dict=lambda: {"step": "title", "selected": [3], "new_cost": "8.00", "estimate": True},
            lines=[SimpleNamespace(ordinals=(3,))],
            blocked=(),
            new_cost=Decimal("8.00"),
        )

    monkeypatch.setattr(ade_tariff, "quote_title_request", fake_title_quote)

    async def fake_title_portal_items(search_id, ordinals):
        assert (search_id, ordinals) == (41, [3])
        return [{"ordinal": 3, "selection_value": "row-3", "registro_particolare": "33", "registro_generale": "44"}]

    monkeypatch.setattr(ade_tariff, "get_formality_portal_items", fake_title_portal_items)
    title_quote = await main._mortgage_search_quote(9, {"step": "title", "ordinal": 3}, object())
    assert title_quote["quote"]["new_cost"] == "8.00"
    assert title_quote["token"] is not None


async def test_api_richieste_save_quote_reads_exact_row_and_returns_bound_token(monkeypatch):
    from sister import ade_tariff, main, mortgage_flow

    monkeypatch.setenv("SISTER_MORTGAGE_CONFIRM_SECRET", "test-only-confirmation-secret-123")

    async def fake_flow_status(flow_id):
        assert flow_id == 19
        return {
            "id": 19,
            "state": "running",
            "current_step": "richieste_list",
            "search_id": 71,
            "state_payload": {},
        }

    portal_row = {
        "id_richiesta": "REQ-919",
        "salva_href": "/Visure/Salva.do?idRichiesta=REQ-919",
        "status": "available",
        "formato": "PDF",
        "costo": "4,00 €",
    }

    class FakeBrowserManager:
        async def inspect_mortgage_request_row(self, portal_request_id):
            assert portal_request_id == "REQ-919"
            return {"row": portal_row, "ambiguous": False, "page_restored": True}

    class FakeService:
        auth_ready = True
        browser_manager = FakeBrowserManager()

    async def fake_save_quote(search_id, portal_request_id, observed_row):
        assert (search_id, portal_request_id, observed_row) == (71, "REQ-919", portal_row)
        return {
            "step": "richieste_save",
            "search_id": 71,
            "portal_request_id": "REQ-919",
            "kind": "note",
            "format": "PDF",
            "amount": "4.00",
            "new_cost": "4.00",
            "estimate": False,
        }

    monkeypatch.setattr(mortgage_flow, "flow_status", fake_flow_status)
    monkeypatch.setattr(ade_tariff, "quote_richieste_save", fake_save_quote)
    result = await main._mortgage_search_quote(
        19,
        {"step": "richieste_save", "portal_request_id": "REQ-919"},
        FakeService(),
    )

    assert result["executor_available"] is True
    assert result["confirmable"] is True
    assert result["quote"]["portal_request_id"] == "REQ-919"
    assert mortgage_flow.verify_confirmation_token(result["token"], flow_id=19)["quote"] == result["quote"]


async def test_admin_homonym_choice_projects_accepted_inspection(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from sister import main, mortgage_flow, web

    values = [
        "1#0#ROSSI#MARIO#RSSMRA70A01H501Z#RAVENNA#01/01/1970#RA",
        "2#0#ROSSI#MARIO#RSSMRA70A01H501Z#RAVENNA#02/02/1970#RA",
    ]
    state_payload = {"last_portal_result": {"homonym_values": values, "homonyms": [{}, {"codice_fiscale": "RSSMRA70A01H501Z"}]}}
    flow = {
        "id": 7,
        "state": "running",
        "current_step": "omonimi",
        "search_id": None,
        "request_ref": "mortgage_flow_7",
        "subject_fiscal_code": "RSSMRA70A01H501Z",
        "office": "RAVENNA (RA)",
        "scope": "office",
        "state_payload": state_payload,
    }
    monkeypatch.setattr(mortgage_flow, "flow_status", AsyncMock(return_value=flow))
    updates = []

    async def update_flow_state(flow_id, *, state, current_step, state_payload=None):
        updates.append((flow_id, state, current_step, state_payload))

    monkeypatch.setattr(mortgage_flow, "update_flow_state", update_flow_state)

    portal_data = {"portal_capture": {"inspection": {"number": "T 12345"}}, "formalities": []}
    selected = {"codice_fiscale": "RSSMRA70A01H501Z", "data_nascita": "02/02/1970"}

    class FakeBrowserManager:
        async def select_mortgage_homonym(self, **kwargs):
            assert kwargs["expected_values"] == values
            assert kwargs["selected_value"] == values[1]
            return {"portal_data": portal_data, "selected_homonym": selected, "page_visits": [{"step": "formalita"}]}

    class FakeService:
        auth_ready = True
        browser_manager = FakeBrowserManager()
        stored = None

        async def get_response(self, request_ref):
            assert request_ref == "mortgage_flow_7"
            return SimpleNamespace(
                request_id=request_ref,
                cadastre_type="T",
                success=True,
                data={"cost": {"value": "6.40"}, "page_visits": [{"step": "preview"}]},
            )

        async def _store_response(self, response):
            self.stored = response

    service = FakeService()
    monkeypatch.setattr(main, "visura_service", service)
    monkeypatch.setattr(web, "record_activity", AsyncMock())
    body = json.dumps({"homonym_value": values[1]}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/mortgage-searches/7/select-homonym",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        },
        receive,
    )
    response = await web.web_admin_mortgage_search_select_homonym(
        flow_id=7, request=request, user=SimpleNamespace(email="admin@example.test")
    )

    assert response.status_code == 200
    assert json.loads(response.body)["search_projected"] is True
    assert service.stored.data["formalities"] == []
    assert service.stored.data["page_visits"] == [{"step": "preview"}, {"step": "formalita"}]
    assert updates[-1][1:3] == ("accepted", "formalita")


def test_paid_action_token_digest_constraint_is_attached_to_the_correct_table():
    from sister.db_models import MortgagePaidAction

    action_constraints = {constraint.name for constraint in MortgagePaidAction.__table__.constraints}
    valuation_constraints = {constraint.name for constraint in ManualPropertyValuation.__table__.constraints}

    assert "uq_mortgage_paid_action_token_digest" in action_constraints
    assert "uq_mortgage_paid_action_token_digest" not in valuation_constraints
