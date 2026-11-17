"""Build a 3D KMZ (extruded building footprints) around a point from OpenStreetMap.

    uv run sister build building-kmz --lat 44.4061530 --lng 12.1963357 --radius 80 \
        --name "Via El Alamein, Ravenna" --out outputs/via_el_alamein_ravenna_3d.kmz
    uv run sister build building-kmz ... --osm-json saved_overpass.json   # offline, reuse a saved answer

The logic lives in ``sister/building_kmz.py`` (also served by ``/web/3d/kmz``). Output © OpenStreetMap contributors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sister.building_kmz import build_kmz, fetch_buildings


def run(*, lat: float, lng: float, radius: int = 80, name: str = "Edificio", nearest: int = 3,
        osm_json: Path | None = None, out: Path) -> int:
    """Build a KMZ from a saved Overpass response or a live building lookup."""
    if osm_json:
        data = json.loads(osm_json.read_text())
    else:
        data = asyncio.run(fetch_buildings(lat, lng, radius, tries=5))
    payload, count = build_kmz(data, lat, lng, name, nearest, radius)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    print(f"{count} buildings -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius", type=int, default=80, help="metres around the point (default 80)")
    ap.add_argument("--name", default="Edificio")
    ap.add_argument("--nearest", type=int, default=3, help="how many closest buildings to highlight")
    ap.add_argument("--osm-json", type=Path, help="reuse a saved Overpass answer instead of querying")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    return run(
        lat=args.lat,
        lng=args.lng,
        radius=args.radius,
        name=args.name,
        nearest=args.nearest,
        osm_json=args.osm_json,
        out=args.out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
