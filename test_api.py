"""
test_api.py
-----------
Smoke tests for the FastAPI layer using Starlette's TestClient
(no running server needed). Live Open-Meteo calls are mocked so this
suite runs offline and deterministically. Run: python3 test_api.py
"""

import time
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from main import app
from state import state

client = TestClient(app)
passed, failed = 0, 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


def fake_open_meteo_response(n_points, precipitation=12.0, visibility=4000.0,
                              wind=15.0, temperature=27.0):
    """Build a mock requests.Response mimicking Open-Meteo's multi-location
    JSON shape, so tests never need real internet access."""
    body = [
        {"current": {
            "precipitation": precipitation, "visibility": visibility,
            "wind_speed_10m": wind, "temperature_2m": temperature,
        }}
        for _ in range(n_points)
    ]
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body
    return resp


def test_health():
    r = client.get("/health")
    body = r.json()
    check("GET /health -> 200", r.status_code == 200)
    check("health payload has status ok", body["status"] == "ok")
    check("health reports poll_interval_seconds", body["poll_interval_seconds"] > 0)
    check("health reports auto_poll_enabled", "auto_poll_enabled" in body)
    check("health reports seconds_until_next_refresh", "seconds_until_next_refresh" in body)


def test_network():
    r = client.get("/network")
    body = r.json()
    check("GET /network -> 200", r.status_code == 200)
    check("network has nodes", len(body["nodes"]) > 0)
    check("network has segments", len(body["segments"]) > 0)


def test_heatmap():
    r = client.get("/risk-heatmap")
    body = r.json()
    check("GET /risk-heatmap -> 200", r.status_code == 200)
    check("heatmap has segments with risk_band", all("risk_band" in s for s in body["segments"]))


def test_weather_refresh_and_outage():
    n = len(state.weather_client.query_points)
    with patch("weather_service.requests.get", return_value=fake_open_meteo_response(n)):
        r = client.post("/weather/refresh")
        check("POST /weather/refresh -> 200", r.status_code == 200)
        check("refresh not using fallback with live API mocked", r.json()["used_fallback"] is False)

    r2 = client.post("/weather/outage", json={"simulate_outage": True})
    check("POST /weather/outage -> 200", r2.status_code == 200)

    r3 = client.post("/weather/refresh")  # simulate_outage=True short-circuits before any HTTP call
    check("refresh uses fallback during simulated outage", r3.json()["used_fallback"] is True)

    # restore for subsequent tests
    client.post("/weather/outage", json={"simulate_outage": False})
    with patch("weather_service.requests.get", return_value=fake_open_meteo_response(n)):
        client.post("/weather/refresh")


def test_weather_network_failure_triggers_fallback():
    """A real network error (e.g. no internet, DNS failure, timeout) must
    degrade gracefully into the NFR-2.1 fallback path, never crash."""
    import requests as _requests
    with patch("weather_service.requests.get", side_effect=_requests.exceptions.ConnectionError):
        r = client.post("/weather/refresh")
    check("network error -> 200 (no crash)", r.status_code == 200)
    check("network error -> used_fallback True", r.json()["used_fallback"] is True)


def test_network_import():
    n = 3
    payload = {
        "nodes": [
            {"id": "T1", "lat": 14.20, "lon": 121.15},
            {"id": "T2", "lat": 14.21, "lon": 121.16},
            {"id": "T3", "lat": 14.22, "lon": 121.17},
        ],
        "segments": [
            {"id": "T1T2", "from_node": "T1", "to_node": "T2", "distance_km": 1.2},
            {"id": "T2T3", "from_node": "T2", "to_node": "T3", "distance_km": 1.4},
        ],
        "source": "osm",
    }
    with patch("weather_service.requests.get", return_value=fake_open_meteo_response(n)):
        r = client.post("/network/import", json=payload)
    body = r.json()
    check("POST /network/import -> 200", r.status_code == 200)
    check("import loaded 3 nodes", body["nodes_loaded"] == 3)
    check("import loaded 2 segments", body["segments_loaded"] == 2)

    net = client.get("/network").json()
    check("network source now 'osm'", net["source"] == "osm")
    check("network reflects imported nodes", len(net["nodes"]) == 3)

    # restore demo network with a mocked fetch so later tests have familiar node ids
    demo_payload_n = 6
    from demo import build_sample_network
    fresh = build_sample_network()
    restore = {
        "nodes": [{"id": nid, "lat": n_.lat, "lon": n_.lon} for nid, n_ in fresh.nodes.items()],
        "segments": [
            {"id": s.id, "from_node": s.from_node, "to_node": s.to_node,
             "distance_km": s.distance_km, "is_elevated_or_bridge": s.is_elevated_or_bridge}
            for s in fresh.all_segments() if not s.id.endswith("_rev")
        ],
        "source": "demo",
    }
    with patch("weather_service.requests.get", return_value=fake_open_meteo_response(demo_payload_n)):
        client.post("/network/import", json=restore)


def test_route():
    r = client.post("/route", json={"start_node": "A", "goal_node": "D", "alpha": 1.5})
    body = r.json()
    check("POST /route -> 200", r.status_code == 200)
    check("route starts at A, ends at D", body["node_ids"][0] == "A" and body["node_ids"][-1] == "D")
    check("route has risk_band field", body["risk_band"] in ("green", "yellow", "red"))


