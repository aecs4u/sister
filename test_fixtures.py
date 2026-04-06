"""Test fixtures based on the examples in README.md.

Covers: input models, request/response dataclasses, endpoint payloads,
validation rules, and realistic sample data matching the README examples.
"""

import json
from datetime import datetime

import pytest

# ---------------------------------------------------------------------------
# README example data (Trieste & Roma)
# ---------------------------------------------------------------------------

TRIESTE_FABBRICATI_INPUT = {
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
    "particella": "166",
    "tipo_catasto": "F",
}

TRIESTE_INTESTATI_INPUT = {
    "provincia": "Trieste",
    "comune": "TRIESTE",
    "foglio": "9",
    "particella": "166",
    "tipo_catasto": "F",
    "subalterno": "3",
}

ROMA_FABBRICATI_INPUT = {
    "provincia": "Roma",
    "comune": "ROMA",
    "foglio": "100",
    "particella": "50",
    "tipo_catasto": "F",
}

ROMA_INTESTATI_INPUT = {
    "provincia": "Roma",
    "comune": "ROMA",
    "foglio": "100",
    "particella": "50",
    "tipo_catasto": "F",
    "subalterno": "3",
}

IMMOBILE_FABBRICATO = {
    "Foglio": "9",
    "Particella": "166",
    "Sub": "3",
    "Categoria": "A/2",
    "Classe": "5",
    "Consistenza": "4.5",
    "Rendita": "500,00",
    "Indirizzo": "VIA ROMA 10",
    "Partita": "12345",
}

INTESTATO_ROSSI = {
    "Nominativo o denominazione": "ROSSI MARIO",
    "Codice fiscale": "RSSMRA85M01H501Z",
    "Titolarità": "Proprietà per 1/1",
}

VISURA_FASE1_DATA = {
    "immobili": [IMMOBILE_FABBRICATO],
    "results": [{"result_index": 1, "immobile": IMMOBILE_FABBRICATO, "intestati": []}],
    "total_results": 1,
    "intestati": [],
}

VISURA_FASE2_DATA = {
    "immobile": {"Foglio": "9", "Particella": "166", "Sub": "3"},
    "intestati": [INTESTATO_ROSSI],
    "total_intestati": 1,
}

VISURA_NESSUNA_CORRISPONDENZA = {
    "immobili": [],
    "results": [],
    "total_results": 0,
    "intestati": [],
    "error": "NESSUNA CORRISPONDENZA TROVATA",
}


# ---------------------------------------------------------------------------
# Pydantic model construction (VisuraInput)
# ---------------------------------------------------------------------------


class TestVisuraInput:
    """VisuraInput model — README examples and validation edge cases."""

    def test_trieste_fabbricati(self, main_module):
        model = main_module.VisuraInput(**TRIESTE_FABBRICATI_INPUT)
        assert model.provincia == "Trieste"
        assert model.comune == "TRIESTE"
        assert model.foglio == "9"
        assert model.particella == "166"
        assert model.tipo_catasto == "F"
        assert model.sezione is None

    def test_roma_fabbricati(self, main_module):
        model = main_module.VisuraInput(**ROMA_FABBRICATI_INPUT)
        assert model.provincia == "Roma"
        assert model.tipo_catasto == "F"

    def test_tipo_catasto_omitted_defaults_to_none(self, main_module):
        """README: se tipo_catasto omesso, vengono accodate due richieste (T+F)."""
        model = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166"
        )
        assert model.tipo_catasto is None

    def test_tipo_catasto_terreni(self, main_module):
        model = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", tipo_catasto="T"
        )
        assert model.tipo_catasto == "T"

    def test_tipo_catasto_case_insensitive(self, main_module):
        model = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", tipo_catasto="f"
        )
        assert model.tipo_catasto == "F"

    def test_tipo_catasto_invalid_raises(self, main_module):
        with pytest.raises(Exception):
            main_module.VisuraInput(
                provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", tipo_catasto="X"
            )

    def test_with_sezione(self, main_module):
        model = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", sezione="P"
        )
        assert model.sezione == "P"

    def test_with_subalterno(self, main_module):
        model = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
            tipo_catasto="F", subalterno="3",
        )
        assert model.subalterno == "3"

    def test_empty_provincia_raises(self, main_module):
        with pytest.raises(Exception):
            main_module.VisuraInput(provincia="", comune="TRIESTE", foglio="9", particella="166")


