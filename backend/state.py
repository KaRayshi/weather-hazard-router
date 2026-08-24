"""
state.py
--------
Process-wide application state: one RoadNetwork, one RiskEngine, one live
OpenMeteoClient. In-memory and single-process — fine for local dev/demo.
For production, replace with a proper datastore (Postgres/PostGIS for the
network graph) and move the scheduler in main.py to something durable
(APScheduler with a persistent job store, or Celery beat) instead of a
single in-process asyncio task.

The network starts as the small synthetic demo graph (demo.py) so the app
has something to show immediately. Call load_network() — wired to
POST /network/import in main.py — to replace it with a real road network,
e.g. one parsed from live OpenStreetMap data via the Overpass API in the
frontend. Whenever the network changes, the weather client's query points
are rebuilt to match the new node coordinates and a fresh Open-Meteo
fetch is triggered automatically.
"""

from __future__ import annotations

import time
import random
from typing import List, Optional, Tuple

from demo import build_sample_network
from models import Node, RoadSegment
from risk_engine import RiskEngine, RiskWeights
from routing import RoadNetwork
from weather_service import OpenMeteoClient, QueryPoint, POLL_INTERVAL_SECONDS

MAX_QUERY_POINTS = 60  # keep a single Open-Meteo request reasonably sized


class AppState:
    def __init__(self):
        self.network: RoadNetwork = RoadNetwork()
        self.engine: RiskEngine = RiskEngine(RiskWeights(w_wind=0.10))
        self.weather_client: OpenMeteoClient = OpenMeteoClient()
        self.last_refresh_ts: Optional[float] = None
        self.next_refresh_ts: Optional[float] = None
        self.used_fallback_last_refresh: bool = False
        self.network_source: str = "osm"

        # FR-2.1: poll every 300s by default. Exposed as an instance
        # attribute (not just the module constant) so a background
        # scheduler — or a test — can read/adjust the cadence without
        # touching global state.
        self.poll_interval_seconds: float = POLL_INTERVAL_SECONDS
        self.auto_poll_enabled: bool = True
        self.auto_poll_runs: int = 0  # how many times the scheduler has fired

        self._sync_query_points()
        self.refresh_weather()  # populate risk scores at startup

    # -----------------------------------------------------------
    # Weather ingestion (FR-2.1 -> FR-3.2, NFR-2.1 fallback)
    # -----------------------------------------------------------
    def _sync_query_points(self) -> None:
        """One Open-Meteo query point per road-network node, capped so a
        very large imported OSM network doesn't blow up a single request."""
        nodes = list(self.network.nodes.values())[:MAX_QUERY_POINTS]
        self.weather_client.set_query_points([
            QueryPoint(station_id=n.id, lat=n.lat, lon=n.lon) for n in nodes
        ])

    def refresh_weather(self) -> dict:
        """
        One full FR-2.1 -> FR-3.2 cycle: fetch live readings from Open-Meteo
        (or detect an outage), geofence them onto segments, apply NFR-2.1
        fallback to any segment that didn't get a live reading, then
        rescore every segment. Called on startup, on-demand (POST
        /weather/refresh), after a network import, and automatically by
        the background scheduler in main.py every poll_interval_seconds.
        """
        readings = self.weather_client.fetch_readings()
        used_fallback = readings is None

        if readings is not None:
            self.engine.geofence_weather_to_segments(
                self.network.all_segments(), self.network.nodes, readings
            )
        else:
            for seg in self.network.all_segments():
                seg.weather_data_stale = True

        self.engine.apply_historical_fallback(self.network.all_segments())
        scores = self.engine.score_all(self.network.all_segments())

        # Simulate dynamic traffic (FR-1.1 overlay)
        # Slower traffic when risk is higher, with some random variation.
        for seg in self.network.all_segments():
            base_speed = 50.0
            if seg.risk_score >= 70:
                base_speed = random.uniform(5.0, 15.0)   # severe congestion
            elif seg.risk_score >= 30:
                base_speed = random.uniform(15.0, 35.0)  # moderate slowdown
            else:
                base_speed = random.uniform(40.0, 60.0)  # free flowing
            seg.traffic_speed_kmh = round(base_speed, 1)

        self.last_refresh_ts = time.time()
        self.next_refresh_ts = self.last_refresh_ts + self.poll_interval_seconds
        self.used_fallback_last_refresh = used_fallback

        stale_count = sum(
            1 for s in self.network.all_segments() if s.weather_data_stale
        )
        return {
            "stations_received": len(readings) if readings else 0,
            "segments_updated": len(scores) - stale_count,
            "segments_stale": stale_count,
            "used_fallback": used_fallback,
        }

    def seconds_until_next_refresh(self) -> Optional[float]:
        if self.next_refresh_ts is None:
            return None
        return max(0.0, round(self.next_refresh_ts - time.time(), 1))

    # -----------------------------------------------------------
    # Dynamic network loading (real OSM data from the frontend)
    # -----------------------------------------------------------
    def load_network(
        self,
        nodes: List[Tuple[str, float, float]],
        segments: List[Tuple[str, str, str, float, bool]],
        source: str = "osm",
    ) -> dict:
        """
        Merge new nodes/segments into the active road network.

        Previously this *replaced* self.network outright on every call. That
        was fine for a single "load roads, then route" pass, but the actual
        UI flow lets someone tap an origin (which silently loads roads for
        that view), then tap a destination somewhere else on the map (which
        silently loads roads for *that* view). A second import wiping out
        the first meant the origin node picked a moment earlier no longer
        existed in the graph at all -> find_route() always returned None,
        surfaced to the user as a generic "couldn't find a route" /
        "might not be connected" message that had nothing to do with actual
        road connectivity.

        Now we union everything loaded so far into one graph. OSM node/way
        ids are globally stable, so re-loading an overlapping area just
        re-adds the same ids (skipped as duplicates) instead of creating a
        second disconnected copy of that intersection.

        Switching data source (e.g. demo -> osm, or an explicit reset) still
        starts a fresh graph, since merging demo + real data would be
        meaningless.
          nodes:    list of (id, lat, lon)
          segments: list of (id, from_node, to_node, distance_km, is_elevated_or_bridge)
        Segments are added one-directional as given; the frontend is
        responsible for emitting both directions if it wants two-way roads
        (real OSM `oneway` tags make this meaningful, unlike the synthetic
        demo network which always adds bidirectional pairs).
        """
        if source != self.network_source:
            self.network = RoadNetwork()
            self.network_source = source

        net = self.network
        nodes_added = 0
        for nid, lat, lon in nodes:
            if nid not in net.nodes:
                net.add_node(Node(nid, lat, lon))
                nodes_added += 1

        skipped = 0
        segments_added = 0
        for sid, frm, to, dist, elevated in segments:
            if frm not in net.nodes or to not in net.nodes:
                skipped += 1
                continue
            if sid in net.segments:
                continue  # already have this edge from an earlier load
            net.add_segment(
                RoadSegment(
                    id=sid, from_node=frm, to_node=to, distance_km=max(dist, 0.001),
                    is_elevated_or_bridge=elevated,
                ),
                bidirectional=False,
            )
            segments_added += 1

        self._sync_query_points()
        refresh_result = self.refresh_weather()

        return {
            "nodes_loaded": nodes_added,
            "segments_loaded": segments_added,
            "segments_skipped": skipped,
            **refresh_result,
        }


# single process-wide instance, imported by main.py
state = AppState()

