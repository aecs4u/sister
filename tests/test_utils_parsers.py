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


def test_parse_table_keeps_the_radio_value_and_drops_the_blank_radio_column():
    html = """
    <table>
      <tr><th></th><th>Foglio</th><th>Particella</th></tr>
      <tr><td><input type="radio" name="visImmSel" value="634568#634568#F#RA/103#1714#H199##2# #RAVENNA"></td>
          <td>RA/103</td><td>1714</td></tr>
      <tr><td><input type="radio" property="visImmSel" value="x#x#T#1#2#G273###"></td><td>1</td><td>2</td></tr>
    </table>
    """

    rows = utils.parse_table(html)

    assert rows[0] == {"Foglio": "RA/103", "Particella": "1714", "visImmSel": "634568#634568#F#RA/103#1714#H199##2# #RAVENNA"}
    assert rows[1]["visImmSel"] == "x#x#T#1#2#G273###" and "" not in rows[1]


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


def _step(index, *cfs):
    return {"result_index": index, "intestati": [{"Codice fiscale": cf} for cf in cfs]}


def test_soggetto_plan_requests_each_owner_once_per_run():
    steps = [_step(1, "AAA", "BBB"), _step(2, "AAA"), _step(3, "BBB", "CCC"), _step(4), _step(5, "DDD")]

    plan = utils._plan_soggetto_requests(steps, bene_comune_indices={3})

    # 1 -> AAA, 2 -> only AAA (already requested), 3 -> BBB (CCC is not needed), 4 bene comune, 5 -> DDD
    assert plan == {1: 0, 2: None, 3: 0, 4: None, 5: 0}
    assert steps[1]["documents"] == {"soggetto": "duplicate"}


def test_soggetto_plan_does_not_merge_owners_without_codice_fiscale():
    steps = [_step(1, ""), _step(2, "")]

    assert utils._plan_soggetto_requests(steps, set()) == {1: 0, 2: 0}


def test_pending_documents_lists_only_what_was_not_requested():
    steps = [
        {"result_index": 1, "documents": {"immobile": "requested", "soggetto": "requested"}},
        {"result_index": 2, "documents": {"immobile": "requested", "soggetto": "pending"}},
        {"result_index": 3, "documents": {"immobile": "pending", "soggetto": "duplicate"}},
        {"result_index": 4},
    ]

    assert utils._pending_documents(steps) == [
        {"result_index": 2, "kind": "soggetto"},
        {"result_index": 3, "kind": "immobile"},
    ]


# --- explicit confirmation that SISTER accepted a document request --------------------------------------------

ACCEPTED_PAGE = (
    'Ti trovi in: Home dei Servizi / Attesa Convenzione: ROSSI MARIO (CONSULTAZIONI - PROFILO B) '
    'Codice di Richiesta: C00075022026 Richiesta inoltrata: Verificare i risultati nella sezione "Richieste".'
)
REJECTED_PAGE = (
    "Dati della ricerca Catasto: Fabbricati Comune di: RAVENNA Foglio: 103 Particella: 1714 "
    "Digitare correttamente il codice di sicurezza Visura immobile Con intestati Codice di sicurezza:"
)


def test_accepted_page_is_confirmed_with_its_portal_code():
    result = utils.classify_submit_page(ACCEPTED_PAGE, has_captcha_input=False)

    assert result == {"confirmed": True, "portal_code": "C00075022026", "reason": ""}


def test_form_coming_back_is_a_rejected_captcha_not_a_success():
    assert utils.classify_submit_page(REJECTED_PAGE, True)["reason"] == "captcha_rejected"
    # the field alone, without the message, is still not a success
    assert utils.classify_submit_page("Tipo di visura", True)["confirmed"] is False


def test_a_page_without_the_acknowledgement_is_not_confirmed_even_if_the_captcha_field_is_gone():
    # the case the old check got wrong: the field vanished, but SISTER did not say "Richiesta inoltrata"
    for text in ("Sessione scaduta o errore caricamento pagina", "Servizio momentaneamente non disponibile", ""):
        result = utils.classify_submit_page(text, has_captcha_input=False)
        assert result["confirmed"] is False and result["reason"] == "no_confirmation"


class _FakePage:
    def __init__(self, texts, captcha=False):
        self._texts = list(texts)
        self._captcha = captcha

    async def inner_text(self, selector):
        return self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]

    def locator(self, selector):
        outer = self

        class _Locator:
            async def count(self_inner):
                return 1 if outer._captcha else 0

        return _Locator()


async def test_confirmation_waits_for_the_page_to_finish_navigating():
    page = _FakePage(["Attendere prego...", ACCEPTED_PAGE])

    result = await utils._confirm_request_submitted(page, wait=3.0)

    assert result["confirmed"] and result["portal_code"] == "C00075022026"


async def test_confirmation_gives_up_with_a_reason_when_nothing_acknowledges():
    page = _FakePage(["Errore imprevisto"])

    result = await utils._confirm_request_submitted(page, wait=0.6)

    assert result == {"confirmed": False, "portal_code": "", "reason": "no_confirmation"}


def test_unconfirmed_requests_are_recorded_and_listed_separately_from_pending():
    step = {"result_index": 4, "documents": {"immobile": "pending", "soggetto": "pending"}}
    utils._record_submit(step, step["documents"], "immobile", {"confirmed": True, "portal_code": "C1234567"})
    utils._record_submit(step, step["documents"], "soggetto", {"confirmed": False, "reason": "captcha_rejected"})

    assert step["documents"] == {"immobile": "requested", "soggetto": "unconfirmed"}
    assert step["portal_codes"] == {"immobile": "C1234567"}
    assert utils._unconfirmed_documents([step]) == [
        {"result_index": 4, "kind": "soggetto", "reason": "captcha_rejected"}]
    assert utils._pending_documents([step]) == []
