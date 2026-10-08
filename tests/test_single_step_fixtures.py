"""Result fixtures of the single-step queries (tests/fixtures/single_step/<query>/<outcome>.json).

Each fixture is a real response captured from the portal (anonymised) paired with the parameters that
produced it; the tests check that the parameters are valid for the query's CLI/form and that the response
has the shape the rest of the code (results page, dossier, cache) relies on.
"""

import json
import re
from pathlib import Path

import pytest

from sister.models import VisuraResponse
from sister.query_forms import QUERY_FORMS, param_names

FIXTURES = Path(__file__).parent / "fixtures" / "single_step"
FILES = sorted(FIXTURES.glob("*/*.json"))

# keys every completed response of a query carries in ``data``
_DATA_KEYS = {
    "search": {"immobili"},
    "intestati": {"immobili"},
    "soggetto": {"soggetto", "immobili", "total_results"},
    "azienda": {"soggetto", "immobili", "total_results"},
    "elenco": {"provincia", "comune", "foglio", "immobili", "total_results"},
    "indirizzo": {"provincia", "comune", "indirizzo", "immobili", "total_results"},
    "partita": {"provincia", "comune", "partita", "immobili", "total_results"},
    "elaborato-planimetrico": {"provincia", "comune", "risultati", "total_results"},
    "fiduciali": {"provincia", "comune", "risultati", "total_results"},
    "ispezioni": {"provincia", "comune", "risultati", "total_results"},
    "riepilogo": {"risultati", "total_results"},
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_single_step_query_has_a_fixture():
    covered = {_load(p)["query"] for p in FILES}
    # mappa (known issue: submit button), ispezioni-cartacee, richieste: no usable live capture yet
    with_form = {q for q, form in QUERY_FORMS.items() if form.fixture}
    assert covered >= with_form - {"mappa", "ispezioni-cartacee"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_fixture_request_uses_parameters_of_the_query(path):
    fixture = _load(path)
    unknown = set(fixture["request"]) - set(param_names(fixture["query"]))
    assert not unknown, f"{path.name}: parameters the query does not have: {sorted(unknown)}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_fixture_response_is_a_valid_service_response(path):
    fixture = _load(path)
    response = fixture["response"]
    assert response["status"] in {"completed", "error"}
    # the same payload the service stores must load as a VisuraResponse
    VisuraResponse(
        request_id=response["request_id"],
        success=response["status"] == "completed",
        cadastre_type=response["tipo_catasto"],
        data=response.get("data"),
        error=response.get("error"),
    )
    outcome = fixture["outcome"]
    data = response.get("data") or {}
    if outcome in {"success", "no-match", "empty"}:
        assert response["status"] == "completed"
        assert _DATA_KEYS[fixture["query"]] <= set(data), f"{path.name}: data keys {sorted(data)}"
    if outcome == "success":
        assert data["total_results"] >= 1 if "total_results" in data else data["immobili"]
    if outcome == "no-match":
        assert data.get("error") == "NESSUNA CORRISPONDENZA TROVATA" and data["total_results"] == 0
    if outcome == "form-rejected":
        assert response["status"] == "error" and response["error"].startswith("SISTER ha rifiutato il modulo")


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_fixture_holds_no_personal_data(path):
    text = path.read_text(encoding="utf-8")
    codes = set(re.findall(r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]", text))
    assert all(code.startswith("TSTUSR") for code in codes), codes
    assert "/data/aecs4u" not in text and "xml_content" not in text
