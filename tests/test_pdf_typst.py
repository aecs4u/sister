"""PDF export with Typst: the context is plain data, and nothing in a visura can reach the Typst source."""

from datetime import date

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sister import pdf_typst, web
from sister.pdf_typst import PdfExportError, build_context, pdf_filename, render_pdf

TODAY = date(2026, 10, 23)


def subject_doc(name="CIMMINO", given="Lorenzo", address="VIA CUMA n. 3 Interno 65 Piano T"):
    return {"id": 7, "filename": "vs_att_X.p7m", "xml_parsed": {"VisuraSoggettoAttuale": {
        "TitoloVisura": {"SituazioneAl": "20261022", "Data": "20261022", "Ora": "184137"},
        "DatiRichiesta": {"TipoCatasto": "E", "Provincia": "NA", "Protocollo": "T1", "Anno": "2026",
                          "SoggettoIndividuato": {"SoggettoPF": {"CodiceFiscale": "CMMLNZ77A17F839A", "Cognome": name,
                                                                 "Nome": given, "DataNascita": "17/01/1977"}}},
        "GruppoUnitaImmobiliari": [{
            "TipoCatasto": "F", "CodiceComune": "F839", "Descrizione": "NAPOLI",
            "ImmobileFabbricatiS": {
                "DatiIdentificativi": {"IdentificativoDefinitivo": [
                    {"Foglio": "4", "ParticellaNum": "51", "Subalterno": "69", "Comune": "NAPOLI"}]},
                "DatiIndirizzo": {"IndirizzoImm": address},
                "DatiClassamentoF": {"Categoria": "C/6: STALLE, SCUDERIE", "Classe": "7", "RenditaEuro": "1999,87",
                                     "Consistenza": {"Valore": "15", "Unita": "MQ"}},
            },
            "IntestazioneGruppo": {"MutazioneSoggettiva": [
                {"IndiceMutazione": "1", "DatiDerivantiDaMutazSogg": "Atto del 14/01/2021 DONAZIONE",
                 "Intestazione": {"Intestato": [{
                     "Nominativo": "CIMMINO Lorenzo  17/01/1977; Comune NAPOLI (NA)", "CF": "CMMLNZ77A17F839A",
                     "DirittiReali": {"Quota": " per 500/1000", "CodiceDiritto": "01", "Descrizione": "Proprieta'",
                                      "FineDiritto": "dal 14/01/2021 al 22/10/2026"}}]}},
            ]},
        }],
    }}}


def test_filename_is_ascii_and_falls_back_to_the_document_id():
    assert pdf_filename("vs_att_CMMLNZ77A17F839A_NA_CE_2026-10-22", 1775) == "vs_att_CMMLNZ77A17F839A_NA_CE_2026-10-22.pdf"
    assert pdf_filename('a/b "c"\r\nSet-Cookie: x', 7) == "a_b_c_Set-Cookie_x.pdf"  # no header injection
    assert pdf_filename("", 1775) == "sister_documento_1775.pdf"
    assert pdf_filename(None, 3) == "sister_documento_3.pdf"


def test_subject_context_has_formatted_values_and_newest_first_ownership():
    context = build_context(subject_doc(), None, None, TODAY)

    assert context["title"] == "CIMMINO Lorenzo"
    assert context["subtitle"] == "Visura soggetto attuale — Entrambi — NA"
    assert ["Situazione al", "22/10/2026"] in context["meta"]
    group = next(section for section in context["sections"] if section["title"] == "NAPOLI")
    table = next(item for item in group["items"] if item["kind"] == "table")
    row = table["rows"][0]
    assert row[:6] == ["VIA CUMA n. 3 Interno 65 Piano T", "NAPOLI", "4", "51", "69", "—"]
    assert row[6:10] == ["C/6", "STALLE, SCUDERIE", "7", "15 MQ"]
    assert row[10] == "€ 1.999,87"  # thousands separator, unlike the theme's "€ 1999,87"
    mutation = next(item for item in group["items"] if item["kind"] == "mutations")["items"][0]
    assert mutation["current"] and mutation["date"] == "14/01/2021"
    assert mutation["owners"][0]["quota"] == "1/2"  # 500/1000 reduced
    assert mutation["owners"][0]["sub"] == "nato il 17/01/1977 · NAPOLI (NA)"


def test_unsupported_documents_are_refused():
    with pytest.raises(PdfExportError):
        build_context({"id": 1, "xml_parsed": {"Altro": {}}, "view": None}, None, None, TODAY)


