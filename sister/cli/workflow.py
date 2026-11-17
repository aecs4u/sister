"""Commands for running, checking and cancelling server-side workflows."""

import asyncio

import typer
from rich.markup import escape

from . import _runtime
from ._runtime import DryRunOpt, ForceOpt, OutputOpt, console
from .render import render_workflow_result, render_workflow_status

workflow_app = typer.Typer(
    name="workflow",
    help="Avvia e gestisce workflow catastali lato server",
    no_args_is_help=True,
)


def _workflow_call(method: str, workflow_id: str) -> dict:
    return _runtime.run_api(lambda client: getattr(client, method)(workflow_id))


@workflow_app.command("run")
def workflow_run(
    preset: str = typer.Option("custom", "--preset", help="Preset: custom, due-diligence, portfolio, fondiario, indirizzo, cross-reference"),
    provincia: str | None = typer.Option(None, "--provincia", "-P", help="Nome della provincia"),
    comune: str | None = typer.Option(None, "--comune", "-C", help="Nome del comune"),
    foglio: str | None = typer.Option(None, "--foglio", "-F", help="Numero di foglio"),
    particella: str | None = typer.Option(None, "--particella", "-p", help="Numero di particella"),
    tipo_catasto: str | None = typer.Option(None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"),
    sezione: str | None = typer.Option(None, "--sezione", help="Sezione catastale"),
    subalterno: str | None = typer.Option(None, "--subalterno", help="Limita agli intestati del subalterno"),
    codice_fiscale: str | None = typer.Option(None, "--cf", help="Codice fiscale"),
    azienda_id: str | None = typer.Option(None, "--azienda", help="Partita IVA o denominazione"),
    indirizzo: str | None = typer.Option(None, "--indirizzo", "-a", help="Indirizzo da cercare"),
    numero_nota: str | None = typer.Option(None, "--nota", help="Numero della nota o annotazione"),
    with_steps: list[str] = typer.Option([], "--with", help="Passo aggiuntivo (ripetibile): elenco, mappa, fiduciali, originali, nota, ispezioni, ispezioni_cart"),
    depth: str = typer.Option("standard", "--depth", "-d", help="Profondità: light, standard, deep, full"),
    max_fanout: int = typer.Option(20, "--max-fanout", help="Numero massimo di immobili o intestatari espansi per passo"),
    max_owners: int = typer.Option(10, "--max-owners", help="Numero massimo di intestatari espansi"),
    max_properties_per_owner: int = typer.Option(20, "--max-properties-per-owner", help="Numero massimo di immobili per intestatario"),
    max_historical_properties: int = typer.Option(5, "--max-history", help="Numero massimo di immobili per il pacchetto storico"),
    max_paid_steps: int = typer.Option(3, "--max-paid", help="Numero massimo di passi a pagamento"),
    max_total_steps: int = typer.Option(100, "--max-steps", help="Numero massimo complessivo di passi"),
    confirm_paid: bool = typer.Option(False, "--confirm-paid", help="Conferma i costi dei servizi a pagamento"),
    include_paid: bool = typer.Option(False, "--include-paid", help="Includi i passi a pagamento"),
    include_history: bool = typer.Option(False, "--history", help="Includi le query storiche"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Attende il completamento e segue lo stato del workflow"),
    output: OutputOpt = None,
    dry_run: DryRunOpt = False,
    force: ForceOpt = False,
):
    """Avvia un workflow gestito dal server; i workflow custom iniziano da un soggetto, azienda, indirizzo o particella."""
    del force  # Reserved for parity with the shared API-command options.

    payload = {
        "preset": preset,
        "provincia": provincia,
        "comune": comune,
        "foglio": foglio,
        "particella": particella,
        "tipo_catasto": tipo_catasto,
        "sezione": sezione,
        "subalterno": subalterno,
        "codice_fiscale": codice_fiscale,
        "identificativo": azienda_id,
        "indirizzo": indirizzo,
        "numero_nota": numero_nota,
        "custom_steps": with_steps if preset == "custom" else None,
        "include_history": include_history,
        "depth": depth,
        "include_paid_steps": include_paid,
        "auto_confirm": confirm_paid,
        "max_fanout": max_fanout,
        "max_owners": max_owners,
        "max_properties_per_owner": max_properties_per_owner,
        "max_historical_properties": max_historical_properties,
        "max_paid_steps": max_paid_steps,
        "max_total_steps": max_total_steps,
    }
    if preset != "custom" and with_steps:
        raise typer.BadParameter("--with si può usare solo con --preset custom")
    if depth in {"deep", "full"} and include_paid and not confirm_paid:
        raise typer.BadParameter(f"--depth {depth} con --include-paid richiede --confirm-paid")

    from ..workflows import prepare_workflow

    try:
        plan = prepare_workflow(payload)
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    if dry_run:
        console.print(f"[bold yellow]SIMULAZIONE[/bold yellow] — POST /api/v1/catasto/workflow/run (preset={preset}, profondità={depth})")
        console.print(f"  {plan.description}")
        console.print(f"  Passaggi: {' → '.join(plan.steps)}")
        _runtime.emit_data(
            {
                "dry_run": True,
                "preset": preset,
                "description": plan.description,
                "steps": plan.steps,
                "depth": depth,
                "wait": wait,
            },
            output=output,
        )
        return

    async def _run(client):
        result = await client.workflow(
            preset=preset,
            provincia=provincia,
            comune=comune,
            foglio=foglio,
            particella=particella,
            tipo_catasto=tipo_catasto,
            sezione=sezione,
            subalterno=subalterno,
            codice_fiscale=codice_fiscale,
            identificativo=azienda_id,
            indirizzo=indirizzo,
            numero_nota=numero_nota,
            include_history=include_history,
            depth=depth,
            max_fanout=max_fanout,
            max_owners=max_owners,
            max_properties_per_owner=max_properties_per_owner,
            max_historical_properties=max_historical_properties,
            max_paid_steps=max_paid_steps,
            max_total_steps=max_total_steps,
            auto_confirm=confirm_paid,
            include_paid_steps=include_paid,
            custom_steps=plan.steps if preset == "custom" else None,
        )
        workflow_id = result.get("workflow_id")
        if workflow_id:
            console.print(f"ID workflow: [cyan]{escape(str(workflow_id))}[/cyan]")
        if wait and workflow_id and result.get("status") in {"pending", "queued", "running"}:
            while result.get("status") in {"pending", "queued", "running"}:
                await asyncio.sleep(2)
                result = await client.workflow_status(str(workflow_id))
        return result

    result = _runtime.run_api(_run)
    result_data = result.get("data") if isinstance(result, dict) else None
    workflow_result = result_data if isinstance(result_data, dict) and result_data.get("steps") else result
    if isinstance(workflow_result, dict) and workflow_result.get("steps") is not None:
        console.print(render_workflow_result(workflow_result))
    elif isinstance(result, dict):
        console.print(render_workflow_status(result, str(result.get("workflow_id") or "workflow")))
    _runtime.emit_data(result, output=output)
    if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed"):
        raise typer.Exit(1)


@workflow_app.command("status")
def workflow_status(
    workflow_id: str = typer.Argument(help="ID del workflow (mostrato da `workflow run`)"),
    output: OutputOpt = None,
):
    """Mostra lo stato di un workflow: passi completati, falliti, saltati."""
    result = _workflow_call("workflow_status", workflow_id)
    console.print(render_workflow_status(result, workflow_id))
    _runtime.emit_data(result, output=output)


@workflow_app.command("cancel")
def workflow_cancel(
    workflow_id: str = typer.Argument(help="ID del workflow da annullare"),
    output: OutputOpt = None,
):
    """Annulla un workflow in corso (i passi gia' eseguiti restano salvati)."""
    result = _workflow_call("cancel_workflow", workflow_id)
    console.print(f"[yellow]Workflow {escape(workflow_id)}[/yellow] — {result.get('status', 'annullamento richiesto')}")
    _runtime.emit_data(result, output=output)


workflow_run.endpoint = "/api/v1/catasto/workflow/run"
workflow_run.method = "POST"
workflow_status.endpoint = "/api/v1/catasto/workflow/runs/{id}"
workflow_status.method = "GET"
workflow_cancel.endpoint = "/api/v1/catasto/workflow/runs/{id}/cancel"
workflow_cancel.method = "POST"
