"""Tests for all query type input models and route handlers.

Covers every single-step and multi-step query type not already exercised in
test_models.py or test_fixtures.py:

Single-step models:
  VisuraSoggettoInput, VisuraPersonaGiuridicaInput, ElencoImmobiliInput,
  IspezioneIpotecariaInput (4 search types), GenericSisterRequest

Single-step endpoints:
  soggetto, persona-giuridica, elenco-immobili, ispezione-ipotecaria,
  indirizzo, partita, nota, mappa, export-mappa, originali, fiduciali,
  ispezioni, ispezioni-cartacee, elaborato-planimetrico

Multi-step:
  WorkflowInput — all 10 presets, field requirements, paid-step flag
"""

import json

import pytest
from pydantic import ValidationError

from sister.models import (
    ElencoImmobiliInput,
    ElencoImmobiliRequest,
    GenericSisterRequest,
    IspezioneIpotecariaInput,
    IspezioneIpotecariaRequest,
    VisuraPersonaGiuridicaInput,
    VisuraPersonaGiuridicaRequest,
    VisuraSoggettoInput,
    VisuraSoggettoRequest,
    WorkflowInput,
)

from tests.fixtures import (
    ELENCO_ROMA_NO_SHEET_INPUT,
    ELENCO_TRIESTE_SHEET_INPUT,
    ELENCO_TRIESTE_TERRENI_INPUT,
    GENERIC_ELABORATO_ARGS,
    GENERIC_EXPORT_MAPPA_ARGS,
    GENERIC_FIDUCIALI_ARGS,
    GENERIC_INDIRIZZO_ARGS,
    GENERIC_ISPEZIONI_ARGS,
    GENERIC_ISPEZIONI_CART_ARGS,
    GENERIC_MAPPA_ARGS,
    GENERIC_NOTA_ARGS,
    GENERIC_ORIGINALI_ARGS,
    GENERIC_PARTITA_ARGS,
    IPOTECARIA_IMMOBILE_INPUT,
    IPOTECARIA_NOTA_INPUT,
    IPOTECARIA_PF_INPUT,
    IPOTECARIA_PG_INPUT,
    PG_ALTRAVIA_PROVINCIAL_INPUT,
    PG_BY_NAME_INPUT,
    PG_TIGULLIO_PIVA_INPUT,
    SOGGETTO_BIANCHI_PROVINCIAL_INPUT,
    SOGGETTO_ROSSI_INPUT,
    SOGGETTO_TERRENI_ONLY_INPUT,
    WORKFLOW_AZIENDALE_INPUT,
    WORKFLOW_CROSS_REF_INPUT,
    WORKFLOW_DUE_DILIGENCE_INPUT,
    WORKFLOW_FONDIARIO_INPUT,
    WORKFLOW_FULL_AZIENDALE_INPUT,
    WORKFLOW_FULL_DUE_DILIGENCE_INPUT,
    WORKFLOW_FULL_PATRIMONIO_INPUT,
    WORKFLOW_INDIRIZZO_INPUT,
    WORKFLOW_PATRIMONIO_INPUT,
    WORKFLOW_STORICO_INPUT,
)


# ---------------------------------------------------------------------------
# VisuraSoggettoInput
# ---------------------------------------------------------------------------


