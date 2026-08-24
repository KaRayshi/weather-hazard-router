"""
models.py
---------
Core data structures for the Localized Weather-Induced Road Hazard Risk
Assessor with Alternative Routing.

Covers spec sections:
  - 2.1 Data Ingestion (weather + road geometry)
  - FR-2.1 API Integration (weather parameter shape)
  - FR-2.2 Geofencing & Segment Mapping (node coordinates)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import radians, sin, cos, asin, sqrt
from typing import Optional


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    lat1_r, lon1_r, lat2_r, lon2_r = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


@dataclass(frozen=True)
class Node:
    """An OSM-style road network node (intersection / waypoint)."""
    id: str
    lat: float
    lon: float


@dataclass
class WeatherReading:
    """
    A single micro-weather station observation.

    FR-2.1: Precipitation rate (mm/h), Visibility (m), Wind speed (km/h),
    Temperature (°C). Station has a location so it can be geofenced to
    nearby road segments (FR-2.2).
    """
    station_id: str
    lat: float
    lon: float
    precipitation_mm_h: float
    visibility_m: float
    wind_speed_kmh: float
    temperature_c: float
    timestamp: float  # unix epoch seconds


@dataclass
class RoadSegment:
    """
    A single edge in the road network graph.

    Static attributes (from OSM / GIS road geometry data):
      - drainage_rating: 0 (excellent drainage / high elevation) to 1
        (poor drainage / low-lying, flood-prone). Feeds Sslope term.
      - historical_flood_index: 0 (no history) to 1 (frequent flooding
        history at this segment). Feeds Fhistory term.
      - is_elevated_or_bridge: used for crosswind vehicle-stability risk.

    Dynamic attributes (populated per routing cycle from WeatherReadings
    geofenced to this segment):
      - precipitation_mm_h, visibility_m, wind_speed_kmh
    """
    id: str
    from_node: str
    to_node: str
    distance_km: float
    drainage_rating: float = 0.3          # Sslope proxy, 0-1
    historical_flood_index: float = 0.1    # Fhistory, 0-1
    is_elevated_or_bridge: bool = False
    friction_index: float = 0.8            # 1.0 = dry/high-friction pavement

    # populated by geofencing step each refresh cycle
    precipitation_mm_h: float = 0.0
    visibility_m: float = 10000.0
    wind_speed_kmh: float = 0.0
    weather_data_stale: bool = True        # NFR-2.1 fallback flag

    # dynamic traffic (FR-1.1 overlay)
    traffic_speed_kmh: float = 50.0

    # last computed risk score, 0-100 (FR-3.2)
    risk_score: float = 0.0

    def midpoint(self, nodes: dict) -> Optional[tuple]:
        a, b = nodes.get(self.from_node), nodes.get(self.to_node)
        if not a or not b:
            return None
        return ((a.lat + b.lat) / 2, (a.lon + b.lon) / 2)
