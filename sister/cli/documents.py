"""Document maintenance commands that need no separate tool process."""

import asyncio
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

import typer

from . import _runtime
from ._runtime import OutputOpt, console

documents_app = typer.Typer(
    name="documents", help="Importa, consulta e gestisce i documenti scaricati.", no_args_is_help=True
)


@documents_app.command("floor-plan-validate")
def documents_floor_plan_validate(
    visura: Path = typer.Option(..., "--visura", help="File JSON con la risposta o i dati della visura SISTER"),
    floor_plan: Path = typer.Option(..., "--floor-plan", help="PDF o immagine della planimetria"),
    calibration: Optional[Path] = typer.Option(None, "--calibration", help="File JSON di calibrazione"),
    rooms: Optional[Path] = typer.Option(None, "--rooms", help="File JSON con locali e poligoni"),
    property_index: int = typer.Option(0, "--property-index", min=0, help="Immobile in data.immobili da confrontare"),
    visura_area_m2: Optional[float] = typer.Option(None, "--visura-area-m2", min=0, help="Sostituisce la superficie estratta"),
    tolerance_m2: float = typer.Option(1.0, "--tolerance-m2", min=0, help="Tolleranza assoluta in metri quadrati"),
    tolerance_percent: float = typer.Option(0.05, "--tolerance-percent", min=0, help="Tolleranza relativa (0.05 = 5%%)"),
    output: OutputOpt = None,
):
    """Confronta la superficie catastale di una visura con la stima della planimetria."""
    try:
        from ..floor_plan import FloorPlanFeatureUnavailable, validate_visura_against_floor_plan

        visura_data = json.loads(visura.read_text(encoding="utf-8"))
        calibration_data = json.loads(calibration.read_text(encoding="utf-8")) if calibration else None
        rooms_data = json.loads(rooms.read_text(encoding="utf-8")) if rooms else None
        result = asyncio.run(
            validate_visura_against_floor_plan(
                visura=visura_data,
                floor_plan_path=floor_plan,
                calibration=calibration_data,
                rooms=rooms_data,
                property_index=property_index,
                visura_area_m2=visura_area_m2,
                tolerance_m2=tolerance_m2,
                tolerance_percent=tolerance_percent,
            )
        )
    except FloorPlanFeatureUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        console.print(f"[red]Dati di input non validi per la planimetria: {exc}[/red]")
        raise typer.Exit(1) from None
    except Exception as exc:  # noqa: BLE001 - keep local validation failures concise for CLI callers
        console.print(f"[red]Validazione della planimetria non riuscita: {exc}[/red]")
        raise typer.Exit(1) from None

    comparison = result["comparison"]
    status = comparison["status"]
    style = {"match": "green", "mismatch": "yellow", "unavailable": "red"}.get(status, "red")
    console.print(f"[{style}]Esito validazione: {status}[/{style}]")
    console.print(f"  Visura:     {comparison.get('visura_area_m2') or 'n/a'} m²")
    console.print(f"  Planimetria: {comparison.get('estimated_area_m2') or 'n/d'} m²")
    if comparison.get("difference_m2") is not None:
        console.print(f"  Differenza: {comparison['difference_m2']} m²")
    _runtime.emit_data(result, output=output)


@documents_app.command("import-receipts")
def documents_import_receipts(path: Path = typer.Argument(..., exists=True, readable=True)):
    """Importa una ricevuta AdE o tutti i file PDF/TXT di una cartella."""
    from .. import ade_ledger

    paths = [path] if path.is_file() else sorted(
        child for child in path.iterdir() if child.is_file() and child.suffix.casefold() in {".pdf", ".txt", ".text"}
    )
    if not paths:
        if _runtime.json_output_enabled():
            console.print("[yellow]Nessuna ricevuta PDF/TXT trovata.[/yellow]")
            _runtime.emit_data([])
        else:
            console.print("[yellow]Nessuna ricevuta PDF/TXT trovata.[/yellow]")
        raise typer.Exit(1)

    async def _import_all():
        results = []
        for source in paths:
            try:
                results.append({"file": source.name, **(await ade_ledger.import_receipt_file(source))})
            except Exception as exc:  # noqa: BLE001 - continue the directory batch and report each source outcome
                results.append({"file": source.name, "error": str(exc)[:200]})
        return results

    results = _runtime.run_db(_import_all)
    if _runtime.json_output_enabled():
        _runtime.emit_data(results)
    else:
        console.print_json(json.dumps(results, ensure_ascii=False, default=str))
    if any("error" in item for item in results):
        raise typer.Exit(1)


