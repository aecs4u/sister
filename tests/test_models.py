"""Tests for Pydantic input models and validators (models.py)."""

import pytest
from pydantic import ValidationError

# Stubs are installed by conftest._install_test_stubs() at import time
from sister.models import (
    SezioniExtractionRequest,
    VisuraInput,
    VisuraIntestatiInput,
    VisuraRequest,
    VisuraResponse,
)


# ---------------------------------------------------------------------------
# VisuraInput
# ---------------------------------------------------------------------------


class TestVisuraInput:
    def test_valid_minimal(self):
        v = VisuraInput(provincia="Trieste", comune="TRIESTE", foglio="9", particella="166")
        assert v.provincia == "Trieste"
        assert v.tipo_catasto is None

    def test_valid_with_tipo_catasto(self):
        v = VisuraInput(
            provincia="Roma", comune="ROMA", foglio="1", particella="1", tipo_catasto="f"
        )
        assert v.tipo_catasto == "F"

    def test_tipo_catasto_normalised_to_upper(self):
        v = VisuraInput(provincia="X", comune="X", foglio="1", particella="1", tipo_catasto="t")
        assert v.tipo_catasto == "T"

    def test_invalid_tipo_catasto_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(
                provincia="X", comune="X", foglio="1", particella="1", tipo_catasto="X"
            )

    def test_empty_provincia_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(provincia="", comune="TRIESTE", foglio="9", particella="166")

    def test_empty_foglio_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(provincia="Trieste", comune="TRIESTE", foglio="", particella="166")


# ---------------------------------------------------------------------------
# VisuraIntestatiInput
# ---------------------------------------------------------------------------


class TestVisuraIntestatiInput:
    def test_valid_fabbricati_with_subalterno(self):
        v = VisuraIntestatiInput(
            provincia="Trieste",
            comune="TRIESTE",
            foglio="9",
            particella="166",
            tipo_catasto="F",
            subalterno="3",
        )
        assert v.tipo_catasto == "F"
        assert v.subalterno == "3"

    def test_valid_terreni_without_subalterno(self):
        v = VisuraIntestatiInput(
            provincia="Roma",
            comune="ROMA",
            foglio="50",
            particella="10",
            tipo_catasto="T",
        )
        assert v.subalterno is None

    def test_fabbricati_requires_subalterno(self):
        with pytest.raises(ValidationError, match="subalterno"):
            VisuraIntestatiInput(
                provincia="Trieste",
                comune="TRIESTE",
                foglio="9",
                particella="166",
                tipo_catasto="F",
            )

    def test_terreni_rejects_subalterno(self):
        with pytest.raises(ValidationError, match="subalterno"):
            VisuraIntestatiInput(
                provincia="Trieste",
                comune="TRIESTE",
                foglio="9",
                particella="166",
                tipo_catasto="T",
                subalterno="3",
            )

    def test_tipo_catasto_normalised(self):
        v = VisuraIntestatiInput(
            provincia="X", comune="X", foglio="1", particella="1",
            tipo_catasto="f", subalterno="1",
        )
        assert v.tipo_catasto == "F"

    def test_whitespace_subalterno_normalised_to_none(self):
        with pytest.raises(ValidationError, match="subalterno"):
            # Whitespace-only subalterno should normalise to None,
            # then fail the F-requires-subalterno validator
            VisuraIntestatiInput(
                provincia="X", comune="X", foglio="1", particella="1",
                tipo_catasto="F", subalterno="   ",
            )


# ---------------------------------------------------------------------------
# SezioniExtractionRequest
# ---------------------------------------------------------------------------


class TestSezioniExtractionRequest:
    def test_defaults(self):
        s = SezioniExtractionRequest()
        assert s.tipo_catasto == "T"
        assert s.max_province == 200

    def test_custom_values(self):
        s = SezioniExtractionRequest(tipo_catasto="f", max_province=10)
        assert s.tipo_catasto == "F"
        assert s.max_province == 10

    def test_max_province_bounds(self):
        with pytest.raises(ValidationError):
            SezioniExtractionRequest(max_province=0)
        with pytest.raises(ValidationError):
            SezioniExtractionRequest(max_province=201)


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


class TestVisuraRequest:
    def test_default_timestamp(self):
        r = VisuraRequest(
            request_id="req_T_1", tipo_catasto="T",
            provincia="X", comune="X", foglio="1", particella="1",
        )
        assert r.timestamp is not None

    def test_optional_fields_default_none(self):
        r = VisuraRequest(
            request_id="req_T_2", tipo_catasto="T",
            provincia="X", comune="X", foglio="1", particella="1",
        )
        assert r.sezione is None
        assert r.subalterno is None


class TestVisuraResponse:
    def test_success_response(self):
        r = VisuraResponse(
            request_id="req_F_1", success=True, tipo_catasto="F",
            data={"immobili": []},
        )
        assert r.success is True
        assert r.error is None
        assert r.timestamp is not None

    def test_error_response(self):
        r = VisuraResponse(
            request_id="req_F_2", success=False, tipo_catasto="F",
            error="something broke",
        )
        assert r.success is False
        assert r.data is None
