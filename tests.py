"""
tests.py
--------
Lightweight sanity tests (no pytest dependency required).
Run: python3 tests.py
"""

from models import Node, RoadSegment, WeatherReading, haversine_km
from risk_engine import RiskEngine, RiskWeights
from routing import RoadNetwork
from weather_service import WeatherAPIClient

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


def test_haversine():
    # Manila to Quezon City is roughly 12km
    d = haversine_km(14.5995, 120.9842, 14.6760, 121.0437)
    check("haversine_km ~12km Manila-QC", 8 < d < 16)


def test_risk_bounds():
    engine = RiskEngine()
    seg = RoadSegment(
        id="X", from_node="A", to_node="B", distance_km=1.0,
        precipitation_mm_h=200.0, visibility_m=0.0,
        drainage_rating=1.0, historical_flood_index=1.0, friction_index=0.0,
    )
    score = engine.score_segment(seg)
    check("extreme storm caps at 100", 0.0 <= score <= 100.0)
    check("extreme storm -> red band", engine.risk_band(score) == "red")

    dry = RoadSegment(
        id="Y", from_node="A", to_node="B", distance_km=1.0,
        precipitation_mm_h=0.0, visibility_m=10000.0,
        drainage_rating=0.0, historical_flood_index=0.0, friction_index=1.0,
    )
    dry_score = engine.score_segment(dry)
    check("clear/dry -> near-zero risk", dry_score < 5.0)
    check("clear/dry -> green band", engine.risk_band(dry_score) == "green")


def test_weights_validate():
    try:
        RiskWeights(w_rain=1.5)
        check("out-of-range weight rejected", False)
    except ValueError:
        check("out-of-range weight rejected", True)


def test_weights_normalize_sum_to_one():
    w = RiskWeights(w_rain=0.35, w_visibility=0.20, w_drainage=0.30, w_history=0.15).normalized()
    total = w.w_rain + w.w_visibility + w.w_drainage + w.w_history + w.w_wind
    check("normalized weights sum to 1", abs(total - 1.0) < 1e-9)


def test_geofence_out_of_range_flags_stale():
    engine = RiskEngine()
    net = RoadNetwork()
    net.add_node(Node("A", 0.0, 0.0))
    net.add_node(Node("B", 0.0, 0.05))  # ~5.5km east, outside 1km geofence
    net.add_segment(RoadSegment(id="AB", from_node="A", to_node="B", distance_km=5.5))

    far_reading = [WeatherReading(
        station_id="S1", lat=10.0, lon=10.0,  # far away
        precipitation_mm_h=50.0, visibility_m=100.0,
        wind_speed_kmh=10.0, temperature_c=25.0, timestamp=0.0,
    )]
    engine.geofence_weather_to_segments(net.all_segments(), net.nodes, far_reading)
    seg = net.segments["AB"]
    check("segment flagged stale when no station in range", seg.weather_data_stale is True)

    engine.apply_historical_fallback(net.all_segments())
    check("fallback does not crash / leaves usable values", seg.precipitation_mm_h >= 0)


def test_route_avoids_flood_zone():
    net = RoadNetwork()
    for nid, (lat, lon) in {
        "A": (14.21, 121.16), "B": (14.212, 121.17), "D": (14.214, 121.18),
        "E": (14.205, 121.165), "F": (14.208, 121.178),
    }.items():
        net.add_node(Node(nid, lat, lon))

    net.add_segment(RoadSegment(id="AB", from_node="A", to_node="B", distance_km=1.0))
    net.add_segment(RoadSegment(id="BD", from_node="B", to_node="D", distance_km=1.0))
    net.add_segment(RoadSegment(id="AE", from_node="A", to_node="E", distance_km=1.1))
    net.add_segment(RoadSegment(id="EF", from_node="E", to_node="F", distance_km=1.1))
    net.add_segment(RoadSegment(id="FD", from_node="F", to_node="D", distance_km=1.1))

    # Flood the direct A-B-D path
    net.segments["AB"].risk_score = 95.0
    net.segments["AB_rev"].risk_score = 95.0
    net.segments["BD"].risk_score = 95.0
    net.segments["BD_rev"].risk_score = 95.0

    fast = net.find_route("A", "D", alpha=0.0)
    safe = net.find_route("A", "D", alpha=2.0)

    check("fast route takes shorter flooded path", fast.node_ids == ["A", "B", "D"])
    check("risk-averse route detours around flood", safe.node_ids == ["A", "E", "F", "D"])
    check("risk-averse route has lower aggregate risk", safe.aggregate_risk < fast.aggregate_risk)


def test_weather_outage_fallback():
    client = WeatherAPIClient(stations=[], simulate_outage=True)
    result = client.fetch_readings()
    check("outage returns None (caller must fall back)", result is None)


if __name__ == "__main__":
    for fn in [
        test_haversine, test_risk_bounds, test_weights_validate,
        test_weights_normalize_sum_to_one, test_geofence_out_of_range_flags_stale,
        test_route_avoids_flood_zone, test_weather_outage_fallback,
    ]:
        print(f"\n{fn.__name__}")
        fn()

    print(f"\n{'='*40}\n{passed} passed, {failed} failed\n{'='*40}")
    raise SystemExit(1 if failed else 0)