class TestVisuraSoggettoInput:
    def test_nationwide_both_catasti(self):
        m = VisuraSoggettoInput(**SOGGETTO_ROSSI_INPUT)
        assert m.fiscal_code == "RSSMRI85E28H501E"
        assert m.cadastre_type is None
        assert m.province is None

    def test_provincial_fabbricati(self):
        m = VisuraSoggettoInput(**SOGGETTO_BIANCHI_PROVINCIAL_INPUT)
        assert m.province == "MI"
        assert m.cadastre_type == "F"

    def test_terreni_only(self):
        m = VisuraSoggettoInput(**SOGGETTO_TERRENI_ONLY_INPUT)
        assert m.cadastre_type == "T"

    def test_tipo_catasto_e_explicit(self):
        m = VisuraSoggettoInput(fiscal_code="RSSMRI85E28H501E", cadastre_type="E")
        assert m.cadastre_type == "E"

    def test_tipo_catasto_normalised_to_upper(self):
        m = VisuraSoggettoInput(codice_fiscale="RSSMRI85E28H501E", tipo_catasto="f")
        assert m.cadastre_type == "F"

    def test_fiscal_code_normalised_to_upper(self):
        m = VisuraSoggettoInput(fiscal_code="rssmri85e28h501e")
        assert m.fiscal_code == "RSSMRI85E28H501E"

    def test_invalid_tipo_catasto_raises(self):
        with pytest.raises(ValidationError):
            VisuraSoggettoInput(fiscal_code="RSSMRI85E28H501E", cadastre_type="X")

    def test_too_short_fiscal_code_raises(self):
        with pytest.raises(ValidationError):
            VisuraSoggettoInput(fiscal_code="SHORT")

    def test_too_long_fiscal_code_raises(self):
        with pytest.raises(ValidationError):
            VisuraSoggettoInput(fiscal_code="A" * 17)


# ---------------------------------------------------------------------------
# VisuraPersonaGiuridicaInput
# ---------------------------------------------------------------------------


class TestVisuraPersonaGiuridicaInput:
    def test_piva_nationwide(self):
        m = VisuraPersonaGiuridicaInput(**PG_TIGULLIO_PIVA_INPUT)
        assert m.identifier == "02471840997"
        assert m.cadastre_type is None
        assert m.province is None

    def test_piva_provincial_fabbricati(self):
        m = VisuraPersonaGiuridicaInput(**PG_ALTRAVIA_PROVINCIAL_INPUT)
        assert m.province == "BO"
        assert m.cadastre_type == "F"

    def test_by_name(self):
        m = VisuraPersonaGiuridicaInput(**PG_BY_NAME_INPUT)
        assert "TIGULLIO" in m.identifier
        assert m.cadastre_type == "E"

    def test_tipo_catasto_t(self):
        m = VisuraPersonaGiuridicaInput(identifier="02471840997", cadastre_type="T")
        assert m.cadastre_type == "T"

    def test_tipo_catasto_normalised(self):
        m = VisuraPersonaGiuridicaInput(identificativo="02471840997", tipo_catasto="e")
        assert m.cadastre_type == "E"

    def test_invalid_tipo_catasto_raises(self):
        with pytest.raises(ValidationError):
            VisuraPersonaGiuridicaInput(identifier="02471840997", cadastre_type="Z")

    def test_empty_identifier_raises(self):
        with pytest.raises(ValidationError):
            VisuraPersonaGiuridicaInput(identifier="")


# ---------------------------------------------------------------------------
# ElencoImmobiliInput
# ---------------------------------------------------------------------------


class TestElencoImmobiliInput:
    def test_terreni_minimal(self):
        m = ElencoImmobiliInput(**ELENCO_TRIESTE_TERRENI_INPUT)
        assert m.province == "Trieste"
        assert m.municipality == "TRIESTE"
        assert m.cadastre_type == "T"
        assert m.sheet is None

    def test_fabbricati_with_sheet(self):
        m = ElencoImmobiliInput(**ELENCO_TRIESTE_SHEET_INPUT)
        assert m.cadastre_type == "F"
        assert m.sheet == "9"

    def test_tipo_catasto_omitted_defaults_to_none(self):
        m = ElencoImmobiliInput(**ELENCO_ROMA_NO_SHEET_INPUT)
        assert m.cadastre_type is None

    def test_tipo_catasto_normalised(self):
        m = ElencoImmobiliInput(province="X", municipality="X", cadastre_type="t")
        assert m.cadastre_type == "T"

    def test_invalid_tipo_catasto_raises(self):
        with pytest.raises(ValidationError):
            ElencoImmobiliInput(province="X", municipality="X", cadastre_type="E")

    def test_empty_province_raises(self):
        with pytest.raises(ValidationError):
            ElencoImmobiliInput(province="", municipality="TRIESTE")

    def test_alias_provincia_comune(self):
        m = ElencoImmobiliInput(provincia="Bologna", comune="BOLOGNA", tipo_catasto="F")
        assert m.province == "Bologna"
        assert m.municipality == "BOLOGNA"


