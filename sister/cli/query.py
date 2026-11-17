"""Generated single-step queries, workflows, and generic batch execution."""

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from ..client import API_V1_PREFIX, VisuraAPIError, VisuraClient
from ..fiscal_code import valid_codice_fiscale
from ..query_forms import QUERY_FORMS, documents_note, option_flags, validate_params
from . import _runtime
from ._runtime import console
from .render import _print_result

_handle_api_error = _runtime.handle_api_error
_write_output = _runtime.write_output

query_app = typer.Typer(
    name="query",
    help="Invia query catastali (ricerca, intestati, workflow, batch)",
    no_args_is_help=True,
)


def _load_batch_engine():
    """Import batch execution only for commands that need it, keeping ``sister --help`` light."""
    from .. import batch

    return batch


# =============================================================================
# query subcommands
# =============================================================================


# ---------------------------------------------------------------------------
# Single-step queries: every ``sister query <command>`` is generated from sister.query_forms
# ---------------------------------------------------------------------------


def _report_submission(
    params: dict,
    result: dict,
    request_ids: list[str],
    wait: bool,
    output: Optional[str],
    waited_results: dict[str, dict],
    wait_errors: list[tuple[str, Exception]],
) -> None:
    """Print a submission and the results already collected in its API event loop."""
    console.print(f"[bold green]Request submitted[/bold green] (status: {result.get('status', 'unknown')})")
    for rid in request_ids:
        console.print(f"  ID: [cyan]{rid}[/cyan]")
    if result.get("flow_id") is not None:
        console.print(f"  Flow: [cyan]{result['flow_id']}[/cyan]")
        console.print(f"  Admin review: [cyan]/admin/mortgage-searches/{result['flow_id']}[/cyan]")
    if params.get("codice_fiscale"):
        console.print(f"  CF: {params['codice_fiscale'].upper()}  Scope: {result.get('provincia', 'NAZIONALE')}")

    if not wait:
        console.print(
            "[dim]Poll results with:[/dim]\n" + "\n".join(f"  [bold]sister get {rid}[/bold]" for rid in request_ids)
        )
        console.print(
            "[dim]Or wait automatically:[/dim]\n"
            + "\n".join(f"  [bold]sister wait {rid}[/bold]" for rid in request_ids)
        )
        if output:
            _write_output(result, output)
        return

    for request_id, error in wait_errors:
        if isinstance(error, VisuraAPIError):
            console.print(f"[red]{request_id}: HTTP {error.status_code}: {error.detail}[/red]")
        else:
            console.print(f"[yellow]{error}[/yellow]")
    for response in waited_results.values():
        _print_result(response)
    failed = bool(wait_errors) or any(
        response.get("status") in {"error", "expired", "needs_human"} for response in waited_results.values()
    )
    if output and waited_results:
        _write_output(waited_results if len(waited_results) > 1 else next(iter(waited_results.values())), output)
    if failed:
        raise typer.Exit(1)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _confirm_download_again(result: dict, redownload: Optional[bool]) -> bool:
    """Whether to download again an identical query answered before: ``--yes``/``--no`` skip the question (default no)."""
    if redownload is not None:
        return redownload
    when = result.get("timestamp") or next(
        (item.get("timestamp") for item in result.get("cached_results", []) if item.get("timestamp")), ""
    )
    console.print(
        f"[yellow]Already downloaded[/yellow]{f' on {when[:10]}' if when else ''}: "
        + ", ".join(f"[cyan]{rid}[/cyan]" for rid in VisuraClient.request_ids(result))
    )
    if not _interactive():
        return False
    return typer.confirm("Download it again (a new request, possibly paid)?", default=False)


def _run_query_command(
    command: str,
    params: dict,
    wait: bool,
    output: Optional[str],
    dry_run: bool,
    force: bool,
    redownload: Optional[bool] = None,
) -> None:
    """Shared body of every generated query command: validate → (dry run) → submit → report.

    An identical query answered before the cache window is not run again silently: the previous answer is shown and
    the user is asked whether to download again (default no; ``--yes`` / ``--no`` answer in advance).
    """
    spec = QUERY_FORMS[command]
    try:
        validate_params(command, params)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(2)
    code = params.get("codice_fiscale", "").strip().upper()
    if len(code) == 16 and code.isalnum() and not valid_codice_fiscale(code):
        # SISTER answers a wrong check character with a confirmation page and then "nessuna corrispondenza": the answer is
        # cheap and final, so the query is still sent, but a typo is better noticed now than after the wait
        console.print(f"[yellow]Attenzione:[/yellow] il codice fiscale {code} ha un carattere di controllo errato")

    if dry_run:
        client = VisuraClient()
        console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent")
        console.print(f"  POST {client.base_url}{API_V1_PREFIX}{spec.path}")
        console.print(f"  Body: {json.dumps(params, ensure_ascii=False)}")
        if (note := documents_note(spec)) and not _documents_skipped(spec, params):
            console.print(f"  [yellow]{note}[/yellow]")
        return
    async def _submit_and_wait(client):
        if force or redownload is True:
            result = await client.submit(command, params, force=True)
        else:
            result = await client.submit(command, params, reuse_previous=True)
            if result.get("stale") and _confirm_download_again(result, redownload):
                result = await client.submit(command, params, force=True)

        request_ids = client.request_ids(result)
        waited_results: dict[str, dict] = {}
        wait_errors: list[tuple[str, Exception]] = []
        if wait:
            for request_id in request_ids:
                console.print(f"\n[dim]Waiting for {request_id}...[/dim]")
                try:
                    waited_results[request_id] = await client.wait_for_result(request_id)
                except TimeoutError as exc:
                    wait_errors.append((request_id, exc))
                except VisuraAPIError as exc:
                    if len(request_ids) == 1:
                        raise
                    wait_errors.append((request_id, exc))
        return result, request_ids, waited_results, wait_errors

    try:
        result, request_ids, waited_results, wait_errors = _runtime.run_api(_submit_and_wait)
    except TimeoutError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None
    _report_submission(params, result, request_ids, wait, output, waited_results, wait_errors)


