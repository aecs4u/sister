"""Contract tests for the Pydantic ontology (ontology.py) against the realistic payloads in tests/fixtures.py."""

import pytest
from pydantic import ValidationError

from sister import ontology as o
from tests.fixtures import (
    SISTER_ELENCO_RESPONSE,
    SISTER_INTESTATI_RESPONSE,
    SISTER_NESSUNA_CORRISPONDENZA,
    SISTER_PG_EMPTY_RESPONSE,
    SISTER_PNF_RESPONSE,
    SISTER_SEARCH_RESPONSE_FABBRICATI,
    SISTER_SEARCH_RESPONSE_TERRENI,
    SISTER_SOGGETTO_EMPTY_RESPONSE,
    SISTER_SOGGETTO_RESPONSE,
)


def _dossier(model, api_response: dict):
    """Build a dossier envelope from an API-style fixture (status → success)."""
    return model.model_validate(
        {
            "request_id": api_response["request_id"],
            "success": api_response["status"] == "completed",
            "tipo_catasto": api_response["tipo_catasto"],
            "data": api_response.get("data"),
            "error": api_response.get("error"),
            "timestamp": api_response.get("timestamp"),
        }
    )


# ---------------------------------------------------------------------------
# Payloads validate against their data models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,fixture",
    [
        (o.DossierRicercaImmobile, SISTER_SEARCH_RESPONSE_FABBRICATI),
        (o.DossierRicercaImmobile, SISTER_SEARCH_RESPONSE_TERRENI),
        (o.DossierRicercaImmobile, SISTER_NESSUNA_CORRISPONDENZA),
        (o.DossierSoggetto, SISTER_SOGGETTO_RESPONSE),
        (o.DossierSoggetto, SISTER_SOGGETTO_EMPTY_RESPONSE),
        (o.DossierPersonaGiuridica, SISTER_PNF_RESPONSE),
        (o.DossierPersonaGiuridica, SISTER_PG_EMPTY_RESPONSE),
    ],
    ids=lambda v: v.get("request_id") if isinstance(v, dict) else v.__name__,
)
def test_fixture_payloads_validate(model, fixture):
    dossier = _dossier(model, fixture)

    assert dossier.success is True
    assert dossier.data is not None
    assert dossier.data.total_results == fixture["data"]["total_results"]


def test_intestati_payload_validates():
    data = o.DatiIntestati.model_validate(SISTER_INTESTATI_RESPONSE["data"])

    assert data.intestati[0]["Codice fiscale"] == "RSSMRI85E28H501E"


def test_elenco_payload_validates():
    data = o.DatiElencoImmobili.model_validate(SISTER_ELENCO_RESPONSE["data"])

    assert data.total_results == len(data.immobili) == 3


def test_persona_giuridica_keeps_no_match_error():
    dossier = _dossier(o.DossierPersonaGiuridica, SISTER_PG_EMPTY_RESPONSE)

    assert dossier.data.error == "NESSUNA CORRISPONDENZA TROVATA"


# Fields that real payloads carry but the ontology silently drops (pydantic ignores extras).
_DROPPED = {
    "soggetto_no_match": (o.DatiSoggetto, SISTER_SOGGETTO_EMPTY_RESPONSE["data"]),
    "ricerca_no_match": (o.DatiRicercaImmobile, SISTER_NESSUNA_CORRISPONDENZA["data"]),
    "elenco_location": (o.DatiElencoImmobili, SISTER_ELENCO_RESPONSE["data"]),
}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("soggetto_no_match"),
        pytest.param("ricerca_no_match"),
        pytest.param("elenco_location"),
    ],
)
def test_ontology_does_not_drop_payload_fields(case):
    model, payload = _DROPPED[case]

    dropped = set(payload) - set(model.model_fields)

    assert not dropped, f"{model.__name__} drops {sorted(dropped)}"


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_ricerca_dossier_rejects_entity_cadastre_type():
    with pytest.raises(ValidationError):
        o.DossierRicercaImmobile(request_id="x", success=True, tipo_catasto="E")


def test_dossier_base_rejects_unknown_cadastre_type():
    with pytest.raises(ValidationError):
        o.DossierSoggetto(request_id="x", success=True, tipo_catasto="Z")


def test_soggetto_payload_requires_subject():
    with pytest.raises(ValidationError):
        o.DatiSoggetto(immobili=[])


def test_workflow_step_status_is_constrained():
    assert o.WorkflowStep(step="search").status == "pending"
    with pytest.raises(ValidationError):
        o.WorkflowStep(step="search", status="done")


def test_workflow_dossier_defaults_are_independent():
    a = o.DossierWorkflow(workflow_id="w1", preset="due-diligence")
    b = o.DossierWorkflow(workflow_id="w2", preset="due-diligence")
    a.aggregate.properties.append({"x": 1})

    assert b.aggregate.properties == []
    assert a.aggregate.risk_scores.severity_counts.high == 0
    assert a.summary.total_steps == 0


def test_coppia_wraps_both_cadastres():
    coppia = o.DossierCoppia(
        request_id="pair",
        fabbricati=_dossier(o.DossierRicercaImmobile, SISTER_SEARCH_RESPONSE_FABBRICATI),
        terreni=_dossier(o.DossierRicercaImmobile, SISTER_SEARCH_RESPONSE_TERRENI),
    )

    assert coppia.tipo_catasto == "E"
    assert coppia.fabbricati.tipo_catasto == "F"
    assert coppia.terreni.data.total_results == 1


def test_batch_item_defaults_to_pending():
    item = o.BatchItem(vat_number="02471840997", endpoint="/visura/persona-giuridica")

    assert item.status == "pending"
    assert item.data is None


def test_visura_document_parses_owner_and_property_details():
    doc = o.VisuraDocument(
        document_type="visura_immobile",
        file_format="XML",
        filename="visura.xml",
        tipo_catasto="F",
        intestati=[{"nominativo": "ROSSI MARIO", "quota": "1/1"}],
        dati_immobile={"categoria": "A/2", "rendita": "500,00"},
    )

    assert doc.intestati[0].nominativo == "ROSSI MARIO"
    assert doc.dati_immobile.categoria == "A/2"


def test_visura_fabbricati_attuale_nested_structures():
    visura = o.VisuraFabbricatiAttuale(
        locazione={"provincia": "Trieste", "comune": "TRIESTE", "foglio": "9", "particella": "166"},
        situazione_attuale=[{"categoria": "A/2", "rendita_catastale": "500,00"}],
        mutazioni_soggettive=[{"tipo_mutazione": "COMPRAVENDITA", "soggetti": [{"nominativo": "ROSSI MARIO"}]}],
    )

    assert visura.locazione.subalterno is None
    assert visura.mutazioni_soggettive[0].soggetti[0].nominativo == "ROSSI MARIO"