# ---------------------------------------------------------------------------
# IspezioneIpotecariaInput — all 4 search types
# ---------------------------------------------------------------------------


class TestIspezioneIpotecariaInput:
    def test_immobile(self):
        m = IspezioneIpotecariaInput(**IPOTECARIA_IMMOBILE_INPUT)
        assert m.search_type == "immobile"
        assert m.province == "Roma"
        assert m.sheet == "100"
        assert m.parcel == "50"
        assert m.cadastre_type == "F"
        assert m.auto_confirm is False

    def test_persona_fisica(self):
        m = IspezioneIpotecariaInput(**IPOTECARIA_PF_INPUT)
        assert m.search_type == "persona_fisica"
        assert m.fiscal_code == "RSSMRI85E28H501E"
        assert m.identifier is None

    def test_persona_giuridica(self):
        m = IspezioneIpotecariaInput(**IPOTECARIA_PG_INPUT)
        assert m.search_type == "persona_giuridica"
        assert m.identifier == "02471840997"
        assert m.fiscal_code is None

    def test_nota(self):
        m = IspezioneIpotecariaInput(**IPOTECARIA_NOTA_INPUT)
        assert m.search_type == "nota"
        assert m.note_number == "12345"
        assert m.note_year == "2020"

    def test_search_type_normalised_dashes(self):
        m = IspezioneIpotecariaInput(search_type="persona-fisica", province="Roma")
        assert m.search_type == "persona_fisica"

    def test_search_type_case_insensitive_upper(self):
        m = IspezioneIpotecariaInput(search_type="IMMOBILE", province="Roma")
        assert m.search_type == "immobile"

    def test_invalid_search_type_raises(self):
        with pytest.raises(ValidationError):
            IspezioneIpotecariaInput(search_type="unknown", province="Roma")

    def test_invalid_tipo_catasto_raises(self):
        with pytest.raises(ValidationError):
            IspezioneIpotecariaInput(search_type="immobile", province="Roma", cadastre_type="E")

    def test_auto_confirm_true(self):
        m = IspezioneIpotecariaInput(search_type="immobile", province="Roma", auto_confirm=True)
        assert m.auto_confirm is True

    def test_alias_tipo_ricerca(self):
        m = IspezioneIpotecariaInput(tipo_ricerca="immobile", provincia="Roma")
        assert m.search_type == "immobile"
        assert m.province == "Roma"


# ---------------------------------------------------------------------------
# GenericSisterRequest (internal model)
# ---------------------------------------------------------------------------


class TestGenericSisterRequest:
    def test_indirizzo_request(self):
        r = GenericSisterRequest(
            request_id="indirizzo_F_abc",
            search_type="indirizzo",
            province="Terni",
            municipality="TERNI",
            cadastre_type="F",
            params={"indirizzo": "VIA DEL RIVO"},
        )
        assert r.search_type == "indirizzo"
        assert r.params["indirizzo"] == "VIA DEL RIVO"
        assert r.timestamp is not None

    def test_nota_request_no_municipality(self):
        r = GenericSisterRequest(
            request_id="nota_T_abc",
            search_type="nota",
            province="Roma",
            cadastre_type="T",
            params={"numero_nota": "12345", "anno_nota": "2020"},
        )
        assert r.municipality is None
        assert r.params["numero_nota"] == "12345"

    def test_default_tipo_catasto(self):
        r = GenericSisterRequest(
            request_id="mappa_T_xyz",
            search_type="mappa",
            province="Trieste",
        )
        assert r.cadastre_type == "T"
        assert r.params == {}


# ---------------------------------------------------------------------------
# Endpoint: POST /visura/soggetto
# ---------------------------------------------------------------------------


