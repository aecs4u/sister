"""Temporary registry for commands that still forward to standalone tools."""

import subprocess
import sys

import typer

from . import _runtime

FORWARD_CONTEXT = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": [],
}


def forward_command(group: typer.Typer, name: str, target: str, description: str, *, shell: bool = False) -> None:
    """Register a command that forwards its arguments to a packaged tool or shell helper."""

    def forward(ctx: typer.Context) -> None:
        try:
            project_root = _runtime.project_root()
        except FileNotFoundError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if shell:
            command = ["bash", str(project_root / target)]
        else:
            command = [sys.executable, "-m", target]
        command.extend(ctx.args)
        try:
            result = subprocess.run(command, cwd=project_root, check=False)
        except KeyboardInterrupt:
            raise typer.Exit(130) from None
        raise typer.Exit(result.returncode)

    forward.__name__ = f"forward_{group.info.name}_{name.replace('-', '_')}"
    forward.__doc__ = description
    group.command(
        name=name,
        context_settings=FORWARD_CONTEXT,
        help=description,
    )(forward)


app_tools_app = typer.Typer(name="app", help="Start and maintain the local app and login session.", no_args_is_help=True)
batch_app = typer.Typer(name="batch", help="Prepare, run, and monitor query batches.", no_args_is_help=True)
documents_app = typer.Typer(
    name="documents", help="Import, inspect, and maintain downloaded documents.", no_args_is_help=True
)
build_app = typer.Typer(name="build", help="Create dossier graphs and building map exports.", no_args_is_help=True)
audit_app = typer.Typer(name="audit", help="Run browser-based UI audits.", no_args_is_help=True)
dev_app = typer.Typer(name="dev", help="Developer utilities.", no_args_is_help=True)

_tool_commands = {
    app_tools_app: {
        "login": ("sister.command_tools.ade_login", "Log in to SISTER or close a stale session."),
        "start": ("scripts/start.sh", "Start the SISTER app.", True),
        "stop": ("../bin/kill_app.sh", "Stop the local app on a port (default 8025).", True),
        "hash-password": ("sister.command_tools.hash_local_password", "Generate a local user password hash."),
    },
    batch_app: {
        "build-list": ("sister.command_tools.build_batch_from_list", "Build a batch plan from a supported list."),
        "convert-pvp-csv": (
            "sister.command_tools.convert_pvp_sales_csvs_to_markdown",
            "Convert PVP CSV files to Markdown lists.",
        ),
        "build-pdf-parseable": (
            "sister.command_tools.build_pdf_parseable_batch",
            "Build the PDF-parseable batch.",
        ),
        "run-session": ("sister.command_tools.run_in_session", "Run saved batches in the current SISTER session."),
        "run-list": ("scripts/run_list_in_session.sh", "Build and run a supported Markdown list.", True),
        "run-pvp-is": ("scripts/run_pvp_sales_is.sh", "Prompt for and run a PVP IS list.", True),
        "run-pdf-parseable": ("scripts/run_pdf_parseable_batch.sh", "Build and run the PDF-parseable batch.", True),
        "progress": ("sister.command_tools.batch_progress", "Display saved batch progress."),
        "prune-list": (
            "sister.command_tools.prune_code_list",
            "Remove rows already answered from a Markdown batch list.",
        ),
        "convert-list": (
            "sister.command_tools.convert_code_list",
            "Rewrite a legacy codici list as a Markdown batch file.",
        ),
        "recrowd": (
            "sister.command_tools.recrowd_storica_analitica",
            "Check or request Recrowd subject or property history (choose --items).",
        ),
    },
    documents_app: {
        "define-workflow": (
            "sister.command_tools.define_document_workflow_batch",
            "Build a document due-diligence manifest.",
        ),
        "import": ("sister.command_tools.import_documents", "Index local SISTER documents."),
        "import-ocular": (
            "sister.command_tools.import_ocular_extraction",
            "Import one structured Ocular extraction.",
        ),
        "smart-ocr": (
            "sister.command_tools.process_sister_pdf_smart_ocr",
            "Run Smart OCR for eligible PDFs.",
        ),
        "download-missing-p7m": (
            "sister.command_tools.download_missing_p7m",
            "Find and request missing signed P7M files.",
        ),
        "rename-date": (
            "sister.command_tools.rename_documents_with_date",
            "Preview or apply date-based renames.",
        ),
        "normalize": (
            "sister.command_tools.normalize_documents",
            "Preview or apply canonical names and duplicate cleanup.",
        ),
        "cleanup-xml": (
            "sister.command_tools.cleanup_extracted_xml",
            "Review or clean extracted XML sidecars.",
        ),
        "reconcile": (
            "sister.command_tools.reconcile_missing_documents",
            "Review or reconcile missing document rows.",
        ),
    },
    build_app: {
        "dossier-graph": ("sister.command_tools.build_dossier_graph", "Build or resume an ownership graph."),
        "building-kmz": ("sister.command_tools.build_building_kmz", "Build a building KMZ from a location."),
    },
    audit_app: {
        "sweep": ("sister.command_tools.ui_audit.sweep", "Check pages for visual and accessibility issues."),
        "interactions": ("sister.command_tools.ui_audit.interactions", "Check UI interactions."),
        "demo-purchases": ("sister.command_tools.ui_audit.demo_purchases", "Check the demo purchase flow."),
        "purchase-paths": (
            "sister.command_tools.ui_audit.purchase_paths",
            "Check document purchase access paths.",
        ),
    },
    dev_app: {
        "fixture-payloads": (
            "scripts/sister_fixture_payload_commands.sh",
            "Exercise CLI query fixtures (dry-run by default).",
            True,
        ),
    },
}

for _group, _commands in _tool_commands.items():
    for _name, _command in _commands.items():
        _target, _description, *_shell = _command
        forward_command(_group, _name, _target, _description, shell=bool(_shell and _shell[0]))