# ---------------------------------------------------------------------------
# Pydantic model construction (VisuraIntestatiInput)
# ---------------------------------------------------------------------------


class TestVisuraIntestatiInput:
    """VisuraIntestatiInput — README examples and subalterno validation."""

    def test_trieste_intestati(self, main_module):
        model = main_module.VisuraIntestatiInput(**TRIESTE_INTESTATI_INPUT)
        assert model.provincia == "Trieste"
        assert model.tipo_catasto == "F"
        assert model.subalterno == "3"

    def test_roma_intestati(self, main_module):
        model = main_module.VisuraIntestatiInput(**ROMA_INTESTATI_INPUT)
        assert model.provincia == "Roma"
        assert model.subalterno == "3"

    def test_terreni_no_subalterno(self, main_module):
        model = main_module.VisuraIntestatiInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", tipo_catasto="T"
        )
        assert model.tipo_catasto == "T"
        assert model.subalterno is None

    def test_fabbricati_without_subalterno_raises(self, main_module):
        """README: subalterno obbligatorio per Fabbricati."""
        with pytest.raises(Exception):
            main_module.VisuraIntestatiInput(
                provincia="Trieste", comune="TRIESTE", foglio="9", particella="166", tipo_catasto="F"
            )

    def test_terreni_with_subalterno_raises(self, main_module):
        """README: subalterno vietato per Terreni."""
        with pytest.raises(Exception):
            main_module.VisuraIntestatiInput(
                provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
                tipo_catasto="T", subalterno="3",
            )

    def test_tipo_catasto_case_insensitive(self, main_module):
        model = main_module.VisuraIntestatiInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
            tipo_catasto="f", subalterno="3",
        )
        assert model.tipo_catasto == "F"

    def test_subalterno_whitespace_stripped(self, main_module):
        model = main_module.VisuraIntestatiInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
            tipo_catasto="F", subalterno="  3  ",
        )
        assert model.subalterno == "3"

    def test_subalterno_blank_string_treated_as_none(self, main_module):
        """Blank subalterno should be treated as missing (None)."""
        with pytest.raises(Exception):
            main_module.VisuraIntestatiInput(
                provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
                tipo_catasto="F", subalterno="   ",
            )


# ---------------------------------------------------------------------------
# SezioniExtractionRequest
# ---------------------------------------------------------------------------


class TestSezioniExtractionRequest:

    def test_defaults(self, main_module):
        model = main_module.SezioniExtractionRequest()
        assert model.tipo_catasto == "T"
        assert model.max_province == 200

    def test_fabbricati(self, main_module):
        model = main_module.SezioniExtractionRequest(tipo_catasto="F", max_province=10)
        assert model.tipo_catasto == "F"
        assert model.max_province == 10

    def test_max_province_out_of_range(self, main_module):
        with pytest.raises(Exception):
            main_module.SezioniExtractionRequest(max_province=0)

        with pytest.raises(Exception):
            main_module.SezioniExtractionRequest(max_province=201)


# ---------------------------------------------------------------------------
# Dataclass fixtures (internal request/response)
# ---------------------------------------------------------------------------


