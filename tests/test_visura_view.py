"""The interpreted visura model (sister/visura_view.py), its name/period parsers and the story macros."""

from __future__ import annotations

from pathlib import Path

from sister import result_parsers as rp
from sister.visura_view import build_visura_view

FIXTURES = Path(__file__).parent / "fixtures" / "visura_xml"

# A unit created by a variation in 2013 whose rights, listed as of the creation, ended with a death in 2012: the
# portal prints the predecessor's right "dal 22/07/2013 al 01/09/2012" and numbers the newest act #1.
SUCCESSION = """<?xml version="1.0" encoding="UTF-8"?>
<Visura><VisuraFabbricatiStorica>
  <TitoloVisura Titolo="Visura storico fabbricato" SituazioneAl="20261010" Data="20261010" Ora="082357"/>
  <DatiRichiesta Provincia="PA" CodiceComune="G273" Comune="PALERMO" Foglio="9" ParticellaNum="1452" Subalterno="7"/>
  <SituazioneAttualeFabbricati>
    <DatiIdentificativi><IdentificativoDefinitivo Provincia="PA" CodiceComune="G273" Comune="PALERMO" Foglio="9" ParticellaNum="1452" Subalterno="7" ProgrId="1"/></DatiIdentificativi>
    <DatiClassamentoF Categoria="F/5 LASTRICO SOLARE"><Consistenza Unita="MQ" Valore="120"/></DatiClassamentoF>
    <IndirizzoImm>VIA ROMA n. 1 Piano 2</IndirizzoImm>
    <IntestazioneAttuale>
      <Intestato IndiceIntestato="1"><Nominativo>ROSSI Mario 04/07/1974; Comune PALERMO (PA)</Nominativo><CF>RSSMRA74L04G273K</CF>
        <DirittiReali CodiceDiritto="05" Descrizione="Enfiteusi" Quota=" per 2/9" FineDiritto="dal 01/09/2012 al 10/10/2026"/></Intestato>
      <Intestato IndiceIntestato="2"><Nominativo>MONTE DI PIETA'; Comune PALERMO (PA)</Nominativo><CF>00000000018</CF>
        <DirittiReali CodiceDiritto="04" Descrizione="Diritto del concedente" Quota=" per 1/1" FineDiritto="dal 01/09/2012 al 10/10/2026"/></Intestato>
    </IntestazioneAttuale>
  </SituazioneAttualeFabbricati>
  <StoriaImmobileFabbricati>
    <DatiIdentificativi><IdentificativoDefinitivo Provincia="PA" CodiceComune="G273" Comune="PALERMO" Foglio="9" ParticellaNum="1452" Subalterno="7" ProgrId="1" StatoImmobile="attuale">
      <DatiDerivantiDa Descrizione="(ALTRE) del 22/07/2013"/><Situazione>dal 22/07/2013 al 10/10/2026</Situazione></IdentificativoDefinitivo></DatiIdentificativi>
    <DatiIndirizzo><IdentificativoDefinitivoRiferimento Provincia="PA" Foglio="9" ParticellaNum="1452" Subalterno="7" StatoImmobile="attuale"/>
      <DatiDerivantiDa Descrizione="(ALTRE) del 22/07/2013"/><Situazione>dal 22/07/2013 al 10/10/2026</Situazione><IndirizzoImm>VIA ROMA n. 1 Piano 2</IndirizzoImm></DatiIndirizzo>
  </StoriaImmobileFabbricati>
  <StoriaIntestazione>
    <MutazioneSoggettiva IndiceMutazione="1">
      <DatiDerivantiDaMutazSogg>DENUNZIA (NEI PASSAGGI PER CAUSA DI MORTE) del 01/09/2012 - SUCC DI ROSSI GAETANO</DatiDerivantiDaMutazSogg>
      <Intestazione>
        <Intestato IndiceIntestato="1"><Nominativo>ROSSI Mario 04/07/1974; Comune PALERMO (PA)</Nominativo><CF>RSSMRA74L04G273K</CF>
          <DirittiReali CodiceDiritto="05" Descrizione="Enfiteusi" Quota=" per 2/9" FineDiritto="dal 01/09/2012 al 10/10/2026"/></Intestato>
      </Intestazione>
    </MutazioneSoggettiva>
    <MutazioneSoggettiva IndiceMutazione="2">
      <DatiDerivantiDaMutazSogg>(ALTRE) del 22/07/2013 Pratica n. PA0211854 in atti dal 22/07/2013</DatiDerivantiDaMutazSogg>
      <Intestazione>
        <Intestato IndiceIntestato="1"><Nominativo>ROSSI Gaetano 13/07/1938; Comune PALERMO (PA)</Nominativo><CF>RSSGTN38L13G273T</CF>
          <DirittiReali CodiceDiritto="21" Descrizione="Livellario per" Quota="per 333/1000" FineDiritto="dal 22/07/2013 al 01/09/2012"/></Intestato>
      </Intestazione>
    </MutazioneSoggettiva>
  </StoriaIntestazione>
</VisuraFabbricatiStorica></Visura>
"""


