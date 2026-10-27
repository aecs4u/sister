"""The subject page lists every immobile of the saved search (elenco), the stored visura winning for the units it has."""

from sister.web import _elenco_groups, _subject_tree_with_elenco


def _unit(**kwargs):
    return {"property_type": "building", "subunit": "1", "category": "A/2", "income": "100,50", "consistency": "7,5 vani", **kwargs}


def _parcel(units, sheet="10", parcel="20", code="G273", municipality="PALERMO"):
    return {
        "province": "Palermo", "province_code": "PA", "municipality": municipality, "municipality_code": code,
        "section": "", "sheet": sheet, "parcel": parcel, "units": units,
    }


def _visura(sheet, parcel, sub, code="G273"):
    row = {"DatiIdentificativi": {"IdentificativoDefinitivo": {
        "Provincia": "PA", "CodiceComune": code, "Foglio": sheet, "ParticellaNum": parcel, "Subalterno": sub}}}
    return {"VisuraSoggettoAttuale": {"GruppoUnitaImmobiliari": [
        {"CodiceComune": code, "Descrizione": "PALERMO", "TipoCatasto": "F", "ImmobileFabbricatiS": [row]}]}}


def test_elenco_groups_one_group_per_comune_and_type_with_visura_shaped_rows():
    profile = {"map_parcels": [
        _parcel([_unit(), _unit(subunit="2")]),
        _parcel([{"property_type": "land", "cadastral_class": "2", "consistency": "700", "dominical_income": "0,11"}], parcel="21"),
    ]}
    groups = _elenco_groups(profile)
    assert {(g["TipoCatasto"], len(g["ImmobileFabbricatiS"]), len(g["ImmobileTerreniS"])) for g in groups} == {("F", 2, 0), ("T", 0, 1)}
    building = next(g for g in groups if g["TipoCatasto"] == "F")["ImmobileFabbricatiS"][0]
    assert building["DatiClassamentoF"]["Consistenza"] == {"Valore": "7,5", "Unita": "vani"}
    assert building["DatiClassamentoF"]["RenditaEuro"] == "100,50"
    land = next(g for g in groups if g["TipoCatasto"] == "T")["ImmobileTerreniS"][0]
    assert land["DatiClassamentoT"]["ClassamentoT"]["SuperficieMQ"] == "700"
    assert all(g["FonteElenco"] for g in groups)


def test_units_the_visura_lists_are_not_repeated():
    profile = {"map_parcels": [_parcel([_unit(subunit="1"), _unit(subunit="2")])]}
    merged = _subject_tree_with_elenco(_visura("10", "20", "1"), profile)
    groups = merged["VisuraSoggettoAttuale"]["GruppoUnitaImmobiliari"]
    assert len(groups) == 2 and not groups[0].get("FonteElenco")
    only_elenco = groups[1]["ImmobileFabbricatiS"]
    assert [r["DatiIdentificativi"]["IdentificativoDefinitivo"]["Subalterno"] for r in only_elenco] == ["2"]


def test_visura_covering_the_elenco_is_returned_unchanged():
    tree = _visura("10", "20", "0001")  # leading zeros do not make a different unit
    profile = {"map_parcels": [_parcel([_unit(subunit="1")])]}
    assert _subject_tree_with_elenco(tree, profile) is tree


def test_without_a_visura_the_elenco_is_the_whole_tree():
    profile = {"map_parcels": [_parcel([_unit()])]}
    merged = _subject_tree_with_elenco({"VisuraSoggetto": {"DatiRichiesta": {}}}, profile)
    assert len(merged["VisuraSoggetto"]["GruppoUnitaImmobiliari"]) == 1
