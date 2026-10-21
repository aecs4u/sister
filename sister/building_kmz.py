"""3D KMZ of the buildings around a point, from OpenStreetMap footprints (extruded volumes).

OSM footprints in Emilia-Romagna come from the Regione's topographic database (``source=Regione Emilia Romagna``) and
usually carry ``height`` or ``building:levels``. Heights missing from OSM are estimated (3 m per level, 9 m default) and
coloured differently. Google Earth imagery/3D data is deliberately not used: its terms forbid extracting geometry.

Used by ``/3d/kmz`` (web.py) and ``scripts/build_building_kmz.py``. Output © OpenStreetMap contributors (ODbL);
footprints/heights originally © Regione Emilia-Romagna.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
import zipfile
from html import escape
from pathlib import Path

OVERPASS = "https://overpass-api.de/api/interpreter"
LEVEL_HEIGHT_M = 3.0
DEFAULT_HEIGHT_M = 9.0
# KML colours are aabbggrr.
COLOR_NEAR = "ff3c8cf5"  # orange: closest to the requested point
COLOR_TAGGED = "ffb4a69a"  # grey-blue: height known from OSM
COLOR_ESTIMATED = "ffd9d9d9"  # light grey: height estimated
COLOR_ROOF = "ff4a56b8"  # brick red: roof slopes of buildings with a roof override
COLOR_ROAD = "ffa39587"  # slate: roads
COLOR_ROAD_TARGET = "ff4fd5ff"  # amber: the street that was searched for
ROAD_MARGIN_M = 40  # roads are fetched this much beyond the building radius, then clipped to the square around the point
# Carriageway width (m) per OSM highway class, for the viewer's ribbons (Google Earth uses the pixel width in the style).
ROAD_WIDTH_M = {
    "motorway": 12, "trunk": 11, "primary": 10, "secondary": 9, "tertiary": 8, "unclassified": 6, "residential": 6,
    "living_street": 5, "service": 4, "pedestrian": 4, "track": 3, "footway": 2, "path": 2, "cycleway": 2, "steps": 2,
}
_SKIPPED_HIGHWAYS = "proposed|construction|bus_guideway|raceway|elevator|platform"

OVERRIDES_FILE = Path(__file__).with_name("building_overrides.json")

_CACHE: dict[tuple[float, float, int], tuple[float, dict]] = {}
_CACHE_TTL_S = 3600


class OverpassUnavailable(RuntimeError):
    """The public Overpass service is at capacity or unreachable."""


async def fetch_buildings(lat: float, lng: float, radius: int = 80, tries: int = 3) -> dict:
    """Building and road ways (with geometry) around a point; cached for an hour, retried with backoff (Overpass is often busy)."""
    key = (round(lat, 5), round(lng, 5), radius)
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    import httpx

    query = (
        f'[out:json][timeout:25];(way["building"](around:{radius},{lat},{lng});'
        f'way["highway"]["highway"!~"^({_SKIPPED_HIGHWAYS})$"](around:{radius + ROAD_MARGIN_M},{lat},{lng}););'
        "out tags geom;"
    )
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=40.0, headers={"User-Agent": "sister/1 (info@aecs4u.com)"}) as client:
        for attempt in range(1, tries + 1):
            try:
                response = await client.post(OVERPASS, data={"data": query})
                response.raise_for_status()
                data = response.json()
                _CACHE[key] = (time.monotonic(), data)
                return data
            except Exception as exc:
                last = exc
                await asyncio.sleep(attempt * 5)
    raise OverpassUnavailable(type(last).__name__)


def load_overrides() -> dict[str, dict]:
    """Facts verified by a person (floors, height) keyed by OSM way id; they win over the OSM tags."""
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def parse_height(tags: dict, override: dict | None = None) -> tuple[float, str]:
    """Return (metres, 'override'|'osm'|'levels'|'estimated'); a verified override beats the OSM tags."""
    if override:
        try:
            if float(override.get("height", 0)) > 0:
                return float(override["height"]), "override"
            if float(override.get("levels", 0)) > 0:
                return float(override["levels"]) * LEVEL_HEIGHT_M, "override"
        except (TypeError, ValueError):
            pass
    raw = str(tags.get("height", "")).lower().replace("m", "").replace(",", ".").strip()
    try:
        if raw and float(raw) > 0:
            return float(raw), "osm"
    except ValueError:
        pass
    try:
        levels = float(str(tags.get("building:levels", "")).replace(",", "."))
        if levels > 0:
            return levels * LEVEL_HEIGHT_M, "levels"
    except ValueError:
        pass
    return DEFAULT_HEIGHT_M, "estimated"


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    return math.hypot((lat2 - lat1) * 111_320, (lng2 - lng1) * 111_320 * math.cos(math.radians(lat1)))


def _cluster(values: list[float], tol: float) -> list[float]:
    ordered = sorted(values)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] <= tol:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


class Frame:
    """Metric frame aligned with a footprint's longest edge: u along it, v perpendicular (u points east-ish)."""

    def __init__(self, ring: list[dict]) -> None:
        self.lat0, self.lng0 = ring[0]["lat"], ring[0]["lon"]
        self.kx = 111_320 * math.cos(math.radians(self.lat0))
        xy = [((p["lon"] - self.lng0) * self.kx, (p["lat"] - self.lat0) * 111_320) for p in ring]
        a, b = max(zip(xy, xy[1:]), key=lambda e: math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1]))
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        while angle > math.pi / 2:
            angle -= math.pi
        while angle <= -math.pi / 2:
            angle += math.pi
        self.cos, self.sin = math.cos(angle), math.sin(angle)
        self.uv = [self.to_uv(x, y) for x, y in xy]

    def to_uv(self, x: float, y: float) -> tuple[float, float]:
        return x * self.cos + y * self.sin, -x * self.sin + y * self.cos

    def to_lonlat(self, u: float, v: float) -> tuple[float, float]:
        x, y = u * self.cos - v * self.sin, u * self.sin + v * self.cos
        return self.lng0 + x / self.kx, self.lat0 + y / 111_320


