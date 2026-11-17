"""Commands for the admin-reviewed mortgage search flow."""

import json
from typing import Optional

import typer

from . import _runtime
from ._runtime import DryRunOpt, ForceOpt, OutputOpt, WaitOpt, console
from .query import _run_query_command

mortgage_app = typer.Typer(
    name="mortgage",
    help="Ispezioni ipotecarie persona fisica con anteprima, quote e conferma admin web",
    no_args_is_help=True,
)


@mortgage_app.command("start", hidden=True)
def mortgage_start(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Provincia dell'ufficio SISTER"),
    codice_fiscale: str = typer.Option(..., "--cf", help="Codice fiscale del soggetto"),
    output: OutputOpt = None,
    wait: WaitOpt = False,
    dry_run: DryRunOpt = False,
    force: ForceOpt = False,
):
    """Deprecated alias for `query ipotecaria-persona-fisica`."""
    typer.echo("`mortgage start` è deprecato; usa `query ipotecaria-persona-fisica`.", err=True)
    _run_query_command(
        "ipotecaria-persona-fisica",
        {"provincia": provincia, "codice_fiscale": codice_fiscale},
        wait,
        output,
        dry_run,
        force,
    )


@mortgage_app.command("status")
def mortgage_status(flow_id: int = typer.Argument(..., help="ID del flusso")):
    result = _runtime.run_api(lambda client: client.mortgage_search_status(flow_id))
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
    else:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))


@mortgage_app.command("quote")
def mortgage_quote(
    flow_id: int = typer.Argument(..., help="ID del flusso"),
    step: str = typer.Option("accept", "--step", help="Passaggio a pagamento da quotare"),
    ordinals: Optional[str] = typer.Option(None, "--ordinals", help="Formalità separate da virgola, per --step notes"),
    ordinal: Optional[int] = typer.Option(None, "--ordinal", help="Numero formalità, per --step title"),
    portal_request_id: Optional[str] = typer.Option(
        None, "--portal-request-id", help="idRichiesta esatto, per --step richieste_save"
    ),
    document_format: str = typer.Option("PDF", "--format", help="Formato richiesto (solo PDF è supportato)"),
):
    parsed_ordinals = None
    if ordinals is not None:
        try:
            parsed_ordinals = [int(value.strip()) for value in ordinals.split(",") if value.strip()]
        except ValueError as exc:
            raise typer.BadParameter("Inserisci numeri di formalità positivi, separati da virgole") from exc
    result = _runtime.run_api(
        lambda client: client.mortgage_search_quote(
            flow_id,
            step=step,
            ordinals=parsed_ordinals,
            ordinal=ordinal,
            portal_request_id=portal_request_id,
            format=document_format,
        )
    )
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
    else:
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))


mortgage_status.endpoint = "/api/v1/mortgage-searches/{id}"
mortgage_status.method = "GET"
mortgage_quote.endpoint = "/api/v1/mortgage-searches/{id}/quote"
mortgage_quote.method = "POST"
