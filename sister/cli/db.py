"""Direct PostgreSQL and migration commands."""

import typer

from ._runtime import console, project_root, run_db

db_app = typer.Typer(
    name="db",
    help="Gestione del database (init, migrate, status)",
    no_args_is_help=True,
)


def _alembic_config():
    """Build Alembic configuration from the owning checkout, including editable installs."""
    from alembic.config import Config

    return Config(project_root() / "alembic.ini")


@db_app.command("init")
def db_init():
    """Inizializza il database ed esegue tutte le migrazioni."""
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    console.print("[green]Database initialized and migrations applied.[/green]")


@db_app.command("migrate")
def db_migrate():
    """Esegue le migrazioni Alembic in sospeso."""
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    console.print("[green]Migrations applied.[/green]")


@db_app.command("status")
def db_status():
    """Mostra la revisione corrente delle migrazioni del database."""
    from alembic import command

    command.current(_alembic_config(), verbose=True)


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

    run_db(_run)
