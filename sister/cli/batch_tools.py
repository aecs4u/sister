"""Typer entry points for checkout-backed batch workflows."""

from pathlib import Path
from typing import Literal

import typer

from . import _runtime
from ._runtime import DryRunOpt

batch_app = typer.Typer(name="batch", help="Prepara, esegue e monitora i batch di visure.", no_args_is_help=True)


@batch_app.command("run-session")
def batch_run_session(
    batch: list[Path] = typer.Option(..., "--batch", help="Cartella dei piani da eseguire; ripetibile e in ordine"),
    minutes: float = typer.Option(26, "--minutes", help="Minuti dal login prima di fermare il batch"),
    logged_in_at: str | None = typer.Option(None, "--logged-in-at", help="Ora HH:MM del login"),
    grace_seconds: float = typer.Option(60, "--grace-seconds", help="Secondi di tolleranza per una riga gia' avviata"),
    port: int = typer.Option(8027, "--port", help="Porta del servizio SISTER"),
    preflight_cf: str | None = typer.Option(None, "--preflight-cf", help="Codice fiscale per la ricerca di verifica"),
    stop_stale_service: bool = typer.Option(False, "--stop-stale-service", help="Ferma un servizio SISTER residuo sulla porta"),
    cdp: str | None = typer.Option(None, "--cdp", help="Endpoint Chrome DevTools"),
    email_progress: bool = typer.Option(False, "--email-progress", help="Invia il riepilogo della sessione via email"),
    email_to: str = typer.Option("demo@aecs4u.com", "--email-to", help="Destinatario del riepilogo"),
    dry_run: DryRunOpt = False,
    keep_answered: bool = typer.Option(False, "--keep-answered", help="Mantiene nel piano le richieste gia' risposte"),
    probe: bool = typer.Option(False, "--probe", help="Apre una scheda temporanea per verificare la sessione"),
):
    """Esegue piani salvati nella sessione SISTER aperta."""
    from ..command_tools import run_in_session

    result = _runtime.run_local_tool(
        "batch run-session",
        lambda: run_in_session.run(
            batch=batch,
            minutes=minutes,
            logged_in_at=logged_in_at,
            grace_seconds=grace_seconds,
            port=port,
            preflight_cf=preflight_cf,
            stop_stale_service=stop_stale_service,
            cdp=cdp,
            email_progress=email_progress,
            email_to=email_to,
            dry_run=dry_run,
            keep_answered=keep_answered,
            probe=probe,
        ),
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("progress")
def batch_progress(
    directories: list[Path] = typer.Argument(..., help="Cartelle dei batch da monitorare"),
    once: bool = typer.Option(False, "--once", help="Stampa un aggiornamento e termina"),
    interval: float = typer.Option(0.5, "--interval", min=0.05, help="Intervallo tra i controlli in secondi"),
):
    """Mostra l'avanzamento dei batch salvati."""
    from ..command_tools import batch_progress as progress_tool

    result = _runtime.run_local_tool(
        "batch progress", lambda: progress_tool.run(directories, once=once, interval=interval)
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("build-list")
def batch_build_list(
    list_file: Path = typer.Argument(..., help="Lista Markdown supportata"),
    output_dir: Path = typer.Option(..., "--output-dir", help="Cartella del piano batch"),
    tipo_richiesta: Literal["attualita", "storica"] | None = typer.Option(
        None, "--tipo-richiesta", help="Tipo richiesta per le ricerche persona"
    ),
    extra: Literal["none", "immobili", "intestati", "all"] | None = typer.Option(
        None, "--extra", help="Dati aggiuntivi da leggere oltre alla lista"
    ),
    captcha_free: bool = typer.Option(False, "--captcha-free", help="Evita le pagine che richiedono CAPTCHA"),
    name: str | None = typer.Option(None, "--name", help="Nome visualizzato del piano"),
):
    """Crea un piano batch da una lista Markdown senza inviare richieste."""
    from ..command_tools import build_batch_from_list

    result = _runtime.run_local_tool(
        "batch build-list",
        lambda: build_batch_from_list.run(
            list_file,
            output_dir,
            tipo_richiesta=tipo_richiesta,
            extra=extra,
            captcha_free=captcha_free,
            name=name,
        ),
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("prune-list")
def batch_prune_list(
    list_file: Path = typer.Argument(..., help="Lista Markdown da controllare"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra le righe da rimuovere senza riscrivere il file"),
):
    """Rimuove dalla lista le righe gia' risposte, creando prima una copia di sicurezza."""
    from ..command_tools import prune_code_list

    result = _runtime.run_local_tool("batch prune-list", lambda: prune_code_list.run(list_file, dry_run=dry_run))
    if result:
        raise typer.Exit(result)


@batch_app.command("convert-list")
def batch_convert_list(
    list_file: Path = typer.Argument(..., help="Lista legacy da convertire"),
    destination: Path | None = typer.Option(None, "--destination", help="Percorso Markdown di destinazione"),
    legacy_output: Path | None = typer.Option(None, "-o", "--output", hidden=True),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra la destinazione senza scrivere"),
):
    """Converte una lista codici legacy nel formato batch Markdown."""
    from ..command_tools import convert_code_list

    if destination is not None and legacy_output is not None:
        raise typer.BadParameter("usa --destination oppure il vecchio --output, non entrambi")
    if legacy_output is not None:
        typer.echo("Per le liste Markdown, --output è deprecato; usa --destination.", err=True)
    result = _runtime.run_local_tool(
        "batch convert-list",
        lambda: convert_code_list.run(list_file, output=destination or legacy_output, apply=not dry_run),
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("convert-pvp-csv")
def batch_convert_pvp_csv(
    source_dir: Path = typer.Argument(..., help="Cartella dei CSV per provincia"),
    output_dir: Path = typer.Argument(..., help="Cartella delle liste Markdown create"),
):
    """Converte CSV PVP in liste Markdown."""
    from ..command_tools import convert_pvp_sales_csvs_to_markdown

    result = _runtime.run_local_tool(
        "batch convert-pvp-csv", lambda: convert_pvp_sales_csvs_to_markdown.run(source_dir, output_dir)
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("run-list")
def batch_run_list(
    list_file: Path = typer.Argument(..., help="Lista Markdown supportata"),
    batch_dir: Path | None = typer.Argument(None, help="Cartella del piano da riprendere o creare"),
    preflight_cf: str | None = typer.Option(None, "--preflight-cf", help="Codice fiscale per verificare la sessione"),
    email_progress: bool = typer.Option(False, "--email-progress", help="Invia il riepilogo via email"),
    email_to: str = typer.Option("demo@aecs4u.com", "--email-to", help="Destinatario del riepilogo"),
    stop_stale_service: bool = typer.Option(False, "--stop-stale-service", help="Ferma il servizio residuo sulla porta"),
    minutes: float = typer.Option(26, "--minutes", min=1, help="Minuti dal login prima di fermare nuove righe"),
    logged_in_at: str | None = typer.Option(None, "--logged-in-at", help="Ora HH:MM del login"),
    grace_seconds: float = typer.Option(60, "--grace-seconds", min=0, help="Tolleranza per la riga già avviata"),
    port: int = typer.Option(8027, "--port", min=1, max=65535, help="Porta del servizio SISTER"),
    cdp: str | None = typer.Option(None, "--cdp", help="Endpoint Chrome DevTools"),
    tipo_richiesta: Literal["attualita", "storica"] | None = typer.Option(
        None, "--tipo-richiesta", help="Tipo richiesta per le ricerche persona"
    ),
    extra: Literal["none", "immobili", "intestati", "all"] | None = typer.Option(
        None, "--extra", help="Dati aggiuntivi da leggere oltre alla lista"
    ),
    captcha_free: bool = typer.Option(False, "--captcha-free", help="Evita le pagine che richiedono CAPTCHA"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepara il piano in una cartella temporanea e non avvia il servizio"),
    keep_answered: bool = typer.Option(False, "--keep-answered", help="Mantiene nel piano le righe gia' risposte"),
    probe: bool = typer.Option(False, "--probe", help="Verifica la sessione con una scheda temporanea"),
    resume: bool = typer.Option(False, "--resume", hidden=True, help="Riprende il batch più recente (comportamento predefinito)"),
):
    """Prepara una lista Markdown e la esegue nella sessione SISTER già aperta."""
    from ..command_tools import run_list

    result = _runtime.run_local_tool(
        "batch run-list",
        lambda: run_list.run(
            list_file=list_file,
            batch_dir=batch_dir,
            preflight_cf=preflight_cf,
            email_progress=email_progress,
            email_to=email_to,
            stop_stale_service=stop_stale_service,
            minutes=minutes,
            logged_in_at=logged_in_at,
            grace_seconds=grace_seconds,
            port=port,
            cdp=cdp,
            tipo_richiesta=tipo_richiesta,
            extra=extra,
            captcha_free=captcha_free,
            dry_run=dry_run,
            keep_answered=keep_answered,
            probe=probe,
        ),
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("build-pdf-parseable")
def batch_build_pdf_parseable(
    documents_dir: Path | None = typer.Option(None, "--documents-dir", help="Cartella con i PDF"),
    batch_csv: Path | None = typer.Option(None, "--batch-csv", help="CSV delle richieste da inviare"),
    inventory_csv: Path | None = typer.Option(None, "--inventory-csv", help="CSV di inventario e stato"),
):
    """Crea il batch per i PDF senza un file SISTER in formato analizzabile."""
    from ..command_tools import build_pdf_parseable_batch

    kwargs = {}
    if documents_dir is not None:
        kwargs["documents_dir"] = documents_dir
    if batch_csv is not None:
        kwargs["batch_csv"] = batch_csv
    if inventory_csv is not None:
        kwargs["inventory_csv"] = inventory_csv
    result = _runtime.run_local_tool("batch build-pdf-parseable", lambda: build_pdf_parseable_batch.run(**kwargs))
    if result:
        raise typer.Exit(result)


@batch_app.command(
    "recrowd",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []},
)
def batch_recrowd(
    ctx: typer.Context,
    items: Literal["proponents", "properties"] = typer.Option(..., "--items", help="Elenco Recrowd da elaborare"),
):
    """Verifica o richiede visure storiche per le entità Recrowd selezionate."""
    from ..command_tools import recrowd_storica_analitica

    result = _runtime.run_local_tool(
        "batch recrowd", lambda: recrowd_storica_analitica.run(items, forwarded=list(ctx.args))
    )
    if result:
        raise typer.Exit(result)


@batch_app.command("run-pvp-is")
def batch_run_pvp_is(
    source_path: Path | None = typer.Argument(None, help="Lista Markdown (predefinita: inputs/pvp_sales/IS.md)"),
    batch_dir: Path | None = typer.Argument(None, help="Cartella del piano da riprendere o creare"),
):
    """Chiede la lista PVP IS e la esegue nella sessione SISTER aperta."""
    from ..command_tools import run_pvp_is

    result = _runtime.run_local_tool("batch run-pvp-is", lambda: run_pvp_is.run(source_path, batch_dir))
    if result:
        raise typer.Exit(result)


@batch_app.command("run-pdf-parseable")
def batch_run_pdf_parseable(
    documents_dir: Path | None = typer.Option(None, "--documents-dir", help="Cartella con i PDF"),
    batch_csv: Path | None = typer.Option(None, "--batch-csv", help="CSV delle richieste da inviare"),
    inventory_csv: Path | None = typer.Option(None, "--inventory-csv", help="CSV di inventario e stato"),
    output_dir: Path | None = typer.Option(None, "--output-dir", help="Cartella dei risultati JSON e journal"),
):
    """Crea ed esegue le query JSON per i PDF senza un compagno analizzabile."""
    from ..command_tools import run_pdf_parseable_batch

    kwargs = {
        key: value
        for key, value in {
            "documents_dir": documents_dir,
            "batch_csv": batch_csv,
            "inventory_csv": inventory_csv,
            "output_dir": output_dir,
        }.items()
        if value is not None
    }
    result = _runtime.run_local_tool("batch run-pdf-parseable", lambda: run_pdf_parseable_batch.run(**kwargs))
    if result:
        raise typer.Exit(result)