@documents_app.command("normalize")
def documents_normalize(
    documents: Path = typer.Option(..., "--documents", help="Cartella dei documenti"),
    links: Path | None = typer.Option(None, "--links", help="Cartella degli alias"),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Cartella di backup"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra rinomini e duplicati senza modificare i dati"),
    only: list[str] | None = typer.Option(None, "--only", help="Nome corrente da includere; ripetibile"),
):
    """Verifica i nomi canonici e i documenti duplicati."""
    from ..command_tools import normalize_documents

    try:
        result = _runtime.run_local_tool(
            "documents normalize",
            lambda: normalize_documents.run(
                documents=documents, links=links, backup_dir=backup_dir, apply=not dry_run, only=only
            ),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if result:
        raise typer.Exit(result)


@documents_app.command("reconcile")
def documents_reconcile(
    documents: Path = typer.Option(..., "--documents", help="Cartella dei documenti"),
    links: Path | None = typer.Option(None, "--links", help="Cartella degli alias"),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Cartella di backup"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra le righe da rimuovere senza modificare il database"),
):
    """Riconcilia righe documentali senza file locali equivalenti."""
    from ..command_tools import reconcile_missing_documents

    result = _runtime.run_local_tool(
        "documents reconcile",
        lambda: reconcile_missing_documents.run(
            documents=documents, links=links, backup_dir=backup_dir, apply=not dry_run
        ),
    )
    if result:
        raise typer.Exit(result)


@documents_app.command("cleanup-xml")
def documents_cleanup_xml(
    documents: Path = typer.Option(..., "--documents", help="Cartella dei documenti"),
    links: Path | None = typer.Option(None, "--links", help="Cartella degli alias"),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Cartella di backup PostgreSQL"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra i file XML estratti senza eliminarli"),
    skip_db: bool = typer.Option(False, "--skip-db", help="Controlla solo i file"),
):
    """Pulisce copie XML estratte già archiviate in modo sicuro."""
    from ..command_tools import cleanup_extracted_xml

    result = _runtime.run_local_tool(
        "documents cleanup-xml",
        lambda: cleanup_extracted_xml.run(
            documents=documents, links=links, backup_dir=backup_dir, apply=not dry_run, skip_db=skip_db
        ),
    )
    if result:
        raise typer.Exit(result)


@documents_app.command("sync-gcs")
def documents_sync_gcs(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra i file mancanti senza caricarli"),
):
    """Carica nel bucket GCS privato i documenti che non ci sono ancora (mai sovrascritti)."""
    from ..command_tools import sync_documents_to_gcs

    result = _runtime.run_local_tool(
        "documents sync-gcs", lambda: sync_documents_to_gcs.run(apply=not dry_run)
    )
    if result:
        raise typer.Exit(result)


@documents_app.command("rename-date")
def documents_rename_date(
    documents: Path = typer.Option(..., "--documents", help="Cartella dei documenti"),
    links: Path | None = typer.Option(None, "--links", help="Cartella degli alias"),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Cartella di backup"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Mostra le rinomine senza modificare i dati"),
    revert: Path | None = typer.Option(None, "--revert", help="Piano precedente da ripristinare"),
):
    """Rinomina i documenti usando le date di richiesta e di contenuto."""
    from ..command_tools import rename_documents_with_date

    result = _runtime.run_local_tool(
        "documents rename-date",
        lambda: rename_documents_with_date.run(
            documents=documents,
            links=links,
            backup_dir=backup_dir,
            apply=not dry_run,
            revert=revert,
        ),
    )
    if result:
        raise typer.Exit(result)


