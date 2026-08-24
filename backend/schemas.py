"""
schemas.py
----------
Pydantic models defining the API's request/response contracts. Kept
separate from the domain dataclasses in models.py so the wire format can
evolve independently of the internal engine representation.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------
# Network / map data (for a frontend to draw nodes + segments)
# ---------------------------------------------------------------------
class NodeOut(BaseModel):
    id: str
    lat: float
    lon: float


class SegmentOut(BaseModel):
    id: str
    from_node: str
    to_node: str
    distance_km: float
    risk_score: float
    risk_band: str
    is_elevated_or_bridge: bool
    weather_data_stale: bool
    traffic_speed_kmh: float


class NetworkOut(BaseModel):
    nodes: List[NodeOut]
    segments: List[SegmentOut]
    source: str = "demo"


# ---------------------------------------------------------------------
# FR-1.2 Risk heatmap
# ---------------------------------------------------------------------
class HeatmapSegment(BaseModel):
    segment_id: str
    from_node: str
    to_node: str
    risk_score: float = Field(..., ge=0, le=100)
    risk_band: str  # "green" | "yellow" | "red"
    hazard_triggered: bool


class HeatmapOut(BaseModel):
    generated_at: float
    segments: List[HeatmapSegment]


# ---------------------------------------------------------------------
# FR-4.1 Routing
# ---------------------------------------------------------------------
class RouteRequest(BaseModel):
    start_node: str
    goal_node: str
    ignore_oneway: bool = False
    alpha: float = Field(
        1.0, ge=0.0, le=10.0,
        description="Risk-avoidance preference factor. 0 = fastest/shortest "
                    "route ignoring risk; higher values avoid risky segments "
                    "more aggressively.",
    )


class RouteNodeCoord(BaseModel):
    id: str
    lat: float
    lon: float


class RouteOut(BaseModel):
    node_ids: List[str]
    node_coords: List[RouteNodeCoord] = Field(default_factory=list)
    segment_ids: List[str]
    total_distance_km: float
    total_travel_time_min: float
    aggregate_risk: float
    max_segment_risk: float
    worst_segment_id: Optional[str]
    max_rain_mm_h: float = 0.0
    min_vis_m: float = 10000.0
    max_wind_kmh: float = 0.0
    risk_band: str
    hazard_triggered: bool
    compute_seconds: float
    approximate: bool = False
    wrong_way_segment_ids: List[str] = Field(default_factory=list)


class RouteCompareOut(BaseModel):
    fastest: RouteOut
    balanced: RouteOut
    safest: RouteOut
    tradeoff_summary: str
    rankings: List[str]


# ---------------------------------------------------------------------
# FR-4.2 Mid-transit reroute check
# ---------------------------------------------------------------------
class RerouteCheckRequest(BaseModel):
    route_node_ids: List[str]
    route_segment_ids: List[str]
    current_node: str
    goal_node: str
    alpha: float = 1.0


class RerouteCheckOut(BaseModel):
    rerouted: bool
    new_route: Optional[RouteOut] = None
    message: str


# ---------------------------------------------------------------------
# Weather ingestion / refresh
# ---------------------------------------------------------------------
class WeatherRefreshOut(BaseModel):
    success: bool
    stations_received: int
    segments_updated: int
    segments_stale: int
    used_fallback: bool
    message: str


class OutageToggleRequest(BaseModel):
    simulate_outage: bool
    simulate_storm: bool = False


class StationOut(BaseModel):
    station_id: str
    lat: float
    lon: float
    precipitation_mm_h: float
    visibility_m: float
    wind_speed_kmh: float
    temperature_c: float


class StationsOut(BaseModel):
    stations: List[StationOut]
    simulate_outage: bool
    simulate_storm: bool = False


class AutoPollToggleRequest(BaseModel):
    enabled: bool


class AutoPollOut(BaseModel):
    auto_poll_enabled: bool


class HealthOut(BaseModel):
    status: str
    last_weather_refresh: Optional[float] = None
    next_weather_refresh: Optional[float] = None
    seconds_until_next_refresh: Optional[float] = None
    used_fallback_last_refresh: bool = False
    auto_poll_enabled: bool = True
    poll_interval_seconds: float
    auto_poll_runs: int = 0


# ---------------------------------------------------------------------
# Loading a real road network (e.g. parsed from OpenStreetMap/Overpass
# data in the frontend) to replace the in-memory demo network.
# ---------------------------------------------------------------------
class NetworkImportNode(BaseModel):
    id: str
    lat: float
    lon: float


class NetworkImportSegment(BaseModel):
    id: str
    from_node: str
    to_node: str
    distance_km: float = Field(..., gt=0)
    is_elevated_or_bridge: bool = False


class NetworkImportRequest(BaseModel):
    nodes: List[NetworkImportNode]
    segments: List[NetworkImportSegment]
    source: str = Field("osm", description="Label for where this network came from.")


class NetworkImportOut(BaseModel):
    success: bool
    nodes_loaded: int
    segments_loaded: int
    segments_skipped: int
    used_fallback: bool
    message: str


# ---------------------------------------------------------------------
# Geocoding (location search by name via Open-Meteo Geocoding API)
# ---------------------------------------------------------------------
class GeocodeResult(BaseModel):
    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""           # state / province / region
    country_code: str = ""
    elevation: Optional[float] = None


class GeocodeSearchOut(BaseModel):
    query: str
    results: List[GeocodeResult]