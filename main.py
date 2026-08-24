"""
main.py
-------
FastAPI layer wrapping risk_engine.py + routing.py.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Then browse interactive docs at http://127.0.0.1:8000/docs

Endpoint -> spec traceability:
    GET  /network              FR-1.1 (map data for a frontend to draw)
    GET  /risk-heatmap         FR-1.2
    POST /weather/refresh      FR-2.1, FR-3.2, NFR-2.1 (manual trigger of the poll cycle)
    POST /weather/outage       NFR-2.1 (demo/test hook to simulate an API outage)
    GET  /weather/stations     FR-2.1 (raw live readings, for a station panel)
    POST /network/import       load a real road network (e.g. from OSM/Overpass)
    POST /route                FR-4.1
    POST /route/compare        FR-4.3 (fastest vs safest + trade-off text)
    POST /reroute-check        FR-4.2, FR-3.3
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from risk_engine import RiskEngine
from routing import RouteResult
from schemas import (
    AutoPollOut, AutoPollToggleRequest, GeocodeResult, GeocodeSearchOut,
    HealthOut,
    HeatmapOut, HeatmapSegment, NetworkImportOut, NetworkImportRequest,
    NetworkOut, NodeOut, SegmentOut,
    OutageToggleRequest, RerouteCheckOut, RerouteCheckRequest,
    RouteCompareOut, RouteOut, RouteRequest, StationOut, StationsOut,
    WeatherRefreshOut,
)
from weather_service import geocode_search
from state import state

logger = logging.getLogger("stormline.scheduler")


async def _weather_poll_loop() -> None:
    """
    Background task: re-fetches live weather every state.poll_interval_seconds
    (300s / 5 min by default, per FR-2.1), independent of anyone touching the
    UI. This is what actually satisfies "the system shall poll... at minimum
    5-minute intervals" — the manual 'Poll weather now' button and this loop
    both just call state.refresh_weather(), so behavior is identical either
    way. Runs the (blocking) refresh in a thread so it never stalls the
    event loop, and never lets a transient error kill the scheduler.
    """
    while True:
        try:
            await asyncio.sleep(state.poll_interval_seconds)
            if state.auto_poll_enabled:
                await asyncio.to_thread(state.refresh_weather)
                state.auto_poll_runs += 1
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("weather poll loop error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_weather_poll_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Weather-Hazard Risk Route Assessor API",
    description="Backend for the Localized Weather-Induced Road Hazard "
                 "Risk Assessor with Alternative Routing.",
    version="1.0.0",
    lifespan=lifespan,
)

# Wide-open CORS for local frontend dev. Lock this down to your actual
# frontend origin(s) before deploying anywhere real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _to_route_out(route: RouteResult) -> RouteOut:
    # Build coordinate list from the backend's full node map so the
    # frontend can always draw the complete path even if its local
    # currentNetwork cache doesn't contain every intermediate node.
    from schemas import RouteNodeCoord
    coords = []
    for nid in route.node_ids:
        node = state.network.nodes.get(nid)
        if node:
            coords.append(RouteNodeCoord(id=nid, lat=node.lat, lon=node.lon))
    return RouteOut(
        node_ids=route.node_ids,
        node_coords=coords,
        segment_ids=route.segment_ids,
        total_distance_km=route.total_distance_km,
        total_travel_time_min=route.total_travel_time_min,
        aggregate_risk=route.aggregate_risk,
        max_segment_risk=route.max_segment_risk,
        worst_segment_id=route.worst_segment_id,
        max_rain_mm_h=route.max_rain_mm_h,
        min_vis_m=route.min_vis_m,
        max_wind_kmh=route.max_wind_kmh,
        risk_band=route.risk_band(),
        hazard_triggered=route.has_hazard(),
        compute_seconds=route.compute_seconds,
        approximate=route.approximate,
        wrong_way_segment_ids=route.wrong_way_segment_ids,
    )


def _find_route_with_fallback(start: str, goal: str, alpha: float, ignore_oneway: bool = False) -> Optional[RouteResult]:
    """
    Try a normal, direction-respecting route first. If (and only if) that
    genuinely fails, retry allowing the search to travel loaded segments
    against their marked direction as a last resort (RouteResult.approximate
    will be True on the result). This never invents roads that were never
    loaded -- if there's truly no loaded road connecting the two points,
    both attempts return None and the caller still reports "no route".
    """
    if ignore_oneway:
        # User explicitly requested ignoring one-way constraints (e.g. for walking)
        return state.network.find_route(start, goal, alpha, allow_wrong_way=True)

    route = state.network.find_route(start, goal, alpha)
    if route is not None:
        return route
    return state.network.find_route(start, goal, alpha, allow_wrong_way=True)


@app.get("/health", response_model=HealthOut)
def health():
    return HealthOut(
        status="ok",
        last_weather_refresh=state.last_refresh_ts,
        next_weather_refresh=state.next_refresh_ts,
        seconds_until_next_refresh=state.seconds_until_next_refresh(),
        used_fallback_last_refresh=state.used_fallback_last_refresh,
        auto_poll_enabled=state.auto_poll_enabled,
        poll_interval_seconds=state.poll_interval_seconds,
        auto_poll_runs=state.auto_poll_runs,
    )


@app.post("/weather/auto-poll", response_model=AutoPollOut)
def toggle_auto_poll(req: AutoPollToggleRequest):
    """Pause/resume the background 5-minute scheduler (FR-2.1) without
    stopping the process. Manual 'Poll weather now' still works either way."""
    state.auto_poll_enabled = req.enabled
    return AutoPollOut(auto_poll_enabled=state.auto_poll_enabled)


# ---------------------------------------------------------------------
# FR-1.1 Map data
# ---------------------------------------------------------------------
@app.get("/network", response_model=NetworkOut)
def get_network():
    net = state.network
    nodes = [NodeOut(id=n.id, lat=n.lat, lon=n.lon) for n in net.nodes.values()]
    segments = [
        SegmentOut(
            id=s.id, from_node=s.from_node, to_node=s.to_node,
            distance_km=s.distance_km, risk_score=s.risk_score,
            risk_band=RiskEngine.risk_band(s.risk_score),
            is_elevated_or_bridge=s.is_elevated_or_bridge,
            weather_data_stale=s.weather_data_stale,
            traffic_speed_kmh=s.traffic_speed_kmh,
        )
        for s in net.all_segments()
    ]
    return NetworkOut(nodes=nodes, segments=segments, source=state.network_source)


# ---------------------------------------------------------------------
# FR-1.2 Risk heatmap
# ---------------------------------------------------------------------
@app.get("/risk-heatmap", response_model=HeatmapOut)
def get_risk_heatmap():
    segs = []
    for s in state.network.all_segments():
        segs.append(HeatmapSegment(
            segment_id=s.id, from_node=s.from_node, to_node=s.to_node,
            risk_score=s.risk_score,
            risk_band=RiskEngine.risk_band(s.risk_score),
            hazard_triggered=RiskEngine.is_hazard_triggered(s.risk_score),
        ))
    return HeatmapOut(generated_at=time.time(), segments=segs)


# ---------------------------------------------------------------------
# FR-2.1 / NFR-2.1 Weather ingestion
# ---------------------------------------------------------------------
@app.post("/weather/refresh", response_model=WeatherRefreshOut)
def refresh_weather():
    """
    Manually trigger one poll cycle (fetch -> geofence -> fallback -> score).
    In production this fires automatically every 300s per FR-2.1 via a
    scheduler; exposed here as an endpoint so a frontend/demo can force it.
    """
    result = state.refresh_weather()
    msg = (
        "Live weather unavailable — used historical fallback (NFR-2.1)."
        if result["used_fallback"]
        else f"Ingested {result['stations_received']} station reading(s) "
             f"and rescored all segments."
    )
    return WeatherRefreshOut(success=True, message=msg, **result)


@app.post("/weather/outage")
def toggle_outage(req: OutageToggleRequest):
    """Demo/test hook: force the mock weather API to simulate an outage."""
    state.weather_client.simulate_outage = req.simulate_outage
    state.weather_client.simulate_storm = req.simulate_storm
    return {"simulate_outage": req.simulate_outage, "simulate_storm": req.simulate_storm}


@app.get("/weather/stations", response_model=StationsOut)
def get_stations():
    """Current raw station readings, for a frontend station panel."""
    stations = [
        StationOut(
            station_id=s.station_id, lat=s.lat, lon=s.lon,
            precipitation_mm_h=s.precipitation_mm_h, visibility_m=s.visibility_m,
            wind_speed_kmh=s.wind_speed_kmh, temperature_c=s.temperature_c,
        )
        for s in state.weather_client.stations
    ]
    return StationsOut(
        stations=stations, 
        simulate_outage=state.weather_client.simulate_outage,
        simulate_storm=state.weather_client.simulate_storm
    )


# ---------------------------------------------------------------------
# Loading a real road network (e.g. from OpenStreetMap via Overpass,
# fetched client-side and posted here to replace the demo network)
# ---------------------------------------------------------------------
@app.post("/network/import", response_model=NetworkImportOut)
def import_network(req: NetworkImportRequest):
    if not req.nodes or not req.segments:
        raise HTTPException(status_code=400, detail="Provide at least one node and one segment.")

    result = state.load_network(
        nodes=[(n.id, n.lat, n.lon) for n in req.nodes],
        segments=[
            (s.id, s.from_node, s.to_node, s.distance_km, s.is_elevated_or_bridge)
            for s in req.segments
        ],
        source=req.source,
    )
    msg = (
        f"Loaded {result['nodes_loaded']} nodes / {result['segments_loaded']} segments "
        f"from {req.source}."
    )
    if result["segments_skipped"]:
        msg += f" ({result['segments_skipped']} segment(s) skipped — dangling node refs.)"
    if result["used_fallback"]:
        msg += " Live weather unavailable for this area — using historical fallback."

    return NetworkImportOut(
        success=True,
        nodes_loaded=result["nodes_loaded"],
        segments_loaded=result["segments_loaded"],
        segments_skipped=result["segments_skipped"],
        used_fallback=result["used_fallback"],
        message=msg,
    )


# ---------------------------------------------------------------------
# FR-4.1 Routing
# ---------------------------------------------------------------------
@app.post("/route", response_model=RouteOut)
def compute_route(req: RouteRequest):
    route = _find_route_with_fallback(req.start_node, req.goal_node, req.alpha, req.ignore_oneway)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"No loaded road connects '{req.start_node}' and '{req.goal_node}' "
                    f"(checked both directions of every loaded segment).",
        )
    return _to_route_out(route)


# ---------------------------------------------------------------------
# FR-4.3 Delay-vs-safety comparison
# ---------------------------------------------------------------------
@app.post("/route/compare", response_model=RouteCompareOut)
def compare_routes(req: RouteRequest):
    routes = state.network.find_alternative_routes(
        req.start_node, req.goal_node, alpha=req.alpha,
        allow_wrong_way=req.ignore_oneway, num_alternatives=3,
    )
    
    if not routes:
        raise HTTPException(
            status_code=404,
            detail=f"No loaded road connects '{req.start_node}' and '{req.goal_node}' "
                    f"(checked both directions of every loaded segment).",
        )
    
    # Ensure we always have 3 slots (pad with duplicates if needed)
    while len(routes) < 3:
        routes.append(routes[-1])
    
    # Label them: first found = fastest, last = safest, middle = balanced
    fastest, balanced, safest = routes[0], routes[1], routes[2]
    
    tradeoff = state.network.describe_tradeoff(safest, fastest)
    rankings = state.network.evaluate_rankings({
        "fastest": fastest,
        "balanced": balanced,
        "safest": safest
    })
    
    return RouteCompareOut(
        fastest=_to_route_out(fastest),
        balanced=_to_route_out(balanced),
        safest=_to_route_out(safest),
        tradeoff_summary=tradeoff,
        rankings=rankings
    )


# ---------------------------------------------------------------------
# FR-4.2 / FR-3.3 Mid-transit reroute check
# ---------------------------------------------------------------------
@app.post("/reroute-check", response_model=RerouteCheckOut)
def reroute_check(req: RerouteCheckRequest):
    fake_route = RouteResult(
        node_ids=req.route_node_ids,
        segment_ids=req.route_segment_ids,
        total_distance_km=0.0,
        total_travel_time_min=0.0,
        aggregate_risk=0.0,
        max_segment_risk=0.0,
        worst_segment_id=None,
        compute_seconds=0.0,
    )
    rerouted, new_route = state.network.check_and_reroute(
        fake_route, req.current_node, req.goal_node, req.alpha
    )
    if not rerouted:
        return RerouteCheckOut(rerouted=False, new_route=None,
                                message="Current route remains safe. No reroute needed.")
    if new_route is None:
        return RerouteCheckOut(rerouted=True, new_route=None,
                                message="Hazard detected but no alternative route exists "
                                        "from the current position.")
    return RerouteCheckOut(
        rerouted=True,
        new_route=_to_route_out(new_route),
        message="Hazard detected ahead — rerouted to a safer path.",
    )


# ---------------------------------------------------------------------
# Geocoding (location search by name)
# ---------------------------------------------------------------------
@app.get("/geocode", response_model=GeocodeSearchOut)
def geocode(q: str = "", count: int = 5):
    """Resolve a place name to lat/lon coordinates (Open-Meteo Geocoding)."""
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required.")
    count = max(1, min(count, 20))
    raw = geocode_search(q.strip(), count)
    if raw is None:
        raise HTTPException(status_code=502, detail="Geocoding service unreachable.")
    results = [
        GeocodeResult(
            name=r.get("name", ""),
            latitude=r["latitude"],
            longitude=r["longitude"],
            country=r.get("country", ""),
            admin1=r.get("admin1", ""),
            country_code=r.get("country_code", ""),
            elevation=r.get("elevation"),
        )
        for r in raw
    ]
    return GeocodeSearchOut(query=q.strip(), results=results)


# ---------------------------------------------------------------------
# Static frontend (frontend/index.html, styles.css, app.js).
# Mounted last so it never shadows the API routes above; serves the
# console UI at "/" when you run `uvicorn main:app`.
#
# Cache-Control: no-cache forces the browser to revalidate with the
# server on every load (a fast 304 if unchanged) instead of trusting its
# own heuristic cache — plain StaticFiles let browsers silently keep
# serving an old cached app.js/index.html/styles.css even after the file
# on disk changed, which is confusing during active development.
# ---------------------------------------------------------------------
import os


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


_frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", NoCacheStaticFiles(directory=_frontend_dir, html=True), name="frontend")