def _documents_skipped(spec, params: dict) -> bool:
    return spec.documents == "optional" and str(params.get("richiedi_documenti", "")).lower() == "false"


def _make_query_command(form):
    """Build the Typer command of a query: one option per parameter of its spec plus the common ones."""
    parameters: list[inspect.Parameter] = []
    seen: set[str] = set()
    for fld in form.fields:
        if fld.param in seen:
            continue
        seen.add(fld.param)
        flags = option_flags(fld.param)
        if fld.kind == "checkbox":
            option, annotation = typer.Option(None, f"{flags[0]}/--no-{flags[0][2:]}", help=fld.help), Optional[bool]
        elif fld.param in form.required:
            option, annotation = typer.Option(..., *flags, help=fld.help), str
        else:
            option, annotation = typer.Option(None, *flags, help=fld.help), Optional[str]
        parameters.append(inspect.Parameter(fld.param, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation))
    common = {
        "output": (Optional[str], typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)")),
        "wait": (bool, typer.Option(False, "--wait", "-w", help="Attende il risultato invece di tornare subito")),
        "dry_run": (bool, typer.Option(False, "--dry-run", help="Mostra la richiesta senza inviarla")),
        "force": (bool, typer.Option(False, "--force", help="Ignora la cache e invia sempre una nuova richiesta")),
        "redownload": (
            Optional[bool],
            typer.Option(
                None,
                "--yes/--no",
                help="Se la stessa richiesta e' gia' stata scaricata: scarica di nuovo (--yes) o riusa il risultato "
                "(--no) senza chiedere. Senza opzione chiede conferma (default: no)",
            ),
        ),
    }
    for name, (annotation, option) in common.items():
        parameters.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation))

    def command(**kwargs):
        output, wait, dry_run, force, redownload = (kwargs.pop(name) for name in common)
        params = {
            name: (("true" if value else "false") if isinstance(value, bool) else str(value))
            for name, value in kwargs.items()
            if value is not None
        }
        _run_query_command(form.command, params, wait, output, dry_run, force, redownload)

    command.__signature__ = inspect.Signature(parameters)
    command.__annotations__ = {p.name: p.annotation for p in parameters}
    command.__name__ = form.command.replace("-", "_")
    command.__doc__ = f"{form.summary}\n\n{note}" if (note := documents_note(form)) else form.summary
    return command


for _form in QUERY_FORMS.values():
    query_app.command(_form.command)(_make_query_command(_form))


# -- Ispezioni Ipotecarie (paid service) --------------------------------------


def _ipotecaria_command(
    tipo_ricerca: str,
    provincia: str,
    client: VisuraClient,
    wait: bool,
    output: Optional[str],
    yes: bool = False,
    comune: Optional[str] = None,
    tipo_catasto: Optional[str] = None,
    codice_fiscale: Optional[str] = None,
    identificativo: Optional[str] = None,
    foglio: Optional[str] = None,
    particella: Optional[str] = None,
    numero_nota: Optional[str] = None,
    anno_nota: Optional[str] = None,
    force: bool = False,
):
    """Shared logic for ispezione ipotecaria CLI commands."""
    console.print(
        "[bold yellow]Preview only:[/bold yellow] paid portal steps require confirmation in the signed-in admin flow. "
        "The legacy [bold]--yes[/bold] option does not authorize a payment."
    )

    async def _submit_and_maybe_wait(active_client):
        result = await active_client.ispezione_ipotecaria(
            tipo_ricerca=tipo_ricerca,
            provincia=provincia,
            comune=comune,
            tipo_catasto=tipo_catasto,
            codice_fiscale=codice_fiscale,
            identificativo=identificativo,
            foglio=foglio,
            particella=particella,
            numero_nota=numero_nota,
            anno_nota=anno_nota,
            auto_confirm=False,
            force=force,
        )
        request_id = result.get("request_id", "")
        if not wait:
            return result, None
        console.print(f"\n[dim]Waiting for {request_id}...[/dim]")
        return result, await active_client.wait_for_result(request_id)

    try:
        result, waited_result = _runtime.run_api(_submit_and_maybe_wait, client=client)
    except TimeoutError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None

    request_id = result.get("request_id", "")
    console.print(f"[bold green]Request submitted[/bold green] (ispezione ipotecaria — {tipo_ricerca})")
    console.print(f"  ID: [cyan]{request_id}[/cyan]")

    if not wait:
        console.print(f"[dim]Poll result with:[/dim]\n  [bold]sister get {request_id}[/bold]")
        if output:
            _write_output(result, output)
        return

    # Show cost info if present.
    data = (waited_result or {}).get("data", {})
    if isinstance(data, dict) and data.get("cost"):
        cost = data["cost"]
        console.print(f"[yellow]Cost: {cost.get('text', 'N/A')} (€{cost.get('value', 0):.2f})[/yellow]")
        if not data.get("confirmed"):
            console.print("[yellow]Cost not confirmed. Continue through the admin paid-flow page.[/yellow]")
    _print_result(waited_result)
    if output:
        _write_output(waited_result, output)
    if waited_result.get("status") in {"error", "expired", "needs_human"}:
        raise typer.Exit(1)


