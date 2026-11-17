"""Typer entry points for browser-driven UI audits."""

import typer

from . import _runtime

audit_app = typer.Typer(name="audit", help="Esegue controlli automatici dell'interfaccia web.", no_args_is_help=True)


@audit_app.command("sweep")
def audit_sweep(user: str = typer.Argument("admin@aecs4u.com", help="Utente con cui eseguire il controllo")):
    """Controlla le pagine principali in temi chiaro/scuro e viewport mobile."""
    from ..command_tools.ui_audit.runner import run_audit

    result = _runtime.run_local_tool("audit sweep", lambda: run_audit("sweep", argv=[user]))
    if result:
        raise typer.Exit(result)


@audit_app.command("interactions")
def audit_interactions():
    """Controlla le interazioni dei moduli e delle pagine protette."""
    from ..command_tools.ui_audit.runner import run_audit

    result = _runtime.run_local_tool("audit interactions", lambda: run_audit("interactions"))
    if result:
        raise typer.Exit(result)


@audit_app.command("demo-purchases")
def audit_demo_purchases():
    """Controlla l'acquisto demo e i controlli di accesso ai documenti."""
    from ..command_tools.ui_audit.runner import run_audit

    result = _runtime.run_local_tool("audit demo-purchases", lambda: run_audit("demo_purchases"))
    if result:
        raise typer.Exit(result)


@audit_app.command("purchase-paths")
def audit_purchase_paths():
    """Controlla i link di acquisto e il flusso dei crediti prepagati."""
    from ..command_tools.ui_audit.runner import run_audit

    result = _runtime.run_local_tool("audit purchase-paths", lambda: run_audit("purchase_paths"))
    if result:
        raise typer.Exit(result)
