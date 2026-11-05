"""Entities that share a fiscal code stay distinguishable: one ``subject_record`` per (code, denominazione, sede)."""

import pytest
from sqlalchemy import text

from sister import database
from sister.reference import parse_seat

CODE = "80050050154"


def _row(n: int, record: int, name: str, seat: str) -> dict:
    return {
        "Foglio": str(n), "Particella": "1", "Titolarità": "Proprieta' per 1/1", "Categoria": "A/2",
        "omonimo": record, "omonimo_denominazione": name, "omonimo_sede": seat,
    }


def _combined() -> dict:
    return {
        "soggetto": CODE,
        "immobili": [
            _row(1, 1, "COMUNE DI BAGOLINO", "BAGOLINO (BS)"),
            _row(2, 1, "COMUNE DI BAGOLINO", "BAGOLINO (BS)"),
            _row(3, 2, "DEMANIO DELLA REGIONE", "MILANO (MI)"),
        ],
        "omonimi": [],
        "omonimi_totale": 2,
    }


def _records_list() -> dict:
    """What the old flow stored: the records page itself."""
    return {
        "soggetto": CODE,
        "immobili": [
            {"Denominazione": "ATS DELLA BRIANZA", "Sede": "LECCO (LC)", "Codice Fiscale": CODE, "omonimoSelezionato": "1#0#ATS#LECCO (LC)#" + CODE},
            {"Denominazione": "COMUNE DI BERGAMO", "Sede": "BERGAMO (BG)", "Codice Fiscale": CODE, "omonimoSelezionato": "2#0#COMUNE#BERGAMO (BG)#" + CODE},
        ],
    }


def test_seat_is_split_into_municipality_and_province():
    assert parse_seat("LECCO (LC)") == ("LECCO", "LC")
    assert parse_seat("SAN GIORGIO (di) X") == ("SAN GIORGIO (di) X", None)
    assert parse_seat(None) == (None, None) and parse_seat("") == (None, None)


def test_every_row_of_the_combined_answer_carries_its_record():
    properties, _, _ = database._project_response("r1", "E", _combined())
    assert [p.record["denomination"] for p in properties] == ["COMUNE DI BAGOLINO", "COMUNE DI BAGOLINO", "DEMANIO DELLA REGIONE"]
    assert {p.record["fiscal_code"] for p in properties} == {CODE}
    assert properties[2].record["seat"] == "MILANO (MI)"


def test_a_row_without_record_data_has_no_record():
    properties, _, _ = database._project_response("r1", "E", {"soggetto": CODE, "immobili": [{"Foglio": "1", "Particella": "1"}]})
    assert properties[0].record is None


def test_the_old_records_list_still_yields_the_records():
    properties, _, _ = database._project_response("r1", "E", _records_list())
    assert [(p.record["denomination"], p.record["seat"]) for p in properties] == [
        ("ATS DELLA BRIANZA", "LECCO (LC)"), ("COMUNE DI BERGAMO", "BERGAMO (BG)")]


async def _save(request_id: str, data: dict) -> None:
    await database.save_request(
        request_id=request_id, request_type="persona_giuridica", tipo_catasto="E", provincia="NAZIONALE", comune="-",
        foglio="-", particella="-",
    )
    await database.save_response(request_id, True, "E", data=data)


@pytest.mark.usefixtures("fresh_db")
async def test_properties_and_owner_rows_point_at_their_own_record():
    await _save("pnf_E_1", _combined())
    async with database._get_session_factory()() as session:
        records = (await session.execute(text("SELECT denomination, seat, seat_municipality, seat_province_code FROM subject_record ORDER BY denomination"))).all()
        by_record = (await session.execute(text(
            "SELECT r.denomination, count(p.id) FROM subject_record r JOIN property p ON p.subject_record_id = r.id GROUP BY 1 ORDER BY 1"))).all()
        owners = (await session.execute(text(
            "SELECT r.denomination, s.fiscal_code FROM result_owner o JOIN subject_record r ON r.id = o.subject_record_id "
            "JOIN subject s ON s.id = o.subject_id ORDER BY r.denomination"))).all()
        subjects = (await session.execute(text("SELECT count(*) FROM subject WHERE fiscal_code = :c"), {"c": CODE})).scalar_one()
        view = (await session.execute(text("SELECT denomination, count(property_id) FROM v_subject_record_property GROUP BY 1 ORDER BY 1"))).all()
    assert records == [("COMUNE DI BAGOLINO", "BAGOLINO (BS)", "BAGOLINO", "BS"), ("DEMANIO DELLA REGIONE", "MILANO (MI)", "MILANO", "MI")]
    assert [tuple(r) for r in by_record] == [("COMUNE DI BAGOLINO", 2), ("DEMANIO DELLA REGIONE", 1)]
    assert {tuple(o) for o in owners} == {("COMUNE DI BAGOLINO", CODE), ("DEMANIO DELLA REGIONE", CODE)}
    assert subjects == 1  # still one subject per code: the records are what tells the entities apart
    assert [tuple(v) for v in view] == [("COMUNE DI BAGOLINO", 2), ("DEMANIO DELLA REGIONE", 1)]