class TestVisuraRequest:
    """VisuraRequest dataclass used internally by the queue."""

    def test_from_readme_trieste(self, main_module):
        req = main_module.VisuraRequest(
            request_id="req_F_2f7f40f95cfb4bd8a8d8fe7b89612268",
            tipo_catasto="F",
            provincia="Trieste",
            comune="TRIESTE",
            foglio="9",
            particella="166",
        )
        assert req.request_id.startswith("req_F_")
        assert req.timestamp is not None

    def test_with_sezione_and_subalterno(self, main_module):
        req = main_module.VisuraRequest(
            request_id="req_F_test",
            tipo_catasto="F",
            provincia="Roma",
            comune="ROMA",
            foglio="100",
            particella="50",
            sezione="A",
            subalterno="3",
        )
        assert req.sezione == "A"
        assert req.subalterno == "3"


class TestVisuraResponse:
    """VisuraResponse — README result payloads."""

    def test_successful_fase1(self, main_module):
        resp = main_module.VisuraResponse(
            request_id="req_F_2f7f40f95cfb4bd8a8d8fe7b89612268",
            success=True,
            tipo_catasto="F",
            data=VISURA_FASE1_DATA,
        )
        assert resp.success is True
        assert resp.data["total_results"] == 1
        assert resp.data["immobili"][0]["Categoria"] == "A/2"
        assert resp.timestamp is not None

    def test_successful_fase2_intestati(self, main_module):
        resp = main_module.VisuraResponse(
            request_id="intestati_F_9f3fa9cf2fcb49c6a8a21bf2312e3ef3",
            success=True,
            tipo_catasto="F",
            data=VISURA_FASE2_DATA,
        )
        assert resp.data["total_intestati"] == 1
        assert resp.data["intestati"][0]["Codice fiscale"] == "RSSMRA85M01H501Z"

    def test_nessuna_corrispondenza(self, main_module):
        resp = main_module.VisuraResponse(
            request_id="req_F_empty",
            success=True,
            tipo_catasto="F",
            data=VISURA_NESSUNA_CORRISPONDENZA,
        )
        assert resp.data["total_results"] == 0
        assert resp.data["error"] == "NESSUNA CORRISPONDENZA TROVATA"

    def test_error_response(self, main_module):
        resp = main_module.VisuraResponse(
            request_id="req_F_fail",
            success=False,
            tipo_catasto="F",
            error="Login failed: timeout",
        )
        assert resp.success is False
        assert "timeout" in resp.error


# ---------------------------------------------------------------------------
# Endpoint: POST /visura (queueing)
# ---------------------------------------------------------------------------


