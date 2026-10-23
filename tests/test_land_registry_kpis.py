"""Subject-visura KPIs: cadastral values per table row and in the totals, and the OMI estimate from the omi database."""

import pytest

from sister import land_registry_kpis, omi_quotes
from sister.land_registry_kpis import subject_land_registry_kpis


def building(sheet, parcel, category, rendita, area):
    return {
        "DatiIdentificativi": {"IdentificativoDefinitivo": {"Foglio": sheet, "ParticellaNum": parcel, "Subalterno": "1"}},
        "DatiClassamentoF": {"Categoria": category, "RenditaEuro": rendita},
        "SuperficieF": {"Totale": area},
    }


def land(sheet, parcel, classamento):
    return {
        "DatiIdentificativi": {"IdentificativoDefinitivo": {"Foglio": sheet, "ParticellaNum": parcel}},
        "DatiClassamentoT": classamento,
    }


def visura(buildings=(), lands=()):
    return {"VisuraSoggettoAttuale": {"GruppoUnitaImmobiliari": [{
        "CodiceComune": "G273", "Descrizione": "PALERMO (PA)", "Provincia": "PA",
        "ImmobileFabbricatiS": list(buildings), "ImmobileTerreniS": list(lands),
    }]}}


async def test_cadastral_values_per_row_and_in_the_totals():
    xml = visura(
        buildings=[
            building("11", "100", "A02", "1.000,00", "100"),  # 1000 x 1.05 x 160 = 168.000
            building("11", "101", "F01", "50,00", "10"),  # category F: no multiplier
            building("11", "102", "C06", None, "20"),  # no rendita
        ],
        lands=[
            land("12", "5", {"ClassamentoT": [
                {"Qualita": "ULIVETO", "SuperficieMQ": "1000", "RedditoDominicaleEuro": "10,00", "RedditoAgrarioEuro": "5,00"},
                {"Qualita": "PRATO", "SuperficieMQ": "500", "RedditoDominicaleEuro": "2,00"},
            ]}),
            land("12", "6", {}),  # no classamento: the theme still draws one empty row
            land("12", "7", {"ClassamentoT": {"Qualita": "BOSCO", "RedditoDominicaleEuro": "4,00"}}),  # single dict
        ],
    )

    kpis = await subject_land_registry_kpis(xml, None)

    group = kpis["table_estimates"][0]
    assert [row["cadastral_value"] for row in group["buildings"]] == ["€ 168.000", "", ""]
    assert "rendita € 1.000 × 1,05 × 160 (categoria A2)" in group["buildings"][0]["cadastral_note"]
    assert "non è un valore di mercato" in group["buildings"][0]["cadastral_note"]
    assert "senza moltiplicatore" in group["buildings"][1]["cadastral_note"]
    assert group["buildings"][2]["cadastral_note"] == "Rendita catastale assente."

    # one entry per rendered land row, in page order, including the empty placeholder row
    assert [row["cadastral_value"] for row in group["lands"]] == ["€ 1.688", "€ 338", "", "€ 675"]
    assert group["lands"][2]["cadastral_note"] == "Reddito dominicale assente."
    assert "75 per coltivatori diretti" in group["lands"][0]["cadastral_note"]

    # 10 x 1.25 x 135 + 2 x 1.25 x 135 + 4 x 1.25 x 135 = 2.700 ; buildings 168.000
    assert kpis["cadastral_value_buildings"] == "€ 168.000"
    assert kpis["cadastral_value_lands"] == "€ 2.700"
    assert kpis["cadastral_value"] == "€ 170.700"
    assert "1 fabbricato con rendita e 3 classamenti" in kpis["cadastral_value_note"]