def _ipotecaria_dry_run(tipo_ricerca: str, provincia: str, **fields: Optional[str]) -> None:
    """What an ispezione ipotecaria would send (paid service: nothing is sent)."""
    body = {"tipo_ricerca": tipo_ricerca, "provincia": provincia, **{k: v for k, v in fields.items() if v}}
    if body.get("codice_fiscale"):
        body["codice_fiscale"] = body["codice_fiscale"].upper()
    if body.get("tipo_catasto"):
        body["tipo_catasto"] = body["tipo_catasto"].upper()
    console.print("[bold yellow]DRY RUN[/bold yellow] — request will not be sent (SERVIZIO A PAGAMENTO)")
    console.print(f"  POST {VisuraClient().base_url}{API_V1_PREFIX}/visura/ispezione-ipotecaria")
    console.print(f"  Body: {json.dumps(body, ensure_ascii=False)}")


@query_app.command("ipotecaria-immobile")
def ipotecaria_immobile(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Nome della provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Nome del comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Numero di foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Numero di particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="File di output"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Attendi il risultato"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo anteprima"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache"),
    yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
):
    """Ispezione ipotecaria per immobile. SERVIZIO A PAGAMENTO."""
    client = VisuraClient()
    if dry_run:
        _ipotecaria_dry_run("immobile", provincia, comune=comune, tipo_catasto=tipo_catasto, foglio=foglio, particella=particella)
        return
    _ipotecaria_command(
        "immobile",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        comune=comune,
        tipo_catasto=tipo_catasto,
        foglio=foglio,
        particella=particella,
        force=force,
    )


