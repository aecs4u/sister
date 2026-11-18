"""Generated single-step queries, workflows, and generic batch execution."""

import inspect
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from ..client import API_V1_PREFIX, VisuraAPIError, VisuraClient
from ..fiscal_code import valid_codice_fiscale
from ..query_forms import LEGACY_FLAGS, QUERY_FORMS, documents_note, option_flags, validate_params
from . import _runtime
from ._runtime import DryRunOpt, ForceOpt, OutputOpt, RedownloadOpt, WaitOpt, console
from .render import render_result

_LEGACY_PREFIX = "legacy_"  # name prefix of the hidden option that carries a deprecated flag (see LEGACY_FLAGS)

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
    console.print(f"[bold green]Richiesta inviata[/bold green] (stato: {result.get('status', 'unknown')})")
    for rid in request_ids:
        console.print(f"  ID richiesta: [cyan]{rid}[/cyan]")
    if result.get("flow_id") is not None:
        console.print(f"  Flusso: [cyan]{result['flow_id']}[/cyan]")
        console.print(f"  Revisione admin: [cyan]/admin/mortgage-searches/{result['flow_id']}[/cyan]")
    if params.get("codice_fiscale"):
        console.print(f"  CF: {params['codice_fiscale'].upper()}  Ambito: {result.get('provincia', 'NAZIONALE')}")

    if not wait:
        console.print(
            "[dim]Recupera i risultati con:[/dim]\n"
            + "\n".join(f"  [bold]sister requests get {rid}[/bold]" for rid in request_ids)
        )
        console.print(
            "[dim]Oppure attendi il completamento:[/dim]\n"
            + "\n".join(f"  [bold]sister requests wait {rid}[/bold]" for rid in request_ids)
        )
        _runtime.emit_data(result, output=output)
        return

    for request_id, error in wait_errors:
        if isinstance(error, VisuraAPIError):
            console.print(f"[red]{request_id}: HTTP {error.status_code}: {error.detail}[/red]")
        else:
            console.print(f"[yellow]{error}[/yellow]")
    for response in waited_results.values():
        console.print(render_result(response))
    if waited_results:
        payload = waited_results if len(waited_results) > 1 else next(iter(waited_results.values()))
    else:
        payload = result
    _runtime.emit_data(payload, output=output)
    if wait_errors:
        raise typer.Exit(1)
    _runtime.exit_for_status(*waited_results.values())


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
        f"[yellow]Già scaricata[/yellow]{f' il {when[:10]}' if when else ''}: "
        + ", ".join(f"[cyan]{rid}[/cyan]" for rid in VisuraClient.request_ids(result))
    )
    if not _interactive():
        return False
    return typer.confirm("Scaricarla di nuovo (nuova richiesta, potenzialmente a pagamento)?", default=False)


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
        console.print(f"[bold red]Errore:[/bold red] {e}")
        raise typer.Exit(2)
    code = params.get("codice_fiscale", "").strip().upper()
    if len(code) == 16 and code.isalnum() and not valid_codice_fiscale(code):
        # SISTER answers a wrong check character with a confirmation page and then "nessuna corrispondenza": the answer is
        # cheap and final, so the query is still sent, but a typo is better noticed now than after the wait
        console.print(f"[yellow]Attenzione:[/yellow] il codice fiscale {code} ha un carattere di controllo errato")

    if spec.paid:
        console.print(
            "[bold yellow]Solo anteprima:[/bold yellow] i passaggi a pagamento richiedono conferma nel flusso admin autenticato. "
            "La vecchia opzione [bold]--yes[/bold] non autorizza pagamenti."
        )

    if dry_run:
        client = VisuraClient()
        paid_label = " (SERVIZIO A PAGAMENTO)" if spec.paid else ""
        params = {**spec.fixed_params, **params}
        endpoint = f"{client.base_url}{API_V1_PREFIX}{spec.path}"
        console.print(f"[bold yellow]SIMULAZIONE[/bold yellow] — la richiesta non verrà inviata{paid_label}")
        console.print(f"  POST {endpoint}")
        console.print(f"  Dati: {json.dumps(params, ensure_ascii=False)}")
        if (note := documents_note(spec)) and not _documents_skipped(spec, params):
            console.print(f"  [yellow]{note}[/yellow]")
        _runtime.emit_data(
            {
                "dry_run": True,
                "command": command,
                "endpoint": endpoint,
                "params": params,
                "paid": spec.paid,
            },
            output=output,
        )
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
                console.print(f"\n[dim]In attesa della richiesta {request_id}...[/dim]")
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
        if (legacy := LEGACY_FLAGS.get(fld.param)) and fld.kind != "checkbox":
            parameters.append(
                inspect.Parameter(
                    f"{_LEGACY_PREFIX}{fld.param}",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=typer.Option(None, legacy, hidden=True),
                    annotation=Optional[str],
                )
            )
    common = {
        "output": (OutputOpt, None),
        "wait": (WaitOpt, False),
        "dry_run": (DryRunOpt, False),
        "force": (ForceOpt, False),
    }
    if not form.paid:
        common["redownload"] = (RedownloadOpt, None)
    for name, (annotation, default) in common.items():
        parameters.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    if form.paid:
        parameters.append(
            inspect.Parameter(
                "deprecated_yes",
                inspect.Parameter.KEYWORD_ONLY,
                default=typer.Option(False, "--yes", "-y", hidden=True),
                annotation=bool,
            )
        )

    def command(**kwargs):
        output, wait, dry_run, force = (kwargs.pop(name) for name in ("output", "wait", "dry_run", "force"))
        redownload = kwargs.pop("redownload", None)
        deprecated_yes = kwargs.pop("deprecated_yes", False)
        for legacy_name in [name for name in kwargs if name.startswith(_LEGACY_PREFIX)]:
            legacy_value = kwargs.pop(legacy_name)
            param = legacy_name.removeprefix(_LEGACY_PREFIX)
            if legacy_value is not None:
                typer.echo(f"`{LEGACY_FLAGS[param]}` è deprecato; usa `--{param.replace('_', '-')}`.", err=True)
                if kwargs.get(param) is None:
                    kwargs[param] = legacy_value
        params = {
            name: (("true" if value else "false") if isinstance(value, bool) else str(value))
            for name, value in kwargs.items()
            if value is not None
        }
        if deprecated_yes:
            typer.echo("Per le ispezioni a pagamento, la vecchia opzione --yes/-y viene ignorata.", err=True)
        _run_query_command(form.command, params, wait, output, dry_run, force, redownload)

    command.__signature__ = inspect.Signature(parameters)
    command.__annotations__ = {p.name: p.annotation for p in parameters}
    command.__name__ = form.command.replace("-", "_")
    command.__doc__ = f"{form.summary}\n\n{note}" if (note := documents_note(form)) else form.summary
    command.endpoint = f"/api/v1{form.path}"
    command.method = "POST"
    command.query_form = True
    command.paid = form.paid
    return command


