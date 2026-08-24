# Weather-Hazard Risk Route Assessor — Backend Engine

Backend risk-scoring + routing engine implementing the core algorithms from
_Localized Weather-Induced Road Hazard Risk Assessor with Alternative
Routing_ (System Proposal v1.0, Aug 2026). Pure Python 3, no external
dependencies — designed to sit behind a REST/GraphQL API and a map frontend
you build separately (or ask me for next).

## Files

| File                 | Responsibility                                                                                      | Spec sections                       |
| -------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `models.py`          | `Node`, `RoadSegment`, `WeatherReading`, haversine distance                                         | 2.1, FR-2.1                         |
| `risk_engine.py`     | Geofencing weather→segments, `Rsegment` formula, color bands, hazard threshold, historical fallback | FR-2.2, FR-3.1–3.3, FR-1.2, NFR-2.1 |
| `routing.py`         | Risk-weighted A\* pathing, mid-transit reroute check, trade-off summary                             | FR-4.1–4.3, NFR-1.1                 |
| `weather_service.py` | Mock weather API client (swap for a real provider), outage simulation                               | FR-2.1, NFR-2.1                     |
| `demo.py`            | Runnable end-to-end walkthrough on a synthetic 6-node network                                       | 2.1 workflow                        |
| `tests.py`           | Sanity tests (no pytest needed): `python3 tests.py`                                                 | —                                   |

## Run it

```bash
python3 demo.py      # end-to-end scenario with a simulated storm
python3 tests.py     # 13 sanity checks
```

## How the pieces fit together

```
WeatherAPIClient.fetch_readings()
        │  (list[WeatherReading], or None on outage → NFR-2.1 fallback)
        ▼
RiskEngine.geofence_weather_to_segments()   # FR-2.2, ≤1km radius
        │  attaches precipitation/visibility/wind to each RoadSegment
        ▼
RiskEngine.score_all()                      # FR-3.2
        │  Rsegment ∈ [0,100] per segment, cached on segment.risk_score
        ▼
RoadNetwork.find_route(start, goal, alpha)  # FR-4.1
        │  A* using Wcost = distance × (1 + alpha·Rsegment/100)
        ▼
RouteResult  →  RoadNetwork.describe_tradeoff()   # FR-4.3
        │
        ▼ (periodically, while driving)
RoadNetwork.check_and_reroute()             # FR-4.2 / FR-3.3
```

## Key design decisions worth knowing about

1. **Formula reconciliation.** The proposal's formula
   `Rsegment = w1·Prain + w2·Vvis + w3·Sslope + w4·Fhistory` doesn't include
   a term for the "Vehicle Stability / crosswind" hazard that FR-3.1
   separately requires. `risk_engine.py` adds an _optional_ 5th weighted
   term (`w_wind`, default `0`) so the engine reproduces the spec's formula
   exactly out of the box, but can satisfy FR-3.1 fully by setting
   `RiskWeights(w_wind=0.10)` (see `demo.py`). All sub-scores are normalized
   to `[0,1]` before weighting so results stay bounded to `[0,100]`.

2. **Threshold unification.** FR-1.2 expresses bands as fractions
   (`R < 0.3`, etc.) while FR-3.2/3.3 define `Rsegment ∈ [0,100]` with a
   trigger at `≥ 70`. The engine standardizes everything on the `[0,100]`
   scale (green `<30`, yellow `30–70`, red `≥70`) so there's one source of
   truth.

3. **A\* admissibility.** The heuristic is straight-line (haversine)
   distance to the goal. Since `Wcost ≥ distance` always (risk penalty is
   strictly additive, `alpha ≥ 0`), the heuristic never overestimates —
   the search is guaranteed optimal and won't miss a safer path.

4. **`alpha` is the user's risk-avoidance dial** (FR-4.1's α). `0` = pure
   shortest-path/ETA routing; higher values increasingly refuse to route
   through risky segments even at a time cost. Expose this as a user
   preference/slider in the frontend.

