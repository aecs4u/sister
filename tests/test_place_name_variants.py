from sister.reference import place_name_variants


def test_apostrophe_becomes_accent():
    assert place_name_variants("CANICATTI'") == ["CANICATTI'", "CANICATTÌ"]
    assert place_name_variants("FORLI'") == ["FORLI'", "FORLÌ"]


def test_ambiguous_e_gives_both_accents():
    assert place_name_variants("CAFFE'") == ["CAFFE'", "CAFFÈ", "CAFFÉ"]


def test_inner_apostrophes_are_kept():
    assert place_name_variants("SANT'ANGELO") == ["SANT'ANGELO"]
    assert place_name_variants("L'AQUILA") == ["L'AQUILA"]
    assert place_name_variants("PALERMO") == ["PALERMO"]
