import hashlib
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from sister.ade_receipt import ReceiptParseError, parse_receipt_text
from sister.ade_statement import StatementParseError, parse_statement_detail_html, parse_statement_html


def statement_html() -> str:
    rows = [
        ("19/10/2026", "RAVENNA (RA)", "Ricarica castelletto", "19,20", ""),
        ("06/11/2026", "RAVENNA (RA)", "Per ogni nominativo e/o immobile richiesto", "", "25,60"),
        ("06/11/2026", "SICILIA", "Per ogni nominativo e/o immobile richiesto (Sicilia)", "", "6,40"),
        ("06/11/2026", "RAVENNA (RA)", "Per ogni nota visionata", "", "8,00"),
        ("06/11/2026", "RAVENNA (RA)", "Per ogni titolo visionato", "", "8,00"),
        ("06/11/2026", "SICILIA", "Per ogni titolo visionato (Sicilia)", "", "16,00"),
        ("06/11/2026", "RAVENNA (RA)", "Ricarica castelletto", "48,00", ""),
        ("11/11/2026", "SICILIA", "Per ogni nominativo e/o immobile richiesto (Sicilia)", "", "6,40"),
        ("11/11/2026", "SICILIA", "Per ogni nota visionata (Sicilia)", "", "28,00"),
        ("11/11/2026", "SICILIA", "Per ogni titolo visionato (Sicilia)", "", "8,00"),
        ("11/11/2026", "SICILIA", "Ricarica castelletto", "40,00", ""),
        ("12/11/2026", "RAVENNA (RA)", "Per ogni nominativo e/o immobile richiesto", "", "6,40"),
        ("12/11/2026", "RAVENNA (RA)", "Ricarica castelletto", "40,00", ""),
        ("13/11/2026", "RAVENNA (RA)", "Per ogni nota visionata", "", "32,00"),
    ]
    body = "".join(
        "<tr><td><input type='radio' name='riga' value='"
        + f"{201 + index}~{posted_on}~RM~0~~"
        + "'></td>"
        + f"<td>{posted_on}</td><td>{office}</td><td>{description}</td>"
        + f"<td>{credit}</td><td>{debit}</td></tr>"
        for index, (posted_on, office, description, credit, debit) in enumerate(rows)
    )
    return (
        "<html><body><p>Periodo dal 15/01/2026 al 15/11/2026</p>"
        "<table><thead><tr><th></th><th>Data</th><th>Ufficio</th><th>Causale</th>"
        "<th>Accredito</th><th>Addebito</th></tr></thead><tbody>"
        + body
        + "</tbody></table><p>Totale accrediti 147,20</p><p>Totale addebiti 144,80</p>"
        "<p>Deposito castelletto 2,40</p></body></html>"
    )


def receipt_text() -> str:
    return """AGENZIA DELLE ENTRATE
RICEVUTA PER ISPEZIONI DEL 11/11/2026
Ispezione T 99999
Modalità di pagamento: castelletto
Quietanza
Elementi                 Importo
Ispezione nominativo      6,40
Note visionate           28,00
Titoli visionati          8,00
Totale quietanza         42,40
"""


def test_statement_parser_checks_radio_keys_period_totals_and_closing_balance():
    snapshot = parse_statement_html(statement_html())
    assert snapshot.period_start == date(2026, 1, 15)
    assert snapshot.period_end == date(2026, 11, 15)
    assert len(snapshot.rows) == 14
    assert snapshot.total_credit == Decimal("147.20")
    assert snapshot.total_debit == Decimal("144.80")
    assert snapshot.closing_balance == Decimal("2.40")
    assert snapshot.rows[1].statement_key == "202~06/11/2026~RM~0~~"


def test_statement_parser_fails_closed_on_totals_mismatch():
    try:
        parse_statement_html(statement_html().replace("Totale addebiti 144,80", "Totale addebiti 144,81"))
    except StatementParseError as exc:
        assert "non quadrano" in str(exc)
    else:
        raise AssertionError("statement parser accepted inconsistent totals")


