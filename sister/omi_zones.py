"""Helpers for loading OMI boundary polygons and matching cadastral parcels to zones."""

from __future__ import annotations

import functools
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("sister")


@functools.lru_cache(maxsize=1)
def _omi_zone_kmz_inventory() -> tuple[dict[str, Any], ...]:
    """Available local OMI boundary archives; this performs no network requests."""
    try:
        from aecs4u_stats.omi.boundaries import list_remote_kmz

        return tuple(list_remote_kmz())
    except Exception as exc:  # OMI boundaries are an optional map layer.
        logger.debug("OMI boundary archive inventory unavailable: %s", type(exc).__name__)
        return ()


@functools.lru_cache(maxsize=32)
def omi_boundary_features(province: str) -> tuple[dict[str, Any], ...]:
    """Load one province's cached boundary collection, falling back to its local KMZ mirror."""
    try:
        from aecs4u_stats.omi.boundaries import (
            kmz_to_geojson,
            province_from_kmz_name,
            zone_boundaries,
        )
        from .provinces import province_code, province_name

        province_sigla = province_code(province)
        province_label = province_name(province_sigla) if province_sigla else province.strip()
        collection = zone_boundaries(province_label or province)
        features = tuple(
            feature for feature in ((collection or {}).get("features") or [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        )
        if features:
            return features

        for archive in _omi_zone_kmz_inventory():
            archive_province = province_from_kmz_name(str(archive.get("name") or ""))
            archive_sigla = province_code(archive_province)
            same_province = (
                province_sigla and archive_sigla == province_sigla
            ) or archive_province.strip().casefold() == (province_label or province).strip().casefold()
            if not same_province:
                continue
            path = Path(str(archive.get("path") or ""))
            if not path.is_file():
                continue
            collection = kmz_to_geojson(path)
            return tuple(
                feature for feature in ((collection or {}).get("features") or [])
                if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
            )
    except Exception as exc:  # Missing optional geometry must never hide the cadastral map.
        logger.debug("OMI boundaries unavailable for %s: %s", province, type(exc).__name__)
    return ()


@functools.lru_cache(maxsize=32)
def omi_boundary_geometries(province: str) -> tuple[tuple[dict[str, Any], Any], ...]:
    """Shapely geometries for a province's OMI boundary features, cached per worker."""
    try:
        from shapely.geometry import shape

        features = []
        for feature in omi_boundary_features(province):
            try:
                features.append((feature, shape(feature["geometry"])))
            except Exception:
                continue
        return tuple(features)
    except ImportError:
        return ()


def omi_zone_code(properties: dict[str, Any]) -> str:
    """OMI boundary code from the local KML mirror's property variants."""
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in properties.items()
    }
    for key in ("zona", "codzona", "zonaomi", "zonecode", "name"):
        value = normalized.get(key)
        if value is None:
            continue
        match = re.search(r"(?:^|\b)([A-Z][0-9]{1,3})(?:\b|$)", str(value).strip().upper())
        if match:
            return match.group(1)
    return ""


def omi_zone_for_parcel(
    province: str,
    cadastral_code: str,
    parcel_feature: dict[str, Any] | None,
) -> str | None:
    """Choose the OMI zone covering the largest area of a cadastral parcel polygon."""
    if not parcel_feature or not isinstance(parcel_feature.get("geometry"), dict):
        return None
    try:
        from shapely.geometry import shape

        parcel_geometry = shape(parcel_feature["geometry"])
        if parcel_geometry.is_empty or parcel_geometry.area <= 0:
            return None
    except Exception:
        return None

    comune_code = str(cadastral_code or "").strip().upper()
    if not comune_code:
        return None
    parcel_min_x, parcel_min_y, parcel_max_x, parcel_max_y = parcel_geometry.bounds
    overlap_by_zone: dict[str, float] = {}
    for zone_feature, zone_geometry in omi_boundary_geometries(province):
        if zone_geometry.is_empty:
            continue
        properties = zone_feature.get("properties") or {}
        zone_comune = str(properties.get("cod_comune") or properties.get("codcom") or "").strip().upper()
        if zone_comune != comune_code:
            continue
        zone_min_x, zone_min_y, zone_max_x, zone_max_y = zone_geometry.bounds
        if (
            zone_max_x < parcel_min_x or parcel_max_x < zone_min_x
            or zone_max_y < parcel_min_y or parcel_max_y < zone_min_y
        ):
            continue
        zone = omi_zone_code(properties)
        if not zone:
            continue
        try:
            overlap = zone_geometry.intersection(parcel_geometry).area
        except Exception:
            continue
        if overlap > 0:
            overlap_by_zone[zone] = overlap_by_zone.get(zone, 0.0) + overlap

    if not overlap_by_zone:
        return None
    return min(overlap_by_zone, key=lambda zone: (-overlap_by_zone[zone], zone))
