"""Comandi di sviluppo che restano avvolti da helper locali."""

import subprocess

import typer

from . import _runtime

dev_app = typer.Typer(name="dev", help="Utilità per lo sviluppo.", no_args_is_help=True)


@dev_app.command(
    "fixture-payloads",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []},
)
def fixture_payloads(ctx: typer.Context) -> None:
    """Verifica i payload CLI (solo anteprima, per impostazione predefinita)."""
    try:
        project_root = _runtime.project_root()
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc

    command = ["bash", str(project_root / "scripts" / "sister_fixture_payload_commands.sh"), *ctx.args]
    try:
        def invoke():
            if _runtime.json_output_enabled():
                result = subprocess.run(command, cwd=project_root, capture_output=True, text=True, check=False)
                if result.stdout:
                    print(result.stdout, end="")
                if result.stderr:
                    typer.echo(result.stderr.rstrip("\n"), err=True)
                return result.returncode
            return subprocess.run(command, cwd=project_root, check=False).returncode

        returncode = _runtime.run_local_tool("dev fixture-payloads", invoke)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    raise typer.Exit(returncode)