5. **NFR-2.1 fallback** is explicit and testable: `weather_data_stale` is
   set per-segment during geofencing, and `apply_historical_fallback()`
   backfills a conservative estimate from `historical_flood_index` so
   routing never crashes or blocks when live data is missing — it degrades
   gracefully instead.

## REST API

`main.py` wraps the engine in a FastAPI app.

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# interactive docs: http://127.0.0.1:8000/docs
```

| Endpoint             | Method | Spec section            | Purpose                                                                      |
| -------------------- | ------ | ----------------------- | ---------------------------------------------------------------------------- |
| `/health`            | GET    | FR-2.1                  | liveness + scheduler status (next refresh, auto-poll on/off, run count)      |
| `/network`           | GET    | FR-1.1                  | nodes + segments (with current risk) for a map frontend                      |
| `/risk-heatmap`      | GET    | FR-1.2                  | per-segment risk score/band/hazard flag                                      |
| `/weather/refresh`   | POST   | FR-2.1, FR-3.2, NFR-2.1 | trigger one live Open-Meteo ingest→geofence→fallback→score cycle             |
| `/weather/auto-poll` | POST   | FR-2.1                  | pause/resume the background 5-minute scheduler                               |
| `/weather/outage`    | POST   | NFR-2.1                 | demo hook: force the weather client to simulate an outage                    |
| `/weather/stations`  | GET    | FR-2.1                  | raw live Open-Meteo readings per road node                                   |
| `/network/import`    | POST   | FR-2.2                  | replace the active network with real nodes/segments (e.g. from OSM/Overpass) |
| `/route`             | POST   | FR-4.1                  | `{start_node, goal_node, alpha}` → single risk-weighted route                |
| `/route/compare`     | POST   | FR-4.3                  | fastest vs. safest route + trade-off sentence                                |
| `/reroute-check`     | POST   | FR-4.2, FR-3.3          | mid-transit hazard check → new route if triggered                            |

State is a single in-memory `AppState` (`state.py`) seeded from
`demo.py`'s sample network — fine for local dev/demo, not for multiple
concurrent processes. In production, back `/weather/refresh` with a
scheduler (APScheduler/Celery beat) hitting it every 300s instead of
calling it on-request, and move the network/segment data into Postgres
(PostGIS for geometry) so state survives restarts and scales horizontally.

Test the API without running a server: `python3 test_api.py` (20 checks,
uses FastAPI's `TestClient`).

## Frontend — STORMLINE console

`frontend/` is a static console UI (vanilla HTML/CSS/JS, no build step)
served directly by the FastAPI app.

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# open http://127.0.0.1:8000
```

**Two modes, one app.** The console opens in **Simple mode** by default —
built for someone who just wants to know "is it safe to drive right now,"
with no jargon. Flip the switch in the top-right to **Advanced mode** for
the full technical console (risk scores, the risk-aversion slider,
station-by-station readings, event log, manual reroute checks, etc.).
Both modes share the same live data and the same map underneath.

**Simple mode:**

- One button — **"📍 Use my location"** — centers the map on you, pulls
  real roads for that area, and sets your origin automatically. Tap the
  map once more to set your destination (or use the dropdowns).
- **"Find the safest route"** gives a single plain-language answer:
  _"You're good to go"_, _"Take the alternate route — it avoids flooding
  on your usual road"_, or _"Not safe to drive right now"_ — with the
  drive time and distance for whichever route it's recommending. No risk
  scores, no jargon, up front. A "See technical details" link is there
  for anyone curious enough to expand it.
- The map is still visible and still color-coded (green/yellow/red), so
  even without reading anything you can see which roads look risky.

**Advanced mode** (same as before, plus scheduler controls):

- Real map, real roads, real weather (see below)
- Risk-aversion slider, manual route compare/reroute-check, station list,
  activity log
- **Auto-refresh weather** toggle + a live countdown to the next automatic
  check — the backend polls Open-Meteo every 5 minutes on its own (see
  "Automatic weather polling" below); this just lets you watch it happen
  or pause it for a demo

**Real map, real roads, real weather.** This runs against actual data:

