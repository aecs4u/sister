"""Typer entry points for dossier and building exports."""

from pathlib import Path

import typer

from . import _runtime

build_app = typer.Typer(name="build", help="Crea grafi documentali ed esportazioni cartografiche.", no_args_is_help=True)


@build_app.command("dossier-graph")
def build_dossier_graph(
    seed: list[str] = typer.Option([], "--seed", help="Codice fiscale o partita IVA; ripetibile"),
    from_batch: Path | None = typer.Option(None, "--from-batch", help="Cartella con risultati batch da cui ricavare i soggetti"),
    resume: Path | None = typer.Option(None, "--resume", help="File JSON del grafo da continuare"),
    out: Path | None = typer.Option(None, "--out", help="Percorso del file JSON"),
    max_depth: int = typer.Option(0, "--max-depth", min=0, help="Profondità massima; 0 senza limite"),
    max_nodes: int = typer.Option(0, "--max-nodes", min=0, help="Numero massimo di nodi; 0 senza limite"),
    retry_errors: bool = typer.Option(False, "--retry-errors", help="Rimette in coda le espansioni fallite"),
    max_requests: int = typer.Option(300, "--max-requests", min=0, help="Richieste portale massime per sessione"),
):
    """Crea o riprende il grafo delle proprietà e dei titolari."""
    from ..command_tools import build_dossier_graph as graph_tool

    result = _runtime.run_local_tool(
        "build dossier-graph",
        lambda: graph_tool.run(
            seeds=seed,
            from_batch=from_batch,
            resume=resume,
            out=out,
            max_depth=max_depth,
            max_nodes=max_nodes,
            retry_errors=retry_errors,
            max_requests=max_requests,
        ),
    )
    if result:
        raise typer.Exit(result)


@build_app.command("building-kmz")
def build_building_kmz(
    lat: float = typer.Option(..., "--lat", help="Latitudine WGS84"),
    lng: float = typer.Option(..., "--lng", help="Longitudine WGS84"),
    radius: int = typer.Option(80, "--radius", min=1, help="Raggio di ricerca in metri"),
    name: str = typer.Option("Edificio", "--name", help="Nome visualizzato nel KMZ"),
    nearest: int = typer.Option(3, "--nearest", min=0, help="Edifici vicini da evidenziare"),
    osm_json: Path | None = typer.Option(None, "--osm-json", help="Risposta Overpass salvata da riutilizzare"),
    out: Path = typer.Option(..., "--out", help="File KMZ da creare"),
):
    """Crea un KMZ 3D degli edifici OpenStreetMap attorno a un punto."""
    from ..command_tools import build_building_kmz as kmz_tool

    result = _runtime.run_local_tool(
        "build building-kmz",
        lambda: kmz_tool.run(lat=lat, lng=lng, radius=radius, name=name, nearest=nearest, osm_json=osm_json, out=out),
    )
    if result:
        raise typer.Exit(result)
