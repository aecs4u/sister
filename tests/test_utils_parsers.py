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


def test_open_richieste_view_selects_category_and_whole_period():
    import asyncio

    calls = []

    class FakePage:
        async def check(self, selector):
            calls.append(("check", selector))

        async def select_option(self, selector, value):
            calls.append(("select", selector, value))

        async def click(self, selector):
            calls.append(("click", selector))

        async def wait_for_load_state(self, state, timeout):
            calls.append(("wait", state))

    asyncio.run(utils._open_richieste_view(FakePage(), "prelevate"))

    assert calls == [
        ("check", "input[name='radioCount'][value='prelevate']"),
        ("select", "select[name='comboGiorni']", "-"),
        ("click", "input[name='metodo'][value='Aggiorna']"),
        ("wait", "networkidle"),
    ]
    assert utils._RICHIESTE_VIEWS == ("espletate", "prelevate")


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


def test_subject_filename_carries_the_office_so_results_of_different_offices_do_not_share_a_name():
    base = {"tipo": "visura_soggetto", "visura_subtype": "attuale", "tipo_catasto": "E", "identificativo": "RSSMRA85M01H501Z"}

    ravenna = utils._descriptive_filename({**base, "provincia": "ra"})
    palermo = utils._descriptive_filename({**base, "provincia": "PA"})
    limited = utils._descriptive_filename({**base, "provincia": "RA", "codice_comune": "H199"})

    assert (ravenna, palermo) == ("vs_att_RSSMRA85M01H501Z_RA_CE", "vs_att_RSSMRA85M01H501Z_PA_CE")
    assert limited == "vs_att_RSSMRA85M01H501Z_RA_H199_CE"
    assert utils._descriptive_filename(base) == "vs_att_RSSMRA85M01H501Z_CE"  # national scope: no office


def test_subject_filename_always_names_its_cadastre():
    base = {"tipo": "visura_soggetto", "visura_subtype": "attuale", "identificativo": "RSSMRA85M01H501Z", "provincia": "RA"}

    assert utils._descriptive_filename({**base, "tipo_catasto": "E"}) == "vs_att_RSSMRA85M01H501Z_RA_CE"
    assert utils._descriptive_filename({**base, "tipo_catasto": "F"}) == "vs_att_RSSMRA85M01H501Z_RA_CF"
    assert utils._descriptive_filename({**base, "tipo_catasto": "T"}) == "vs_att_RSSMRA85M01H501Z_RA_CT"
    # the document does not say (older PDF, unknown value): both cadastres, the portal default
    assert utils._descriptive_filename(base) == "vs_att_RSSMRA85M01H501Z_RA_CE"
    assert utils._descriptive_filename({**base, "tipo_catasto": "X"}) == "vs_att_RSSMRA85M01H501Z_RA_CE"


def test_historical_subject_filename_carries_the_situation_date_when_it_is_not_the_request_date():
    parsed = {
        "tipo": "visura_soggetto",
        "visura_subtype": "storica",
        "tipo_catasto": "E",
        "identificativo": "03870361205",
        "provincia": "BO",
        "situazione_al": "26/12/2023",
    }

    assert utils._descriptive_filename(parsed, requested_at="26/06/2026 22:53") == "vs_sto_03870361205_BO_CE_al2023-12-26"
    assert utils._descriptive_filename({**parsed, "situazione_al": "26/06/2026"}, requested_at="26/06/2026") == (
        "vs_sto_03870361205_BO_CE"
    )
    # an attuale visura is "al" its request day: no extra token whatever the portal prints
    assert utils._descriptive_filename({**parsed, "visura_subtype": "attuale"}, requested_at="26/06/2026") == (
        "vs_att_03870361205_BO_CE"
    )
    assert utils._document_key("vs_sto_03870361205_BO_CE_al2023-12-26_2026-06-26") == "vs_sto_03870361205_BO_CE_al2023-12-26"


def test_unique_document_stem_adds_the_practice_id_on_collision(tmp_path):
    (tmp_path / "vi_att_ROMA.pdf").touch()
    (tmp_path / "vi_att_ROMA_123_2024.pdf").touch()

    stem = utils._unique_document_stem(
        str(tmp_path),
        "vi_att_ROMA",
        ".pdf",
        parsed={"protocollo": "123", "anno": "2024"},
        request_id="req_1",
    )

    assert stem == "vi_att_ROMA_123_2024_2"


def test_unique_document_stem_appends_the_iso_request_date_then_the_practice_id(tmp_path):
    def name(requested_at):
        stem = utils._unique_document_stem(
            str(tmp_path),
            "vs_att_SNTSFN77C08F205Y_CF",
            ".p7m",
            parsed={"protocollo": "T265733", "anno": "2026"},
            request_id="req_1",
            requested_at=requested_at,
        )
        (tmp_path / f"{stem}.p7m").touch()
        return stem

    # every download carries the date (not the time) of its request; the practice id only separates repeats
    assert name("06/10/2026 16:06") == "vs_att_SNTSFN77C08F205Y_CF_2026-10-06"
    assert name("06/10/2026\xa016:08") == "vs_att_SNTSFN77C08F205Y_CF_2026-10-06_T265733_2026"
    assert name("06/10/2026 16:34") == "vs_att_SNTSFN77C08F205Y_CF_2026-10-06_T265733_2026_2"
    assert name("07/10/2026 09:00") == "vs_att_SNTSFN77C08F205Y_CF_2026-10-07"


