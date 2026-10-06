"""Deterministic HTML and document naming helpers used by browser flows."""

from sister import utils


def test_parse_table_pads_short_rows_to_header_count():
    html = """
    <table>
      <tr><th>Foglio</th><th>Particella</th><th>Sub</th></tr>
      <tr><td>9</td><td>166</td></tr>
    </table>
    """

    assert utils.parse_table(html) == [{"Foglio": "9", "Particella": "166", "Sub": ""}]


def test_parse_richieste_table_extracts_save_link_and_request_id():
    html = """
    <table>
      <tr><th>Richiesta del</th><th>Oggetto</th><th>Formato</th><th>Costo</th></tr>
      <tr>
        <td>01/10/2026&nbsp;10:30</td><td>Visura immobile</td><td>PDF</td><td>1,00</td>
        <td><a href="/Visure/Salva.do?azione=salva&amp;idRichiesta=abc-123">Salva</a></td>
      </tr>
      <tr><td>02/10/2026</td><td>Non scaricabile</td><td>PDF</td><td>0,00</td></tr>
    </table>
    """

    assert utils._parse_richieste_table(html) == [
        {
            "richiesta_del": "01/10/2026 10:30",
            "oggetto": "Visura immobile",
            "formato": "PDF",
            "costo": "1,00",
            "salva_href": "/Visure/Salva.do?azione=salva&idRichiesta=abc-123",
            "id_richiesta": "abc-123",
        }
    ]
    assert utils._parse_richieste_table("<html><table><tr><td>other</td></tr></table></html>") == []


def test_extract_intestati_rows_from_matching_table_and_pad_cells():
    html = """
    <table>
      <tr><th>Nominativo o denominazione</th><th>Codice fiscale</th><th>Titolarità</th></tr>
      <tr><td>ROSSI MARIO</td><td>RSSMRA85M01H501Z</td></tr>
    </table>
    """

    assert utils._extract_intestati_from_page(html) == [
        {
            "Nominativo o denominazione": "ROSSI MARIO",
            "Codice fiscale": "RSSMRA85M01H501Z",
            "Titolarità": "",
        }
    ]


def test_extract_result_tables_skips_unrelated_tables():
    html = """
    <table><tr><th>Descrizione</th></tr><tr><td>Menu</td></tr></table>
    <table><tr><th>Foglio</th><th>Particella</th></tr><tr><td>9</td><td>166</td></tr></table>
    """

    assert utils._extract_result_tables(html) == [{"Foglio": "9", "Particella": "166"}]
    assert utils._extract_result_tables("<table><tr><td>no headers</td></tr></table>") == []


def test_descriptive_filename_uses_subject_and_property_metadata():
    assert utils._descriptive_filename(
        {
            "tipo": "visura_soggetto",
            "visura_subtype": "storica",
            "tipo_catasto": "E",
            "identificativo": "01234567890",
        }
    ) == "vs_sto_01234567890_CE"
    assert utils._descriptive_filename(
        {
            "tipo": "visura_terreni",
            "visura_subtype": "sintetica",
            "tipo_catasto": "T",
            "provincia": "Roma",
            "foglio": "001",
            "particella": "02",
            "subalterno": "7",
        }
    ) == "vi_sin_ter_ROMA_FG001_PT02_SUB7"


def test_unique_document_stem_handles_pdf_and_xml_collisions(tmp_path):
    (tmp_path / "vi_att_ROMA.pdf").touch()
    (tmp_path / "vi_att_ROMA_123_2024.xml").touch()

    stem = utils._unique_document_stem(
        str(tmp_path),
        "vi_att_ROMA",
        ".pdf",
        include_xml=True,
        parsed={"protocollo": "123", "anno": "2024"},
        request_id="req_1",
    )

    assert stem == "vi_att_ROMA_123_2024_2"