def test_statement_parser_accepts_saved_page_period_and_total_row_labels():
    saved_page_shape = """<html><body>
    <p>Parametri di ricerca impostati: Dal giorno 15/01/2026 al giorno 15/11/2026</p>
    <table><thead><tr><th></th><th>Descrizione</th><th>Data operazione</th>
    <th>Accredito</th><th>Addebito</th></tr></thead><tbody>
    <tr><td><input type="radio" value="1~15/01/2026~RM~0~~"></td><td>Ricarica castelletto</td>
    <td>15/01/2026</td><td>10,00</td><td>0,00</td></tr>
    <tr><td><input type="radio" value="2~15/01/2026~RM~0~~"></td><td>Consultazione</td>
    <td>15/01/2026</td><td>0,00</td><td>7,60</td></tr>
    <tr><td></td><td>TOTALE</td><td></td><td>10,00</td><td>7,60</td></tr>
    </tbody></table>
    <p>Saldo del periodo: 2,40</p>
    <p>Deposito castelletto alla data 15/11/2026: 2,40</p>
    </body></html>"""

    snapshot = parse_statement_html(saved_page_shape)

    assert snapshot.period_start == date(2026, 1, 15)
    assert snapshot.period_end == date(2026, 11, 15)
    assert [row.description for row in snapshot.rows] == ["Ricarica castelletto", "Consultazione"]
    assert snapshot.total_credit == Decimal("10.00")
    assert snapshot.total_debit == Decimal("7.60")
    assert snapshot.closing_balance == Decimal("2.40")

    try:
        parse_statement_html(
            saved_page_shape.replace("<td>7,60</td></tr>\n    </tbody>", "<td>7,61</td></tr>\n    </tbody>")
        )
    except StatementParseError as exc:
        assert "non quadrano" in str(exc)
    else:
        raise AssertionError("statement parser accepted a mismatched total row")


def test_statement_detail_parser_extracts_only_explicit_identifiers():
    detail = parse_statement_detail_html(
        """<html><body>
        <p>Dettaglio ispezione T 99999</p><a href="Dettaglio.do?idRichiesta=REQ-123">Richiesta</a>
        <table><tr><td>Registro particolare n. 55 - Registro generale n. 123</td></tr></table>
        </body></html>"""
    )
    assert detail["inspection_numbers"] == ["T 99999"]
    assert detail["portal_request_ids"] == ["REQ-123"]
    assert detail["registers"] == [{"registro_particolare": "55", "registro_generale": "123"}]
    assert detail["identified"] is True
    assert parse_statement_detail_html("<p>Dettaglio senza riferimenti</p>")["identified"] is False


def test_receipt_parser_reconciles_line_items_and_total():
    receipt = parse_receipt_text(receipt_text())
    assert receipt.receipt_date == date(2026, 11, 11)
    assert receipt.payment_method == "castelletto"
    assert receipt.receipt_data["inspection_numbers"] == ["T 99999"]
    assert [line.amount for line in receipt.lines] == [Decimal("6.40"), Decimal("28.00"), Decimal("8.00")]
    assert receipt.amount == Decimal("42.40")


def test_receipt_parser_rejects_mismatched_quietanza():
    try:
        parse_receipt_text(receipt_text().replace("Totale quietanza         42,40", "Totale quietanza         41,40"))
    except ReceiptParseError as exc:
        assert "non quadra" in str(exc)
    else:
        raise AssertionError("receipt parser accepted inconsistent total")


def test_receipt_cost_kind_mapping_is_exact_and_fail_closed():
    from sister.ade_ledger import _receipt_charge_kind

    assert _receipt_charge_kind("Ispezione nominativo") == "inspection"
    assert _receipt_charge_kind("Note visionate") == "note"
    assert _receipt_charge_kind("Titoli visionati (Sicilia)") == "title"
    assert _receipt_charge_kind("Servizio non classificato") is None


async def test_statement_import_rebuilds_dedicated_euro_ledger_and_is_idempotent(fresh_db):
    from sister import ade_ledger, database
    from sister.db_models import AdeLedgerEntry, AdeStatementImport, AdeStatementRow

    snapshot = parse_statement_html(statement_html())
    source_hash = hashlib.sha256(statement_html().encode()).hexdigest()
    first = await ade_ledger.import_statement_snapshot(
        snapshot,
        source_filename="statement.html",
        source_sha256=source_hash,
    )
    repeated = await ade_ledger.import_statement_snapshot(
        snapshot,
        source_filename="statement.html",
        source_sha256=source_hash,
    )
    assert first["rows_added"] == 14
    assert first["balance"]["balance"] == "2.40"
    assert repeated["duplicate"] is True
    assert repeated["rows_added"] == 0
    async with database._get_session_factory()() as session:
        assert len((await session.execute(select(AdeStatementImport))).scalars().all()) == 1
        assert len((await session.execute(select(AdeStatementRow))).scalars().all()) == 14
        entries = list((await session.execute(select(AdeLedgerEntry))).scalars())
        assert len(entries) == 14
        topup = next(row for row in entries if row.statement_key.startswith("207~"))
        same_day_charge = next(row for row in entries if row.statement_key.startswith("202~"))
        assert topup.sequence_no < same_day_charge.sequence_no
    admin_data = await ade_ledger.get_ade_ledger_admin_data()
    assert admin_data["total_credit"] == Decimal("147.20")
    assert admin_data["total_debit"] == Decimal("144.80")
    assert admin_data["balance"] == Decimal("2.40")
    assert admin_data["balance_difference"] == Decimal("0.00")


