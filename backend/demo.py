"""
demo.py
-------
End-to-end demonstration of the full pipeline against a small synthetic
road network, reproducing the core operational workflow in spec section 2.1:

  1. Data Ingestion        (weather_service.WeatherAPIClient)
  2. Risk Assessment       (risk_engine.RiskEngine)
  3. Route Optimization    (routing.RoadNetwork.find_route)
  4. User Guidance         (printed route summary + reroute alert)

Run:  python3 demo.py
"""

from __future__ import annotations

from models import Node, RoadSegment, WeatherReading
from risk_engine import RiskEngine, RiskWeights
from routing import RoadNetwork
from weather_service import WeatherAPIClient


def build_sample_network() -> RoadNetwork:
    """
    A tiny grid-ish network with a 'fast but flood-prone' path down
    Main Street, and a slightly longer 'safe' path via Highland Ave.

        A --- B --- C --- D   (Main Street: low-lying, poor drainage)
         \\               /
          E ----------- F     (Highland Ave: longer, better drainage)
    """
    net = RoadNetwork()
    coords = {
        "A": (14.2100, 121.1650),
        "B": (14.2115, 121.1710),
        "C": (14.2130, 121.1770),
        "D": (14.2145, 121.1830),
        "E": (14.2060, 121.1660),
        "F": (14.2100, 121.1820),
    }
    for nid, (lat, lon) in coords.items():
        net.add_node(Node(nid, lat, lon))

    # Main Street: flood-prone, poor drainage, history of flooding
    net.add_segment(RoadSegment(
        id="AB", from_node="A", to_node="B", distance_km=0.8,
        drainage_rating=0.85, historical_flood_index=0.7, friction_index=0.6,
    ))
    net.add_segment(RoadSegment(
        id="BC", from_node="B", to_node="C", distance_km=0.9,
        drainage_rating=0.9, historical_flood_index=0.75, friction_index=0.55,
    ))
    net.add_segment(RoadSegment(
        id="CD", from_node="C", to_node="D", distance_km=0.8,
        drainage_rating=0.8, historical_flood_index=0.6, friction_index=0.6,
    ))

    # Highland Ave: longer detour, good drainage, elevated bridge segment
    net.add_segment(RoadSegment(
        id="AE", from_node="A", to_node="E", distance_km=0.9,
        drainage_rating=0.2, historical_flood_index=0.05, friction_index=0.85,
    ))
    net.add_segment(RoadSegment(
        id="EF", from_node="E", to_node="F", distance_km=1.9,
        drainage_rating=0.15, historical_flood_index=0.05, friction_index=0.9,
        is_elevated_or_bridge=True,
    ))
    net.add_segment(RoadSegment(
        id="FD", from_node="F", to_node="D", distance_km=0.7,
        drainage_rating=0.2, historical_flood_index=0.05, friction_index=0.85,
    ))

    return net


def build_storm_readings() -> list:
    """One weather station near Main Street reporting heavy rain / fog."""
    return [
        WeatherReading(
            station_id="STN-MAIN", lat=14.2122, lon=121.1740,
            precipitation_mm_h=58.0, visibility_m=150.0,
            wind_speed_kmh=20.0, temperature_c=26.0, timestamp=0.0,
        ),
        WeatherReading(
            station_id="STN-HIGHLAND", lat=14.2080, lon=121.1740,
            precipitation_mm_h=3.0, visibility_m=8000.0,
            wind_speed_kmh=35.0, temperature_c=27.0, timestamp=0.0,
        ),
    ]


def print_route(label: str, route) -> None:
    if route is None:
        print(f"{label}: NO ROUTE FOUND")
        return
    print(f"\n{label}")
    print(f"  Path:              {' -> '.join(route.node_ids)}")
    print(f"  Distance:          {route.total_distance_km} km")
    print(f"  ETA:               {route.total_travel_time_min} min")
    print(f"  Aggregate risk:    {route.aggregate_risk}  ({route.risk_band()})")
    print(f"  Worst segment:     {route.worst_segment_id} (R={route.max_segment_risk})")
    print(f"  Hazard triggered:  {route.has_hazard()}")
    print(f"  Compute time:      {route.compute_seconds}s  (NFR-1.1 target < 2.5s)")


def main():
    net = build_sample_network()
    engine = RiskEngine(RiskWeights(w_wind=0.10))  # enable FR-3.1 wind term

    # ---- 1. Data Ingestion ----
    client = WeatherAPIClient(stations=build_storm_readings(), simulate_outage=False)
    readings = client.fetch_readings()

    # ---- 2. Risk Assessment ----
    engine.geofence_weather_to_segments(net.all_segments(), net.nodes, readings)
    engine.apply_historical_fallback(net.all_segments())  # no-op unless stale
    scores = engine.score_all(net.all_segments())

    print("=== Segment Risk Scores (storm on Main Street) ===")
    for seg_id, score in sorted(scores.items()):
        seg = net.segments[seg_id]
        print(f"  {seg_id:6s} R={score:6.2f}  band={engine.risk_band(score):6s} "
              f"stale={seg.weather_data_stale}")

    # ---- 3. Route Optimization ----
    fast_route = net.find_route("A", "D", alpha=0.0)   # ignores risk entirely
    safe_route = net.find_route("A", "D", alpha=1.5)   # risk-averse

    print_route("Fastest route (alpha=0, distance-only)", fast_route)
    print_route("Risk-averse route (alpha=1.5)", safe_route)

    if fast_route and safe_route:
        print(f"\nFR-4.3 Trade-off: {net.describe_tradeoff(safe_route, fast_route)}")

    # ---- 4. Automated Rerouting mid-transit (FR-4.2) ----
    print("\n=== Mid-transit reroute check ===")
    print("Driver departs on the fastest (Main Street) route...")
    driver_at = fast_route.node_ids[1]  # arrived at node B
    rerouted, new_route = net.check_and_reroute(fast_route, driver_at, "D", alpha=1.5)
    print(f"Driver now at node '{driver_at}'. Reroute triggered: {rerouted}")
    if rerouted:
        print_route("New route after hazard detection", new_route)


if __name__ == "__main__":
    main()