async def test_nothing_to_value_is_reported_as_not_available():
    kpis = await subject_land_registry_kpis(visura(buildings=[building("1", "1", "A02", None, "50")]), None)
    # a building without rendita is "not available"; a visura without terreni says so instead
    assert kpis["cadastral_value"] == kpis["cadastral_value_buildings"] == "Non disponibile"
    assert kpis["cadastral_value_lands"] == kpis["grounds_area"] == kpis["dominical_income"] == "Nessun terreno in visura"
    assert kpis["grounds_area_note"] == "La visura non elenca terreni"
    assert kpis["land_rows"] == 0 and kpis["building_rows"] == 1


async def test_non_subject_documents_have_no_kpis():
    assert await subject_land_registry_kpis({"VisuraTerreniAttuale": {}}, None) is None


MAP_LOOKUP = {"parcel_markers": [
    {"municipality_code": "G273", "sheet": "11", "parcel": "100", "latitude": 38.1, "longitude": 13.3, "province": "PA"},
    {"municipality_code": "G273", "sheet": "11", "parcel": "101", "latitude": 38.1, "longitude": 13.3, "province": "PA"},
]}


def omi_quote(code, label, low, high, state="NORMALE", prevalent="P"):
    return {"cod_tipologia": code, "tipologia": label, "stato_conservazione": state, "stato_prevalente": prevalent,
            "prezzo_min": low, "prezzo_max": high, "periodo": "2025S1", "superficie": "L", "zona": "D1"}


@pytest.fixture
def omi_stubs(monkeypatch):
    from land_registry import stats_service

    calls = []

    async def zone_at_point(province, lat, lng, cadastral_code=None):
        return {"zone": "d1"}

    async def zone_quotes(comune, zone, **kwargs):
        calls.append((comune, zone))
        return [omi_quote(20, "Abitazioni civili", 1400.0, 1800.0), omi_quote(13, "Box", 1300.0, 1700.0)]

    monkeypatch.setattr(stats_service, "aget_omi_zone_at_point", zone_at_point)
    monkeypatch.setattr(omi_quotes, "omi_zone_quotes", zone_quotes)
    return calls


async def test_omi_estimate_uses_the_zone_quote_for_the_category_and_the_catastral_area(omi_stubs):
    xml = visura(buildings=[
        building("11", "100", "A02", "1.000,00", "100"),  # Abitazioni civili 1400-1800 x 100 m2
        building("11", "101", "C06", "100,00", "20"),  # Box 1300-1700 x 20 m2
    ])

    kpis = await subject_land_registry_kpis(xml, MAP_LOOKUP)

    rows = kpis["table_estimates"][0]["buildings"]
    assert rows[0]["range"] == "€ 140.000 – € 180.000"
    assert rows[1]["range"] == "€ 26.000 – € 34.000"
    assert "Abitazioni civili" in rows[0]["note"] and "2025S1" in rows[0]["note"] and "zona D1" in rows[0]["note"]
    assert "€ 1.400–1.800 al m² di superficie lorda" in rows[0]["note"]
    assert kpis["asset_value"] == "€ 166.000 – € 214.000"
    assert "2 di 2 fabbricati" in kpis["asset_value_note"]
    assert omi_stubs == [("G273", "D1")]  # one lookup per distinct comune/zone, zone code upper-cased


async def test_omi_leaves_unmatched_categories_unvalued_with_a_reason(omi_stubs):
    xml = visura(buildings=[building("11", "100", "A09", "100,00", "60"), building("11", "101", "C01", "100,00", "40")])

    kpis = await subject_land_registry_kpis(xml, MAP_LOOKUP)

    rows = kpis["table_estimates"][0]["buildings"]
    assert rows[0]["range"] == "" and "tipologia OMI corrispondente" in rows[0]["note"]
    assert rows[1]["range"] == "" and "non quotata" in rows[1]["note"]  # the zone has no shop quote
    assert kpis["asset_value"] == "Non disponibile"
    # the cadastral value does not depend on OMI at all
    assert rows[0]["cadastral_value"] != "" and rows[1]["cadastral_value"] != ""