@documents_app.command("import")
def documents_import(
    documents: Path = typer.Option(
        Path(__file__).resolve().parents[2] / "documents", "--documents", help="Cartella dei documenti da indicizzare"
    ),
):
    """Indicizza i documenti locali in PostgreSQL senza spostare i file."""
    from ..command_tools import import_documents

    try:
        result = _runtime.run_local_tool(
            "documents import", lambda: _runtime.run_db(lambda: import_documents.run(documents=documents))
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    if result:
        raise typer.Exit(result)


@documents_app.command("import-ocular")
def documents_import_ocular(
    document_id: int = typer.Argument(..., min=1, help="ID del documento SISTER"),
    structured_json: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
):
    """Importa una singola estrazione strutturata Ocular."""
    from ..command_tools import import_ocular_extraction

    try:
        result = _runtime.run_local_tool(
            "documents import-ocular", lambda: import_ocular_extraction.run(document_id, structured_json)
        )
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"[red]Import Ocular non riuscito:[/red] {exc}")
        raise typer.Exit(1) from None
    if result:
        raise typer.Exit(result)


@documents_app.command("smart-ocr")
def documents_smart_ocr(
    output_root: Path | None = typer.Option(None, "--output-root", help="Cartella delle estrazioni Ocular"),
    workers: int = typer.Option(2, "--workers", min=1, max=4, help="Elaborazioni concorrenti"),
):
    """Esegue Smart OCR sui PDF idonei e importa i risultati strutturati."""
    from ..command_tools import process_sister_pdf_smart_ocr

    kwargs = {"workers": workers}
    if output_root is not None:
        kwargs["output_root"] = output_root
    result = _runtime.run_local_tool("documents smart-ocr", lambda: process_sister_pdf_smart_ocr.run(**kwargs))
    if result:
        raise typer.Exit(result)


@documents_app.command("download-missing-p7m")
def documents_download_missing_p7m(
    documents: Path | None = typer.Option(None, "--documents", help="Cartella PDF da controllare"),
    output: Path | None = typer.Option(None, "--output", help="File JSON con gli esiti delle richieste"),
    sister_url: str | None = typer.Option(None, "--sister-url", help="URL del servizio SISTER"),
):
    """Richiede le P7M mancanti per i PDF senza un compagno firmato."""
    from ..command_tools import download_missing_p7m

    kwargs = {key: value for key, value in {"documents": documents, "output": output, "base_url": sister_url}.items() if value is not None}
    if _runtime.json_output_enabled():
        with redirect_stdout(sys.stderr):
            status, report = download_missing_p7m.run(**kwargs, return_report=True)
    else:
        status, report = download_missing_p7m.run(**kwargs, return_report=True)
    _runtime.emit_data(report, output=output)
    if status:
        raise typer.Exit(status)


@documents_app.command("define-workflow")
def documents_define_workflow(
    documents: Path = typer.Option(..., "--documents", help="Cartella dei documenti sorgente"),
    manifest: Path | None = typer.Option(None, "--manifest", help="Percorso del manifest JSONL privato"),
    legacy_output: Path | None = typer.Option(None, "--output", hidden=True),
):
    """Crea un manifest privato di workflow dai documenti senza inviare richieste."""
    from ..command_tools import define_document_workflow_batch

    if legacy_output is not None and manifest is not None:
        raise typer.BadParameter("usa --manifest oppure il vecchio --output, non entrambi")
    if legacy_output is not None:
        typer.echo("--output è deprecato per il manifest; usa --manifest.", err=True)
    manifest_path = manifest or legacy_output or Path("outputs/document_workflow_batch.jsonl")
    if _runtime.json_output_enabled():
        with redirect_stdout(sys.stderr):
            status, report = define_document_workflow_batch.run(
                documents=documents, output=manifest_path, return_report=True
            )
        _runtime.emit_data(report)
    else:
        status, _report = define_document_workflow_batch.run(
            documents=documents, output=manifest_path, return_report=True
        )
    if status:
        raise typer.Exit(status)
