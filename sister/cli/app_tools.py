"""Typer entry points for local app and SISTER session tools."""

import os
import signal
import subprocess
import time
from pathlib import Path

import typer

from . import _runtime

app_tools_app = typer.Typer(name="app", help="Avvia e gestisce l'app locale e la sessione SISTER.", no_args_is_help=True)


@app_tools_app.command("login")
def app_login(close: bool = typer.Option(False, "--close", help="Chiude solo la sessione SISTER precedente")):
    """Accede a SISTER nella sessione Chrome condivisa."""
    import asyncio

    from ..command_tools import ade_login

    try:
        result = _runtime.run_local_tool("app login", lambda: asyncio.run(ade_login.run(close_only=close)))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    if result:
        raise typer.Exit(result)


@app_tools_app.command("hash-password")
def app_hash_password(username: str = typer.Argument(..., help="Nome utente locale")):
    """Genera una voce hash per local_users.txt."""
    import getpass

    from ..command_tools import hash_local_password

    password = getpass.getpass("Password: ")
    status, entry = hash_local_password.run(username, password, getpass.getpass("Ripeti: "))
    if status:
        if _runtime.json_output_enabled():
            _runtime.emit_data({"error": "Le password non coincidono", "exit_code": status})
        else:
            typer.echo("Le password non coincidono", err=True)
        raise typer.Exit(status)
    if _runtime.json_output_enabled():
        _runtime.emit_data({"username": username, "entry": entry})
    else:
        typer.echo(entry)


@app_tools_app.command("import-local-users")
def app_import_local_users(
    file: Path = typer.Option(None, "--file", help="Legacy hashed-account file (default: LOCAL_USERS_FILE)"),
    apply: bool = typer.Option(False, "--apply", help="Scrive nella tabella users (default: solo anteprima)"),
):
    """Importa gli account legacy nella tabella aecs4u-auth users (idempotente)."""
    import asyncio

    from ..command_tools import import_local_users

    default_users_file = Path(__file__).resolve().parents[3] / "local_users.txt"
    path = file or Path(os.getenv("LOCAL_USERS_FILE", str(default_users_file))).expanduser()
    admins = {name.strip() for name in os.getenv("SISTER_ADMIN_USERS", "").split(",") if name.strip()}
    result = asyncio.run(import_local_users.run(path, admins, apply))
    if _runtime.json_output_enabled():
        _runtime.emit_data(result)
        return
    typer.echo("APPLICATO" if apply else "ANTEPRIMA (usa --apply per scrivere)")
    for label in ("created", "updated", "unchanged", "skipped_plaintext", "invalid_email", "conflicts"):
        typer.echo(f"{label}: {result[label]}")


@app_tools_app.command("start")
def app_start(
    port: int = typer.Option(8025, "--port", min=1, max=65535, help="Porta di ascolto"),
    no_reload: bool = typer.Option(False, "--no-reload", help="Disabilita il riavvio automatico"),
):
    """Avvia il servizio SISTER locale con Uvicorn."""
    root = _runtime.project_root()
    command = ["bash", str(root / "scripts" / "start.sh"), "--port", str(port)]
    if no_reload:
        command.append("--no-reload")
    try:
        if _runtime.json_output_enabled():
            def start_with_captured_output():
                completed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
                if completed.stdout:
                    print(completed.stdout, end="")
                if completed.stderr:
                    typer.echo(completed.stderr.rstrip("\n"), err=True)
                return completed.returncode

            result = _runtime.run_local_tool("app start", start_with_captured_output)
            raise typer.Exit(result)
        result = subprocess.run(command, cwd=root, check=False)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    raise typer.Exit(result.returncode)


def _listening_processes(port: int, proc_root: Path = Path("/proc")) -> dict[int, bool]:
    """Map listening PIDs on ``port`` to whether their command line names the SISTER app."""
    socket_inodes: set[str] = set()
    for table in (proc_root / "net" / "tcp", proc_root / "net" / "tcp6"):
        try:
            lines = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                socket_inodes.add(fields[9])

    found: dict[int, bool] = {}
    if not socket_inodes:
        return found
    try:
        processes = (entry for entry in proc_root.iterdir() if entry.name.isdigit())
        for process in processes:
            try:
                owns_listener = False
                for fd in (process / "fd").iterdir():
                    try:
                        target = os.readlink(fd)
                    except OSError:
                        continue
                    if target.startswith("socket:[") and target[8:-1] in socket_inodes:
                        owns_listener = True
                        break
                if not owns_listener:
                    continue
                command = (process / "cmdline").read_bytes()
                found[int(process.name)] = b"sister.main" in command
            except (OSError, PermissionError, ValueError):
                continue
    except OSError:
        return found
    return found


def stop_server(port: int = 8025, *, pid: int | None = None, proc_root: Path = Path("/proc")) -> int:
    """Stop only a process that both listens on ``port`` and runs ``sister.main``."""
    if not 1 <= port <= 65535:
        typer.echo("La porta deve essere tra 1 e 65535", err=True)
        return 2
    listeners = _listening_processes(port, proc_root)
    if not listeners:
        typer.echo(f"Nessun processo in ascolto sulla porta {port}.")
        return 0
    matches = sorted(pid for pid, is_sister in listeners.items() if is_sister)
    if pid is not None:
        if pid not in matches:
            typer.echo(f"Il processo {pid} non e' un servizio SISTER in ascolto sulla porta {port}.", err=True)
            return 2
        target = pid
    elif len(matches) == 1:
        target = matches[0]
    elif len(matches) > 1:
        typer.echo(f"Trovati più processi SISTER sulla porta {port}: {', '.join(map(str, matches))}; scegli --pid.", err=True)
        return 2
    else:
        typer.echo(f"La porta {port} e' occupata da un processo che non risulta SISTER; non lo fermo.", err=True)
        return 1

    try:
        os.kill(target, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"Il processo {target} e' già terminato.")
        return 0
    except PermissionError:
        typer.echo(f"Permesso negato per fermare il processo {target}.", err=True)
        return 1

    for _ in range(20):
        if target not in _listening_processes(port, proc_root):
            typer.echo(f"Servizio SISTER fermato sulla porta {port}.")
            return 0
        time.sleep(0.25)
    try:
        os.kill(target, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        typer.echo(f"Permesso negato per terminare il processo {target}.", err=True)
        return 1
    for _ in range(20):
        if target not in _listening_processes(port, proc_root):
            typer.echo(f"Servizio SISTER terminato sulla porta {port}.")
            return 0
        time.sleep(0.25)
    typer.echo(f"Il processo {target} continua ad ascoltare sulla porta {port}.", err=True)
    return 1


@app_tools_app.command("stop")
def app_stop(
    port: int = typer.Argument(8025, min=1, max=65535, help="Porta del servizio SISTER"),
    pid: int | None = typer.Option(None, "--pid", min=1, help="PID SISTER da fermare se ci sono più listener"),
):
    """Ferma un servizio SISTER verificando il PID e il comando associato alla porta."""
    result = _runtime.run_local_tool("app stop", lambda: stop_server(port, pid=pid))
    if result:
        raise typer.Exit(result)