def decompose_rectilinear(uv: list[tuple[float, float]], tol: float = 0.8) -> list[tuple[float, float, float, float]] | None:
    """Split an axis-aligned footprint (L, T, U, ... shapes) into rectangles (u0, u1, v0, v1), north first.

    Returns None when an edge is not axis-aligned or the outline is too complex for a simple gabled roof.
    """
    for a, b in zip(uv, uv[1:]):
        if min(abs(b[0] - a[0]), abs(b[1] - a[1])) > tol:
            return None
    us = _cluster([p[0] for p in uv[:-1]], tol)
    vs = _cluster([p[1] for p in uv[:-1]], tol)
    if len(us) > 6 or len(vs) > 6:
        return None

    def inside(u: float, v: float) -> bool:
        hit = False
        for a, b in zip(uv, uv[1:]):
            if (a[1] > v) != (b[1] > v) and u < (b[0] - a[0]) * (v - a[1]) / (b[1] - a[1]) + a[0]:
                hit = not hit
        return hit

    rows: list[list] = []  # [v0, v1, intervals]
    for i in range(len(vs) - 1):
        vm = (vs[i] + vs[i + 1]) / 2
        cells = [j for j in range(len(us) - 1) if inside((us[j] + us[j + 1]) / 2, vm)]
        intervals, start = [], None
        for j in range(len(us) - 1):
            if j in cells and start is None:
                start = j
            if (j not in cells or j == len(us) - 2) and start is not None:
                end = j if j in cells else j - 1
                intervals.append((us[start], us[end + 1]))
                start = None
        if not intervals:
            continue
        if rows and rows[-1][2] == intervals and abs(rows[-1][1] - vs[i]) < 1e-6:
            rows[-1][1] = vs[i + 1]
        else:
            rows.append([vs[i], vs[i + 1], intervals])
    rects = [(u0, u1, v0, v1) for v0, v1, intervals in rows for u0, u1 in intervals]
    return sorted(rects, key=lambda r: (-r[3], r[0])) or None