@query_app.command("ipotecaria-persona")
def ipotecaria_persona(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Nome della provincia"),
    codice_fiscale: str = typer.Option(..., "--cf", help="Codice fiscale"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="File di output"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Attendi il risultato"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo anteprima"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache"),
    yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
):
    """Ispezione ipotecaria per persona (codice fiscale). SERVIZIO A PAGAMENTO."""
    client = VisuraClient()
    if dry_run:
        _ipotecaria_dry_run("persona_fisica", provincia, codice_fiscale=codice_fiscale)
        return
    _ipotecaria_command(
        "persona_fisica",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        codice_fiscale=codice_fiscale,
        force=force,
    )


@query_app.command("ipotecaria-azienda")
def ipotecaria_azienda(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Nome della provincia"),
    identificativo: str = typer.Option(..., "--id", help="P.IVA o denominazione"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="File di output"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Attendi il risultato"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo anteprima"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache"),
    yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
):
    """Ispezione ipotecaria per società (P.IVA o denominazione). SERVIZIO A PAGAMENTO."""
    client = VisuraClient()
    if dry_run:
        _ipotecaria_dry_run("persona_giuridica", provincia, identificativo=identificativo)
        return
    _ipotecaria_command(
        "persona_giuridica",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        identificativo=identificativo,
        force=force,
    )


@query_app.command("ipotecaria-nota")
def ipotecaria_nota(
    provincia: str = typer.Option(..., "--provincia", "-P", help="Nome della provincia"),
    numero_nota: str = typer.Option(..., "--numero", "-n", help="Numero nota"),
    anno_nota: Optional[str] = typer.Option(None, "--anno", help="Anno nota"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="File di output"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Attendi il risultato"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo anteprima"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache"),
    yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
):
    """Ispezione ipotecaria per riferimento nota. SERVIZIO A PAGAMENTO."""
    client = VisuraClient()
    if dry_run:
        _ipotecaria_dry_run("nota", provincia, numero_nota=numero_nota, anno_nota=anno_nota)
        return
    _ipotecaria_command(
        "nota",
        provincia,
        client,
        wait,
        output,
        yes=yes,
        numero_nota=numero_nota,
        anno_nota=anno_nota,
        force=force,
    )


# -- Workflow presets ---------------------------------------------------------

def _run_step(client, label, coro):
    """Run a single workflow step: submit → wait → print."""
    console.print(f"\n  [dim]{label}...[/dim]")
    try:
        submit = asyncio.run(coro)
        rid = submit.get("request_id", "")
        rids = submit.get("request_ids", [rid] if rid else [])
        results = _wait_for_requests(client, rids)
        return results[0] if len(results) == 1 else results
    except (TimeoutError, VisuraAPIError) as e:
        console.print(f"  [red]{e}[/red]")
        return {"status": "error", "error": str(e)}


def _wait_for_requests(client, request_ids, *, show_ids=True):
    """Wait for every submitted request, retaining per-request failures."""
    if show_ids:
        for request_id in request_ids:
            console.print(f"  ID: [cyan]{request_id}[/cyan]")

    results = []
    for request_id in request_ids:
        console.print(f"  [dim]Waiting for {request_id}...[/dim]")
        try:
            result = asyncio.run(client.wait_for_result(request_id))
            _print_result(result)
        except (TimeoutError, VisuraAPIError) as exc:
            console.print(f"  [red]{exc}[/red]")
            result = {"request_id": request_id, "status": "error", "error": str(exc)}
        results.append(result)
    return results


@query_app.command()
def workflow(
    preset: Optional[str] = typer.Option(
        None,
        "--preset",
        help="Famiglia di workflow: due-diligence, portfolio, fondiario, indirizzo, cross-reference",
    ),
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Nome della provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Nome del comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Numero di foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Numero di particella"),
    tipo_catasto: Optional[str] = typer.Option(
        None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati (ometti per entrambi)"
    ),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Sezione (facoltativa)"),
    subalterno: Optional[str] = typer.Option(
        None, "--subalterno", "-sub",
        help="Limita gli intestati a questo subalterno",
    ),
    codice_fiscale: Optional[str] = typer.Option(None, "--cf", help="Codice fiscale per la ricerca soggetto"),
    azienda_id: Optional[str] = typer.Option(None, "--azienda", help="P.IVA o denominazione"),
    indirizzo_str: Optional[str] = typer.Option(None, "--indirizzo", "-a", help="Indirizzo"),
    numero_nota: Optional[str] = typer.Option(None, "--nota", help="Numero nota/annotazione"),
    with_elenco: bool = typer.Option(False, "--elenco", help="Elenca tutti gli immobili del comune"),
    with_mappa: bool = typer.Option(False, "--mappa", help="Recupera i dati della mappa catastale"),
    with_ispezioni: bool = typer.Option(False, "--ispezioni", help="Recupera le ispezioni"),
    with_fiduciali: bool = typer.Option(False, "--fiduciali", help="Recupera i punti fiduciali"),
    with_originali: bool = typer.Option(False, "--originali", help="Recupera gli originali di impianto"),
    with_nota: bool = typer.Option(False, "--with-nota", help="Recupera i dati di note/annotazioni"),
    with_ispezioni_cart: bool = typer.Option(False, "--ispezioni-cart", help="Recupera le ispezioni cartacee"),
    depth: str = typer.Option("standard", "--depth", "-d", help="Profondità del workflow: light, standard, deep, full"),
    max_fanout: int = typer.Option(
        20, "--max-fanout",
        help="Numero massimo di immobili/intestati da espandere per passo",
    ),
    max_owners: int = typer.Option(10, "--max-owners", help="Numero massimo di intestati da espandere in owner_expand"),
    max_properties_per_owner: int = typer.Option(
        20, "--max-properties-per-owner", help="Numero massimo di immobili per intestatario nell'analisi del patrimonio"
    ),
    max_historical_properties: int = typer.Option(
        5, "--max-history",
        help="Numero massimo di immobili su cui eseguire il pacchetto storico",
    ),
    max_paid_steps: int = typer.Option(
        3, "--max-paid",
        help="Numero massimo di passaggi a pagamento (ispezione ipotecaria)",
    ),
    max_total_steps: int = typer.Option(100, "--max-steps", help="Limite complessivo di esecuzioni dei passaggi"),
    confirm_paid: bool = typer.Option(
        False, "--confirm-paid", help="Conferma automaticamente i costi dei servizi a pagamento"
    ),
    deprecated_yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
    include_paid: bool = typer.Option(
        False, "--include-paid",
        help="Includi i passaggi a pagamento (es. ispezione ipotecaria)",
    ),
    include_history: bool = typer.Option(
        False, "--history",
        help="Includi le query storiche su note e ispezioni cartacee",
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Percorso del file di output (.json)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Mostra i passaggi senza eseguirli"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache e invia sempre una nuova richiesta"),
):
    """Workflow multi-fase con preset facoltativo.

    \b
    Without --preset: runs search → intestati plus any flags you enable.
    With --preset: runs a named sequence of steps automatically.

    \b
    Workflow families:
      due-diligence        parcel checks; add --history for notes and paper inspections
      portfolio            person (--cf) or company (--azienda) asset investigation
      fondiario            land survey and cadastral-map records
      indirizzo            address → property search
      cross-reference      compare a person's and a company's properties

    \b
    Depth modes:
      light     Only core discovery steps (fast, free)
      standard  Adds per-property enrichment (default)
      deep      Adds owner expansion, paid inspections (requires --include-paid --confirm-paid)
      full      Multi-hop graph expansion with budgets (requires --include-paid --confirm-paid for paid steps)

    \b
    Examples:
      uv run sister query workflow --preset due-diligence -P Trieste -C TRIESTE -F 9 -p 166
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --history --nota 5678
      uv run sister query workflow --preset portfolio --cf RSSMRA85M01H501Z
      uv run sister query workflow --preset portfolio --azienda 02471840997 --depth full
      uv run sister query workflow --preset fondiario -P Roma -C ROMA -F 100
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --depth deep --include-paid --confirm-paid
      uv run sister query workflow --preset due-diligence -P Roma -C ROMA -F 1 -p 1 --depth full --include-paid --confirm-paid --max-paid 5
      uv run sister query workflow -P Trieste -C TRIESTE -F 9 -p 166 --elenco --mappa
    """
    if deprecated_yes:
        console.print("[red]--yes/-y no longer confirms paid workflow costs. Use --confirm-paid instead.[/red]")
        raise typer.Exit(2)

    if depth in {"deep", "full"} and include_paid and not confirm_paid:
        console.print(f"[red]--depth {depth} --include-paid requires --confirm-paid.[/red]")
        raise typer.Exit(2)

    # -- Server-side preset execution ------------------------------------------

    if preset:
        from ..workflows import prepare_workflow

        try:
            plan = prepare_workflow(
                {
                    "preset": preset,
                    "provincia": provincia,
                    "comune": comune,
                    "foglio": foglio,
                    "particella": particella,
                    "tipo_catasto": tipo_catasto,
                    "sezione": sezione,
                    "sezione_urbana": None,
                    "subalterno": subalterno,
                    "codice_fiscale": codice_fiscale,
                    "identificativo": azienda_id,
                    "indirizzo": indirizzo_str,
                    "numero_nota": numero_nota,
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
            )
        except (ValueError, KeyError) as exc:
            console.print(f"[red]Invalid workflow: {exc}[/red]")
            raise typer.Exit(1) from exc

        if dry_run:
            console.print(
                f"[bold yellow]DRY RUN[/bold yellow] — POST {API_V1_PREFIX}/catasto/workflow/run (preset={preset}, depth={depth})"
            )
            console.print(f"  {plan.description}")
            console.print(f"  Steps: {' → '.join(plan.steps)}")
            console.print(
                f"  Depth: [cyan]{depth}[/cyan]  Fanout: [cyan]{max_fanout}[/cyan]  Owners: [cyan]{max_owners}[/cyan]"
            )
            console.print(
                f"  Props/owner: [cyan]{max_properties_per_owner}[/cyan]  History: [cyan]{max_historical_properties}[/cyan]  Paid: [cyan]{max_paid_steps}[/cyan]  Max steps: [cyan]{max_total_steps}[/cyan]"
            )
            if include_paid:
                console.print(f"  [yellow]Paid steps enabled (auto_confirm={confirm_paid})[/yellow]")
            return

        console.print(f"[bold]Preset: {preset}[/bold] — {plan.description}")
        console.print(f"[dim]Depth: {depth} | Max fanout: {max_fanout}[/dim]")

        result = _runtime.run_api(
            lambda client: client.workflow(
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
                indirizzo=indirizzo_str,
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
            )
            )

        if result.get("error"):
            console.print(f"[red]Error: {result['error']}[/red]")
            raise typer.Exit(1)

        # Print step results
        steps_data = result.get("steps", [])
        for step in steps_data:
            status = step.get("status", "unknown")
            step_name = step.get("step", "?")
            style = "green" if status == "completed" else ("red" if status == "error" else "yellow")
            console.print(f"\n  [{style}]{step_name}[/{style}] — {status}")

            if status == "error":
                console.print(f"    [red]{step.get('error', '')}[/red]")
            elif status == "completed" and step.get("data"):
                data = step["data"]
                if isinstance(data, dict):
                    # Show counts for known keys
                    for key in ("immobili", "intestati", "risultati", "drill_results"):
                        items = data.get(key, [])
                        if items and isinstance(items, list):
                            console.print(f"    {key}: [cyan]{len(items)}[/cyan] records")
                    if "total" in data:
                        console.print(f"    total: [cyan]{data['total']}[/cyan]")
                    if data.get("truncated"):
                        console.print("    [yellow]Results truncated (max 20 drill-down properties)[/yellow]")

        # Summary
        summary = result.get("summary", {})
        console.rule("[bold green]Workflow complete[/bold green]")
        console.print(
            f"  Steps: [green]{summary.get('completed', 0)}[/green] completed, "
            f"[red]{summary.get('failed', 0)}[/red] failed, "
            f"[yellow]{summary.get('skipped', 0)}[/yellow] skipped"
        )
        if summary.get("properties", 0) > 0:
            console.print(
                f"  Properties: [cyan]{summary['properties']}[/cyan]  Owners: [cyan]{summary.get('owners', 0)}[/cyan]"
            )
        if summary.get("risk_flags", 0) > 0:
            console.print(f"  [yellow]Risk flags: {summary['risk_flags']}[/yellow]")

        if output:
            _write_output(result, output)
        return

    # -- Custom workflow (no preset) — client-side orchestration ---------------

    client = VisuraClient()
    all_data: dict = {}

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — custom workflow preview")
        if codice_fiscale:
            console.print(f"  → soggetto CF={codice_fiscale}")
        if azienda_id:
            console.print(f"  → azienda ID={azienda_id}")
        if indirizzo_str:
            console.print(f"  → indirizzo '{indirizzo_str}' {provincia}/{comune}")
        if foglio and particella:
            console.print(f"  → search {provincia}/{comune} F.{foglio} P.{particella}")
            console.print("  → intestati (for each sub-unit)")
        if with_elenco:
            console.print(f"  → elenco immobili {provincia}/{comune}")
        if with_mappa:
            console.print(f"  → mappa F.{foglio}")
        if with_ispezioni:
            console.print("  → ispezioni")
        if with_fiduciali:
            console.print("  → fiduciali")
        if with_originali:
            console.print("  → originali")
        if with_nota:
            console.print("  → nota")
        if with_ispezioni_cart:
            console.print("  → ispezioni cartacee")
        return

    # -- Phase: soggetto / azienda (if starting from person/company) ----------

    is_person_start = bool(codice_fiscale and not foglio)
    is_company_start = bool(azienda_id and not foglio and not codice_fiscale)

    if codice_fiscale and is_person_start:
        console.rule("[bold cyan]Soggetto — National CF search[/bold cyan]")
        all_data["soggetto"] = _run_step(
            client,
            f"Soggetto CF={codice_fiscale}",
            client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia),
        )

    if azienda_id and is_company_start:
        console.rule("[bold cyan]Azienda — Company search[/bold cyan]")
        all_data["persona_giuridica"] = _run_step(
            client,
            f"Persona giuridica ID={azienda_id}",
            client.persona_giuridica(identificativo=azienda_id, tipo_catasto=tipo_catasto, provincia=provincia),
        )

    # For patrimonio/aziendale: if we got properties from soggetto/azienda,
    # we could drill into each one — but that requires parsing the result table
    # and submitting per-property requests. For now, the soggetto/azienda result
    # already contains the property list. Intestati drill-down from soggetto
    # results would need the user to select specific properties.

    # -- Phase: indirizzo lookup (if starting from address) -------------------

    if indirizzo_str and provincia and comune:
        console.rule("[bold cyan]Indirizzo — Address lookup[/bold cyan]")
        all_data["indirizzo"] = _run_step(
            client,
            f"Indirizzo '{indirizzo_str}' in {provincia}/{comune}",
            client.generic_search(
                search_type="indirizzo",
                provincia=provincia,
                comune=comune,
                tipo_catasto=tipo_catasto or "T",
                indirizzo=indirizzo_str,
            ),
        )

    # -- Phase: search immobili (if we have foglio/particella) ----------------

    all_immobili = []
    search_results = {}

    if foglio and particella and provincia and comune:
        console.rule("[bold cyan]Search — Immobili[/bold cyan]")
        try:
            search_result = asyncio.run(
                client.search(
                    provincia=provincia,
                    comune=comune,
                    foglio=foglio,
                    particella=particella,
                    tipo_catasto=tipo_catasto,
                    sezione=sezione,
                )
            )
            request_ids = search_result.get("request_ids", [])
            console.print(f"Submitted {len(request_ids)} request(s)")
            search_results = dict(
                zip(request_ids, _wait_for_requests(client, request_ids, show_ids=False))
            )
        except VisuraAPIError as e:
            console.print(f"[red]Search failed: {e}[/red]")

        for res in search_results.values():
            if res.get("status") == "completed":
                data = res.get("data", {})
                if isinstance(data, dict):
                    all_immobili.extend(data.get("immobili", []))
        all_data["immobili"] = all_immobili

    # -- Phase: intestati (if search found immobili) --------------------------

    intestati_results = []

    if all_immobili and provincia and comune and foglio and particella:
        intestati_targets = []
        for res in search_results.values():
            if res.get("status") != "completed":
                continue
            tc = res.get("tipo_catasto", "")
            data = res.get("data", {})
            immobili = data.get("immobili", []) if isinstance(data, dict) else []

            if tc == "T":
                if not subalterno:
                    intestati_targets.append(("T", None))
            elif tc == "F":
                subs_found = {imm.get("Sub", "").strip() for imm in immobili if imm.get("Sub", "").strip()}
                if subalterno:
                    intestati_targets.append(("F", subalterno))
                elif subs_found:
                    intestati_targets.extend(("F", sub) for sub in sorted(subs_found))

        if intestati_targets:
            console.rule("[bold cyan]Intestati — Ownership[/bold cyan]")
            for tc, sub in intestati_targets:
                sub_label = f" Sub.{sub}" if sub else ""
                response = _run_step(
                    client,
                    f"{tc}{sub_label}",
                    client.intestati(
                        provincia=provincia,
                        comune=comune,
                        foglio=foglio,
                        particella=particella,
                        tipo_catasto=tc,
                        subalterno=sub,
                        sezione=sezione,
                    ),
                )
                responses = response if isinstance(response, list) else [response]
                for res in responses:
                    intestati_results.append(
                        {
                            "tipo_catasto": tc,
                            "subalterno": sub,
                            "request_id": res.get("request_id", ""),
                            **res,
                        }
                    )

    all_data["intestati_results"] = intestati_results

    # -- Phase: enrichment (elenco, mappa, fiduciali, originali, etc.) --------

    enrichment_steps = []
    if with_elenco and provincia and comune:
        enrichment_steps.append(
            (
                "elenco_immobili",
                f"Elenco immobili {provincia}/{comune}",
                client.elenco_immobili(provincia=provincia, comune=comune, tipo_catasto=tipo_catasto, foglio=foglio),
            )
        )
    if with_mappa and provincia and comune and foglio:
        enrichment_steps.append(
            (
                "mappa",
                f"Mappa F.{foglio}",
                client.generic_search(
                    search_type="mappa",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_fiduciali and provincia and comune:
        enrichment_steps.append(
            (
                "fiduciali",
                f"Punti fiduciali {provincia}/{comune}",
                client.generic_search(
                    search_type="fiduciali",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_originali and provincia and comune:
        enrichment_steps.append(
            (
                "originali",
                f"Originali di impianto {provincia}/{comune}",
                client.generic_search(
                    search_type="originali",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                ),
            )
        )
    if with_nota and provincia and numero_nota:
        enrichment_steps.append(
            (
                "nota",
                f"Nota {numero_nota}",
                client.generic_search(
                    search_type="nota", provincia=provincia, tipo_catasto=tipo_catasto or "T", numero_nota=numero_nota
                ),
            )
        )
    if with_ispezioni and provincia and comune:
        enrichment_steps.append(
            (
                "ispezioni",
                f"Ispezioni {provincia}/{comune}",
                client.generic_search(
                    search_type="ispezioni",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                    particella=particella,
                ),
            )
        )
    if with_ispezioni_cart and provincia and comune:
        enrichment_steps.append(
            (
                "ispezioni_cartacee",
                f"Ispezioni cartacee {provincia}/{comune}",
                client.generic_search(
                    search_type="ispezioni_cart",
                    provincia=provincia,
                    comune=comune,
                    tipo_catasto=tipo_catasto or "T",
                    foglio=foglio,
                    particella=particella,
                ),
            )
        )

    # Also add soggetto/azienda as enrichment if not already run as starting phase
    if codice_fiscale and not is_person_start:
        enrichment_steps.append(
            (
                "soggetto",
                f"Soggetto CF={codice_fiscale}",
                client.soggetto(codice_fiscale=codice_fiscale, tipo_catasto=tipo_catasto, provincia=provincia),
            )
        )
    if azienda_id and not is_company_start:
        enrichment_steps.append(
            (
                "persona_giuridica",
                f"Persona giuridica ID={azienda_id}",
                client.persona_giuridica(identificativo=azienda_id, tipo_catasto=tipo_catasto, provincia=provincia),
            )
        )

    if enrichment_steps:
        console.rule("[bold cyan]Enrichment[/bold cyan]")
        for key, label, coro in enrichment_steps:
            all_data[key] = _run_step(client, label, coro)

    # -- Summary --------------------------------------------------------------

    console.rule("[bold green]Workflow complete[/bold green]")
    parts = []
    if "immobili" in all_data:
        parts.append(f"Immobili: [cyan]{len(all_data['immobili'])}[/cyan]")
    if intestati_results:
        ok = sum(1 for r in intestati_results if r.get("status") == "completed")
        parts.append(f"Intestati: [green]{ok}[/green]/{len(intestati_results)}")
    for key in (
        "soggetto",
        "persona_giuridica",
        "elenco_immobili",
        "mappa",
        "fiduciali",
        "originali",
        "nota",
        "ispezioni",
        "ispezioni_cartacee",
        "indirizzo",
    ):
        if key in all_data and isinstance(all_data[key], dict):
            s = all_data[key].get("status", "?")
            style = "green" if s == "completed" else "red"
            parts.append(f"{key}: [{style}]{s}[/{style}]")
    console.print("  " + "  ".join(parts))

    if output:
        _write_output(all_data, output)


# -- batch command (supports all query types) ---------------------------------


def _parse_stop_at(value: str):
    """``HH:MM`` (today, local time) or an ISO date-time."""
    from datetime import datetime

    value = value.strip()
    if len(value) <= 5 and ":" in value:
        hour, minute = (int(part) for part in value.split(":"))
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return datetime.fromisoformat(value)


def _batch_printer(total: int):
    """Console progress for the batch engine's events."""
    batch_engine = _load_batch_engine()

    def emit(event: str, item, **info) -> None:
        tag = f"[dim]({item.index}/{total})[/dim]"
        if event == "skip":
            why = "already done (resume)" if info.get("resumed") else info.get("reason") or info["status"]
            detail = item.error or (f"same as {item.duplicate_of}" if item.duplicate_of else "")
            style = "red" if info["status"] == "invalid" else "yellow"
            console.print(f"  {tag} [{style}]{why}[/{style}] {item.id} {escape(item.label)} {detail}".rstrip())
        elif event == "start":
            console.print(f"\n{tag} {item.id} {escape(item.label)}")
        elif event == "submitted":
            console.print(f"  Submitted: {', '.join(info['request_ids'])}")
        elif event == "result":
            _print_result(info["result"])
        elif event == "retry":
            console.print(
                f"  [yellow]Attempt {info['attempt']} failed ({info['error']}); retrying in {info['delay']:.0f}s[/yellow]"
            )
        elif event == "done" and info["entry"]["status"] not in batch_engine.OK_STATUSES:
            entry = info["entry"]
            console.print(f"  [red]{entry['status']}[/red] {entry.get('error', '')}".rstrip())

    return emit


@query_app.command()
def batch(
    input_file: str = typer.Option(
        ...,
        "--input",
        "-I",
        help="File JSON (vedi `sister query batch-schema`), Markdown (docs/batch_markdown_format.md) o CSV",
    ),
    command: Optional[str] = typer.Option(
        None,
        "--command",
        "-c",
        help=(
            "Comando di query per le righe senza comando proprio (default: search)."
            " 'auto' = solo quello indicato nella riga/nel file"
        ),
    ),
    wait: Optional[bool] = typer.Option(
        None, "--wait/--no-wait", "-w", help="Attende ogni risultato (default: options.wait del file, altrimenti no)"
    ),
    output_dir: Optional[str] = typer.Option(
        None, "--output-dir", "-O", help="Cartella: un JSON per riga + il journal che permette --resume"
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="File di output unico (rapporto con tutti i risultati)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Valida e mostra il piano senza inviare nulla"),
    force: bool = typer.Option(False, "--force", help="Ignora la cache e invia sempre una nuova richiesta"),
    redownload: Optional[bool] = typer.Option(
        None,
        "--yes/--no",
        help="Righe gia' scaricate: scarica di nuovo (--yes) o riusa il risultato (--no) senza chiedere. "
        "Senza opzione chiede conferma per ogni riga (default: no)",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Riprende dal journal: salta le righe completate, riattende quelle gia' inviate (senza reinviarle) "
        "e ripete quelle fallite",
    ),
    journal: Optional[str] = typer.Option(
        None, "--journal", help="File journal (default: <output-dir>/batch_journal.jsonl)"
    ),
    max_in_flight: Optional[int] = typer.Option(None, "--max-in-flight", min=1, max=8, help="Righe gestite insieme"),
    retries: Optional[int] = typer.Option(None, "--retries", min=0, max=5, help="Tentativi extra per riga fallita"),
    poll_timeout: Optional[float] = typer.Option(None, "--poll-timeout", help="Secondi di attesa massima per richiesta"),
    on_error: Optional[str] = typer.Option(None, "--on-error", help="'continue' (default) o 'stop' al primo errore"),
    skip_invalid: bool = typer.Option(False, "--skip-invalid", help="Esegue le righe valide anche se altre non lo sono"),
    stop_at: Optional[str] = typer.Option(
        None,
        "--stop-at",
        help="Ora (HH:MM, oggi) o data ISO dopo cui non parte nessuna nuova riga: quella in corso finisce e resta nel "
        "journal, le altre restano da fare (--resume). Usato da `batch run-session` per chiudere prima della sessione",
    ),
):
    """Invia piu' query da un file JSON, Markdown o CSV, con validazione preventiva, retry e ripresa.

    Il file JSON (formato: docs/batch_format.md, schema: `sister query batch-schema`) contiene `defaults`, `options`
    e la lista `queries`. Il CSV resta supportato: le colonne sono i parametri del comando (stessi input del form
    SISTER, vedi ``sister query <command> --help``), una colonna `command` sceglie il comando di ogni riga, le altre
    colonne sono metadati.

    Tutte le righe sono validate prima di inviare qualsiasi richiesta; le query identiche sono eseguite una volta.
    Con --output-dir ogni cambio di stato va nel journal: dopo un'interruzione, --resume non reinvia le righe gia'
    inviate (una richiesta ripetuta puo' essere a pagamento).

    \b
    Example JSON:
        {"version": 1, "defaults": {"command": "search", "params": {"tipo_catasto": "F"}},
         "options": {"wait": true},
         "queries": [{"params": {"provincia": "Trieste", "comune": "TRIESTE", "foglio": 9, "particella": 166}},
                     {"command": "soggetto", "params": {"codice_fiscale": "RSSMRA85M01H501Z"}}]}

    \b
    Example CSV (mixed, using 'command' column):
        command,provincia,comune,foglio,particella,codice_fiscale,tipo_catasto
        search,Trieste,TRIESTE,9,166,,F
        soggetto,,,,,RSSMRA85M01H501Z,
    """
    batch_engine = _load_batch_engine()
    path = Path(input_file)
    if not path.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(1)
    try:
        spec = batch_engine.load_batch(path, None if command in (None, "auto") else command)
    except ValueError as e:  # pydantic's ValidationError is one
        console.print(f"[red]Invalid input file: {e}[/red]")
        raise typer.Exit(1)

    overrides = {
        "wait": wait,
        "output_dir": output_dir,
        "max_in_flight": max_in_flight,
        "retries": retries,
        "poll_timeout": poll_timeout,
        "on_error": on_error,
        "reuse": "force" if (force or redownload is True) else ("reuse" if redownload is False else None),
    }
    try:
        options = batch_engine.BatchOptions.model_validate(
            {**spec.options.model_dump(), **{k: v for k, v in overrides.items() if v is not None}}
        )
    except ValueError as e:
        console.print(f"[red]Invalid option: {e}[/red]")
        raise typer.Exit(2)

    plan = batch_engine.plan_batch(spec, options)
    console.print(f"Loaded [cyan]{len(plan)}[/cyan] row(s) from {path.name}" + (f" — {spec.name}" if spec.name else ""))

    invalid = [item for item in plan if item.state == "invalid"]
    for item in invalid:
        console.print(f"  [red]({item.index}/{len(plan)}) {item.id}: {item.error}[/red]")
    if invalid and not (skip_invalid or dry_run):
        console.print(
            f"[red]{len(invalid)} invalid row(s): nothing was sent. Fix the file or use --skip-invalid.[/red]"
        )
        raise typer.Exit(2)

    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — requests will not be sent")
        for item in plan:
            note = {"duplicate": f" (duplicate of {item.duplicate_of})", "disabled": " (disabled)"}.get(item.state, "")
            if item.state != "invalid":
                console.print(f"  {item.index}. {item.id} {escape(item.label)}{note}")
        return

    journal_path = Path(journal) if journal else (Path(options.output_dir) / batch_engine.JOURNAL_NAME if options.output_dir else None)
    if resume and journal_path is None:
        console.print("[red]--resume needs --output-dir or --journal (where the previous run was recorded).[/red]")
        raise typer.Exit(2)
    if resume and not journal_path.exists():
        console.print(f"[yellow]No journal at {journal_path}: running everything.[/yellow]")
    if journal_path and not resume and journal_path.exists():
        console.print(f"[dim]Appending to existing journal {journal_path} (use --resume to continue it).[/dim]")

    try:
        stop_time = _parse_stop_at(stop_at) if stop_at else None
    except ValueError as e:
        console.print(f"[red]Invalid --stop-at: {e}[/red]")
        raise typer.Exit(2)

    client = VisuraClient()
    runner = batch_engine.BatchRunner(
        client,
        options,
        journal=batch_engine.BatchJournal(journal_path) if journal_path else None,
        resume=resume,
        confirm=(lambda result: _confirm_download_again(result, None)) if options.reuse == "ask" else None,
        emit=_batch_printer(len(plan)),
        stop_at=stop_time,
    )

    try:
        report = _runtime.run_api(lambda active_client: runner.run(plan, spec.name), client=client)
    except typer.Exit as exc:
        if exc.exit_code == 130:
            console.print("\n[yellow]Interrupted. Rerun with --resume to continue without resending.[/yellow]")
        raise

    console.rule("[bold green]Batch complete[/bold green]")
    counts = report.counts
    ok = counts.get("completed", 0) + counts.get("queued", 0)
    console.print(
        f"  Total: [cyan]{len(report.entries)}[/cyan]  OK: [green]{ok}[/green]  Errors: [red]{report.failed}[/red]"
        + "".join(f"  {k}: {v}" for k, v in sorted(counts.items()) if k not in ("completed", "queued", "error"))
    )
    if output:
        _write_output(report.to_dict(), output)
    if report.stopped == batch_engine.STOPPED_SESSION_EXPIRED:
        console.print(
            "[red]La sessione SISTER e' scaduta: le righe rimaste non sono state inviate.[/red] "
            "Nuovo login (sister app login), poi rilancia con --resume."
        )
        raise typer.Exit(_runtime.EXIT_SESSION_EXPIRED)
    if report.stopped == batch_engine.STOPPED_DEADLINE:
        console.print("[yellow]--stop-at raggiunto: le righe rimaste restano da fare (--resume).[/yellow]")
    if report.failed:
        raise typer.Exit(1)


@query_app.command("batch-schema")
def batch_schema(output: Optional[str] = typer.Option(None, "--output", "-o", help="Scrive lo schema su file")):
    """Stampa lo JSON Schema del file di sottomissione di `query batch` (per editor e validatori)."""
    batch_engine = _load_batch_engine()
    schema = batch_engine.batch_json_schema()
    if output:
        _write_output(schema, output)
    else:
        typer.echo(json.dumps(schema, indent=2, ensure_ascii=False))