@pytest.mark.usefixtures("fresh_db")
async def test_a_record_is_created_once_across_requests():
    await _save("pnf_E_1", _combined())
    await _save("pnf_E_2", _combined())
    async with database._get_session_factory()() as session:
        assert (await session.execute(text("SELECT count(*) FROM subject_record"))).scalar_one() == 2


@pytest.mark.usefixtures("fresh_db")
async def test_the_stored_records_list_creates_the_records_too():
    await _save("pnf_E_old", _records_list())
    async with database._get_session_factory()() as session:
        rows = (await session.execute(text("SELECT denomination, seat FROM subject_record ORDER BY denomination"))).all()
    assert [tuple(r) for r in rows] == [("ATS DELLA BRIANZA", "LECCO (LC)"), ("COMUNE DI BERGAMO", "BERGAMO (BG)")]


# --- the persons and companies of the Elenco Omonimi page ----------------------------------------------------------

PERSON = "CVNFNC98D12D423G"


def _person_entry(**extra) -> dict:
    return {
        "tipo": "persona", "codice_fiscale": PERSON, "cognome": "IACOVINO", "nome": "FRANCESCO", "sesso": "M",
        "data_nascita": "12/04/1998", "luogo_nascita": "ERICE", "provincia_nascita": "TP", "indice": 1, "selezionato": True, **extra,
    }


def test_a_listed_person_is_decoded_from_the_radio_value_and_the_table():
    from sister.result_parsers import omonimo_details, parse_omonimo_value

    value = f"22702531#0#IACOVINO#FRANCESCO#{PERSON}#ERICE#12/04/1998#TP"
    assert parse_omonimo_value(value) == {
        "tipo": "persona", "codice_fiscale": PERSON, "cognome": "IACOVINO", "nome": "FRANCESCO", "luogo_nascita": "ERICE",
        "data_nascita": "12/04/1998", "provincia_nascita": "TP",
    }
    row = {"Cognome": "IACOVINO", "Nome": "FRANCESCO", "Sesso": "M", "Luogo di nascita": "ERICE (TP)", "Data di nascita": "12/04/1998",
           "Codice Fiscale": PERSON, "omonimoSelezionato": value}
    details = omonimo_details(row, 1, True)
    assert (details["luogo_nascita"], details["provincia_nascita"], details["sesso"], details["selezionato"]) == ("ERICE", "TP", "M", True)
    assert details["riga"]["Cognome"] == "IACOVINO" and "omonimoSelezionato" not in details["riga"]


def test_a_listed_company_is_decoded():
    from sister.result_parsers import omonimo_details, parse_omonimo_value

    value = "9799268475#0#ATS DELLA BRIANZA#LECCO (LC)#80050050154"
    assert parse_omonimo_value(value) == {"tipo": "azienda", "codice_fiscale": "80050050154", "denominazione": "ATS DELLA BRIANZA", "sede": "LECCO (LC)"}
    assert omonimo_details({"Denominazione": "ATS DELLA BRIANZA", "Sede": "LECCO (LC)", "Codice Fiscale": "80050050154", "omonimoSelezionato": value}, 2)["tipo"] == "azienda"
    assert parse_omonimo_value("garbage") == {} and parse_omonimo_value(None) == {}


@pytest.mark.usefixtures("fresh_db")
async def test_a_person_listed_on_the_omonimi_page_gets_its_name_and_birth_data():
    # the subject is first met by code only, as the earlier searches left it
    async with database._get_session_factory()() as session:
        await session.execute(text("INSERT INTO subject (fiscal_code, name, subject_type) VALUES (:c, :c, 'person')"), {"c": PERSON})
        await session.commit()
    await _save("sogg_1", {"soggetto": PERSON, "soggetti": [_person_entry()], "province": [], "immobili": []})
    async with database._get_session_factory()() as session:
        row = (await session.execute(text("SELECT name, last_name, first_name, gender, birth_date FROM subject WHERE fiscal_code = :c"), {"c": PERSON})).one()
    assert (row.name, row.last_name, row.first_name, row.gender, str(row.birth_date)) == ("IACOVINO FRANCESCO", "IACOVINO", "FRANCESCO", "M", "1998-04-12")


@pytest.mark.usefixtures("fresh_db")
async def test_every_person_of_the_page_is_stored_not_only_the_selected_one():
    other = _person_entry(codice_fiscale="RSSMRA80A01H501U", cognome="ROSSI", nome="MARIO", indice=2, selezionato=False, luogo_nascita="ROMA", provincia_nascita="RM", data_nascita="01/01/1980")
    await _save("sogg_2", {"soggetto": PERSON, "soggetti": [_person_entry(), other], "province": [], "immobili": []})
    async with database._get_session_factory()() as session:
        names = (await session.execute(text("SELECT fiscal_code, name FROM subject WHERE fiscal_code IN (:a, :b) ORDER BY 1"), {"a": PERSON, "b": "RSSMRA80A01H501U"})).all()
    assert [tuple(n) for n in names] == [(PERSON, "IACOVINO FRANCESCO"), ("RSSMRA80A01H501U", "ROSSI MARIO")]


