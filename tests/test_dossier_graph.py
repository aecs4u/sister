"""Unit tests for the dossier graph builder's pure logic (no service, browser or portal)."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_dossier_graph", Path(__file__).resolve().parents[1] / "scripts" / "build_dossier_graph.py"
)
graph_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(graph_mod)

ROW = {
    "Catasto": "F",
    "Titolarità": "Proprieta' per 1/2",
    "Ubicazione": "RAVENNA(RA) VIA EL ALAMEIN n. 2 Piano T-1",
    "Foglio": "RA/103",
    "Particella": "1714",
    "Sub": "2",
    "provincia": "RA",
    "visImmSel": "634568#634568#F#RA/103#1714#H199##2# #RAVENNA",
    "intestati": [
        {"codice_fiscale": "PGGPLA52C15H199K", "tipo": "persona", "nome": "POGGI PAOLO", "quota": "1/2"},
        {"codice_fiscale": "03531340960", "tipo": "azienda", "nome": "ZEROTRE S.R.L.", "quota": "1/2"},
    ],
}


def test_property_key_uses_portal_selector_value():
    assert graph_mod.property_key(ROW) == "F|RA|H199|103|1714||2"


def test_property_key_falls_back_to_listing_columns():
    row = {key: value for key, value in ROW.items() if key != "visImmSel"}
    assert graph_mod.property_key(row) == "F|RA||103|1714||2"


def test_absorb_links_owners_both_ways_and_queues_new_ones():
    graph = graph_mod.Graph()
    graph.add_owner({"codice_fiscale": "PGGPLA52C15H199K"}, 0)
    graph.queue.pop(0)
    graph.expanded["PGGPLA52C15H199K"] = {}
    info = graph.absorb("PGGPLA52C15H199K", [ROW], depth=0)

    assert info == {"properties": 1, "new_owners": 1}  # the seed already existed, only the company is new
    assert graph.nodes["03531340960"]["type"] == "company"
    assert graph.nodes["03531340960"]["depth"] == 1
    assert graph.queue == ["03531340960"]  # the seed is already expanded, the company is next
    assert set(graph.edges) == {"PGGPLA52C15H199K->F|RA|H199|103|1714||2", "03531340960->F|RA|H199|103|1714||2"}
    assert graph.nodes["F|RA|H199|103|1714||2"]["comune"] == "RAVENNA"


def test_graph_round_trips_through_json():
    graph = graph_mod.Graph()
    graph.add_owner({"codice_fiscale": "PGGPLA52C15H199K"}, 0)
    graph.absorb("PGGPLA52C15H199K", [ROW], 0)
    again = graph_mod.Graph(graph.to_json())
    assert again.nodes == graph.nodes and again.edges == graph.edges and again.queue == graph.queue


def test_harvest_ids_finds_nested_codici_fiscali():
    data = {"results": {"x": {"data": {"soggetto": "PGGPLA52C15H199K", "intestati": [{"Codice fiscale": "03531340960"}]}}}}
    assert graph_mod.harvest_ids(data) == {"PGGPLA52C15H199K", "03531340960"}