async def test_unavailable_omi_database_is_reported_and_does_not_break_the_page(monkeypatch):
    from land_registry import stats_service

    async def zone_at_point(province, lat, lng, cadastral_code=None):
        return {"zone": "D1"}

    async def no_database(comune, zone, **kwargs):
        return None

    monkeypatch.setattr(stats_service, "aget_omi_zone_at_point", zone_at_point)
    monkeypatch.setattr(omi_quotes, "omi_zone_quotes", no_database)

    kpis = await subject_land_registry_kpis(visura(buildings=[building("11", "100", "A02", "1.000,00", "100")]), MAP_LOOKUP)

    row = kpis["table_estimates"][0]["buildings"][0]
    assert row["note"] == "Database OMI non disponibile." and row["range"] == ""
    assert row["cadastral_value"] == "€ 168.000"
    assert kpis["asset_value"] == "Non disponibile"


def test_old_label_matching_helpers_are_gone():
    assert not hasattr(land_registry_kpis, "_category_terms")


async def test_omi_reason_is_visible_and_summarised_when_no_range_can_be_computed(omi_stubs):
    xml = visura(buildings=[building("11", "100", "A09", "100,00", "60"), building("11", "101", "C01", "100,00", "40")])

    kpis = await subject_land_registry_kpis(xml, MAP_LOOKUP)

    rows = kpis["table_estimates"][0]["buildings"]
    assert rows[0]["missing"] == "Categoria senza tipologia OMI"
    assert rows[1]["missing"] == "Non quotata da OMI"
    assert "non stimati: " in kpis["asset_value_note"] and "Non quotata da OMI (1)" in kpis["asset_value_note"]


async def test_visura_without_buildings_is_not_reported_as_unavailable():
    kpis = await subject_land_registry_kpis(visura(buildings=[], lands=[land("12", "5", {})]), None)

    assert kpis["building_area"] == kpis["building_income"] == kpis["asset_value"] == "Nessun fabbricato in visura"
    assert kpis["grounds_area"] == "Non disponibile"  # a terreno is listed, it only lacks the data


async def test_comune_range_reads_the_omi_database_when_the_zone_is_unknown(monkeypatch):
    seen = []

    async def comune_quotes(code):
        seen.append(code)
        return [
            {**omi_quote(20, "Abitazioni civili", 2000.0, 3000.0), "zona": "B1"},
            {**omi_quote(20, "Abitazioni civili", 4000.0, 6000.0), "zona": "C2"},
            {**omi_quote(13, "Box", 1000.0, 1500.0), "zona": "C2"},
        ]

    monkeypatch.setattr(omi_quotes, "omi_comune_quotes", comune_quotes)
    xml = visura(buildings=[building("99", "1", "A02", "1.000,00", "100"), building("99", "2", "A09", "100,00", "50")])

    kpis = await subject_land_registry_kpis(xml, {"parcel_markers": []})

    rows = kpis["table_estimates"][0]["buildings"]
    assert rows[0]["range"] == "€ 200.000 – € 600.000"  # lowest minimum to highest maximum across the zones x 100 m2
    assert "Intervallo OMI comunale" in rows[0]["note"]
    assert rows[1]["missing"] == "Categoria senza tipologia OMI"  # A/4 has no OMI typology
    assert seen == ["G273"]
    assert "1 con intervallo comunale" in kpis["asset_value_note"] or "1 di 2 fabbricati" in kpis["asset_value_note"]


async def test_valued_rows_are_not_listed_as_unvalued(monkeypatch):
    async def comune_quotes(code):
        return [{**omi_quote(20, "Abitazioni civili", 2000.0, 3000.0), "zona": "B1"}]

    monkeypatch.setattr(omi_quotes, "omi_comune_quotes", comune_quotes)

    kpis = await subject_land_registry_kpis(visura(buildings=[building("99", "1", "A02", "1.000,00", "100")]), {"parcel_markers": []})

    row = kpis["table_estimates"][0]["buildings"][0]
    assert row["range"] and "missing" not in row
    assert "non stimati" not in kpis["asset_value_note"]


SUBJECT_CF = "CMMLNZ77A17F839A"