def test_xml_nominativo_variants():
    assert rp.parse_xml_nominativo("ROSSI Mario 04/07/1974; Comune PALERMO (PA)") == {
        "name": "ROSSI Mario", "birth_date": "04/07/1974", "birth_place": "PALERMO (PA)"}
    assert rp.parse_xml_nominativo("MONTE DI PIETA'; Comune PALERMO (PA)") == {
        "name": "MONTE DI PIETA'", "registered_office": "PALERMO (PA)"}
    assert rp.parse_xml_nominativo("ROSSI Maria /0/26/ il ; Comune Nata a MONTAZZOLI (CH)") == {
        "name": "ROSSI Maria", "birth_date_partial": "/0/26/", "sex": "F", "birth_place": "MONTAZZOLI (CH)"}
    assert rp.parse_xml_nominativo("ROSSI Anna ; Bianchi") == {"name": "ROSSI Anna", "note": "Bianchi"}
    assert rp.parse_xml_nominativo("AAA &amp;amp; C; Comune MILANO (MI)")["name"] == "AAA & C"


def test_right_period_flags():
    ongoing = rp.parse_right_period("dal 01/09/2012 al 10/10/2026", "10/10/2026")
    assert (ongoing["start"], ongoing["end"], ongoing["ongoing"]) == ("01/09/2012", "", True)
    inverted = rp.parse_right_period("dal 22/07/2013 al 01/09/2012", "10/10/2026")
    assert (inverted["start"], inverted["end"], inverted["inverted"]) == ("", "01/09/2012", True)
    origin = rp.parse_right_period("dall'impianto al 29/03/1983", "10/10/2026")
    assert origin["from_origin"] and origin["end"] == "29/03/1983" and not origin["ongoing"]
    assert rp.clean_quota(" per 2/9") == "2/9" and rp.clean_right_description("Livellario per") == "Livellario"


def test_fabbricati_storica_orders_acts_by_their_rights_not_by_index():
    view = build_visura_view(SUCCESSION)

    assert (view["family"], view["variant"], view["as_of"]) == ("fabbricati", "storica", "10/10/2026")
    unit = view["units"][0]
    assert unit["identifier"]["reference"] == "PALERMO (G273) · FG 9 · PT 1452 · Sub 7 · Prog. 1" or "Sub 7" in unit["identifier"]["reference"]
    assert (unit["classification"]["category"], unit["classification"]["category_description"]) == ("F/5", "LASTRICO SOLARE")
    assert unit["classification"]["consistency"] == "120 MQ" and unit["address"] == "VIA ROMA n. 1 Piano 2"

    newest, oldest = view["ownership"]  # newest first
    assert newest["current"] and newest["index"] == "1" and newest["act"]["date"] == "01/09/2012"
    assert newest["owners"][0]["period"]["ongoing"] and newest["owners"][0]["period"]["label"] == "dal 01/09/2012 · in corso"
    assert not oldest["current"] and oldest["end"] == "01/09/2012"
    predecessor = oldest["owners"][0]
    assert predecessor["period"]["inverted"] and predecessor["period"]["label"] == "fino al 01/09/2012"
    assert (predecessor["right"], predecessor["quota"], predecessor["birth_place"]) == ("Livellario", "333/1000", "PALERMO (PA)")
    assert predecessor["sex"] == "M" and predecessor["kind"] == "person"


def test_current_owners_and_company_are_read_from_the_current_heading():
    owners = build_visura_view(SUCCESSION)["units"][0]["current_owners"]

    assert [o["name"] for o in owners] == ["ROSSI Mario", "MONTE DI PIETA'"]
    assert owners[1]["kind"] == "legal_entity" and owners[1]["registered_office"] == "PALERMO (PA)"
    assert owners[0]["birth_date"] == "04/07/1974" and owners[0]["quota"] == "2/9"


def test_history_facets_of_one_period_are_grouped_with_their_derivation():
    history = build_visura_view(SUCCESSION)["history"]

    assert len(history) == 1
    period = history[0]
    assert period["period"]["label"] == "dal 22/07/2013 · in corso" and period["current"]
    assert [f["kind"] for f in period["facets"]] == ["identificativi", "indirizzo"]
    assert period["derived"]["description"] == "(ALTRE) del 22/07/2013"  # shared by the facets: shown once


def test_terreni_ownership_runs_oldest_to_newest_and_totals_are_summed():
    view = build_visura_view((FIXTURES / "terreni_attuale.xml").read_text(encoding="utf-8"))

    assert view["family"] == "terreni"
    unit = view["units"][0]
    assert unit["totals"]["rows"] == len(unit["classifications"])
    assert unit["totals"]["area_m2"] == str(sum(int(c["area_m2"] or 0) for c in unit["classifications"]))


def test_unknown_documents_have_no_view():
    assert build_visura_view("<Visura><VisuraSoggettoAttuale/></Visura>") is None
    assert build_visura_view("") is None and build_visura_view("not xml") is None


def test_story_macros_render(tmp_path):
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(Path(__file__).parents[1] / "sister" / "templates")), autoescape=True)
    view = build_visura_view(SUCCESSION)
    template = env.from_string(
        '{% from "parts/_visura_story.html" import unit_facts, property_history, ownership_history %}'
        "{{ unit_facts(view, view.units[0]) }}{{ property_history(view) }}{{ ownership_history(view) }}"
    )

    html = template.render(view=view)

    assert "fino al 01/09/2012" in html and "dal 01/09/2012 · in corso" in html
    assert "Livellario (21)" in html and "ROSSI Gaetano" in html and "333/1000" in html
    assert html.index("Attuale") < html.index("Storico")  # newest act first