def test_ade_ledger_migration_round_trips(fresh_db, pg_test_schema):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    schema, _async_dsn, sync_url = pg_test_schema
    migration_path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261206_ade_statement_ledger.py"
    charge_projection_path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20261207_mortgage_charge_allocation.py"
    spec = importlib.util.spec_from_file_location("ade_statement_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    projection_spec = importlib.util.spec_from_file_location("mortgage_charge_allocation_migration", charge_projection_path)
    projection = importlib.util.module_from_spec(projection_spec)
    projection_spec.loader.exec_module(projection)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection, opts={"sister_schema": schema})
            with Operations.context(context):
                projection.downgrade()
                migration.downgrade()
                migration.upgrade()
                projection.upgrade()
                projection.downgrade()
                migration.downgrade()
                migration.upgrade()
                projection.upgrade()
        inspector = inspect(engine)
        for table in (
            "ade_statement_import",
            "ade_statement_row",
            "ade_ledger_entry",
            "ade_receipt",
            "ade_receipt_line",
            "ade_charge_allocation",
        ):
            assert inspector.has_table(table, schema=schema)
    finally:
        engine.dispose()


async def test_receipt_import_is_idempotent_and_keeps_receipt_lines_separate(fresh_db):
    from sister import ade_ledger

    raw = receipt_text()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    imported = await ade_ledger.import_receipt_text(raw, source_filename="receipt.txt", source_sha256=digest)
    repeated = await ade_ledger.import_receipt_text(raw, source_filename="receipt.txt", source_sha256=digest)
    assert imported["duplicate"] is False
    assert imported["amount"] == "42.40"
    assert imported["line_count"] == 3
    assert repeated["duplicate"] is True


