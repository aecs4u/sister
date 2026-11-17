"""Entry point and compatibility exports for the SISTER CLI.

The command definitions are imported only when the CLI is invoked or a caller asks for
the legacy public attributes. Shared runtime modules can therefore be imported without
initializing Typer and every CLI command.
"""

from importlib import import_module

__all__ = [
    "EXIT_SESSION_EXPIRED",
    "VisuraClient",
    "_interactive",
    "_parse_stop_at",
    "_project_root",
    "_tool_commands",
    "app",
    "run",
    "subprocess",
]

_COMMAND_EXPORTS = frozenset(__all__) - {"EXIT_SESSION_EXPIRED", "run"}


def __getattr__(name: str):
    if name == "EXIT_SESSION_EXPIRED":
        runtime = import_module("._runtime", __name__)
        return runtime.EXIT_SESSION_EXPIRED
    if name in _COMMAND_EXPORTS:
        commands = import_module("._commands", __name__)
        return getattr(commands, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run() -> None:
    """Load environment settings and launch the Typer app."""
    commands = import_module("._commands", __name__)
    commands.run()