def roof_facets(
    rect: tuple[float, float, float, float],
    eave: float,
    ridge: str,
    pitch_deg: float | None = None,
    rise: float | None = None,
    gables: tuple[bool, bool] = (True, True),
):
    """Slopes and gable triangles of one gabled wing as lists of (u, v, height); ridge is 'u' or 'v' (its direction).

    The rise comes from *pitch_deg* unless given; *gables* says whether the start/end of the ridge gets a gable triangle
    (a wing that runs into another roof has none there).
    """
    u0, u1, v0, v1 = rect
    half = ((u1 - u0) if ridge == "v" else (v1 - v0)) / 2
    if rise is None:
        rise = half * math.tan(math.radians(pitch_deg or 0))
    top = eave + rise
    if ridge == "v":  # ridge parallel to v, slopes fall towards +-u
        um = (u0 + u1) / 2
        slopes = [
            [(u0, v0, eave), (u0, v1, eave), (um, v1, top), (um, v0, top)],
            [(u1, v0, eave), (u1, v1, eave), (um, v1, top), (um, v0, top)],
        ]
        ends = [[(u0, v, eave), (u1, v, eave), (um, v, top)] for v in (v0, v1)]
    else:  # ridge parallel to u, slopes fall towards +-v
        vm = (v0 + v1) / 2
        slopes = [
            [(u0, v0, eave), (u1, v0, eave), (u1, vm, top), (u0, vm, top)],
            [(u0, v1, eave), (u1, v1, eave), (u1, vm, top), (u0, vm, top)],
        ]
        ends = [[(u, v0, eave), (u, v1, eave), (u, vm, top)] for u in (u0, u1)]
    return slopes, [g for g, wanted in zip(ends, gables) if wanted], rise


def plan_roofs(rects: list[tuple[float, float, float, float]], ridges: list[str], pitch_deg: float, tol: float = 0.8):
    """Roof description per wing: (roof_rect, ridge, rise, gables).

    The largest wing is the main one (its own pitch). A wing whose ridge is perpendicular to it and that abuts it runs
    into the main roof up to the main ridge, with the same ridge height (a cross gable: valleys form where they meet)
    and a gable only at its free end. Other wings keep the main pitch and gables at both ends.
    """
    main = max(range(len(rects)), key=lambda i: (rects[i][1] - rects[i][0]) * (rects[i][3] - rects[i][2]))
    mu0, mu1, mv0, mv1 = rects[main]
    half = ((mu1 - mu0) if ridges[main] == "v" else (mv1 - mv0)) / 2
    main_rise = half * math.tan(math.radians(pitch_deg))
    plans = []
    for i, ((u0, u1, v0, v1), ridge) in enumerate(zip(rects, ridges)):
        if i == main:
            plans.append(((u0, u1, v0, v1), ridge, main_rise, (True, True)))
            continue
        own_half = ((u1 - u0) if ridge == "v" else (v1 - v0)) / 2
        if ridge != ridges[main]:
            if ridge == "v" and ridges[main] == "u":  # abuts the main wing along v
                line = (mv0 + mv1) / 2
                if v1 <= mv0 + tol:
                    plans.append(((u0, u1, v0, line), ridge, main_rise, (True, False)))
                    continue
                if v0 >= mv1 - tol:
                    plans.append(((u0, u1, line, v1), ridge, main_rise, (False, True)))
                    continue
            if ridge == "u" and ridges[main] == "v":  # abuts the main wing along u
                line = (mu0 + mu1) / 2
                if u1 <= mu0 + tol:
                    plans.append(((u0, line, v0, v1), ridge, main_rise, (True, False)))
                    continue
                if u0 >= mu1 - tol:
                    plans.append(((line, u1, v0, v1), ridge, main_rise, (False, True)))
                    continue
        plans.append(((u0, u1, v0, v1), ridge, own_half * math.tan(math.radians(pitch_deg)), (True, True)))
    return plans