@pytest.mark.usefixtures("fresh_db")
async def test_companies_of_the_page_get_their_records_and_a_single_record_names_the_code():
    one = {"tipo": "azienda", "codice_fiscale": "02342730120", "denominazione": "ACME SRL", "sede": "VARESE (VA)", "indice": 1}
    many = [{"tipo": "azienda", "codice_fiscale": CODE, "denominazione": f"ENTE {n}", "sede": f"SEDE {n} (XX)", "indice": n} for n in (1, 2)]
    await _save("sogg_3", {"soggetto": "02342730120", "soggetti": [one], "province": [], "immobili": []})
    await _save("sogg_4", {"soggetto": CODE, "soggetti": many, "province": [], "immobili": []})
    async with database._get_session_factory()() as session:
        records = (await session.execute(text("SELECT count(*) FROM subject_record"))).scalar_one()
        single = (await session.execute(text("SELECT name FROM subject WHERE fiscal_code = '02342730120'"))).scalar_one()
        shared = (await session.execute(text("SELECT name FROM subject WHERE fiscal_code = :c"), {"c": CODE})).scalar_one()
    assert records == 3 and single == "ACME SRL"
    assert shared == CODE  # several records share this code: none of the names is "the" name


# --- the pages SISTER shows for a code search (screenshots of 2026-10-26) ------------------------------------------

PERSON_PAGE = """<html><body><fieldset><legend>Elenco Omonimi</legend><table>
<tr><th></th><th>Cognome</th><th>Nome</th><th>Data di nascita</th><th>Luogo di nascita</th><th>Sesso</th><th>Codice Fiscale</th></tr>
<tr><td><input type="radio" name="omonimoSelezionato" value="123#0#CORTESI#MAURIZIO#CRTMRZ59P07H199E#RAVENNA#07/09/1959#RA"/></td>
<td>CORTESI</td><td>MAURIZIO</td><td>07/09/1959</td><td>RAVENNA (RA)</td><td>M</td><td><a href="#">CRTMRZ59P07H199E</a></td></tr>
</table></fieldset></body></html>"""

SELECTED_BLOCK = """Soggetto selezionato
Tipo richiesta: Attualità
Cognome: CORTESI Nome: MAURIZIO
Data di Nascita: 07/09/1959
Comune di Nascita: RAVENNA (RA)
Codice Fiscale: CRTMRZ59P07H199E

Immobili individuati: 3
Motivazione: Esplorazione"""


def test_the_omonimi_table_of_a_person_is_found_by_its_radios_not_by_a_property_header():
    from sister import utils
    from sister.result_parsers import omonimo_details

    rows = utils._omonimi_table_rows(PERSON_PAGE)
    assert len(rows) == 1 and rows[0]["Cognome"] == "CORTESI"
    # the property-oriented extractor ignores this table: that is why the details were lost before
    assert utils._extract_result_tables(PERSON_PAGE) == []
    details = omonimo_details(rows[0], 1, True)
    assert (details["cognome"], details["nome"], details["luogo_nascita"], details["provincia_nascita"], details["sesso"]) == (
        "CORTESI", "MAURIZIO", "RAVENNA", "RA", "M")


def test_the_selected_subject_block_of_the_properties_page_is_parsed():
    from sister.result_parsers import parse_soggetto_selezionato

    assert parse_soggetto_selezionato(SELECTED_BLOCK) == {
        "tipo": "persona", "codice_fiscale": "CRTMRZ59P07H199E", "cognome": "CORTESI", "nome": "MAURIZIO", "data_nascita": "07/09/1959",
        "luogo_nascita": "RAVENNA", "provincia_nascita": "RA", "indice": 1, "selezionato": True, "fonte": "Soggetto selezionato",
    }
    assert parse_soggetto_selezionato("Cognome: DE LUCA Nome: ANNA MARIA Data di Nascita: 01/02/1970 Comune di Nascita: SAN GIOVANNI (TO) Codice Fiscale: DLCNMR70B41H501Z")["cognome"] == "DE LUCA"
    company = parse_soggetto_selezionato("Soggetto selezionato Denominazione: ACME SRL Sede: VARESE (VA) Codice Fiscale: 02342730120")
    assert (company["tipo"], company["denominazione"], company["sede"]) == ("azienda", "ACME SRL", "VARESE (VA)")
    assert parse_soggetto_selezionato("Elenco immobili per diritti e quote") == {} and parse_soggetto_selezionato(None) == {}
