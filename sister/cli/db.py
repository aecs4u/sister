"""Direct PostgreSQL and migration commands."""

import typer

from . import _runtime
from ._runtime import console, project_root, run_db

db_app = typer.Typer(
    name="db",
    help="Gestione del database (migrate, status, backfill)",
    no_args_is_help=True,
)


def _alembic_config():
    """Build Alembic configuration from the owning checkout, including editable installs."""
    from alembic.config import Config

    return Config(project_root() / "alembic.ini")


@db_app.command("migrate")
def db_migrate():
    """Esegue le migrazioni Alembic in sospeso."""
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    if _runtime.json_output_enabled():
        _runtime.emit_data({"status": "migrated"})
    else:
        console.print("[green]Migrazioni applicate.[/green]")


@db_app.command("init", hidden=True)
def db_init():
    """Deprecated alias for ``db migrate``."""
    typer.echo("`sister db init` è deprecato; usa `sister db migrate`.", err=True)
    return db_migrate()


async def _migration_status() -> dict:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from .. import database

    config = _alembic_config()
    heads = ScriptDirectory.from_config(config).get_heads()
    async with database._get_engine().connect() as connection:
        current = await connection.run_sync(
            lambda sync_connection: list(
                MigrationContext.configure(
                    sync_connection,
                    opts={"version_table_schema": database.DATABASE_SCHEMA},
                ).get_current_heads()
            )
        )
    expected = database.DATABASE_REVISION
    behind = set(current) != set(heads) or expected not in current
    return {
        "current": current,
        "heads": heads,
        "database_revision": expected,
        "revision_matches": expected in current,
        "behind": behind,
    }


@db_app.command("status")
def db_status():
    """Mostra revisione applicata, testa Alembic e revisione attesa."""
    try:
        result = _runtime.run_db(_migration_status, initialize=False)
    except Exception as exc:  # noqa: BLE001 - report database setup/connection failures without a traceback
        console.print(f"[red]Stato del database non disponibile: {exc}[/red]")
        raise typer.Exit(3) from None

    current = ", ".join(result["current"]) or "none"
    heads = ", ".join(result["heads"]) or "none"
    state = "indietro" if result["behind"] else "aggiornato"
    console.print(f"DATABASE_REVISION: {result['database_revision']}")
    console.print(f"Applicate: {current} | Teste: {heads} | {state}")
    _runtime.emit_data(result)
    if result["behind"]:
        raise typer.Exit(1)


@db_app.command("backfill")
def db_backfill(
    what: str = typer.Argument(
        "all",
        help="projections (risultati -> immobili/intestati), xml (documenti -> tabelle tipizzate), "
        "holdings (riepilogo immobili/valori per soggetto), names (nomi dei soggetti noti solo per codice), "
        "history (storico delle proprieta'), mortgage-searches (ispezioni ipotecarie persona fisica), "
        "mortgage-notes (note di trascrizione/iscrizione estratte dai PDF delle ispezioni) o all",
    ),
    force: bool = typer.Option(False, "--force", help="Rielabora anche i documenti gia' popolati"),
    limit: int = typer.Option(0, "--limit", help="Massimo di righe da elaborare (0 = tutte)"),
    contains: str = typer.Option(
        "", "--contains", help="projections: solo le risposte il cui JSON contiene questo testo (es. immobili_intestatari)"
    ),
):
    """Riempie le tabelle strutturate dai dati gia' salvati (JSON delle risposte e XML dei documenti)."""
    from .. import database, xml_ingest

    if what not in {"all", "projections", "xml", "holdings", "names", "history", "mortgage-searches", "mortgage-notes"}:
        raise typer.BadParameter("usa projections, xml, holdings, names, history, mortgage-searches, mortgage-notes o all")

    async def _run():
        import getpass
        from datetime import datetime, timezone

        started = datetime.now(timezone.utc)
        result: dict = {}
        try:
            if what in {"all", "projections"}:
                result["projections"] = await database.backfill_projections(limit=limit or None, contains=contains or None)
                console.print(f"Risposte: {result['projections']}")
            if what in {"all", "mortgage-searches"}:
                result["mortgage_searches"] = await database.backfill_mortgage_searches(limit=limit or None)
                console.print(f"Ispezioni ipotecarie: {result['mortgage_searches']}")
            if what in {"all", "mortgage-notes"}:
                from ..mortgage_note_projection import backfill_mortgage_notes

                result["mortgage_notes"] = await backfill_mortgage_notes(force=force, limit=limit or None)
                console.print(f"Note ipotecarie: {result['mortgage_notes']}")
            if what in {"all", "xml"}:
                result["xml"] = await xml_ingest.backfill_documents(force=force, limit=limit or None)
                console.print(f"Documenti XML: {result['xml']}")
            if what in {"all", "names"}:
                result["names"] = await database.backfill_subject_names()
                console.print(f"Nomi dei soggetti: {result['names']}")
            if what in {"all", "history"}:
                result["history"] = await database.backfill_ownership_history()
                console.print(f"Storico proprieta': {result['history']}")
            if what in {"all", "holdings"}:
                result["holdings"] = await database.refresh_subject_holdings()
                console.print(f"Riepiloghi per soggetto: {result['holdings']}")
        except Exception as exc:  # noqa: BLE001
            await database.record_activity(
                "db.backfill",
                actor=getpass.getuser(),
                source="cli",
                status="error",
                params={"what": what, "force": force, "limit": limit},
                result=result,
                error=str(exc) or exc.__class__.__name__,
                started_at=started,
            )
            raise
        await database.record_activity(
            "db.backfill",
            actor=getpass.getuser(),
            source="cli",
            params={"what": what, "force": force, "limit": limit},
            result=result,
            started_at=started,
        )
        return result

    result = run_db(_run)
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