for _form in QUERY_FORMS.values():
    query_app.command(_form.command)(_make_query_command(_form))


def _deprecated_query_alias(alias: str, form):
    """Build one hidden alias that forwards to the same generated query implementation."""
    target = _make_query_command(form)

    def command(**kwargs):
        typer.echo(f"`query {alias}` è deprecato; usa `query {form.command}`.", err=True)
        return target(**kwargs)

    command.__signature__ = target.__signature__
    command.__annotations__ = target.__annotations__
    command.__name__ = alias.replace("-", "_")
    command.__doc__ = f"Deprecated alias for `query {form.command}`."
    return command


query_app.command("ipotecaria-persona", hidden=True)(
    _deprecated_query_alias("ipotecaria-persona", QUERY_FORMS["ipotecaria-persona-fisica"])
)


# -- Deprecated workflow alias ------------------------------------------------


@query_app.command(hidden=True)
def workflow(
    preset: Optional[str] = typer.Option(None, "--preset", help="Famiglia di workflow"),
    provincia: Optional[str] = typer.Option(None, "--provincia", "-P", help="Nome della provincia"),
    comune: Optional[str] = typer.Option(None, "--comune", "-C", help="Nome del comune"),
    foglio: Optional[str] = typer.Option(None, "--foglio", "-F", help="Numero di foglio"),
    particella: Optional[str] = typer.Option(None, "--particella", "-p", help="Numero di particella"),
    tipo_catasto: Optional[str] = typer.Option(None, "--tipo-catasto", "-t", help="'T' = Terreni, 'F' = Fabbricati"),
    sezione: Optional[str] = typer.Option(None, "--sezione", help="Sezione catastale"),
    subalterno: Optional[str] = typer.Option(None, "--subalterno", "-sub", help="Limita agli intestati del subalterno"),
    codice_fiscale: Optional[str] = typer.Option(None, "--cf", help="Codice fiscale"),
    azienda_id: Optional[str] = typer.Option(None, "--azienda", help="Partita IVA o denominazione"),
    indirizzo_str: Optional[str] = typer.Option(None, "--indirizzo", "-a", help="Indirizzo"),
    numero_nota: Optional[str] = typer.Option(None, "--nota", help="Numero nota/annotazione"),
    with_elenco: bool = typer.Option(False, "--elenco", help="Elenca gli immobili del comune"),
    with_mappa: bool = typer.Option(False, "--mappa", help="Recupera i dati della mappa catastale"),
    with_ispezioni: bool = typer.Option(False, "--ispezioni", help="Recupera le ispezioni"),
    with_fiduciali: bool = typer.Option(False, "--fiduciali", help="Recupera i punti fiduciali"),
    with_originali: bool = typer.Option(False, "--originali", help="Recupera gli originali di impianto"),
    with_nota: bool = typer.Option(False, "--with-nota", help="Recupera i dati delle note"),
    with_ispezioni_cart: bool = typer.Option(False, "--ispezioni-cart", help="Recupera le ispezioni cartacee"),
    depth: str = typer.Option("standard", "--depth", "-d", help="Profondità del workflow"),
    max_fanout: int = typer.Option(20, "--max-fanout"),
    max_owners: int = typer.Option(10, "--max-owners"),
    max_properties_per_owner: int = typer.Option(20, "--max-properties-per-owner"),
    max_historical_properties: int = typer.Option(5, "--max-history"),
    max_paid_steps: int = typer.Option(3, "--max-paid"),
    max_total_steps: int = typer.Option(100, "--max-steps"),
    confirm_paid: bool = typer.Option(False, "--confirm-paid"),
    deprecated_yes: bool = typer.Option(False, "--yes", "-y", hidden=True),
    include_paid: bool = typer.Option(False, "--include-paid"),
    include_history: bool = typer.Option(False, "--history"),
    output: OutputOpt = None,
    dry_run: DryRunOpt = False,
    force: ForceOpt = False,
):
    """Deprecated alias for `sister workflow run`."""
    if deprecated_yes:
        console.print("[red]--yes/-y non conferma i costi del workflow. Usa --confirm-paid.[/red]")
        raise typer.Exit(2)

    typer.echo("`sister query workflow` è deprecato; usa `sister workflow run`.", err=True)
    from .workflow import workflow_run

    with_steps = [
        step
        for enabled, step in (
            (with_elenco, "elenco"),
            (with_mappa, "mappa"),
            (with_fiduciali, "fiduciali"),
            (with_originali, "originali"),
            (with_nota, "nota"),
            (with_ispezioni, "ispezioni"),
            (with_ispezioni_cart, "ispezioni_cart"),
        )
        if enabled
    ]
    return workflow_run(
        preset=preset or "custom",
        provincia=provincia,
        comune=comune,
        foglio=foglio,
        particella=particella,
        tipo_catasto=tipo_catasto,
        sezione=sezione,
        subalterno=subalterno,
        codice_fiscale=codice_fiscale,
        azienda_id=azienda_id,
        indirizzo=indirizzo_str,
        numero_nota=numero_nota,
        with_steps=with_steps,
        depth=depth,
        max_fanout=max_fanout,
        max_owners=max_owners,
        max_properties_per_owner=max_properties_per_owner,
        max_historical_properties=max_historical_properties,
        max_paid_steps=max_paid_steps,
        max_total_steps=max_total_steps,
        confirm_paid=confirm_paid,
        include_paid=include_paid,
        include_history=include_history,
        wait=True,
        output=output,
        dry_run=dry_run,
        force=force,
    )