class TestVisuraEndpoint:
    """POST /visura — queue and polling flow from README examples."""

    @pytest.mark.asyncio
    async def test_queues_single_tipo_catasto(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraInput(**TRIESTE_FABBRICATI_INPUT)

        response = await main_module.richiedi_visura(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert len(payload["request_ids"]) == 1
        assert payload["tipos_catasto"] == ["F"]
        assert "TRIESTE" in payload["message"]

    @pytest.mark.asyncio
    async def test_queues_both_when_tipo_omitted(self, main_module):
        """README: se tipo_catasto omesso, vengono accodate due richieste."""
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraInput(
            provincia="Roma", comune="ROMA", foglio="100", particella="50"
        )

        response = await main_module.richiedi_visura(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert len(payload["request_ids"]) == 2
        assert payload["tipos_catasto"] == ["T", "F"]

    @pytest.mark.asyncio
    async def test_request_ids_have_expected_prefix(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraInput(**TRIESTE_FABBRICATI_INPUT)

        response = await main_module.richiedi_visura(request, service)
        payload = json.loads(response.body)

        assert payload["request_ids"][0].startswith("req_F_")

    @pytest.mark.asyncio
    async def test_sezione_underscore_treated_as_none(self, main_module):
        """Sezione '_' should be normalised to None."""
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraInput(
            provincia="Trieste", comune="TRIESTE", foglio="9", particella="166",
            tipo_catasto="F", sezione="_",
        )

        response = await main_module.richiedi_visura(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"


# ---------------------------------------------------------------------------
# Endpoint: POST /visura/intestati (queueing)
# ---------------------------------------------------------------------------


class TestIntestatiEndpoint:
    """POST /visura/intestati — README examples."""

    @pytest.mark.asyncio
    async def test_queues_intestati_request(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraIntestatiInput(**TRIESTE_INTESTATI_INPUT)

        response = await main_module.richiedi_intestati_immobile(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["request_id"].startswith("intestati_F_")
        assert payload["tipo_catasto"] == "F"
        assert payload["subalterno"] == "3"
        assert "TRIESTE" in payload["message"]

    @pytest.mark.asyncio
    async def test_queue_position_reported(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraIntestatiInput(**ROMA_INTESTATI_INPUT)

        response = await main_module.richiedi_intestati_immobile(request, service)
        payload = json.loads(response.body)

        assert "queue_position" in payload


# ---------------------------------------------------------------------------
# Endpoint: GET /visura/{request_id} (polling)
# ---------------------------------------------------------------------------


class TestPollingEndpoint:
    """GET /visura/{request_id} — README polling states."""

    @pytest.mark.asyncio
    async def test_processing_state(self, main_module):
        service = main_module.VisuraService()
        service.pending_request_ids.add("req_F_abc")

        response = await main_module.ottieni_visura("req_F_abc", service)
        payload = json.loads(response.body)

        assert payload["status"] == "processing"

    @pytest.mark.asyncio
    async def test_completed_state_with_data(self, main_module):
        service = main_module.VisuraService()
        service.response_store["req_F_done"] = main_module.VisuraResponse(
            request_id="req_F_done",
            success=True,
            tipo_catasto="F",
            data=VISURA_FASE1_DATA,
        )

        response = await main_module.ottieni_visura("req_F_done", service)
        payload = json.loads(response.body)

        assert payload["status"] == "completed"
        assert payload["tipo_catasto"] == "F"
        assert payload["data"]["immobili"][0]["Rendita"] == "500,00"
        assert payload["error"] is None
        assert "timestamp" in payload

    @pytest.mark.asyncio
    async def test_error_state(self, main_module):
        service = main_module.VisuraService()
        service.response_store["req_F_err"] = main_module.VisuraResponse(
            request_id="req_F_err",
            success=False,
            tipo_catasto="F",
            error="Sessione scaduta",
        )

        response = await main_module.ottieni_visura("req_F_err", service)
        payload = json.loads(response.body)

        assert payload["status"] == "error"
        assert payload["error"] == "Sessione scaduta"

    @pytest.mark.asyncio
    async def test_expired_state_returns_410(self, main_module):
        service = main_module.VisuraService()
        service.expired_request_ids["req_F_old"] = datetime.now()

        response = await main_module.ottieni_visura("req_F_old", service)
        payload = json.loads(response.body)

        assert response.status_code == 410
        assert payload["status"] == "expired"

    @pytest.mark.asyncio
    async def test_unknown_request_id_returns_404(self, main_module):
        service = main_module.VisuraService()

        with pytest.raises(main_module.HTTPException) as exc_info:
            await main_module.ottieni_visura("nonexistent", service)

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Endpoint: GET /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """GET /health — README example response shape."""

    @pytest.mark.asyncio
    async def test_health_response_shape(self, main_module):
        service = main_module.VisuraService()

        response = await main_module.health_check(service)
        payload = json.loads(response.body)

        assert payload["status"] == "healthy"
        assert "authenticated" in payload
        assert "queue_size" in payload
        assert "pending_requests" in payload
        assert "cached_responses" in payload
        assert "response_ttl_seconds" in payload
        assert "response_max_items" in payload

    @pytest.mark.asyncio
    async def test_health_reflects_queue_state(self, main_module):
        service = main_module.VisuraService()
        service.pending_request_ids.update({"r1", "r2", "r3"})

        response = await main_module.health_check(service)
        payload = json.loads(response.body)

        assert payload["pending_requests"] == 3
        assert payload["queue_size"] == 0
