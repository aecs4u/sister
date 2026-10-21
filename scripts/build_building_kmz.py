"""Build a 3D KMZ (extruded building footprints) around a point from OpenStreetMap.

    python scripts/build_building_kmz.py --lat 44.4061530 --lng 12.1963357 --radius 80 \
        --name "Via El Alamein, Ravenna" --out outputs/via_el_alamein_ravenna_3d.kmz
    python scripts/build_building_kmz.py ... --osm-json saved_overpass.json   # offline, reuse a saved answer

The logic lives in ``sister/building_kmz.py`` (also served by ``/web/3d/kmz``). Output © OpenStreetMap contributors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sister.building_kmz import build_kmz, fetch_buildings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius", type=int, default=80, help="metres around the point (default 80)")
    ap.add_argument("--name", default="Edificio")
    ap.add_argument("--nearest", type=int, default=3, help="how many closest buildings to highlight")
    ap.add_argument("--osm-json", type=Path, help="reuse a saved Overpass answer instead of querying")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if args.osm_json:
        data = json.loads(args.osm_json.read_text())
    else:
        data = asyncio.run(fetch_buildings(args.lat, args.lng, args.radius, tries=5))
    payload, count = build_kmz(data, args.lat, args.lng, args.name, args.nearest, args.radius)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(payload)
    print(f"{count} buildings -> {args.out}")


if __name__ == "__main__":
    main()