workflow.endpoint = f"{API_V1_PREFIX}/catasto/workflow/run"
workflow.method = "POST"


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
        if _runtime.json_output_enabled():
            typer.echo(
                json.dumps(
                    {"event": event, "index": item.index, "total": total, "id": item.id, **info},
                    ensure_ascii=False,
                    default=str,
                ),
                err=True,
            )
            return
        tag = f"[dim]({item.index}/{total})[/dim]"
        if event == "skip":
            why = "già completata (ripresa)" if info.get("resumed") else info.get("reason") or info["status"]
            detail = item.error or (f"same as {item.duplicate_of}" if item.duplicate_of else "")
            style = "red" if info["status"] == "invalid" else "yellow"
            console.print(f"  {tag} [{style}]{why}[/{style}] {item.id} {escape(item.label)} {detail}".rstrip())
        elif event == "start":
            console.print(f"\n{tag} {item.id} {escape(item.label)}")
        elif event == "submitted":
            console.print(f"  Inviate: {', '.join(info['request_ids'])}")
        elif event == "result":
            console.print(render_result(info["result"]))
        elif event == "retry":
            console.print(
                f"  [yellow]Tentativo {info['attempt']} non riuscito ({info['error']}); nuovo tentativo tra {info['delay']:.0f}s[/yellow]"
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
    output: OutputOpt = None,
    dry_run: DryRunOpt = False,
    force: ForceOpt = False,
    redownload: RedownloadOpt = None,
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
        console.print(f"[red]File non trovato: {input_file}[/red]")
        raise typer.Exit(1)
    try:
        spec = batch_engine.load_batch(path, None if command in (None, "auto") else command)
    except ValueError as e:  # pydantic's ValidationError is one
        console.print(f"[red]File di input non valido: {e}[/red]")
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
        console.print(f"[red]Opzione non valida: {e}[/red]")
        raise typer.Exit(2)

    plan = batch_engine.plan_batch(spec, options)
    console.print(f"Caricate [cyan]{len(plan)}[/cyan] righe da {path.name}" + (f" — {spec.name}" if spec.name else ""))

    invalid = [item for item in plan if item.state == "invalid"]
    for item in invalid:
        console.print(f"  [red]({item.index}/{len(plan)}) {item.id}: {item.error}[/red]")
    if invalid and not (skip_invalid or dry_run):
        console.print(
            f"[red]{len(invalid)} righe non valide: non è stato inviato nulla. Correggi il file o usa --skip-invalid.[/red]"
        )
        raise typer.Exit(2)

    if dry_run:
        console.print("[bold yellow]SIMULAZIONE[/bold yellow] — le richieste non verranno inviate")
        plan_data = []
        for item in plan:
            note = {"duplicate": f" (duplicata di {item.duplicate_of})", "disabled": " (disattivata)"}.get(item.state, "")
            if item.state != "invalid":
                console.print(f"  {item.index}. {item.id} {escape(item.label)}{note}")
            plan_data.append(
                {
                    "index": item.index,
                    "id": item.id,
                    "command": item.command,
                    "params": item.params,
                    "state": item.state,
                    "error": item.error,
                    "duplicate_of": item.duplicate_of,
                }
            )
        if _runtime.json_output_enabled():
            _runtime.emit_data({"name": spec.name, "plan": plan_data})
        return

    journal_path = Path(journal) if journal else (Path(options.output_dir) / batch_engine.JOURNAL_NAME if options.output_dir else None)
    if resume and journal_path is None:
        console.print("[red]--resume richiede --output-dir oppure --journal (dove è stato registrato il run precedente).[/red]")
        raise typer.Exit(2)
    if resume and not journal_path.exists():
        console.print(f"[yellow]Nessun journal in {journal_path}: verranno eseguite tutte le righe.[/yellow]")
    if journal_path and not resume and journal_path.exists():
        console.print(f"[dim]Aggiunta al journal esistente {journal_path} (usa --resume per riprenderlo).[/dim]")

    try:
        stop_time = _parse_stop_at(stop_at) if stop_at else None
    except ValueError as e:
        console.print(f"[red]Valore non valido per --stop-at: {e}[/red]")
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
            console.print("\n[yellow]Interrotto. Rilancia con --resume per continuare senza reinviare le righe.[/yellow]")
        raise

    console.rule("[bold green]Batch completato[/bold green]")
    counts = report.counts
    ok = counts.get("completed", 0) + counts.get("queued", 0)
    console.print(
        f"  Totale: [cyan]{len(report.entries)}[/cyan]  OK: [green]{ok}[/green]  Errori: [red]{report.failed}[/red]"
        + "".join(f"  {k}: {v}" for k, v in sorted(counts.items()) if k not in ("completed", "queued", "error"))
    )
    _runtime.emit_data(report.to_dict(), output=output)
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


batch.endpoint = "/api/v1/visura (×N)"
batch.method = "POST"


@query_app.command("batch-schema")
def batch_schema(output: OutputOpt = None):
    """Stampa lo JSON Schema del file di sottomissione di `query batch` (per editor e validatori)."""
    batch_engine = _load_batch_engine()
    schema = batch_engine.batch_json_schema()
    if output:
        _runtime.emit_data(schema, output=output)
    elif _runtime.json_output_enabled():
        _runtime.emit_data(schema)
    else:
        typer.echo(json.dumps(schema, indent=2, ensure_ascii=False))
