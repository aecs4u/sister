"""Subject-search grants are independent from document purchases and other principals."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.responses import Response

from sister import database, web
from sister.db_models import Document


@pytest.mark.usefixtures("fresh_db")
async def test_subject_grants_match_principal_and_code_and_preserve_provenance():
    user = "email:emanuele@example.test"
    code = "00112580535"
    assert await database.grant_subject_access(user.upper(), "company", code, "operator", "requested access")
    assert await database.has_subject_access(["id:clerk-user", user], "company", code)
    assert not await database.has_subject_access("email:another@example.test", "company", code)
    assert not await database.has_subject_access(user, "company", "00110120821")
    assert not await database.has_subject_access([], "company", code)
    assert not await database.grant_subject_access(user, "company", code, "second operator", "replacement")
    async with database._get_session_factory()() as session:
        row = (await session.execute(text("SELECT created_by, note FROM subject_access"))).one()
        assert tuple(row) == ("operator", "requested access")
        await session.execute(text("UPDATE subject_access SET granted_at = now() + interval '1 day'"))
        await session.commit()
    assert not await database.has_subject_access(user, "company", code)
    async with database._get_session_factory()() as session:
        await session.execute(text("UPDATE subject_access SET granted_at = now(), revoked_at = now()"))
        await session.commit()
    assert not await database.has_subject_access(user, "company", code)
    assert await database.grant_subject_access(user, "company", code, "operator", "restored")
    assert await database.has_subject_access(user, "company", code)


@pytest.mark.usefixtures("fresh_db")
async def test_paid_subject_purchase_is_replay_safe_and_reactivation_is_tracked():
    user = "id:buyer@example.test"
    kind = "person"
    code = "RSSMRA80A01H501U"
    assert await database.complete_subject_purchase(
        user,
        kind,
        code,
        source="stripe",
        payment_ref="cs_entity_1",
        created_by="stripe",
        amount=Decimal("9.90"),
        currency="EUR",
    ) == "created"
    assert await database.complete_subject_purchase(
        user,
        kind,
        code,
        source="stripe",
        payment_ref="cs_entity_1",
        created_by="stripe",
        amount=Decimal("9.90"),
        currency="EUR",
    ) == "replay"
    assert await database.has_subject_access(user, kind, code)
    assert await database.revoke_subject_access(user, kind, code, "admin", "request")
    assert await database.complete_subject_purchase(
        user,
        kind,
        code,
        source="wallet",
        payment_ref="wallet:tx-2",
        created_by="wallet",
        amount=Decimal("8"),
        currency="CREDITS",
    ) == "created"
    rows = await database.list_subject_purchases()
    assert [row["payment_ref"] for row in rows] == ["wallet:tx-2", "cs_entity_1"]
    assert await database.has_subject_access(user, kind, code)


@pytest.mark.usefixtures("fresh_db")
async def test_paid_subject_purchase_already_owned_is_recorded_for_refund_review():
    user = "id:buyer@example.test"
    kind = "company"
    code = "00112580535"
    await database.grant_subject_access(user, kind, code, "admin")
    assert await database.complete_subject_purchase(
        user,
        kind,
        code,
        source="stripe",
        payment_ref="cs_duplicate_entity",
        created_by="stripe",
        amount=Decimal("14.90"),
        currency="EUR",
    ) == "already_owned"
    rows = await database.list_subject_purchases()
    assert len(rows) == 1 and rows[0]["status"] == "refund_required"
    assert await database.has_subject_access(user, kind, code)


@pytest.mark.usefixtures("fresh_db")
async def test_subject_checkout_fulfilment_grants_access_and_deduplicates_webhook(monkeypatch):
    from sister import purchases_billing

    events = []

    async def _record_activity(action, **kwargs):
        events.append({"action": action, **kwargs})

    monkeypatch.setattr(database, "record_activity", _record_activity)
    session = {
        "id": "cs_subject_person_1",
        "payment_status": "paid",
        "amount_total": 990,
        "currency": "eur",
        "metadata": purchases_billing.subject_checkout_metadata(
            "id:buyer@example.test",
            "person",
            "RSSMRA80A01H501U",
            ["email:buyer@example.test"],
        ),
    }
    assert await purchases_billing.fulfil_checkout_session(session) == "created"
    assert await purchases_billing.fulfil_checkout_session(session) == "replay"
    assert await database.has_subject_access(
        ["id:buyer@example.test", "email:buyer@example.test"], "person", "RSSMRA80A01H501U"
    )
    (purchase,) = await database.list_subject_purchases()
    assert (purchase["source"], purchase["amount"], purchase["currency"], purchase["status"]) == (
        "stripe", Decimal("9.90"), "EUR", "completed"
    )
    assert [event["action"] for event in events] == ["subject.purchase.completed"]


@pytest.mark.usefixtures("fresh_db")
@pytest.mark.parametrize(
    "kind,owned,other",
    [
        ("person", "RSSMRA80A01H501U", "VRDLGU75B12F205X"),
        ("company", "00110120821", "00112580535"),
    ],
)
async def test_subject_indexes_show_only_the_signed_in_owners_records(monkeypatch, kind, owned, other):
    import json

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    for index, code in enumerate((owned, other)):
        async with database._get_session_factory()() as session:
            await session.execute(
                text("INSERT INTO subject (fiscal_code, name) VALUES (:code, :name)"),
                {"code": code, "name": f"TEST PROFILE {index}"},
            )
            await session.execute(
                text(
                    "INSERT INTO request (request_ref, created_at, responded_at, success, subject_query) "
                    "VALUES (:ref, now(), now(), true, :code)"
                ),
                {"ref": f"owned-index-{kind}-{index}", "code": code},
            )
            await session.commit()
    await database.grant_subject_access("email:owner@example.test", kind, owned, "test")
    assert (await database.get_subject_access_codes("email:owner@example.test", kind)) == {owned}
    rows, total = await database.query_subject_profiles(kind, allowed_codes={owned})
    assert total == 1 and [row["fiscal_code"] for row in rows] == [owned]
    assert (await database.subject_overview(kind, allowed_codes={owned}))["subjects"] == 1

    class User:
        id = "owner"
        email = "owner@example.test"

    class Query:
        query_params = {}
        headers = {}

    response = await web._subject_index_json(Query(), kind, User())
    payload = json.loads(response.body)
    assert payload["total"] == 1
    assert [row["fiscal_code"] for row in payload["data"]] == [owned]
    csv = await web._subject_index_csv(Query(), kind, User())
    assert owned.encode() in csv.body and other.encode() not in csv.body


@pytest.mark.usefixtures("fresh_db")
@pytest.mark.parametrize("kind,code", [("company", "00112580535"), ("person", "SCRTRS58H41G273E")])
async def test_query_only_profile_opens_for_grantee_and_keeps_document_checks(monkeypatch, kind, code):
    user = SimpleNamespace(id="clerk-user", email="emanuele@example.test")
    await database.grant_subject_access("email:emanuele@example.test", kind, code, "operator")
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    monkeypatch.setattr(web, "_load_subject_profile_indexes", AsyncMock(return_value=(set(), {})))
    monkeypatch.setattr(web, "_load_subject_profile_files", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(web, "_subject_visura_candidates", AsyncMock(return_value=[]))
    profile = {"code": code, "pagination": {}}
    hub = AsyncMock(return_value=profile)
    monkeypatch.setattr(web, "_subject_query_profile", hub)
    monkeypatch.setattr(web, "_subject_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(web, "_subject_cadastral_group_usage", lambda _: {})
    monkeypatch.setattr(web, "_subject_document_archive", AsyncMock(return_value={}))
    monkeypatch.setattr(web, "_get_theme", lambda _: None)
    monkeypatch.setattr(web, "_render_query_only_subject", AsyncMock(return_value=Response("saved subject")))
    monkeypatch.setattr(web, "_render_subject_index", AsyncMock(return_value=Response("no access")))
    kwargs = {"route_prefix": "/companies" if kind == "company" else "/persons", "code_pattern": r"[A-Z0-9]{11,16}"}
    request = SimpleNamespace()

    response = await web._web_subject_profile(request, code, user=user, **kwargs)
    assert response.status_code == 200
    assert hub.call_args.kwargs["allowed_document_ids"] == set()
    hub.reset_mock()

    other = SimpleNamespace(id="other-user", email="another@example.test")
    response = await web._web_subject_profile(request, code, user=other, **kwargs)
    assert response.status_code == 404
    hub.assert_not_called()

    async with database._get_session_factory()() as session:
        document = Document(file_name="separately-protected.pdf", file_size=1)
        session.add(document)
        await session.commit()
    with pytest.raises(HTTPException) as denied:
        await web._require_document_access(user, document.id)
    assert denied.value.status_code == 404


@pytest.mark.usefixtures("fresh_db")
@pytest.mark.parametrize("kind,code", [("company", "00112580535"), ("person", "SCRTRS58H41G273E")])
async def test_subject_map_requires_matching_grant(monkeypatch, kind, code):
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.delenv("SISTER_ADMIN_USERS", raising=False)
    await database.grant_subject_access("email:emanuele@example.test", kind, code, "operator")
    profile = {"code": code}
    hub = AsyncMock(return_value=profile)
    maps = AsyncMock(return_value=Response("subject map"))
    monkeypatch.setattr(web, "_subject_query_profile", hub)
    monkeypatch.setattr(web, "_subject_parcel_map_data", maps)
    route = web.web_company_parcel_map_data if kind == "company" else web.web_person_parcel_map_data
    user = SimpleNamespace(email="emanuele@example.test")
    assert (await route(code, user=user)).status_code == 200
    assert maps.call_args.kwargs["profile"] == profile
    assert hub.call_args.kwargs["allowed_document_ids"] == set()
    with pytest.raises(HTTPException) as denied:
        await route(code, user=SimpleNamespace(email="another@example.test"))
    assert denied.value.status_code == 404


async def test_granted_subject_map_ignores_another_subjects_cached_key(monkeypatch):
    monkeypatch.setattr(web, "_HUB_MAP_DATA", {"other-subject": {}, "authorized-subject": {}})
    monkeypatch.setattr(web, "_hub_map_parcels", lambda profile: [{"subject": profile["code"]}])
    monkeypatch.setattr(web, "_hub_map_key", lambda _: "authorized-subject")
    increment = AsyncMock(return_value={"done": True})
    monkeypatch.setattr(web, "_hub_map_increment", increment)
    await web._subject_parcel_map_data(
        "00112580535", r"\d{11}", "other-subject", 0, profile={"code": "00112580535"}
    )
    increment.assert_awaited_once_with("authorized-subject", 0)


@pytest.mark.usefixtures("fresh_db")
async def test_subject_access_migration_round_trip():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlmodel import SQLModel

    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261128_subject_access.py"
    spec = importlib.util.spec_from_file_location("subject_access_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def round_trip(connection):
        context = MigrationContext.configure(connection, opts={"sister_schema": database.DATABASE_SCHEMA})
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, type_, reflected, compare_to:
                (name == "subject_access" if type_ == "table" else True),
        })
        assert compare_metadata(context, SQLModel.metadata) == []

    async with database._get_engine().begin() as connection:
        await connection.run_sync(round_trip)


@pytest.mark.usefixtures("fresh_db")
async def test_subject_purchase_migration_matches_model():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlmodel import SQLModel

    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261129_subject_purchases.py"
    spec = importlib.util.spec_from_file_location("subject_purchase_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def round_trip(connection):
        context = MigrationContext.configure(connection, opts={"sister_schema": database.DATABASE_SCHEMA})
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
            migration.upgrade()
        context = MigrationContext.configure(
            connection,
            opts={"include_object": lambda obj, name, type_, reflected, compare_to: name == "subject_purchase" if type_ == "table" else True},
        )
        assert compare_metadata(context, SQLModel.metadata) == []

    async with database._get_engine().begin() as connection:
        await connection.run_sync(round_trip)
