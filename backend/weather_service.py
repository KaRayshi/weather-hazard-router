"""
weather_service.py
-------------------
Live weather ingestion via the Open-Meteo Forecast API (https://open-meteo.com).
Free, no API key required for non-commercial use.

Implements:
  FR-2.1  Poll interval contract (>= every 5 minutes) — the client itself
          just fetches on request; state.py is responsible for calling it
          on a 300s cadence in production.
  NFR-2.1 Fallback signaling when the live API is unavailable (timeout,
          non-200 response, network error, or `simulate_outage=True`)

Also keeps the original mock client (`WeatherAPIClient`) used by demo.py
and the test suites, so nothing that already worked needs internet access
to test.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from models import WeatherReading

POLL_INTERVAL_SECONDS = 300  # FR-2.1: minimum 5-minute polling interval
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
CURRENT_VARS = "precipitation,visibility,wind_speed_10m,temperature_2m"
REQUEST_TIMEOUT_SECONDS = 8


@dataclass
class QueryPoint:
    """A coordinate we want live weather for (typically a road network node)."""
    station_id: str
    lat: float
    lon: float


@dataclass
class OpenMeteoClient:
    """
    Real weather client. Give it a list of QueryPoint (one per road-network
    node is the common case — see state.py), and it fetches live conditions
    for ALL of them in a single batched HTTP request using Open-Meteo's
    comma-separated multi-coordinate support, then maps the results back
    into WeatherReading objects that risk_engine.geofence_weather_to_segments
    already knows how to consume.

    `simulate_outage=True` forces fetch_readings() to return None without
    making a network call, for exercising the NFR-2.1 fallback path on
    demand (same demo hook the mock client offered).
    """
    query_points: List[QueryPoint] = field(default_factory=list)
    simulate_outage: bool = False
    simulate_storm: bool = False
    _last_readings: List[WeatherReading] = field(default_factory=list, repr=False)

    @property
    def stations(self) -> List[WeatherReading]:
        """Most recent successfully-fetched readings (for API display)."""
        return self._last_readings

    def set_query_points(self, points: List[QueryPoint]) -> None:
        self.query_points = points

    def fetch_readings(self) -> Optional[List[WeatherReading]]:
        if self.simulate_outage or not self.query_points:
            return None

        now = time.time()
        if self.simulate_storm:
            readings = []
            for p in self.query_points:
                # Add some deterministic pseudo-random variation based on coordinates
                heavy_rain = 35.0 + (p.lat % 0.01) * 1000.0  # ~35-45 mm/h
                low_vis = 250.0 + (p.lon % 0.01) * 10000.0   # ~250-350m
                wind = 60.0 + (p.lat % 0.005) * 2000.0       # ~60-70 km/h
                readings.append(WeatherReading(
                    station_id=p.station_id,
                    lat=p.lat, lon=p.lon,
                    precipitation_mm_h=heavy_rain,
                    visibility_m=low_vis,
                    wind_speed_kmh=wind,
                    temperature_c=22.0,
                    timestamp=now
                ))
            self._last_readings = readings
            return readings

        lats = ",".join(f"{p.lat:.6f}" for p in self.query_points)
        lons = ",".join(f"{p.lon:.6f}" for p in self.query_points)

        try:
            resp = requests.get(
                OPEN_METEO_URL,
                params={
                    "latitude": lats,
                    "longitude": lons,
                    "current": CURRENT_VARS,
                    "wind_speed_unit": "kmh",
                    "precipitation_unit": "mm",
                    "timezone": "auto",
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None  # network error / timeout / bad JSON -> NFR-2.1 fallback

        # Single coordinate -> API returns one object. Multiple coordinates
        # -> API returns a JSON array, one object per point, same order as
        # the request. Normalize to a list either way.
        results = data if isinstance(data, list) else [data]
        if len(results) != len(self.query_points):
            return None  # unexpected shape; fail safe into fallback

        now = time.time()
        readings: List[WeatherReading] = []
        for point, result in zip(self.query_points, results):
            current = result.get("current")
            if not current:
                continue
            readings.append(WeatherReading(
                station_id=point.station_id,
                lat=point.lat,
                lon=point.lon,
                precipitation_mm_h=float(current.get("precipitation", 0.0) or 0.0),
                # Open-Meteo returns visibility in meters already.
                visibility_m=float(current.get("visibility", 10000.0) or 10000.0),
                wind_speed_kmh=float(current.get("wind_speed_10m", 0.0) or 0.0),
                temperature_c=float(current.get("temperature_2m", 25.0) or 25.0),
                timestamp=now,
            ))

        if not readings:
            return None

        self._last_readings = readings
        return readings


def geocode_search(query: str, count: int = 5):
    """Call Open-Meteo Geocoding API to resolve a place name to coordinates.

    Returns a list of result dicts [{name, latitude, longitude, country,
    admin1, ...}], or None on network/service error so the caller can
    decide how to surface the failure (HTTP 502, empty list, etc.).
    """
    try:
        resp = requests.get(
            GEOCODING_URL,
            params={"name": query, "count": count, "format": "json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    return data.get("results") or []


# ---------------------------------------------------------------------
# Mock client (kept for demo.py / offline tests — no internet required)
# ---------------------------------------------------------------------
@dataclass
class WeatherAPIClient:
    """
    Stand-in used by demo.py and the test suites so they run without
    network access. `simulate_outage=True` exercises NFR-2.1 the same way.
    """
    stations: List[WeatherReading]
    simulate_outage: bool = False

    def fetch_readings(self) -> Optional[List[WeatherReading]]:
        if self.simulate_outage:
            return None
        now = time.time()
        for s in self.stations:
            s.timestamp = now
        return self.stations

    def jitter_readings(self, magnitude: float = 0.15) -> None:
        """Demo helper: randomly perturb readings to simulate a live feed."""
        for s in self.stations:
            s.precipitation_mm_h = max(
                0.0, s.precipitation_mm_h * (1 + random.uniform(-magnitude, magnitude))
            )
            s.visibility_m = max(
                50.0, s.visibility_m * (1 + random.uniform(-magnitude, magnitude))
            )
            s.wind_speed_kmh = max(
                0.0, s.wind_speed_kmh * (1 + random.uniform(-magnitude, magnitude))
            )