def test_typst_syntax_in_the_data_is_text_not_code():
    hostile = '#panic("boom") $x$ *bold* _it_ `raw` <label> @ref \\ ]['
    doc = subject_doc(name=hostile, address=hostile)

    pdf = render_pdf(build_context(doc, None, None, TODAY))

    assert pdf.startswith(b"%PDF")


def test_render_pdf_makes_a_landscape_a4_with_every_section():
    pdf = render_pdf(build_context(subject_doc(), None, None, TODAY))

    import io

    PdfReader = pytest.importorskip("pypdf").PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    box = reader.pages[0].mediabox
    assert round(float(box.width)) == 842 and round(float(box.height)) == 595
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "CIMMINO Lorenzo" in text and "1.999,87" in text and "NAPOLI" in text


@pytest.mark.asyncio
async def test_pdf_route_404_for_an_unknown_document(monkeypatch):
    async def no_document(doc_id):
        return None

    monkeypatch.setattr(web, "get_document_by_id", no_document)
    request = Request({"type": "http", "method": "GET", "path": "/documents/9/pdf", "query_string": b"", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await web.web_document_pdf(request, 9, user=None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_pdf_route_422_for_a_document_that_is_not_a_visura(monkeypatch):
    async def document(doc_id):
        return {"id": doc_id, "filename": "x.pdf", "xml_content": ""}

    async def no_lookup(doc):
        return None

    monkeypatch.setattr(web, "get_document_by_id", document)
    monkeypatch.setattr(web, "_google_maps_context", no_lookup)
    request = Request({"type": "http", "method": "GET", "path": "/documents/9/pdf", "query_string": b"", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await web.web_document_pdf(request, 9, user=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_pdf_route_returns_the_typeset_document(monkeypatch):
    async def document(doc_id):
        return {"id": doc_id, "filename": "vs_att_X.p7m", "xml_content": "<x/>"}

    async def no_lookup(doc):
        return None

    monkeypatch.setattr(web, "get_document_by_id", document)
    monkeypatch.setattr(web, "_google_maps_context", no_lookup)
    monkeypatch.setattr(pdf_typst, "build_context", lambda doc, kpis, lookup: {"ctx": True})
    monkeypatch.setattr(pdf_typst, "render_pdf", lambda ctx: b"%PDF-1.7 test")
    request = Request({"type": "http", "method": "GET", "path": "/documents/5/pdf", "query_string": b"", "headers": []})

    response = await web.web_document_pdf(request, 5, user=None)

    assert response.media_type == "application/pdf" and response.body.startswith(b"%PDF")
    assert response.headers["content-disposition"] == 'attachment; filename="vs_att_X.pdf"'


def test_a_company_subject_shows_denomination_and_registered_office():
    doc = subject_doc()
    doc["xml_parsed"]["VisuraSoggettoAttuale"]["DatiRichiesta"]["SoggettoIndividuato"] = {"SoggettoPNF": {
        "CodiceFiscale": "00110110822", "Denominazione": "S.I.A.T. S.P.A.", "Sede": "PALERMO (PA)"}}

    context = build_context(doc, None, None, TODAY)

    assert context["title"] == "S.I.A.T. S.P.A."
    rows = context["sections"][0]["items"][0]["rows"]
    assert rows[0] == ["Denominazione", "S.I.A.T. S.P.A.", "00110110822"] and rows[1][:2] == ["Sede", "PALERMO (PA)"]
    assert not any(row[0] in ("Nascita", "Sesso") for row in rows)
    assert render_pdf(context).startswith(b"%PDF")


def test_owners_with_a_degraded_birth_date_get_the_date_from_the_codice_fiscale():
    doc = subject_doc()
    mutation = doc["xml_parsed"]["VisuraSoggettoAttuale"]["GruppoUnitaImmobiliari"][0]["IntestazioneGruppo"]["MutazioneSoggettiva"][0]
    mutation["Intestazione"]["Intestato"][0].update({"Nominativo": "GRISTINA Antonio  /0/08/ il ; Comune  Nato a PALERMO (PA)", "CF": "GRSNTN62D08G273W"})

    context = build_context(doc, None, None, TODAY)

    owner = next(i for i in next(s for s in context["sections"] if s["title"] == "NAPOLI")["items"] if i["kind"] == "mutations")["items"][0]["owners"][0]
    assert owner["name"] == "GRISTINA Antonio" and owner["sub"] == "nato il 08/04/1962 · PALERMO (PA)"