- **Map & roads**: a Leaflet map with CARTO's dark basemap (styled to fit
  the console), defaulting to Calamba, Laguna, PH. On load it automatically
  tries to pull real OpenStreetMap roads for that default view; click
  **"Load real roads in view"** (Advanced mode) or **"Use my location"**
  (Simple mode) to do the same for any other area. Under the hood, the
  frontend queries the [Overpass API](https://overpass-api.de) directly
  from your browser for every drivable OpenStreetMap way in the current
  map view, converts it into real nodes/segments (with real distances via
  the haversine formula, and real `oneway`/`bridge` tags), and posts it to
  `POST /network/import` on the backend — which replaces the network and
  immediately re-scores it. Zoom to a few blocks before loading; Overpass
  will time out on very large areas (the UI warns you above ~25 km²).
- **Weather**: the backend calls the real [Open-Meteo Forecast API](https://open-meteo.com)
  (no API key needed) for every road node in a single batched request,
  pulling live precipitation, visibility, wind speed, and temperature.
  This is what actually drives the risk scores you see — it's not
  simulated.

**Automatic weather polling (FR-2.1).** The backend doesn't wait for
anyone to click a button — see "Scheduled polling" below.

**Design direction:** dark storm-radar/instrument-panel styling (rotating
logo mark, scanline texture, hazard segments that pulse) layered over a
real basemap, rather than a stock light Google-Maps look. Simple mode
keeps that same visual identity but leads with one big, friendly,
plain-language answer instead of the technical console.

If you'd rather not depend on the public Overpass/Open-Meteo servers
(rate limits, outages), the app still falls back to the small synthetic
demo network from `demo.py` at startup, so it's never empty.

## Scheduled polling (FR-2.1)

The backend polls Open-Meteo automatically every 5 minutes (`poll_interval_seconds`,
default 300s) via an `asyncio` background task started in `main.py`'s
FastAPI `lifespan` — not a cron job or an external scheduler, so it runs
for as long as `uvicorn` is running, no extra setup needed. It calls the
exact same `state.refresh_weather()` the manual "Poll weather now" button
uses, so nothing behaves differently between an automatic and a manual
refresh.

- `GET /health` reports `auto_poll_enabled`, `poll_interval_seconds`,
  `seconds_until_next_refresh`, and how many times the scheduler has fired
  (`auto_poll_runs`).
- `POST /weather/auto-poll {"enabled": false}` pauses it (e.g. for a demo);
  `{"enabled": true}` resumes it. The manual refresh button still works
  either way.
- The frontend polls `/health` every few seconds to show a live countdown
  and to detect when the _backend_ refreshed on its own — when that
  happens it quietly re-pulls the network/stations data, so what you see
  on screen updates without you doing anything.
- `test_api.py` includes a test (`test_auto_poll_background_actually_fires`)
  that actually shrinks the interval to 0.15s, waits, and asserts the
  scheduler fired on its own — proving this isn't just a flag that does
  nothing.

For a production deployment behind a real datastore, swap this for
APScheduler with a persistent job store or Celery beat instead of the
in-process asyncio task, so polling survives process restarts and works
across multiple worker processes.

## Not yet built (natural next steps)

- **Location search** — Simple mode's geolocation button covers "where am
  I," but there's no way yet to type a destination by name. Open-Meteo's
  free geocoding API (`geocoding-api.open-meteo.com`) would add that.
- **Persistence** for imported road networks + `historical_flood_index`
  (currently in-memory — re-importing is needed after a restart; move to
  Postgres+PostGIS in production).
- **Durable scheduler** — swap the in-process asyncio task for APScheduler
  or Celery beat if you run multiple worker processes or need polling to
  survive a restart mid-cycle.
- Turn-by-turn voice alerts (FR-5.1), hands-free confirmation mode (FR-5.2).
- **Auth/rate limiting** before exposing this beyond localhost — and note
  Overpass/Open-Meteo's usage policies if this goes beyond personal use.

Happy to build any of these next — just say which.
