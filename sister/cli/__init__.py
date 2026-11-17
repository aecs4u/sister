"""Entry point for the SISTER CLI.

The command tree is imported only when the CLI is invoked or a caller asks for ``app``.
Shared runtime modules can therefore be imported without initializing Typer and every
CLI command.
"""

from importlib import import_module

__all__ = ["app", "run"]


def __getattr__(name: str):
    if name == "app":
        commands = import_module("._commands", __name__)
        return getattr(commands, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run() -> None:
    """Load environment settings and launch the Typer app."""
    commands = import_module("._commands", __name__)
    commands.run()
