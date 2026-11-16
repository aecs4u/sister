import pytest

from sister.anncsu import parse_street_address


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("VIA PAZIENZA n. 27-29 Piano T - 1 - 2", ("VIA PAZIENZA", ["27", "29"])),
        ("VIA GOETHE 1", ("VIA GOETHE", ["1"])),
        ("VIA EL ALAMEIN n. 2 Piano T-3", ("VIA EL ALAMEIN", ["2"])),
        ("VIA ROMA n. 10/A", ("VIA ROMA", ["10"])),
        ("CONTRADA X n. SNC", None),
        ("VIA ROMA", None),
        ("", None),
    ],
)
def test_parse_street_address(address, expected):
    assert parse_street_address(address) == expected