def _polygon_kml(frame: Frame, points: list[tuple[float, float, float]], extrude: bool) -> str:
    ring = [*points, points[0]]
    coords = " ".join(f"{lon:.7f},{lat:.7f},{h:.2f}" for lon, lat, h in ((*frame.to_lonlat(u, v), h) for u, v, h in ring))
    return (
        f"<Polygon><extrude>{int(extrude)}</extrude><altitudeMode>relativeToGround</altitudeMode>"
        f"<outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )


def roofed_placemarks(element: dict, style: str, override: dict, eave: float, label: str, description: str) -> str | None:
    """Walls (one extruded block per wing) plus gabled roofs, or None when the footprint cannot be decomposed."""
    roof = override.get("roof") or {}
    if roof.get("type") != "gabled":
        return None
    frame = Frame(element["geometry"])
    rects = decompose_rectilinear(frame.uv)
    ridges = roof.get("ridges") or []
    if not rects or len(ridges) != len(rects) or any(r not in ("u", "v") for r in ridges):
        return None
    pitch = float(roof.get("pitch_deg", 20))
    floors = int(override.get("levels") or max(1, round(eave / LEVEL_HEIGHT_M)))
    out = []
    for index, (rect, plan) in enumerate(zip(rects, plan_roofs(rects, ridges, pitch)), start=1):
        u0, u1, v0, v1 = rect
        roof_rect, ridge, rise, gable_flags = plan
        slopes, gables, rise = roof_facets(roof_rect, eave, ridge, rise=rise, gables=gable_flags)
        wing = f"ala {index}"
        block = [(u0, v0, eave), (u1, v0, eave), (u1, v1, eave), (u0, v1, eave)]
        parts = [
            (f"{label} — {wing}: muri ({eave:g} m)", style, _polygon_kml(frame, block, True), "wall"),
            *((f"{label} — {wing}: falda", "roof", _polygon_kml(frame, f, False), "roof") for f in slopes),
            *((f"{label} — {wing}: timpano", style, _polygon_kml(frame, f, False), "gable") for f in gables),
        ]
        facade = json.dumps(override.get("facade") or {}, ensure_ascii=False, separators=(",", ":"))
        direction = "est-ovest (lato lungo)" if ridge == "u" else "nord-sud"
        for name, style_id, geometry, role in parts:
            out.append(
                f"""
  <Placemark>
    <name>{escape(name)}</name>
    <styleUrl>#{style_id}</styleUrl>
    <description><![CDATA[{description}<p>{wing}: {u1 - u0:.1f} × {v1 - v0:.1f} m, colmo {direction}, gronda {eave:g} m, colmo +{rise:.1f} m.</p>]]></description>
    <ExtendedData><Data name="role"><value>{role}</value></Data><Data name="floors"><value>{floors}</value></Data><Data name="facade"><value>{escape(facade)}</value></Data></ExtendedData>
    {geometry}
  </Placemark>"""
            )
    return "".join(out)


def placemark(element: dict, near: set[int], overrides: dict[str, dict] | None = None) -> str:
    tags = element.get("tags", {})
    geom = element["geometry"]
    override = (overrides or {}).get(str(element["id"]))
    height, basis = parse_height(tags, override)
    if override:
        tags = {**tags, **{k: v for k, v in (("building:levels", override.get("levels")), ("height", override.get("height"))) if v}}
        if "levels" in override and "height" not in override:
            tags.pop("height", None)
    ring = " ".join(f"{p['lon']:.7f},{p['lat']:.7f},{height:.1f}" for p in geom)
    if geom[0] != geom[-1]:
        ring += f" {geom[0]['lon']:.7f},{geom[0]['lat']:.7f},{height:.1f}"
    style = "near" if element["id"] in near else ("estimated" if basis == "estimated" else "tagged")
    shown = {k: v for k, v in tags.items() if k in ("building", "building:levels", "height", "roof:shape", "source")}
    rows = "".join(f"<tr><td>{escape(k)}</td><td>{escape(str(v))}</td></tr>" for k, v in shown.items())
    basis_text = {"override": "verificata", "osm": "da OSM", "levels": f"piani × {LEVEL_HEIGHT_M:g} m", "estimated": "stimata"}[basis]
    note = f"<br><i>{escape(str(override['note']))}</i>" if override and override.get("note") else ""
    label = tags.get("addr:housenumber") and f"{tags.get('addr:street', '')} {tags['addr:housenumber']}".strip()
    if override and override.get("roof"):
        description = (
            f"<table>{rows}</table><p>Altezza gronda: {height:g} m ({basis_text}).{note}<br>"
            f'<a href="https://www.openstreetmap.org/way/{element["id"]}">OSM way {element["id"]}</a></p>'
        )
        roofed = roofed_placemarks(element, style, override, height, label or f"Edificio {element['id']}", description)
        if roofed:
            return roofed
    return f"""
  <Placemark>
    <name>{escape(label or f"Edificio {element['id']}")} ({height:g} m)</name>
    <styleUrl>#{style}</styleUrl>
    <description><![CDATA[<table>{rows}</table>
      <p>Altezza: {height:g} m ({basis_text}). Tetto non modellato (volume estruso).{note}<br>
      <a href="https://www.openstreetmap.org/way/{element['id']}">OSM way {element['id']}</a></p>]]></description>
    <Polygon><extrude>1</extrude><altitudeMode>relativeToGround</altitudeMode>
      <outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs>
    </Polygon>
  </Placemark>"""


def contains_point(ring: list[dict], lat: float, lng: float) -> bool:
    """Ray-casting point-in-polygon test on an OSM geometry ring."""
    inside = False
    for a, b in zip(ring, ring[1:]):
        if (a["lat"] > lat) != (b["lat"] > lat):
            if lng < (b["lon"] - a["lon"]) * (lat - a["lat"]) / (b["lat"] - a["lat"]) + a["lon"]:
                inside = not inside
    return inside


def street_of(query: str) -> str:
    """Street part of an address query ("Via El Alamein 12, 48124 Ravenna" -> "via el alamein"), lower-cased."""
    street = query.split(",")[0].casefold()
    street = re.sub(r"\b(n\.?|civico)\s*\d+[\w/]*\s*$", "", street)
    street = re.sub(r"\s+\d+\s*/?\s*\w?\s*$", "", street)
    return re.sub(r"\s+", " ", street).strip()


def is_target_road(road_name: str, street: str) -> bool:
    road = re.sub(r"\s+", " ", road_name.casefold()).strip()
    return bool(road and street and (road == street or street in road or road in street))


def clip_line(points: list[dict], lat: float, lng: float, max_m: float) -> list[list[tuple[float, float]]]:
    """Cut a polyline to the square of half-side *max_m* around the point (Liang-Barsky); returns (lon, lat) runs."""
    kx = 111_320 * math.cos(math.radians(lat))

    def local(p: dict) -> tuple[float, float]:
        return (p["lon"] - lng) * kx, (p["lat"] - lat) * 111_320

    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        (x0, y0), (x1, y1) = local(a), local(b)
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for pk, qk in ((-dx, x0 + max_m), (dx, max_m - x0), (-dy, y0 + max_m), (dy, max_m - y0)):
            if pk == 0:
                if qk < 0:
                    t0, t1 = 1.0, 0.0
                    break
                continue
            t = qk / pk
            if pk < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
        if t0 >= t1:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        start = (lng + (x0 + t0 * dx) / kx, lat + (y0 + t0 * dy) / 111_320)
        end = (lng + (x0 + t1 * dx) / kx, lat + (y0 + t1 * dy) / 111_320)
        if not current or current[-1] != start:
            if len(current) >= 2:
                runs.append(current)
            current = [start]
        current.append(end)
    if len(current) >= 2:
        runs.append(current)
    return runs


def road_placemarks(data: dict, lat: float, lng: float, radius: int, street: str) -> tuple[str, int, int]:
    """KML placemarks for the roads (clipped around the point); returns (kml, roads, target_segments)."""
    out, roads, target = [], 0, 0
    ways = [e for e in data.get("elements", []) if e.get("type") == "way" and "highway" in e.get("tags", {})]
    for element in sorted(ways, key=lambda e: e["id"]):
        tags = element["tags"]
        kind = tags["highway"]
        width = ROAD_WIDTH_M.get(kind, 4)
        name = tags.get("name", "")
        is_target = is_target_road(name, street)
        for run in clip_line(element.get("geometry", []), lat, lng, radius + ROAD_MARGIN_M):
            coords = " ".join(f"{x:.7f},{y:.7f},0" for x, y in run)
            roads += 1
            target += is_target
            label = name or {"footway": "Percorso pedonale", "cycleway": "Pista ciclabile", "service": "Strada di servizio"}.get(kind, f"Strada ({kind})")
            out.append(
                f"""
    <Placemark>
      <name>{escape(label)}</name>
      <styleUrl>#{"road_target" if is_target else "road"}</styleUrl>
      <description><![CDATA[Strada OSM: {escape(kind)} (larghezza ~{width} m)<br>
        <a href="https://www.openstreetmap.org/way/{element['id']}">OSM way {element['id']}</a>]]></description>
      <ExtendedData><Data name="width_m"><value>{width}</value></Data><Data name="kind"><value>{escape(kind)}</value></Data></ExtendedData>
      <LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode><coordinates>{coords}</coordinates></LineString>
    </Placemark>"""
            )
    return "".join(out), roads, target


def build_kml(data: dict, lat: float, lng: float, name: str, nearest: int, radius: int = 80) -> tuple[str, int]:
    elements = [
        e
        for e in data.get("elements", [])
        if e.get("type") == "way" and "building" in e.get("tags", {}) and len(e.get("geometry", [])) >= 4
    ]

    def centroid_distance(e: dict) -> float:
        pts = e["geometry"]
        return distance_m(lat, lng, sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts))

    elements.sort(key=centroid_distance)
    # The building that holds the address point is the target; only without one are the closest buildings highlighted.
    holding = {e["id"] for e in elements if contains_point(e["geometry"], lat, lng)}
    near = holding or {e["id"] for e in elements[:nearest]}
    highlight_note = (
        "Arancione = l’edificio che contiene il punto dell’indirizzo" if holding
        else "Arancione = edifici più vicini al punto cercato"
    )
    styles = "".join(
        f'<Style id="{sid}"><PolyStyle><color>{c}</color><fill>1</fill><outline>1</outline></PolyStyle>'
        f"<LineStyle><color>ff404040</color><width>1</width></LineStyle></Style>"
        for sid, c in (("near", COLOR_NEAR), ("tagged", COLOR_TAGGED), ("estimated", COLOR_ESTIMATED), ("roof", COLOR_ROOF))
    ) + "".join(
        f'<Style id="{sid}"><LineStyle><color>{c}</color><width>{w}</width></LineStyle></Style>'
        for sid, c, w in (("road", COLOR_ROAD, 3), ("road_target", COLOR_ROAD_TARGET, 6))
    )
    overrides = load_overrides()
    marks = "".join(placemark(e, near, overrides) for e in elements)
    street = street_of(name)
    roads_kml, roads, target = road_placemarks(data, lat, lng, radius, street)
    target_note = f" In giallo la via cercata ({escape(street)})." if target else ""
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{escape(name)} — edifici 3D</name>
  <description>Volumi estrusi dai perimetri OpenStreetMap (origine: Regione Emilia-Romagna). {highlight_note}; grigio scuro = altezza da OSM; grigio chiaro = altezza stimata.{target_note}
    © OpenStreetMap contributors (ODbL).</description>
  <LookAt><longitude>{lng}</longitude><latitude>{lat}</latitude><range>180</range><tilt>60</tilt><heading>0</heading></LookAt>
  {styles}
  <Placemark><name>{escape(name)}</name><Point><coordinates>{lng},{lat},0</coordinates></Point></Placemark>
  <Folder><name>Edifici</name>{marks}
  </Folder>
  <Folder><name>Strade</name>{roads_kml}
  </Folder>
</Document>
</kml>
"""
    return kml, len(elements)


def build_kmz(
    data: dict, lat: float, lng: float, name: str, nearest: int = 3, radius: int = 80
) -> tuple[bytes, int]:
    kml, count = build_kml(data, lat, lng, name, nearest, radius)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as kmz:
        kmz.writestr("doc.kml", kml)
    return buffer.getvalue(), count