def test_route_not_found():
    r = client.post("/route", json={"start_node": "A", "goal_node": "NOPE", "alpha": 1.0})
    check("POST /route unknown node -> 404", r.status_code == 404)


def test_route_compare():
    r = client.post("/route/compare", json={"start_node": "A", "goal_node": "D", "alpha": 1.5})
    body = r.json()
    check("POST /route/compare -> 200", r.status_code == 200)
    check("compare includes tradeoff_summary", len(body["tradeoff_summary"]) > 0)
    check("safest route risk <= fastest route risk",
          body["safest"]["aggregate_risk"] <= body["fastest"]["aggregate_risk"])


def test_reroute_check():
    route = client.post("/route", json={"start_node": "A", "goal_node": "D", "alpha": 0.0}).json()
    r = client.post("/reroute-check", json={
        "route_node_ids": route["node_ids"],
        "route_segment_ids": route["segment_ids"],
        "current_node": route["node_ids"][1],
        "goal_node": "D",
        "alpha": 1.5,
    })
    check("POST /reroute-check -> 200", r.status_code == 200)
    check("reroute-check payload has 'rerouted' bool", isinstance(r.json()["rerouted"], bool))


def test_stations():
    r = client.get("/weather/stations")
    body = r.json()
    check("GET /weather/stations -> 200", r.status_code == 200)
    check("stations list non-empty", len(body["stations"]) > 0)


def test_auto_poll_toggle():
    r = client.post("/weather/auto-poll", json={"enabled": False})
    check("POST /weather/auto-poll -> 200", r.status_code == 200)
    check("toggle response reflects disabled", r.json()["auto_poll_enabled"] is False)

    h = client.get("/health").json()
    check("health reflects auto_poll disabled", h["auto_poll_enabled"] is False)

    r2 = client.post("/weather/auto-poll", json={"enabled": True})
    check("re-enable auto-poll -> 200", r2.status_code == 200)
    check("toggle response reflects enabled", r2.json()["auto_poll_enabled"] is True)


def test_auto_poll_background_actually_fires():
    """Proves the scheduler isn't just a flag — it genuinely re-fetches
    weather on its own, on a timer, without any request from a client."""
    n = len(state.weather_client.query_points)
    original_interval = state.poll_interval_seconds
    runs_before = state.auto_poll_runs
    state.poll_interval_seconds = 0.15  # fast cadence just for this test
    state.auto_poll_enabled = True
    try:
        with patch("weather_service.requests.get", return_value=fake_open_meteo_response(n)):
            with TestClient(app):  # entering the context triggers app startup (lifespan)
                time.sleep(0.5)     # long enough for the 0.15s-interval loop to fire 2-3x
        check("background scheduler ran at least once on its own",
              state.auto_poll_runs > runs_before)
    finally:
        state.poll_interval_seconds = original_interval


def test_frontend_served():
    r = client.get("/")
    check("GET / serves frontend index -> 200", r.status_code == 200)
    check("index mentions STORMLINE", "STORMLINE" in r.text)


def test_geocode_success():
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "results": [
            {
                "name": "Calamba",
                "latitude": 14.2117,
                "longitude": 121.1653,
                "country": "Philippines",
                "admin1": "Calabarzon",
                "country_code": "PH",
            }
        ]
    }
    with patch("weather_service.requests.get", return_value=fake_resp):
        r = client.get("/geocode?q=Calamba")
    body = r.json()
    check("GET /geocode -> 200", r.status_code == 200)
    check("geocode returns query echo", body["query"] == "Calamba")
    check("geocode returns at least 1 result", len(body["results"]) >= 1)
    check("first result has correct name", body["results"][0]["name"] == "Calamba")
    check("first result has latitude", abs(body["results"][0]["latitude"] - 14.2117) < 0.01)


def test_geocode_empty_query():
    r = client.get("/geocode?q=")
    check("GET /geocode empty query -> 400", r.status_code == 400)
    r2 = client.get("/geocode")
    check("GET /geocode missing query -> 400", r2.status_code == 400)


def test_geocode_service_down():
    import requests as _requests
    with patch("weather_service.requests.get", side_effect=_requests.exceptions.ConnectionError):
        r = client.get("/geocode?q=Manila")
    check("geocode service down -> 502", r.status_code == 502)
    check("502 detail mentions unreachable", "unreachable" in r.json()["detail"].lower())


if __name__ == "__main__":
    for fn in [
        test_health, test_network, test_heatmap, test_weather_refresh_and_outage,
        test_weather_network_failure_triggers_fallback,
        test_route, test_route_not_found, test_route_compare, test_reroute_check,
        test_network_import, test_stations, test_auto_poll_toggle,
        test_auto_poll_background_actually_fires, test_frontend_served,
        test_geocode_success, test_geocode_empty_query, test_geocode_service_down,
    ]:
        print(f"\n{fn.__name__}")
        fn()

    print(f"\n{'='*40}\n{passed} passed, {failed} failed\n{'='*40}")
    raise SystemExit(1 if failed else 0)
