"""Commands for the admin-reviewed mortgage search flow."""

import json
from typing import Optional

import typer

from . import _runtime
from ._runtime import console

mortgage_app = typer.Typer(
    name="mortgage",
    help="Ispezioni ipotecarie persona fisica con anteprima, quote e conferma admin web",
    no_args_is_help=True,
)


@mortgage_app.command("start")
def mortgage_start(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Provincia dell'ufficio SISTER"),
    codice_fiscale: str = typer.Option(..., "--cf", help="Codice fiscale del soggetto"),
):
    """Queue the free preview stage; paid acceptance remains an admin web action."""
    result = _runtime.run_api(
        lambda client: client.mortgage_search_start(provincia=provincia, codice_fiscale=codice_fiscale)
    )
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("flow_id"):
        console.print(f"Admin review: /admin/mortgage-searches/{result['flow_id']}")


@mortgage_app.command("status")
def mortgage_status(flow_id: int = typer.Argument(..., help="ID del flusso")):
    result = _runtime.run_api(lambda client: client.mortgage_search_status(flow_id))
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
            raise typer.BadParameter("Use comma-separated positive formalità numbers") from exc
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
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))
