"""The planimetria map must place a SISTER export where it is (EPSG from _SistemaDiRappresentazione)."""

import io
import json
import zipfile

import pytest

from sister.web import _read_planimetria_geojson

pyproj = pytest.importorskip("pyproj")


def _zip(tmp_path, crs_text: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("_SistemaDiRappresentazione.txt", crs_text)
        z.writestr("A944_022600.geojson", json.dumps({"type": "FeatureCollection", "features": []}))
    path = tmp_path / "mappa_BO_FG226_GEOJSON.zip"
    path.write_bytes(buf.getvalue())
    return str(path)


@pytest.mark.parametrize(
    ("epsg", "xy", "lonlat"),
    [
        # Bologna, foglio 226 (Gauss-Boaga Ovest): the old UTM 32 fallback put it at 23.66 E, 43.56 N (Bulgaria).
        (3003, (1684450, 4928110), (11.319, 44.483)),
        # Same point in RDN2008 / UTM 33 (the X2/Y2 label coordinates of the same export).
        (6708, (203180.084, 4931251.180), (11.268, 44.474)),
    ],
)
def test_proj4_places_sheet_in_italy(tmp_path, epsg, xy, lonlat):
    result = _read_planimetria_geojson(_zip(tmp_path, f"Sistema di Rappresentazione | EPSG:{epsg}\n"))
    assert result["epsg"] == epsg
    lon, lat = pyproj.Transformer.from_crs(result["proj4"], "EPSG:4326", always_xy=True).transform(*xy)
    assert lon == pytest.approx(lonlat[0], abs=0.002)
    assert lat == pytest.approx(lonlat[1], abs=0.002)