class TestSoggettoEndpoint:
    @pytest.mark.asyncio
    async def test_queues_nationwide_search(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraSoggettoInput(**SOGGETTO_ROSSI_INPUT)

        response = await main_module.richiedi_visura_soggetto(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["request_id"].startswith("soggetto_E_")
        assert payload["codice_fiscale"] == "RSSMRI85E28H501E"
        assert payload["tipo_catasto"] == "E"
        assert payload["provincia"] == "NAZIONALE"

    @pytest.mark.asyncio
    async def test_queues_provincial_search(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraSoggettoInput(**SOGGETTO_BIANCHI_PROVINCIAL_INPUT)

        response = await main_module.richiedi_visura_soggetto(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["tipo_catasto"] == "F"
        assert payload["provincia"] == "MI"

    @pytest.mark.asyncio
    async def test_request_id_prefix(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraSoggettoInput(fiscal_code="RSSMRI85E28H501E", cadastre_type="T")

        response = await main_module.richiedi_visura_soggetto(request, service)
        payload = json.loads(response.body)

        assert payload["request_id"].startswith("soggetto_T_")

    @pytest.mark.asyncio
    async def test_queue_position_reported(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraSoggettoInput(**SOGGETTO_ROSSI_INPUT)

        response = await main_module.richiedi_visura_soggetto(request, service)
        payload = json.loads(response.body)

        assert "queue_position" in payload


# ---------------------------------------------------------------------------
# Endpoint: POST /visura/persona-giuridica
# ---------------------------------------------------------------------------


class TestPersonaGiuridicaEndpoint:
    @pytest.mark.asyncio
    async def test_queues_piva_nationwide(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraPersonaGiuridicaInput(**PG_TIGULLIO_PIVA_INPUT)

        response = await main_module.richiedi_visura_persona_giuridica(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["request_id"].startswith("pnf_E_")
        assert payload["identificativo"] == "02471840997"
        assert payload["provincia"] == "NAZIONALE"

    @pytest.mark.asyncio
    async def test_queues_provincial(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraPersonaGiuridicaInput(**PG_ALTRAVIA_PROVINCIAL_INPUT)

        response = await main_module.richiedi_visura_persona_giuridica(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_catasto"] == "F"
        assert payload["provincia"] == "BO"

    @pytest.mark.asyncio
    async def test_request_id_reflects_tipo_catasto(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraPersonaGiuridicaInput(identifier="02471840997", cadastre_type="T")

        response = await main_module.richiedi_visura_persona_giuridica(request, service)
        payload = json.loads(response.body)

        assert payload["request_id"].startswith("pnf_T_")

    @pytest.mark.asyncio
    async def test_queue_position_reported(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.VisuraPersonaGiuridicaInput(**PG_BY_NAME_INPUT)

        response = await main_module.richiedi_visura_persona_giuridica(request, service)
        payload = json.loads(response.body)

        assert "queue_position" in payload


# ---------------------------------------------------------------------------
# Endpoint: POST /visura/elenco-immobili
# ---------------------------------------------------------------------------


class TestElencoImmobiliEndpoint:
    @pytest.mark.asyncio
    async def test_queues_terreni(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.ElencoImmobiliInput(**ELENCO_TRIESTE_TERRENI_INPUT)

        response = await main_module.richiedi_elenco_immobili(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["request_id"].startswith("eimm_T_")
        assert payload["tipo_catasto"] == "T"
        assert payload["comune"] == "TRIESTE"

    @pytest.mark.asyncio
    async def test_queues_fabbricati_with_sheet(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.ElencoImmobiliInput(**ELENCO_TRIESTE_SHEET_INPUT)

        response = await main_module.richiedi_elenco_immobili(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_catasto"] == "F"
        assert payload["request_id"].startswith("eimm_F_")

    @pytest.mark.asyncio
    async def test_omitted_tipo_defaults_to_terreni(self, main_module):
        """ElencoImmobiliInput with no tipo_catasto → route defaults to 'T'."""
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.ElencoImmobiliInput(**ELENCO_ROMA_NO_SHEET_INPUT)

        response = await main_module.richiedi_elenco_immobili(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_catasto"] == "T"

    @pytest.mark.asyncio
    async def test_queue_position_reported(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.ElencoImmobiliInput(**ELENCO_TRIESTE_TERRENI_INPUT)

        response = await main_module.richiedi_elenco_immobili(request, service)
        payload = json.loads(response.body)

        assert "queue_position" in payload


# ---------------------------------------------------------------------------
# Endpoint: POST /visura/ispezione-ipotecaria (all 4 search types)
# ---------------------------------------------------------------------------


class TestIspezioneIpotecariaEndpoint:
    @pytest.mark.asyncio
    async def test_queues_immobile(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(**IPOTECARIA_IMMOBILE_INPUT)

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["tipo_ricerca"] == "immobile"
        assert payload["request_id"].startswith("ipotecaria_immobile_")
        assert payload["auto_confirm"] is False

    @pytest.mark.asyncio
    async def test_queues_persona_fisica(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(**IPOTECARIA_PF_INPUT)

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_ricerca"] == "persona_fisica"
        assert payload["request_id"].startswith("ipotecaria_persona_fisica_")

    @pytest.mark.asyncio
    async def test_queues_persona_giuridica(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(**IPOTECARIA_PG_INPUT)

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_ricerca"] == "persona_giuridica"
        assert payload["request_id"].startswith("ipotecaria_persona_giuridica_")

    @pytest.mark.asyncio
    async def test_queues_nota(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(**IPOTECARIA_NOTA_INPUT)

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert payload["tipo_ricerca"] == "nota"
        assert payload["request_id"].startswith("ipotecaria_nota_")

    @pytest.mark.asyncio
    async def test_auto_confirm_true_propagates(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(
            search_type="immobile", province="Roma", auto_confirm=True
        )

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert payload["auto_confirm"] is True

    @pytest.mark.asyncio
    async def test_queue_position_reported(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        request = main_module.IspezioneIpotecariaInput(**IPOTECARIA_IMMOBILE_INPUT)

        response = await main_module.richiedi_ispezione_ipotecaria(request, service)
        payload = json.loads(response.body)

        assert "queue_position" in payload


# ---------------------------------------------------------------------------
# Endpoint: richiedi_generic_sister (all search types)
# ---------------------------------------------------------------------------


class TestGenericSisterEndpoint:
    """Tests the generic route handler that covers indirizzo, partita, nota,
    mappa, export-mappa, originali, fiduciali, ispezioni, ispezioni-cartacee,
    elaborato-planimetrico."""

    @pytest.mark.asyncio
    async def test_indirizzo_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_INDIRIZZO_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            tipo_catasto=args.get("tipo_catasto", "T"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "indirizzo"
        assert payload["request_id"].startswith("indirizzo_F_")

    @pytest.mark.asyncio
    async def test_partita_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_PARTITA_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            tipo_catasto=args.get("tipo_catasto", "T"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "partita"

    @pytest.mark.asyncio
    async def test_nota_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_NOTA_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            tipo_catasto=args.get("tipo_catasto", "T"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "nota"

    @pytest.mark.asyncio
    async def test_mappa_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_MAPPA_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "mappa"

    @pytest.mark.asyncio
    async def test_export_mappa_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_EXPORT_MAPPA_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "export_mappa"

    @pytest.mark.asyncio
    async def test_originali_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_ORIGINALI_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "originali"

    @pytest.mark.asyncio
    async def test_fiduciali_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_FIDUCIALI_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "fiduciali"

    @pytest.mark.asyncio
    async def test_ispezioni_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_ISPEZIONI_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            params=args.get("params"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "ispezioni"

    @pytest.mark.asyncio
    async def test_ispezioni_cartacee_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_ISPEZIONI_CART_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "ispezioni_cart"

    @pytest.mark.asyncio
    async def test_elaborato_planimetrico_queued(self, main_module):
        service = main_module.VisuraService()
        service.processing = True
        args = GENERIC_ELABORATO_ARGS

        response = await main_module.richiedi_generic_sister(
            search_type=args["search_type"],
            provincia=args["provincia"],
            service=service,
            comune=args.get("comune"),
            tipo_catasto=args.get("tipo_catasto", "T"),
        )
        payload = json.loads(response.body)

        assert payload["status"] == "queued"
        assert payload["search_type"] == "elaborato_planimetrico"

    @pytest.mark.asyncio
    async def test_provincia_included_in_response(self, main_module):
        service = main_module.VisuraService()
        service.processing = True

        response = await main_module.richiedi_generic_sister(
            search_type="mappa",
            provincia="Bologna",
            service=service,
            comune="BOLOGNA",
        )
        payload = json.loads(response.body)

        assert payload["provincia"] == "Bologna"

    @pytest.mark.asyncio
    async def test_request_id_reflects_search_type_and_tipo(self, main_module):
        service = main_module.VisuraService()
        service.processing = True

        response = await main_module.richiedi_generic_sister(
            search_type="nota",
            provincia="Milano",
            service=service,
            tipo_catasto="F",
        )
        payload = json.loads(response.body)

        assert payload["request_id"].startswith("nota_F_")


# ---------------------------------------------------------------------------
# Multi-step: WorkflowInput — all 10 presets
# ---------------------------------------------------------------------------


class TestWorkflowPresetFixtures:
    """Validate that every fixture in WORKFLOW_*_INPUT produces a valid
    WorkflowInput. Covers field requirements and alias normalization without
    duplicating the preset-structure tests already in test_workflows.py."""

    def test_due_diligence(self):
        wf = WorkflowInput(**WORKFLOW_DUE_DILIGENCE_INPUT)
        assert wf.preset == "due-diligence"
        assert wf.provincia == "Roma"
        assert wf.foglio == "100"
        assert wf.particella == "50"

    def test_patrimonio(self):
        wf = WorkflowInput(**WORKFLOW_PATRIMONIO_INPUT)
        assert wf.preset == "patrimonio"
        assert wf.codice_fiscale == "RSSMRI85E28H501E"

    def test_fondiario(self):
        wf = WorkflowInput(**WORKFLOW_FONDIARIO_INPUT)
        assert wf.preset == "fondiario"
        assert wf.foglio == "9"

    def test_aziendale(self):
        wf = WorkflowInput(**WORKFLOW_AZIENDALE_INPUT)
        assert wf.preset == "aziendale"
        assert wf.identificativo == "02471840997"

    def test_storico(self):
        wf = WorkflowInput(**WORKFLOW_STORICO_INPUT)
        assert wf.preset == "storico"
        assert wf.particella == "166"

    def test_indirizzo_preset(self):
        wf = WorkflowInput(**WORKFLOW_INDIRIZZO_INPUT)
        assert wf.preset == "indirizzo"
        assert wf.indirizzo == "VIA DEL RIVO 1"

    def test_cross_reference(self):
        wf = WorkflowInput(**WORKFLOW_CROSS_REF_INPUT)
        assert wf.preset == "cross-reference"
        assert wf.codice_fiscale == "RSSMRI85E28H501E"
        assert wf.identificativo == "02471840997"

    def test_full_due_diligence(self):
        wf = WorkflowInput(**WORKFLOW_FULL_DUE_DILIGENCE_INPUT)
        assert wf.preset == "full-due-diligence"

    def test_full_patrimonio(self):
        wf = WorkflowInput(**WORKFLOW_FULL_PATRIMONIO_INPUT)
        assert wf.preset == "full-patrimonio"
        assert wf.codice_fiscale == "RSSMRI85E28H501E"

    def test_full_aziendale(self):
        wf = WorkflowInput(**WORKFLOW_FULL_AZIENDALE_INPUT)
        assert wf.preset == "full-aziendale"
        assert wf.identificativo == "02471840997"

    def test_paid_steps_disabled_by_default(self):
        for fixture in [
            WORKFLOW_DUE_DILIGENCE_INPUT,
            WORKFLOW_PATRIMONIO_INPUT,
            WORKFLOW_FONDIARIO_INPUT,
        ]:
            wf = WorkflowInput(**fixture)
            assert wf.include_paid_steps is False

    def test_paid_steps_enabled(self):
        wf = WorkflowInput(**{**WORKFLOW_DUE_DILIGENCE_INPUT, "include_paid_steps": True, "auto_confirm": True})
        assert wf.include_paid_steps is True
        assert wf.auto_confirm is True
