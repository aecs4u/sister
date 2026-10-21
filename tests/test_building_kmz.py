"""Road layer of the 3D building KMZ (sister/building_kmz.py)."""

import io
import zipfile
from xml.dom import minidom

from sister.building_kmz import build_kmz, clip_line, is_target_road, street_of

LAT, LNG = 44.4061530, 12.1963357


def _way(way_id, tags, points):
    return {"type": "way", "id": way_id, "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in points]}


def test_street_of_strips_postcode_town_and_house_number():
    assert street_of("Via El Alamein, 48124 Ravenna") == "via el alamein"
    assert street_of("Via El Alamein 12, Ravenna") == "via el alamein"
    assert street_of("Via El Alamein n. 12/A, Ravenna") == "via el alamein"


def test_target_road_matching_is_case_insensitive_and_tolerant():
    assert is_target_road("Via El Alamein", "via el alamein")
    assert not is_target_road("Via Cassino", "via el alamein")
    assert not is_target_road("", "via el alamein")


def test_clip_line_cuts_long_roads_to_the_square_around_the_point():
    long_road = [{"lat": LAT, "lon": LNG - 0.05}, {"lat": LAT, "lon": LNG + 0.05}]  # ~8 km east-west
    (run,) = clip_line(long_road, LAT, LNG, 100)
    assert abs(run[0][0] - LNG) < 0.0015 and abs(run[-1][0] - LNG) < 0.0015
    assert clip_line([{"lat": LAT + 1, "lon": LNG}, {"lat": LAT + 1, "lon": LNG + 0.001}], LAT, LNG, 100) == []


def test_kmz_has_buildings_and_roads_folders_with_the_target_street_highlighted():
    building = _way(1, {"building": "yes", "height": "12"}, [(44.4060, 12.1960), (44.4060, 12.1962), (44.4062, 12.1962), (44.4060, 12.1960)])
    target = _way(2, {"highway": "residential", "name": "Via El Alamein"}, [(LAT, 12.1955), (LAT, 12.1970)])
    other = _way(3, {"highway": "footway"}, [(44.4058, 12.1955), (44.4058, 12.1970)])
    far = _way(4, {"highway": "primary", "name": "Via Lontana"}, [(LAT + 0.1, LNG), (LAT + 0.1, LNG + 0.001)])
    payload, count = build_kmz({"elements": [building, target, other, far]}, LAT, LNG, "Via El Alamein, Ravenna")
    assert count == 1  # roads are not counted as buildings

    doc = minidom.parseString(zipfile.ZipFile(io.BytesIO(payload)).read("doc.kml"))
    folders = {f.getElementsByTagName("name")[0].firstChild.data: f for f in doc.getElementsByTagName("Folder")}
    assert len(folders["Edifici"].getElementsByTagName("Placemark")) == 1
    roads = folders["Strade"].getElementsByTagName("Placemark")
    assert len(roads) == 2  # the far road is clipped away
    styles = {r.getElementsByTagName("styleUrl")[0].firstChild.data for r in roads}
    assert styles == {"#road_target", "#road"}
    widths = {r.getElementsByTagName("name")[0].firstChild.data: r.getElementsByTagName("value")[0].firstChild.data for r in roads}
    assert widths["Via El Alamein"] == "6" and widths["Percorso pedonale"] == "2"


def test_the_building_holding_the_address_point_is_the_only_one_highlighted():
    holder = _way(10, {"building": "yes", "height": "18"}, [(44.4057, 12.1976), (44.4057, 12.1978), (44.4059, 12.1978), (44.4059, 12.1976), (44.4057, 12.1976)])
    closer_centroid = _way(11, {"building": "yes"}, [(44.40581, 12.19771), (44.40581, 12.19773), (44.40583, 12.19773), (44.40581, 12.19771)])
    payload, _ = build_kmz({"elements": [holder, closer_centroid]}, 44.4058, 12.1977, "Via El Alamein 2, Ravenna")
    doc = minidom.parseString(zipfile.ZipFile(io.BytesIO(payload)).read("doc.kml"))
    edifici = next(f for f in doc.getElementsByTagName("Folder") if f.getElementsByTagName("name")[0].firstChild.data == "Edifici")
    styles = {p.getElementsByTagName("name")[0].firstChild.data: p.getElementsByTagName("styleUrl")[0].firstChild.data for p in edifici.getElementsByTagName("Placemark")}
    assert styles["Edificio 10 (18 m)"] == "#near"
    assert styles["Edificio 11 (9 m)"] == "#estimated"


def test_a_verified_override_beats_the_osm_height_and_is_shown_in_the_description():
    from sister.building_kmz import parse_height

    tags = {"building": "yes", "building:levels": "5", "height": "18"}
    assert parse_height(tags) == (18.0, "osm")
    assert parse_height(tags, {"levels": 4}) == (12.0, "override")
    assert parse_height(tags, {"height": 13.5, "levels": 4}) == (13.5, "override")
    assert parse_height(tags, {"levels": "n/a"}) == (18.0, "osm")  # unusable override falls back to OSM


def test_shipped_overrides_correct_via_el_alamein_2_to_four_floors():
    from sister.building_kmz import load_overrides

    way = _way(158282549, {"building": "yes", "building:levels": "5", "height": "18"}, [(44.4057, 12.1976), (44.4057, 12.1978), (44.4059, 12.1978), (44.4057, 12.1976)])
    payload, _ = build_kmz({"elements": [way]}, 44.4058, 12.1977, "Via El Alamein 2, Ravenna")
    kml = zipfile.ZipFile(io.BytesIO(payload)).read("doc.kml").decode()
    assert load_overrides()["158282549"]["levels"] == 4
    assert "Edificio 158282549 (12 m)" in kml and "verificata" in kml and "<td>height</td>" not in kml


def _l_shape_ring():
    # North wing 26 x 11.5 m plus a 14 x 8 m south arm under its east half (Via El Alamein 2), axis-aligned.
    metres = [(0, 0), (0, 11.5), (26.2, 11.5), (26.4, -7.9), (12.6, -8.1), (12.5, 0.1), (0, 0)]
    return [{"lat": 44.4057 + y / 111_320, "lon": 12.1973 + x / 79_500} for x, y in metres]


def test_an_l_shaped_footprint_splits_into_two_rectangles_north_first():
    from sister.building_kmz import Frame, decompose_rectilinear

    rects = decompose_rectilinear(Frame(_l_shape_ring()).uv)
    assert len(rects) == 2
    (nu0, nu1, nv0, nv1), (su0, su1, sv0, sv1) = rects
    assert round(nu1 - nu0) == 26 and round(nv1 - nv0) == 11  # north wing
    assert round(su1 - su0) == 14 and round(sv1 - sv0) == 8  # south arm
    assert decompose_rectilinear([(0, 0), (10, 5), (20, 0), (10, -5), (0, 0)]) is None  # not axis-aligned


def test_gabled_roof_rises_by_half_the_span_times_tan_pitch():
    from sister.building_kmz import roof_facets

    slopes, gables, rise = roof_facets((0, 10, 0, 20), eave=12.0, ridge="u", pitch_deg=45)
    assert round(rise, 3) == 10.0  # half of the 20 m span
    assert len(slopes) == 2 and len(gables) == 2 and all(len(g) == 3 for g in gables)
    assert max(h for facet in slopes for _, _, h in facet) == 22.0


def test_a_roof_override_emits_walls_slopes_and_gables_per_wing():
    way = {"type": "way", "id": 99, "tags": {"building": "yes"}, "geometry": _l_shape_ring()}
    from sister.building_kmz import placemark

    kml = placemark(way, {99}, {"99": {"levels": 4, "roof": {"type": "gabled", "pitch_deg": 15, "ridges": ["u", "u"]}}})
    assert kml.count("<Placemark>") == 10  # 2 wings x (1 wall block + 2 slopes + 2 gables)
    assert kml.count("#roof") == 4 and "ala 1" in kml and "ala 2" in kml
    bad = placemark(way, {99}, {"99": {"levels": 4, "roof": {"type": "gabled", "ridges": ["u"]}}})  # wrong ridge count
    assert bad.count("<Placemark>") == 1  # falls back to the plain extrusion


def test_cross_gable_south_arm_runs_into_the_north_wing_up_to_its_ridge_at_the_same_height():
    from sister.building_kmz import plan_roofs

    north, south = (0.0, 26.3, 0.0, 11.5), (12.6, 26.3, -8.0, 0.0)
    (n_rect, n_ridge, n_rise, n_gables), (s_rect, s_ridge, s_rise, s_gables) = plan_roofs([north, south], ["u", "v"], 17)
    assert n_rect == north and n_gables == (True, True)
    assert round(n_rise, 2) == round(11.5 / 2 * 0.3057, 2)
    assert s_rect == (12.6, 26.3, -8.0, 5.75)  # extended north to the main ridge line
    assert s_rise == n_rise and s_gables == (True, False)  # same ridge height, gable only at the free (south) end
    # Parallel ridges are not merged: each wing keeps its own span with gables at both ends.
    assert plan_roofs([north, south], ["u", "u"], 17)[1][3] == (True, True)
