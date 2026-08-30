"""Geospatial helpers.

Kept dependency-free on purpose: the AI pipeline and ingestion workers need
these and must not pull in a GIS stack. Anything heavier (routing, polygon
ops) belongs in PostGIS, not here.
"""

from __future__ import annotations

import math

EARTH_R = 6371008.8      # mean Earth radius, metres (WGS84)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    """Point at `distance_m` along `bearing` from (lat, lon)."""
    br = math.radians(bearing)
    d = distance_m / EARTH_R
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def compass_point(bearing: float) -> str:
    """Human-readable direction. Operators read 'NE', not '47.3 degrees'."""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((bearing % 360) / 22.5 + 0.5) % 16]


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) bounding box.

    Used as a cheap index-friendly prefilter before an exact distance test.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def fov_polygon(lat: float, lon: float, heading_deg: float,
                fov_deg: float = 90.0, range_m: float = 60.0,
                steps: int = 16) -> list[tuple[float, float]]:
    """Ground field-of-view wedge as a closed [(lon, lat), ...] ring.

    GeoJSON order (lon, lat), because that is what MapLibre expects and
    silently mis-plots if you get backwards.
    """
    pts = [(lon, lat)]
    start = heading_deg - fov_deg / 2.0
    for i in range(steps + 1):
        b = start + (fov_deg * i / steps)
        plat, plon = destination(lat, lon, b, range_m)
        pts.append((plon, plat))
    pts.append((lon, lat))
    return pts