def owner(name, birth, cf, right, code, quota):
    return {
        "IndiceIntestato": "1", "Nominativo": f"{name}  {birth}; Comune NAPOLI (NA)", "CF": cf,
        "DirittiReali": {"Quota": f" per {quota}", "CodiceDiritto": code, "Descrizione": right,
                         "FineDiritto": "dal 21/04/1999 al 22/10/2026"},
    }


def visura_with_owners(owners, area="100"):
    xml = visura(buildings=[building("99", "1", "A02", "1.000,00", area)])
    group = xml["VisuraSoggettoAttuale"]["GruppoUnitaImmobiliari"][0]
    group["IntestazioneGruppo"] = {"MutazioneSoggettiva": {
        "IndiceMutazione": "1", "DatiDerivantiDaMutazSogg": "VOLTURA D'UFFICIO del 21/04/1999",
        "Intestazione": {"Intestato": owners},
    }}
    xml["VisuraSoggettoAttuale"]["TitoloVisura"] = {"SituazioneAl": "20261022"}
    xml["VisuraSoggettoAttuale"]["DatiRichiesta"] = {"SoggettoIndividuato": {"SoggettoPF": {
        "CodiceFiscale": SUBJECT_CF, "DataNascita": "17/01/1977"}}}
    return xml


@pytest.fixture
def one_zone_range(monkeypatch):
    async def comune_quotes(code):
        return [{**omi_quote(20, "Abitazioni civili", 2000.0, 3000.0), "zona": "B1"}]  # 100 m2 -> 200.000 - 300.000

    monkeypatch.setattr(omi_quotes, "omi_comune_quotes", comune_quotes)


async def test_bare_ownership_is_valued_on_the_usufructuaries_age_not_the_owners(one_zone_range):
    xml = visura_with_owners([
        owner("CIMMINO Giovanni", "16/10/1949", "CMMGNN49R16F839I", "Usufrutto", "08", "500/1000"),  # 77 -> 30%
        owner("CIMMINO Lorenzo", "17/01/1977", SUBJECT_CF, "Nuda proprieta'", "02", "1000/1000"),
        owner("CURCIO Gianna", "14/08/1952", "CRCGNN52M54D612H", "Usufrutto", "08", "500/1000"),  # 74 -> 35%
    ])

    assessment = (await subject_land_registry_kpis(xml, {"parcel_markers": []}))["owner_assessment"]

    bare = next(right for right in assessment["rights"] if right["kind"] == "bare")
    # usufruct = 1/2 x 30% + 1/2 x 35% = 32,5%; bare = 67,5% of 200.000 - 300.000. The owner's own age (49 -> 25%)
    # would have given 75%.
    assert bare["value"] == "€ 135.000 – € 202.500"
    assert bare["shares"] == "1/1"
    assert "CIMMINO Giovanni 77 anni → 30%" in assessment["coefficient_note"]
    assert "CURCIO Gianna 74 anni → 35%" in assessment["coefficient_note"]


async def test_usufruct_with_accrual_is_valued_on_the_youngest_holder(one_zone_range):
    xml = visura_with_owners([
        owner("CIMMINO Giovanni", "16/10/1949", "CMMGNN49R16F839I", "Usufrutto con diritto di accrescimento", "08A", "1/2"),
        owner("CIMMINO Lorenzo", "17/01/1977", SUBJECT_CF, "Nuda proprieta'", "02", "1/1"),
        owner("CURCIO Gianna", "14/08/1952", "CRCGNN52M54D612H", "Usufrutto con diritto di accrescimento", "08A", "1/2"),
    ])

    assessment = (await subject_land_registry_kpis(xml, {"parcel_markers": []}))["owner_assessment"]

    bare = next(right for right in assessment["rights"] if right["kind"] == "bare")
    assert bare["value"] == "€ 130.000 – € 195.000"  # the youngest (74 -> 35%) for the whole usufruct: 65% bare
    assert "più giovane" in assessment["coefficient_note"]