def test_unique_document_stem_without_any_date_keeps_the_plain_name_while_free(tmp_path):
    def name():
        stem = utils._unique_document_stem(
            str(tmp_path), "vi_att_ROMA", ".pdf", parsed={"protocollo": "T1", "anno": "2026"}, request_id="r"
        )
        (tmp_path / f"{stem}.pdf").touch()
        return stem

    assert name() == "vi_att_ROMA"
    assert name() == "vi_att_ROMA_T1_2026"


def test_the_reference_date_stands_in_when_the_request_date_is_unknown(tmp_path):
    stem = utils._unique_document_stem(
        str(tmp_path), "vi_att_ROMA", ".p7m", parsed={"situazione_al": "06/10/2026"}, request_id="r"
    )

    assert stem == "vi_att_ROMA_2026-10-06"


def test_document_key_pairs_files_requested_on_different_days():
    assert utils._document_key("vs_att_SNTSFN77C08F205Y_CF_2026-10-06") == "vs_att_SNTSFN77C08F205Y_CF"
    assert utils._document_key("vi_att_PA_FG11_PT1752_2026-10-06_T1_2026") == "vi_att_PA_FG11_PT1752_T1_2026"
    assert utils._document_key("vs_att_SNTSFN77C08F205Y_CF") == "vs_att_SNTSFN77C08F205Y_CF"


def test_iso_request_date_reads_portal_dates_and_rejects_impossible_ones():
    assert utils._iso_request_date("06/10/2026 16:08") == "2026-10-06"
    assert utils._iso_request_date("6/1/2026") == "2026-01-06"
    assert utils._iso_request_date("20261006") == "2026-10-06"
    assert utils._iso_request_date("31/02/2026 10:00") == ""
    assert utils._iso_request_date("") == ""


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


def _fake_p7m_payload(monkeypatch, payload: bytes):
    import aecs4u_crypto

    monkeypatch.setattr(aecs4u_crypto, "extract_p7m_payload", lambda data: payload)


def test_parsing_a_p7m_leaves_no_extracted_copy_in_the_documents_folder(tmp_path, monkeypatch):
    _fake_p7m_payload(monkeypatch, b"<root><TitoloVisura Titolo='Visura attuale'/></root>")
    p7m = tmp_path / "vi_att_RA_FG1_PT2.p7m"
    p7m.write_bytes(b"signed")

    parsed = utils._parse_visura_xml(str(p7m))

    assert parsed and "TitoloVisura" in parsed["xml_content"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["vi_att_RA_FG1_PT2.p7m"]


def test_p7m_payload_is_returned_in_memory_and_empty_payload_is_none(tmp_path, monkeypatch):
    p7m = tmp_path / "vi_att_RA_FG1_PT2.p7m"
    p7m.write_bytes(b"signed")

    _fake_p7m_payload(monkeypatch, b"<root/>")
    assert utils._p7m_payload(str(p7m)) == b"<root/>"
    _fake_p7m_payload(monkeypatch, b"")
    assert utils._p7m_payload(str(p7m)) is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["vi_att_RA_FG1_PT2.p7m"]


def test_missing_pairs_pairs_a_pdf_and_a_p7m_requested_on_different_days(tmp_path):
    from sister.missing_pairs import build_missing_pair_batch

    (tmp_path / "vi_att_RA_FG1_PT2_2026-10-06.p7m").touch()
    (tmp_path / "vi_att_RA_FG1_PT2_2026-10-08.pdf").touch()

    batch = build_missing_pair_batch(tmp_path)

    assert batch["missing_pairs"] == [] and batch["skipped"] == []


def test_select_richieste_keeps_recent_requests_not_stored_yet():
    rows = [
        {"richiesta_del": "10/09/2026 09:00", "oggetto": "old", "salva_href": "a"},
        {"richiesta_del": "14/10/2026 15:24", "oggetto": "new", "salva_href": "b"},
        {"richiesta_del": "14/10/2026 15:30", "oggetto": "stored", "salva_href": "c"},
        {"richiesta_del": "", "oggetto": "undated", "salva_href": "d"},
    ]

    assert [r["oggetto"] for r in utils._select_richieste(rows)] == ["old", "new", "stored", "undated"]
    assert [r["oggetto"] for r in utils._select_richieste(rows, since="2026-10-14")] == ["new", "stored"]
    known = {("14/10/2026 15:30", "stored")}
    assert [r["oggetto"] for r in utils._select_richieste(rows, since="2026-10-14", known=known)] == ["new"]


def test_empty_downloads_are_not_kept_so_a_pending_request_is_fetched_again():
    import inspect

    source = inspect.getsource(utils._download_richieste_documents)

    assert "os.path.getsize(temp_path) == 0" in source and "non ancora pronto" in source