async def test_receipt_allocation_requires_matching_statement_amount_and_preserves_unattributed_remainder(fresh_db):
    from sister import ade_ledger, database
    from sister.db_models import AdeReceiptLine, MortgageSearch, MortgageSearchCharge

    html = statement_html()
    snapshot = parse_statement_html(html)
    await ade_ledger.import_statement_snapshot(
        snapshot,
        source_filename="statement.html",
        source_sha256=hashlib.sha256(html.encode()).hexdigest(),
    )
    text = receipt_text()
    await ade_ledger.import_receipt_text(
        text,
        source_filename="receipt.txt",
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "ade-allocation-search", "ipotecaria_persona_fisica", None, None)
        search = MortgageSearch(
            request_id=request_pk,
            inspection_number="T 99999",
            inspection_date=date(2026, 11, 11),
            office="RAVENNA (RA)",
            scope="office",
            subject_fiscal_code="RSSMRA70A01H501Z",
            formalities_count=0,
            annotations_count=0,
        )
        session.add(search)
        await session.commit()
        search_id = search.id
        receipt_lines = list((await session.execute(select(AdeReceiptLine).order_by(AdeReceiptLine.ordinal))).scalars())

    allocations = []
    for statement_id, amount, line in zip((208, 209, 210), ("6.40", "28.00", "8.00"), receipt_lines):
        allocation = await ade_ledger.allocate_statement_charge(
            f"{statement_id}~11/11/2026~RM~0~~",
            Decimal(amount),
            evidence="receipt",
            evidence_ref=f"receipt-line-{line.ordinal}",
            created_by="admin@example.test",
            search_id=search_id,
            receipt_line_id=line.id,
        )
        assert allocation["duplicate"] is False
        assert allocation["charge_projected"] is True
        allocations.append(allocation)
    duplicate = await ade_ledger.allocate_statement_charge(
        "208~11/11/2026~RM~0~~",
        Decimal("6.40"),
        evidence="receipt",
        evidence_ref="receipt-line-1",
        created_by="admin@example.test",
        search_id=search_id,
        receipt_line_id=receipt_lines[0].id,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["charge_id"] == allocations[0]["charge_id"]
    async with database._get_session_factory()() as session:
        charges = list((await session.execute(select(MortgageSearchCharge).order_by(MortgageSearchCharge.id))).scalars())
        assert [(row.kind, row.amount, row.basis) for row in charges] == [
            ("inspection", Decimal("6.40"), "receipt"),
            ("note", Decimal("28.00"), "receipt"),
            ("title", Decimal("8.00"), "receipt"),
        ]
        assert {row.allocation_id for row in charges} == {item["allocation_id"] for item in allocations}
    try:
        await ade_ledger.allocate_statement_charge(
            "208~11/11/2026~RM~0~~",
            Decimal("0.01"),
            evidence="statement_detail",
            evidence_ref="extra-detail",
            created_by="admin@example.test",
            search_id=search_id,
        )
    except ade_ledger.AdeLedgerError as exc:
        assert str(exc) == "allocation_exceeds_statement_debit"
    else:
        raise AssertionError("allocation exceeded its statement debit")

    admin_data = await ade_ledger.get_ade_ledger_admin_data()
    by_key = {entry["statement_key"]: entry for entry in admin_data["entries"]}
    assert by_key["208~11/11/2026~RM~0~~"]["unattributed_amount"] == Decimal("0.00")
    assert by_key["202~06/11/2026~RM~0~~"]["unattributed_amount"] == Decimal("25.60")
    assert admin_data["unattributed_total"] == Decimal("102.40")


async def test_topup_links_only_to_a_validated_matching_pagopa_source_row(fresh_db):
    from sister import ade_ledger, database
    from sister.db_models import AdeLedgerEntry, PagoPAPaymentHistory

    html = statement_html()
    await ade_ledger.import_statement_snapshot(
        parse_statement_html(html),
        source_filename="statement.html",
        source_sha256=hashlib.sha256(html.encode()).hexdigest(),
    )
    async with database._get_session_factory()() as session:
        payment = PagoPAPaymentHistory(
            user_id="id:external-payer",
            payment_method="bonifico",
            amount=Decimal("19.20"),
            amount_source="validated_receipt",
            payment_status="validated",
            payment_date=date(2026, 10, 19),
            source_file="pagopa.csv",
            source_sha256="b" * 64,
            source_row=1,
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    await ade_ledger.link_validated_pagopa("201~19/10/2026~RM~0~~", payment_id, linked_by="admin@example.test")
    async with database._get_session_factory()() as session:
        entry = (
            await session.execute(
                select(AdeLedgerEntry).where(AdeLedgerEntry.statement_key == "201~19/10/2026~RM~0~~")
            )
        ).scalar_one()
        assert entry.pagopa_payment_id == payment_id
        assert entry.linked_by == "admin@example.test"


async def test_statement_detail_must_identify_search_before_charge_allocation(fresh_db, tmp_path):
    from sister import ade_ledger, database
    from sister.db_models import MortgageSearch

    html = statement_html()
    await ade_ledger.import_statement_snapshot(
        parse_statement_html(html),
        source_filename="statement.html",
        source_sha256=hashlib.sha256(html.encode()).hexdigest(),
    )
    async with database._get_session_factory()() as session:
        request_pk = await database._upsert_request(session, "ade-detail-search", "ipotecaria_persona_fisica", None, None)
        search = MortgageSearch(
            request_id=request_pk,
            inspection_number="T 99999",
            inspection_date=date(2026, 11, 6),
            office="RAVENNA (RA)",
            scope="office",
            subject_fiscal_code="RSSMRA70A01H501Z",
            formalities_count=0,
            annotations_count=0,
        )
        session.add(search)
        await session.commit()
        search_id = search.id
    detail_file = tmp_path / "detail.html"
    detail_file.write_text("<html><body>Dettaglio consultazione per ispezione T 99999</body></html>", encoding="utf-8")
    parsed = await ade_ledger.import_statement_detail_file("202~06/11/2026~RM~0~~", detail_file)
    assert parsed["identified"] is True
    allocation = await ade_ledger.allocate_statement_charge(
        "202~06/11/2026~RM~0~~",
        Decimal("25.60"),
        evidence="statement_detail",
        evidence_ref="detail.html",
        created_by="admin@example.test",
        search_id=search_id,
    )
    assert allocation["duplicate"] is False
    assert await ade_ledger.get_ade_ledger_admin_data()