async def test_bare_ownership_without_a_known_usufructuary_age_says_why(one_zone_range):
    xml = visura_with_owners([
        {**owner("SOCIETA ALFA SRL", "", "01234567890", "Usufrutto", "08", "1/1"), "Nominativo": "SOCIETA ALFA SRL; Sede NAPOLI (NA)"},
        owner("CIMMINO Lorenzo", "17/01/1977", SUBJECT_CF, "Nuda proprieta'", "02", "1/1"),
    ])

    assessment = (await subject_land_registry_kpis(xml, {"parcel_markers": []}))["owner_assessment"]

    bare = next(right for right in assessment["rights"] if right["kind"] == "bare")
    assert bare["value"] == "Non stimabile: Età dell'usufruttuario non disponibile (1)"
    assert assessment["omi_total"] == "Non disponibile" or assessment["omi_total"].startswith("Non stimabile")


def test_a_unit_with_several_identifiers_shows_them_all_in_the_tables():
    from sister.display import merge_identifiers

    tree = {"Immobile": {"DatiIdentificativi": {"IdentificativoDefinitivo": [
        {"Foglio": "4", "ParticellaNum": "522", "Subalterno": "18", "Comune": "CAPRI "},
        {"Foglio": "4", "ParticellaNum": "2206", "Subalterno": "1", "Comune": "CAPRI "},
    ]}}, "Altro": [{"IdentificativoDefinitivo": {"Foglio": "1"}}]}

    merged = merge_identifiers(tree)

    ident = merged["Immobile"]["DatiIdentificativi"]["IdentificativoDefinitivo"]
    assert ident["Foglio"] == "4" and ident["ParticellaNum"] == "522, 2206" and ident["Subalterno"] == "18, 1"
    assert tree["Immobile"]["DatiIdentificativi"]["IdentificativoDefinitivo"][0]["ParticellaNum"] == "522"  # original untouched
    assert merged["Altro"][0]["IdentificativoDefinitivo"] == {"Foglio": "1"}


def test_property_right_of_the_landowner_in_a_surface_right_is_not_full_ownership():
    from sister.land_registry_kpis import _right_kind

    assert _right_kind("Proprieta' per l'area", "01T") == "other"  # the building is the superficiario's
    assert _right_kind("Proprieta' superficiaria", "01S") == "other"
    assert _right_kind("Proprieta'", "01") == "full"


async def test_category_without_an_omi_typology_is_the_reason_even_when_the_surface_is_missing():
    xml = visura(buildings=[building("1", "1", "F07", None, None), building("1", "2", "D02", "217.710,20", None)])

    kpis = await subject_land_registry_kpis(xml, None)

    rows = kpis["table_estimates"][0]["buildings"]
    assert all("senza una tipologia OMI" in row["note"] for row in rows)  # not "Superficie assente"
    assert "1 fabbricato con rendita" in kpis["cadastral_value_note"]


def test_a_company_reads_like_the_subject_the_theme_expects():
    from sister.display import subject_for_theme

    tree = {"V": {"DatiRichiesta": {"SoggettoIndividuato": {"SoggettoPNF": {
        "CodiceFiscale": "00110110822", "Denominazione": "S.I.A.T. S.P.A.", "Sede": "PALERMO (PA)"}}}}}

    subject = subject_for_theme(tree)["V"]["DatiRichiesta"]["SoggettoIndividuato"]["SoggettoPNF"]

    assert subject["Cognome"] == "S.I.A.T. S.P.A." and subject["ComuneNascita"] == "PALERMO (PA)"
    assert "Cognome" not in tree["V"]["DatiRichiesta"]["SoggettoIndividuato"]["SoggettoPNF"]  # original untouched


def test_amounts_in_value_notes_keep_both_decimals():
    from sister.land_registry_kpis import _note_amount

    assert [_note_amount(v) for v in (217710.2, 1000, 2540.97, 199.87)] == ["217.710,20", "1.000", "2.540,97", "199,87"]
