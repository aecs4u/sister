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
        v = VisuraInput(province="Trieste", municipality="TRIESTE", sheet="9", parcel="166")
        assert v.province == "Trieste"
        assert v.cadastre_type is None

    def test_valid_with_tipo_catasto(self):
        v = VisuraInput(province="Roma", municipality="ROMA", sheet="1", parcel="1", cadastre_type="f")
        assert v.cadastre_type == "F"

    def test_tipo_catasto_normalised_to_upper(self):
        v = VisuraInput(province="X", municipality="X", sheet="1", parcel="1", cadastre_type="t")
        assert v.cadastre_type == "T"

    def test_invalid_tipo_catasto_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(province="X", municipality="X", sheet="1", parcel="1", cadastre_type="X")

    def test_empty_provincia_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(province="", municipality="TRIESTE", sheet="9", parcel="166")

    def test_empty_foglio_rejected(self):
        with pytest.raises(ValidationError):
            VisuraInput(province="Trieste", municipality="TRIESTE", sheet="", parcel="166")


# ---------------------------------------------------------------------------
# VisuraIntestatiInput
# ---------------------------------------------------------------------------


class TestVisuraIntestatiInput:
    def test_valid_fabbricati_with_subalterno(self):
        v = VisuraIntestatiInput(
            province="Trieste",
            municipality="TRIESTE",
            sheet="9",
            parcel="166",
            cadastre_type="F",
            subunit="3",
        )
        assert v.cadastre_type == "F"
        assert v.subunit == "3"

    def test_valid_terreni_without_subalterno(self):
        v = VisuraIntestatiInput(
            province="Roma",
            municipality="ROMA",
            sheet="50",
            parcel="10",
            cadastre_type="T",
        )
        assert v.subunit is None

    def test_fabbricati_requires_subalterno(self):
        with pytest.raises(ValidationError, match="subalterno"):
            VisuraIntestatiInput(
                province="Trieste",
                municipality="TRIESTE",
                sheet="9",
                parcel="166",
                cadastre_type="F",
            )

    def test_terreni_rejects_subalterno(self):
        with pytest.raises(ValidationError, match="subalterno"):
            VisuraIntestatiInput(
                province="Trieste",
                municipality="TRIESTE",
                sheet="9",
                parcel="166",
                cadastre_type="T",
                subunit="3",
            )

    def test_tipo_catasto_normalised(self):
        v = VisuraIntestatiInput(
            province="X",
            municipality="X",
            sheet="1",
            parcel="1",
            cadastre_type="f",
            subunit="1",
        )
        assert v.cadastre_type == "F"

    def test_whitespace_subalterno_normalised_to_none(self):
        with pytest.raises(ValidationError, match="subalterno"):
            # Whitespace-only subunit should normalise to None,
            # then fail the F-requires-subunit validator
            VisuraIntestatiInput(
                province="X",
                municipality="X",
                sheet="1",
                parcel="1",
                cadastre_type="F",
                subunit="   ",
            )


# ---------------------------------------------------------------------------
# SezioniExtractionRequest
# ---------------------------------------------------------------------------


class TestSezioniExtractionRequest:
    def test_defaults(self):
        s = SezioniExtractionRequest()
        assert s.cadastre_type == "T"
        assert s.max_provinces == 200

    def test_custom_values(self):
        s = SezioniExtractionRequest(cadastre_type="f", max_provinces=10)
        assert s.cadastre_type == "F"
        assert s.max_provinces == 10

    def test_max_province_bounds(self):
        with pytest.raises(ValidationError):
            SezioniExtractionRequest(max_provinces=0)
        with pytest.raises(ValidationError):
            SezioniExtractionRequest(max_provinces=201)


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


class TestVisuraRequest:
    def test_default_timestamp(self):
        r = VisuraRequest(
            request_id="req_T_1",
            cadastre_type="T",
            province="X",
            municipality="X",
            sheet="1",
            parcel="1",
        )
        assert r.timestamp is not None

    def test_optional_fields_default_none(self):
        r = VisuraRequest(
            request_id="req_T_2",
            cadastre_type="T",
            province="X",
            municipality="X",
            sheet="1",
            parcel="1",
        )
        assert r.section is None
        assert r.subunit is None


class TestVisuraResponse:
    def test_success_response(self):
        r = VisuraResponse(
            request_id="req_F_1",
            success=True,
            cadastre_type="F",
            data={"immobili": []},
        )
        assert r.success is True
        assert r.error is None
        assert r.timestamp is not None

    def test_error_response(self):
        r = VisuraResponse(
            request_id="req_F_2",
            success=False,
            cadastre_type="F",
            error="something broke",
        )
        assert r.success is False
        assert r.data is None
