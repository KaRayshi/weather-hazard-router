// STORMLINE console — talks to the FastAPI backend (main.py) for
// risk/routing, and directly to two free, no-key public APIs from the
// browser: Overpass (real OpenStreetMap road geometry) and — indirectly,
// via the backend — Open-Meteo (real live weather).

const API_BASE = location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
const HAZARD_THRESHOLD = 70; // mirrors risk_engine.HAZARD_WARNING_THRESHOLD
const OVERPASS_URL = "https://overpass-api.de/api/interpreter";
const DEFAULT_CENTER = [14.2117, 121.1653]; // Calamba, Laguna, PH
const DEFAULT_ZOOM = 15;
const MAX_SEGMENTS_IMPORTED = 6000; // keep imports + rendering snappy
const MAX_SNAP_DISTANCE_KM = 0.35; // don't silently snap a tap to a far-away road

let startPinLatLng = null; // user's actual click/search coordinates (for pin placement)
let goalPinLatLng = null;  // routing still uses the snapped node ID

// Origin and destination are often picked from two different map views
// (tap origin, pan, tap destination). Loading roads per-viewport alone
// leaves two disconnected "islands" with nothing joining them, which is
// the #1 real-world cause of "couldn't find a route". Before routing, we
// try to auto-load whatever's in the corridor between the two picked
// points. This cap is bigger than the single-viewport cap (MAX area a
// "load roads in view" click will accept) because a corridor between two
// real points is inherently bigger than one screen — but still bounded so
// Overpass doesn't time out on a genuinely cross-country request.
const MAX_CORRIDOR_AREA_KM2 = 250;
const CORRIDOR_PADDING_KM = 1.0;

const BAND_WORDS = { green: "Clear", yellow: "Caution", red: "Danger" };
const BAND_ICON = { green: "✅", yellow: "⚠️", red: "⛔" };

const els = {
  apiStatus: document.getElementById("apiStatus"),
  networkSource: document.getElementById("networkSource"),
  nextRefresh: document.getElementById("nextRefresh"),
  clock: document.getElementById("clock"),
  modeSwitch: document.getElementById("modeSwitch"),
  modeToggleLabel: document.getElementById("modeToggleLabel"),
  brandSub: document.getElementById("brandSub"),

  btnUseLocation: document.getElementById("btnUseLocation"),
  originStatus: document.getElementById("originStatus"),
  destStatus: document.getElementById("destStatus"),
  btnFindRoute: document.getElementById("btnFindRoute"),
  btnStartOver: document.getElementById("btnStartOver"),
  roadLoadNote: document.getElementById("roadLoadNote"),

  verdictCard: document.getElementById("verdictCard"),
  verdictIcon: document.getElementById("verdictIcon"),
  verdictHeadline: document.getElementById("verdictHeadline"),
  verdictDetail: document.getElementById("verdictDetail"),
  verdictEta: document.getElementById("verdictEta"),
  verdictDist: document.getElementById("verdictDist"),
  verdictTechBody: document.getElementById("verdictTechBody"),

  startNode: document.getElementById("startNode"),
  goalNode: document.getElementById("goalNode"),
  alphaSlider: document.getElementById("alphaSlider"),
  alphaValue: document.getElementById("alphaValue"),
  btnCompare: document.getElementById("btnCompare"),
  btnReroute: document.getElementById("btnReroute"),
  btnRefresh: document.getElementById("btnRefresh"),
  btnOutage: document.getElementById("btnOutage"),
  btnStorm: document.getElementById("btnStorm"),
  btnLoadRoads: document.getElementById("btnLoadRoads"),
  osmNote: document.getElementById("osmNote"),
  fallbackNote: document.getElementById("fallbackNote"),
  stationList: document.getElementById("stationList"),
  eventLog: document.getElementById("eventLog"),
  resultsPanel: document.getElementById("resultsPanel"),

  autoPollToggle: document.getElementById("autoPollToggle"),
  autoPollCountdown: document.getElementById("autoPollCountdown"),
  pollIntervalLabel: document.getElementById("pollIntervalLabel"),

  // geocoding search inputs
  originSearchInput: document.getElementById("originSearchInput"),
  originSearchResults: document.getElementById("originSearchResults"),
  destSearchInput: document.getElementById("destSearchInput"),
  destSearchResults: document.getElementById("destSearchResults"),
  advSearchInput: document.getElementById("advSearchInput"),
  advSearchResults: document.getElementById("advSearchResults"),
  btnIgnoreOneway: document.getElementById("ignoreOneway"),
  advIgnoreOneway: document.getElementById("advIgnoreOneway"),
  btnAdvUseLocation: document.getElementById("btnAdvUseLocation"),
};

let currentNetwork = null; // last /network payload
let outageActive = false;
let stormActive = false;
let lastFastRoute = null; // for the reroute-check demo
let pickMode = "start"; // which end the next map click sets
let lastKnownRefreshTs = null;

// -------------------------------------------------------------- Leaflet map

const map = L.map("map", { zoomControl: true }).setView(
  DEFAULT_CENTER,
  DEFAULT_ZOOM,
);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  maxZoom: 19,
}).addTo(map);

const segmentLayer = L.layerGroup().addTo(map);
const stationLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const pinLayer = L.layerGroup().addTo(map);

map.on("click", (e) => onMapClick(e.latlng));

// The map container changes size when switching Simple/Advanced (the
// layout is different in each) or when the window resizes. Leaflet only
// measures its container once, so re-measure whenever it changes —
// otherwise tiles render in just part of the map.
if ("ResizeObserver" in window) {
  new ResizeObserver(() => map.invalidateSize()).observe(
    document.getElementById("map"),
  );
}

// -------------------------------------------------------------- utils

async function api(path, options) {
  const res = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function logEvent(text, level = "") {
  const div = document.createElement("div");
  div.className = "evt" + (level ? ` evt--${level}` : "");
  const t = new Date().toLocaleTimeString([], { hour12: false });
  div.textContent = `${t}  ${text}`;
  els.eventLog.prepend(div);
  while (els.eventLog.children.length > 40)
    els.eventLog.removeChild(els.eventLog.lastChild);
}

function setLinkStatus(ok) {
  els.apiStatus.innerHTML = ok
    ? `<span class="dot dot--ok"></span> connected`
    : `<span class="dot dot--fail"></span> unreachable`;
}

function bandOf(score) {
  if (score < 30) return "green";
  if (score < 70) return "yellow";
  return "red";
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0088;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1),
    dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// -------------------------------------------------------------- mode toggle

function setMode(simple) {
  document.body.classList.toggle("mode-simple", simple);
  document.body.classList.toggle("mode-advanced", !simple);
  els.modeToggleLabel.textContent = simple ? "Simple" : "Advanced";
  els.brandSub.textContent = simple
    ? "is it safe to drive right now?"
    : "weather hazard route console";
  els.modeSwitch.checked = !simple;
}
els.modeSwitch.addEventListener("change", () =>
  setMode(!els.modeSwitch.checked),
);
setMode(true);

// -------------------------------------------------------------- clock

function tickClock() {
  els.clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
}
setInterval(tickClock, 1000);
tickClock();

// -------------------------------------------------------------- OSM road loading (Overpass)

const DRIVABLE_HIGHWAYS =
  "motorway|trunk|primary|secondary|tertiary|unclassified|residential|" +
  "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|" +
  "living_street|service|road";

async function loadRoadsInView({
  silent = false,
  boundsOverride = null,
  areaCapKm2 = 50,
} = {}) {
  let south, west, north, east;
  if (boundsOverride) {
    ({ south, west, north, east } = boundsOverride);
  } else {
    const b = map.getBounds();
    south = b.getSouth();
    west = b.getWest();
    north = b.getNorth();
    east = b.getEast();
  }
  const bbox = `${south},${west},${north},${east}`;
  const areaKm2 =
    haversineKm(south, west, north, west) *
    haversineKm(south, west, south, east);

  if (areaKm2 > areaCapKm2) {
    logEvent(
      `View is ~${areaKm2.toFixed(1)} km² — zoom in closer (a few blocks) before loading, or Overpass will time out.`,
      "warn",
    );
    return false;
  }

  const query = `[out:json][timeout:25];(way["highway"~"^(${DRIVABLE_HIGHWAYS})$"](${bbox}););out geom;`;

  if (!silent) {
    els.btnLoadRoads.disabled = true;
    els.btnLoadRoads.textContent = "Loading from OpenStreetMap…";
  }
  els.roadLoadNote.hidden = false;
  els.osmNote.hidden = true;

  try {
    const resp = await fetch(OVERPASS_URL, {
      method: "POST",
      body: "data=" + encodeURIComponent(query),
    });
    if (!resp.ok) throw new Error(`Overpass returned ${resp.status}`);
    const data = await resp.json();

    const { nodes, segments } = parseOverpassWays(data.elements || []);
    if (!nodes.length || !segments.length) {
      logEvent(
        "No drivable roads found in this view. Try a different area.",
        "warn",
      );
      return false;
    }

    // If there's too much to import, drop whole streets from the end
    // rather than slicing the segment array arbitrarily — cutting a
    // street mid-way is exactly what creates disconnected "islands" that
    // can never be routed between.
    let keptSegments = segments;
    if (segments.length > MAX_SEGMENTS_IMPORTED) {
      const keptWayIds = new Set();
      keptSegments = [];
      for (const s of segments) {
        const wayId = s.id.split("_")[0];
        if (
          keptSegments.length >= MAX_SEGMENTS_IMPORTED &&
          !keptWayIds.has(wayId)
        )
          break;
        keptWayIds.add(wayId);
        keptSegments.push(s);
      }
      logEvent(
        `Area has a lot of roads — kept ${keptSegments.length} of ${segments.length} segments (whole streets, not cut mid-way). Zoom in tighter for full coverage.`,
        "warn",
      );
    }

    const payload = {
      nodes: nodes.map((n) => ({ id: n.id, lat: n.lat, lon: n.lon })),
      segments: keptSegments.map((s) => ({
        id: s.id,
        from_node: s.from,
        to_node: s.to,
        distance_km: s.distance_km,
        is_elevated_or_bridge: s.bridge,
      })),
      source: "osm",
    };

    const result = await api("/network/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    logEvent(result.message, result.used_fallback ? "warn" : "ok");
    els.osmNote.hidden = false;
    els.osmNote.textContent = `Loaded ${result.nodes_loaded} real intersections / ${result.segments_loaded} road segments from OpenStreetMap.`;

    await refreshAll();
    return true;
  } catch (e) {
    logEvent(`Failed to load roads from OpenStreetMap: ${e.message}`, "danger");
    return false;
  } finally {
    els.btnLoadRoads.disabled = false;
    els.btnLoadRoads.textContent = "Load real roads in view";
    els.roadLoadNote.hidden = true;
  }
}

function parseOverpassWays(elements) {
  const nodesById = new Map();
  const segments = [];

  for (const way of elements) {
    if (way.type !== "way" || !way.geometry || !way.nodes) continue;
    const bridge =
      way.tags && (way.tags.bridge === "yes" || way.tags.man_made === "bridge");
    const oneway =
      way.tags && (way.tags.oneway === "yes" || way.tags.oneway === "1");

    const wayNodeIds = way.nodes.map((osmId) => `n${osmId}`);
    way.geometry.forEach((pt, i) => {
      const id = wayNodeIds[i];
      if (id && !nodesById.has(id))
        nodesById.set(id, { id, lat: pt.lat, lon: pt.lon });
    });

    for (let i = 0; i < wayNodeIds.length - 1; i++) {
      const a = nodesById.get(wayNodeIds[i]);
      const b = nodesById.get(wayNodeIds[i + 1]);
      if (!a || !b) continue;
      const dist = haversineKm(a.lat, a.lon, b.lat, b.lon);
      if (dist <= 0) continue;

      segments.push({
        id: `${way.id}_${i}_f`,
        from: a.id,
        to: b.id,
        distance_km: dist,
        bridge,
      });
      if (!oneway) {
        segments.push({
          id: `${way.id}_${i}_r`,
          from: b.id,
          to: a.id,
          distance_km: dist,
          bridge,
        });
      }
    }
  }

  return { nodes: Array.from(nodesById.values()), segments };
}

// -------------------------------------------------------------- "Use my location"

function useMyLocation() {
  if (!navigator.geolocation) {
    logEvent(
      "This browser doesn't support location — tap the map instead.",
      "warn",
    );
    return;
  }
  els.btnUseLocation.disabled = true;
  els.btnUseLocation.textContent = "Locating…";
  if (els.btnAdvUseLocation) {
    els.btnAdvUseLocation.disabled = true;
    els.btnAdvUseLocation.textContent = "Locating…";
  }

  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      const { latitude, longitude } = pos.coords;
      map.setView([latitude, longitude], 16);
      els.btnUseLocation.textContent = "📍 Use my location";
      els.btnUseLocation.disabled = false;
      if (els.btnAdvUseLocation) {
        els.btnAdvUseLocation.textContent = "📍 Use my location";
        els.btnAdvUseLocation.disabled = false;
      }

      const loaded = await loadRoadsInView();
      if (loaded && currentNetwork) {
        const hit = nearestNode(L.latLng(latitude, longitude));
        if (hit) {
          const n = hit.node;
          els.startNode.value = n.id;
          startPinLatLng = [latitude, longitude];
          pickMode = "goal";
          updatePinStatuses();
          renderPins();
          logEvent("Origin set to your current location.");
        }
      }
    },
    (err) => {
      els.btnUseLocation.textContent = "📍 Use my location";
      els.btnUseLocation.disabled = false;
      if (els.btnAdvUseLocation) {
        els.btnAdvUseLocation.textContent = "📍 Use my location";
        els.btnAdvUseLocation.disabled = false;
      }
      logEvent(
        `Couldn't get your location (${err.message}). Tap the map instead.`,
        "warn",
      );
    },
    { timeout: 10000 },
  );
}
els.btnUseLocation.addEventListener("click", useMyLocation);
if (els.btnAdvUseLocation) els.btnAdvUseLocation.addEventListener("click", useMyLocation);

// -------------------------------------------------------------- map rendering

function dedupeSegments(segments) {
  const dirMap = new Set();
  for (const s of segments) {
    dirMap.add(`${s.from_node}|${s.to_node}`);
  }

  const seen = new Set();
  const out = [];
  for (const s of segments) {
    const key = [s.from_node, s.to_node].sort().join("|");
    if (seen.has(key)) continue;
    seen.add(key);

    const hasRev = dirMap.has(`${s.to_node}|${s.from_node}`);
    s.isOneway = !hasRev;
    out.push(s);
  }
  return out;
}

function getBearing(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const l1 = lat1 * Math.PI / 180;
  const l2 = lat2 * Math.PI / 180;
  const y = Math.sin(dLon) * Math.cos(l2);
  const x = Math.cos(l1) * Math.sin(l2) - Math.sin(l1) * Math.cos(l2) * Math.cos(dLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function renderNetwork(data) {
  currentNetwork = data;
  segmentLayer.clearLayers();

  const nodesById = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
  const segs = dedupeSegments(data.segments);

  for (const s of segs) {
    const a = nodesById[s.from_node],
      b = nodesById[s.to_node];
    if (!a || !b) continue;
    const band = bandOf(s.risk_score);
    const isHazard = s.risk_score >= HAZARD_THRESHOLD;

    let cls = `seg-line seg-line--${band}`;
    if (isHazard) cls += " seg-line--hazard";

    const line = L.polyline(
      [
        [a.lat, a.lon],
        [b.lat, b.lon],
      ],
      {
        color: "#888",
        weight: 6,
        opacity: 0.9,
        className: cls,
      },
    );
    const riskWord = { green: "Low", yellow: "Moderate", red: "Critical" }[band];
    const speedNow = Math.round(s.traffic_speed_kmh || 50);
    const tooltipHtml =
      `<div class="tip-main">${riskWord} weather risk${isHazard ? " — hazard" : ""}${s.isOneway ? " · one-way" : ""}</div>` +
      `<div>Traffic moving at about ${speedNow} km/h</div>` +
      `<div class="tip-tech">${s.id} · R=${s.risk_score}</div>`;
    line.bindTooltip(tooltipHtml, { sticky: true });
    line.addTo(segmentLayer);

    // FR-1.1 Traffic Overlay (inner line)
    const speed = s.traffic_speed_kmh || 50;
    let trafficColor = "#44cc44"; // green / free flowing
    if (speed < 20) trafficColor = "#ff4444"; // red / congested
    else if (speed < 40) trafficColor = "#ffcc00"; // yellow / moderate

    const trafficLine = L.polyline(
      [
        [a.lat, a.lon],
        [b.lat, b.lon],
      ],
      {
        color: trafficColor,
        weight: 2,
        opacity: 0.9,
        interactive: false, // let tooltip trigger on the thicker hazard line
      },
    );
    trafficLine.addTo(segmentLayer);

    if (s.isOneway) {
      const midLat = (a.lat + b.lat) / 2;
      const midLon = (a.lon + b.lon) / 2;
      const bearing = getBearing(a.lat, a.lon, b.lat, b.lon);
      const arrowIcon = L.divIcon({
        className: "oneway-arrow",
        html: `<div style="transform: rotate(${bearing}deg); font-size: 14px; font-weight: bold; color: rgba(255,255,255,0.7); text-align: center; line-height: 1;">↑</div>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      });
      L.marker([midLat, midLon], { icon: arrowIcon, interactive: false }).addTo(segmentLayer);
    }
  }

  els.networkSource.textContent =
    data.source === "osm" ? "OpenStreetMap (live)" : "demo network";

  if (segs.length && data.source === "osm") {
    const bounds = L.latLngBounds(
      segs.flatMap((s) => {
        const a = nodesById[s.from_node],
          b = nodesById[s.to_node];
        return a && b
          ? [
              [a.lat, a.lon],
              [b.lat, b.lon],
            ]
          : [];
      }),
    );
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
  }
}

function renderPins() {
  pinLayer.clearLayers();
  if (!currentNetwork) return;
  const nodesById = Object.fromEntries(
    currentNetwork.nodes.map((n) => [n.id, n]),
  );

  const startId = els.startNode.value,
    goalId = els.goalNode.value;
  if (startId && nodesById[startId]) {
    // Use user's actual click/search coordinates when available,
    // so the pin appears exactly where the user pointed — not at
    // the nearest graph node which may be noticeably offset.
    const pos = startPinLatLng || [nodesById[startId].lat, nodesById[startId].lon];
    const marker = L.marker(pos, {
      icon: L.divIcon({
        className: "pin-marker pin-marker--start",
        iconSize: [16, 16],
      }),
    })
      .bindTooltip("Origin (click to remove)")
      .on("click", () => {
        els.startNode.value = "";
        startPinLatLng = null;
        pickMode = "start";
        _lastRouteData = null;
        updatePinStatuses();
        renderPins();
        routeLayer.clearLayers();
        els.verdictCard.hidden = true;
        els.resultsPanel.hidden = true;
        logEvent("Origin removed.");
      })
      .addTo(pinLayer);
  }
  if (goalId && nodesById[goalId]) {
    const pos = goalPinLatLng || [nodesById[goalId].lat, nodesById[goalId].lon];
    const marker = L.marker(pos, {
      icon: L.divIcon({
        className: "pin-marker pin-marker--goal",
        iconSize: [16, 16],
      }),
    })
      .bindTooltip("Destination (click to remove)")
      .on("click", () => {
        els.goalNode.value = "";
        goalPinLatLng = null;
        if (!els.startNode.value) pickMode = "start";
        _lastRouteData = null;
        updatePinStatuses();
        renderPins();
        routeLayer.clearLayers();
        els.verdictCard.hidden = true;
        els.resultsPanel.hidden = true;
        logEvent("Destination removed.");
      })
      .addTo(pinLayer);
  }
}

function nearestNode(latlng) {
  if (!currentNetwork || !currentNetwork.nodes.length) return null;
  let best = null,
    bestDist = Infinity;
  for (const n of currentNetwork.nodes) {
    const d = haversineKm(latlng.lat, latlng.lng, n.lat, n.lon);
    if (d < bestDist) {
      bestDist = d;
      best = n;
    }
  }
  return best ? { node: best, distanceKm: bestDist } : null;
}

/** Bounding box covering all given {lat, lon} points, padded outward by
 * paddingKm on every side. */
function boundsForLatLngs(points, paddingKm) {
  let south = Infinity,
    west = Infinity,
    north = -Infinity,
    east = -Infinity;
  for (const p of points) {
    south = Math.min(south, p.lat);
    north = Math.max(north, p.lat);
    west = Math.min(west, p.lon);
    east = Math.max(east, p.lon);
  }
  const midLat = (south + north) / 2;
  const latPad = paddingKm / 111;
  const lonPad = paddingKm / (111 * Math.cos((midLat * Math.PI) / 180));
  return {
    south: south - latPad,
    west: west - lonPad,
    north: north + latPad,
    east: east + lonPad,
  };
}

/** Best-effort: make sure the roads *between* the origin and destination
 * are actually loaded, not just the two small areas around each tap.
 * This is what lets someone plan a real trip across town rather than
 * only ever routing within a single screenful of map. If the two points
 * are too far apart to safely auto-fetch (Overpass would likely time
 * out), this logs a warning instead of silently failing later. */
async function ensureCorridorLoaded(originId, goalId) {
  if (!currentNetwork) return;
  const originNode = currentNetwork.nodes.find((n) => n.id === originId);
  const goalNode = currentNetwork.nodes.find((n) => n.id === goalId);
  if (!originNode || !goalNode) return;

  const bounds = boundsForLatLngs(
    [
      { lat: originNode.lat, lon: originNode.lon },
      { lat: goalNode.lat, lon: goalNode.lon },
    ],
    CORRIDOR_PADDING_KM,
  );
  const areaKm2 =
    haversineKm(bounds.south, bounds.west, bounds.north, bounds.west) *
    haversineKm(bounds.south, bounds.west, bounds.south, bounds.east);

  if (areaKm2 <= MAX_CORRIDOR_AREA_KM2) {
    // Small enough to load in one Overpass request
    logEvent("Loading the roads between your origin and destination…");
    await loadRoadsInView({
      silent: true,
      boundsOverride: bounds,
      areaCapKm2: MAX_CORRIDOR_AREA_KM2,
    });
    return;
  }

  // Corridor is large — break it into overlapping tiles along the
  // straight line from origin to destination so Overpass doesn't time
  // out on a single massive bbox. Each tile is ~MAX_CORRIDOR_AREA_KM2/2
  // and tiles overlap by CORRIDOR_PADDING_KM so roads at tile edges
  // stay connected.
  const TILE_SIDE_KM = 5.0; // ~25 km² per tile, well within Overpass limits
  const dLat = goalNode.lat - originNode.lat;
  const dLon = goalNode.lon - originNode.lon;
  const corridorLenKm = haversineKm(
    originNode.lat, originNode.lon, goalNode.lat, goalNode.lon,
  );
  const nTiles = Math.max(2, Math.ceil(corridorLenKm / (TILE_SIDE_KM * 0.5)));

  logEvent(
    `Long corridor (~${Math.round(corridorLenKm)} km) — loading roads in ${nTiles} tiles…`,
  );

  for (let i = 0; i < nTiles; i++) {
    const t = i / (nTiles - 1); // 0 … 1 along the corridor
    const cLat = originNode.lat + dLat * t;
    const cLon = originNode.lon + dLon * t;
    const tileBounds = boundsForLatLngs(
      [{ lat: cLat, lon: cLon }],
      TILE_SIDE_KM / 2 + CORRIDOR_PADDING_KM,
    );
    await loadRoadsInView({
      silent: true,
      boundsOverride: tileBounds,
      areaCapKm2: MAX_CORRIDOR_AREA_KM2,
    });
  }
}

function updatePinStatuses() {
  const hasStart = !!els.startNode.value;
  const hasGoal = !!els.goalNode.value;
  const sameNode =
    hasStart && hasGoal && els.startNode.value === els.goalNode.value;

  els.originStatus.textContent = hasStart
    ? `Origin set ✓`
    : "Waiting for origin…";
  els.originStatus.classList.toggle("is-set", hasStart);
  els.originStatus.classList.remove("is-warn");

  if (sameNode) {
    els.destStatus.textContent =
      "That's the same as your origin — tap a different spot for your destination";
    els.destStatus.classList.remove("is-set");
    els.destStatus.classList.add("is-warn");
  } else {
    els.destStatus.textContent = hasGoal
      ? `Destination set ✓`
      : "Then tap your destination on the map";
    els.destStatus.classList.toggle("is-set", hasGoal);
    els.destStatus.classList.remove("is-warn");
  }

  els.btnFindRoute.disabled = !(hasStart && hasGoal && !sameNode);
}

function resetTrip() {
  els.startNode.value = "";
  els.goalNode.value = "";
  startPinLatLng = null;
  goalPinLatLng = null;
  pickMode = "start";
  _lastRouteData = null;
  routeLayer.clearLayers();
  els.verdictCard.hidden = true;
  els.resultsPanel.hidden = true;
  els.btnStartOver.hidden = true;
  updatePinStatuses();
  renderPins();
  logEvent("Trip reset — tap a new origin on the map.");
}
els.btnStartOver.addEventListener("click", resetTrip);

// -------------------------------------------------------------- geocoding search

const GEOCODE_DEBOUNCE_MS = 300;
const geocodeTimers = {};

async function geocodeSearch(query) {
  const data = await api(`/geocode?q=${encodeURIComponent(query)}&count=5`);
  return data.results || [];
}

function setupSearchInput(inputEl, resultsEl, target) {
  // target: "start" | "goal" | "auto" (follows pickMode)
  if (!inputEl || !resultsEl) return;

  inputEl.addEventListener("input", () => {
    clearTimeout(geocodeTimers[inputEl.id]);
    const q = inputEl.value.trim();
    if (q.length < 2) {
      resultsEl.hidden = true;
      return;
    }
    geocodeTimers[inputEl.id] = setTimeout(async () => {
      try {
        const results = await geocodeSearch(q);
        renderSearchResults(resultsEl, results, inputEl, target);
      } catch {
        resultsEl.hidden = true;
      }
    }, GEOCODE_DEBOUNCE_MS);
  });

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      resultsEl.hidden = true;
      inputEl.blur();
    }
  });
}

function renderSearchResults(container, results, inputEl, target) {
  container.innerHTML = "";
  if (!results.length) {
    container.innerHTML = `<div class="search-no-results">No places found</div>`;
    container.hidden = false;
    return;
  }
  for (const r of results) {
    const div = document.createElement("div");
    div.className = "search-result-item";
    const sub = [r.admin1, r.country].filter(Boolean).join(", ");
    div.innerHTML =
      `<div class="search-result-name">${r.name}</div>` +
      (sub ? `<div class="search-result-sub">${sub}</div>` : "");
    div.addEventListener("click", () =>
      onSearchResultPick(r, inputEl, container, target),
    );
    container.appendChild(div);
  }
  container.hidden = false;
}

async function onSearchResultPick(result, inputEl, container, target) {
  container.hidden = true;
  inputEl.value = "";
  const { latitude: lat, longitude: lon, name } = result;

  map.setView([lat, lon], 16);
  logEvent(`Searched: ${name} — loading roads…`);

  await loadRoadsInView();

  const hit = nearestNode(L.latLng(lat, lon));
  if (!hit) {
    logEvent("No roads found near that location.", "warn");
    return;
  }

  const which = target === "auto" ? pickMode : target;
  if (which === "start") {
    els.startNode.value = hit.node.id;
    startPinLatLng = [lat, lon];
    pickMode = "goal";
    logEvent(`Origin set to ${name}.`);
  } else {
    els.goalNode.value = hit.node.id;
    goalPinLatLng = [lat, lon];
    pickMode = "start";
    logEvent(`Destination set to ${name}.`);
  }
  updatePinStatuses();
  renderPins();
}

// close search dropdowns on outside click
document.addEventListener("click", (e) => {
  document.querySelectorAll(".search-results").forEach((el) => {
    if (!el.parentElement.contains(e.target)) el.hidden = true;
  });
});

// wire up all three search inputs
setupSearchInput(els.originSearchInput, els.originSearchResults, "start");
setupSearchInput(els.destSearchInput, els.destSearchResults, "goal");
setupSearchInput(els.advSearchInput, els.advSearchResults, "auto");

async function onMapClick(latlng) {
  // If nothing's loaded near this tap yet, fetch real roads for that area
  // automatically instead of making you click "Load roads" first — this
  // is what lets you place a marker anywhere, not just where a colored
  // line already happens to be.
  let hit = nearestNode(latlng);
  if (!hit || hit.distanceKm > MAX_SNAP_DISTANCE_KM) {
    map.panTo(latlng);
    logEvent(
      "No roads loaded near that tap yet — fetching real roads for this area…",
    );
    await loadRoadsInView();
    hit = nearestNode(latlng);
    if (!hit || hit.distanceKm > MAX_SNAP_DISTANCE_KM) {
      logEvent(
        "Still couldn't find a road there — try zooming in a bit first, then tap again.",
        "warn",
      );
      return;
    }
  }
  const n = hit.node;

  if (pickMode === "start") {
    els.startNode.value = n.id;
    startPinLatLng = [latlng.lat, latlng.lng];
    pickMode = "goal";
    logEvent(`Origin set to nearest road (${n.id}).`);
  } else if (n.id === els.startNode.value) {
    logEvent(
      "That's the same as your origin — tap a different spot for your destination.",
      "warn",
    );
    updatePinStatuses();
    return;
  } else {
    els.goalNode.value = n.id;
    goalPinLatLng = [latlng.lat, latlng.lng];
    pickMode = "start";
    logEvent(`Destination set to nearest road (${n.id}).`);
  }
  updatePinStatuses();
  renderPins();
}

// -------------------------------------------------------------- data loads

async function loadNetwork() {
  const data = await api("/network");
  const prevStart = els.startNode.value,
    prevGoal = els.goalNode.value;

  // Raw OSM node IDs (e.g. "n82345671") mean nothing to a person — show a
  // short, friendly coordinate label instead. The real ID is still what's
  // sent to the backend (option value), this only changes what's displayed.
  const optionsHtml = data.nodes
    .map(
      (n) =>
        `<option value="${n.id}">≈ ${n.lat.toFixed(4)}, ${n.lon.toFixed(4)}</option>`,
    )
    .join("");

  els.startNode.innerHTML = `<option value="">—</option>` + optionsHtml;
  els.goalNode.innerHTML = `<option value="">—</option>` + optionsHtml;

  if (prevStart && data.nodes.some((n) => n.id === prevStart))
    els.startNode.value = prevStart;
  if (prevGoal && data.nodes.some((n) => n.id === prevGoal))
    els.goalNode.value = prevGoal;

  renderNetwork(data);
  renderPins();
  updatePinStatuses();
}

async function loadStations() {
  const data = await api("/weather/stations");
  els.fallbackNote.hidden = !data.simulate_outage;
  stationLayer.clearLayers();

  if (!data.stations.length) {
    els.stationList.innerHTML = `<p class="field-hint">No stations reporting.</p>`;
    return;
  }
  els.stationList.innerHTML =
    data.stations
      .slice(0, 12)
      .map(
        (s) => `
    <div class="station-card">
      <div class="station-name">${s.station_id}</div>
      <div class="station-metrics">
        <span>rain <b>${s.precipitation_mm_h.toFixed(1)}</b>mm/h</span>
        <span>vis <b>${s.visibility_m.toFixed(0)}</b>m</span>
        <span>wind <b>${s.wind_speed_kmh.toFixed(0)}</b>km/h</span>
      </div>
    </div>
  `,
      )
      .join("") +
    (data.stations.length > 12
      ? `<p class="field-hint">+${data.stations.length - 12} more…</p>`
      : "");

  for (const s of data.stations) {
    L.circleMarker([s.lat, s.lon], {
      radius: 5,
      className: "station-marker",
      weight: 2,
    })
      .bindTooltip(
        `${s.station_id}: ${s.precipitation_mm_h.toFixed(1)}mm/h rain, ${s.visibility_m.toFixed(0)}m vis, ${s.wind_speed_kmh.toFixed(0)}km/h wind`,
      )
      .addTo(stationLayer);
  }
}

async function refreshAll(logMsg) {
  await Promise.all([loadNetwork(), loadStations()]);
  if (logMsg) logEvent(logMsg);
  // Re-draw any active routes that were on the map before the refresh
  redrawLastRoutes();
}

// -------------------------------------------------------------- route actions

function fillRouteCard(prefix, route) {
  document.getElementById(`${prefix}Band`).textContent =
    route.risk_band.toUpperCase();
  document.getElementById(`${prefix}Band`).style.color =
    route.risk_band === "red"
      ? "var(--risk-red)"
      : route.risk_band === "yellow"
        ? "var(--risk-yellow)"
        : "var(--risk-green)";
  document.getElementById(`${prefix}Path`).textContent =
    `${route.node_ids.length} hops, ${route.node_ids[0]} → ${route.node_ids[route.node_ids.length - 1]}` +
    (route.approximate ? " ⚠ approximate" : "");
  document.getElementById(`${prefix}Dist`).textContent =
    `${route.total_distance_km} km`;
  document.getElementById(`${prefix}Eta`).textContent =
    `${route.total_travel_time_min} min`;
  document.getElementById(`${prefix}Risk`).textContent =
    `${route.aggregate_risk}`;
}

function drawRouteOnMap(route, className) {
  // Prefer server-provided node_coords (always complete) over local lookup
  let latlngs;
  if (route.node_coords && route.node_coords.length >= 2) {
    latlngs = route.node_coords.map((c) => [c.lat, c.lon]);
  } else if (currentNetwork) {
    const nodesById = Object.fromEntries(
      currentNetwork.nodes.map((n) => [n.id, n]),
    );
    latlngs = route.node_ids
      .map((id) => nodesById[id])
      .filter(Boolean)
      .map((n) => [n.lat, n.lon]);
  } else {
    return;
  }
  if (latlngs.length < 2) return;

  // Connect the user's pin location to the route's first/last road
  // node so the drawn path visually reaches both markers, even when
  // the pin is placed slightly off the nearest road.
  if (startPinLatLng) latlngs.unshift(startPinLatLng);
  if (goalPinLatLng) latlngs.push(goalPinLatLng);

  L.polyline(latlngs, { weight: 6, opacity: 0.95, className }).addTo(
    routeLayer,
  );
}

/** Plain-language verdict for non-technical users: which route to take,
 * and why, in one or two short sentences — no risk scores, no jargon. */
function renderVerdict(data) {
  const { fastest, safest, tradeoff_summary } = data;
  let recommended, icon, headline, detail, band;

  if (!fastest.hazard_triggered) {
    recommended = fastest;
    band = fastest.risk_band;
    icon = BAND_ICON[band] || "✅";
    headline =
      band === "green" ? "You're good to go" : "You can go, but stay alert";
    detail =
      band === "green"
        ? "Your usual route looks clear — normal weather conditions along the way."
        : "Weather along your route is a bit rougher than usual (rain or reduced visibility), but not at a dangerous level. Drive carefully.";
  } else if (!safest.hazard_triggered) {
    recommended = safest;
    band = safest.risk_band;
    icon = "⚠️";
    headline = "Take the alternate route";
    const extra =
      Math.round(
        (safest.total_travel_time_min - fastest.total_travel_time_min) * 10,
      ) / 10;
    detail =
      `The usual route currently has severe weather risk (heavy flooding, wind, or poor visibility). ` +
      `We found a safer path` +
      (extra > 0
        ? ` — it takes about ${extra} extra minute${extra === 1 ? "" : "s"}`
        : "") +
      `, but avoids the danger.`;
  } else {
    recommended = safest;
    band = "red";
    icon = "⛔";
    headline = "Not safe to drive right now";
    detail =
      "Both your usual route and the best alternative currently show severe weather risk. If you can, wait until conditions improve before heading out.";
  }

  els.verdictCard.hidden = false;
  els.verdictCard.className = `verdict-card band-${band}`;
  els.verdictIcon.textContent = icon;
  els.verdictHeadline.textContent = headline;
  els.verdictDetail.textContent = recommended.approximate
    ? detail +
      " Note: this route includes a segment traveled against its marked " +
      "one-way direction because no other loaded road connects your two " +
      "points — treat it as approximate and confirm locally before driving it."
    : detail;
  els.verdictEta.textContent = recommended.total_travel_time_min;
  els.verdictDist.textContent = recommended.total_distance_km;
  els.btnStartOver.hidden = false;

  const { balanced } = data;
  
  // Human-friendly safety labels
  function safetyLabel(band) {
    if (band === 'green') return '✅ Safe';
    if (band === 'yellow') return '⚠️ Moderate risk';
    return '🔴 High risk';
  }
  
  function routeCard(emoji, label, route, color) {
    return `
      <div class="verdict-route-card" style="border-left: 3px solid ${color};">
        <div class="route-label">${emoji} ${label}</div>
        <div class="route-stats">
          <span>⏱ <strong>${route.total_travel_time_min}</strong> min</span>
          <span>📏 <strong>${route.total_distance_km}</strong> km</span>
          <span>${safetyLabel(route.risk_band)}</span>
        </div>
        <div class="route-stats" style="margin-top: 4px; font-size: 11px; opacity: 0.8;">
          <span>🌧️ Max rain: <strong>${route.max_rain_mm_h.toFixed(1)}</strong> mm/h</span>
          <span>🌫️ Min vis: <strong>${route.min_vis_m.toFixed(0)}</strong> m</span>
          <span>💨 Max wind: <strong>${route.max_wind_kmh.toFixed(1)}</strong> km/h</span>
        </div>
      </div>
    `;
  }
  
  els.verdictTechBody.innerHTML = 
    routeCard('🔵', 'Fastest Route', fastest, '#4FA3FF') +
    routeCard('🟠', 'Balanced Route', balanced, '#F59E0B') +
    routeCard('🟣', 'Safest Route', safest, '#A855F7') +
    (data.rankings ? `
      <div style="margin-top: 12px; padding-top: 8px; border-top: 1px solid var(--border-soft);">
        <div style="font-size: 12px; font-weight: 700; margin-bottom: 6px;">📊 Route Rankings</div>
        ${data.rankings.map(r => `<div class="verdict-ranking">${r}</div>`).join('')}
      </div>
    ` : '');

  els.verdictCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

let _compareRoutesRunning = false;
let _lastRouteData = null;  // remember last drawn routes so refreshes can redraw

function redrawLastRoutes() {
  if (!_lastRouteData) return;
  routeLayer.clearLayers();
  const data = _lastRouteData;
  drawRouteOnMap(data.safest, "route-line--safe");
  drawRouteOnMap(data.balanced, "route-line--balanced");
  drawRouteOnMap(data.fastest, "route-line--fast");
}

async function compareRoutes() {
  if (_compareRoutesRunning) return;  // prevent re-entrant calls
  const start = els.startNode.value,
    goal = els.goalNode.value;
  if (!start || !goal) {
    logEvent("Pick an origin and destination first.", "warn");
    return;
  }
  if (start === goal) {
    logEvent("Origin and destination are the same node.", "warn");
    return;
  }

  els.btnCompare.disabled = true;
  els.btnFindRoute.disabled = true;
  _compareRoutesRunning = true;
  routeLayer.clearLayers();
  try {
    await ensureCorridorLoaded(start, goal);
    const alpha = parseFloat(els.alphaSlider.value);

    let data;
    try {
      data = await api("/route/compare", {
        method: "POST",
        body: JSON.stringify({
          start_node: start,
          goal_node: goal,
          alpha,
          ignore_oneway: els.btnIgnoreOneway?.checked || els.advIgnoreOneway?.checked || false,
        }),
      });
    } catch (firstErr) {
      // First attempt failed (likely disconnected graph). Try once more
      // with a wider corridor — load extra tiles around both endpoints
      // and along the path to fill gaps.
      logEvent("Route not found — loading more roads and retrying…", "warn");
      const RETRY_RADIUS_KM = 3.0;
      const originNode = currentNetwork?.nodes.find((n) => n.id === start);
      const goalNode = currentNetwork?.nodes.find((n) => n.id === goal);
      if (originNode) {
        await loadRoadsInView({
          silent: true,
          boundsOverride: boundsForLatLngs(
            [{ lat: originNode.lat, lon: originNode.lon }], RETRY_RADIUS_KM,
          ),
          areaCapKm2: MAX_CORRIDOR_AREA_KM2,
        });
      }
      if (goalNode) {
        await loadRoadsInView({
          silent: true,
          boundsOverride: boundsForLatLngs(
            [{ lat: goalNode.lat, lon: goalNode.lon }], RETRY_RADIUS_KM,
          ),
          areaCapKm2: MAX_CORRIDOR_AREA_KM2,
        });
      }
      // Also reload the corridor with extra padding
      await ensureCorridorLoaded(start, goal);

      // Second attempt — if this also fails, let the error propagate
      data = await api("/route/compare", {
        method: "POST",
        body: JSON.stringify({
          start_node: start,
          goal_node: goal,
          alpha,
          ignore_oneway: els.btnIgnoreOneway?.checked || els.advIgnoreOneway?.checked || false,
        }),
      });
    }

    fillRouteCard("fast", data.fastest);
    fillRouteCard("balanced", data.balanced);
    fillRouteCard("safe", data.safest);
    
    // Check if balanced and safest are unique before drawing
    const fId = data.fastest.node_ids.join(",");
    const bId = data.balanced.node_ids.join(",");
    const sId = data.safest.node_ids.join(",");
    
    // Always show all cards, but label duplicates
    const balancedCard = document.getElementById("balancedCard");
    const safeCard = document.getElementById("safeCard");
    balancedCard.style.display = "block";
    safeCard.style.display = "block";
    if (bId === fId) balancedCard.style.opacity = "0.5";
    else balancedCard.style.opacity = "1";
    if (sId === fId) safeCard.style.opacity = "0.5";
    else safeCard.style.opacity = "1";
    
    document.getElementById("tradeoffText").textContent = data.tradeoff_summary;
    const rankingsEl = document.getElementById("rankingsText");
    if (rankingsEl && data.rankings) {
      rankingsEl.innerHTML = data.rankings.map(r => `<div style="margin-bottom:4px;">${r}</div>`).join("");
    }
    els.resultsPanel.hidden = false;
    renderVerdict(data);

    lastFastRoute = data.fastest;
    els.btnReroute.disabled = false;

    // Draw all 3 routes on the map (different colors)
    drawRouteOnMap(data.safest, "route-line--safe");
    drawRouteOnMap(data.balanced, "route-line--balanced");
    drawRouteOnMap(data.fastest, "route-line--fast");
    
    _lastRouteData = data;  // persist so refreshes can redraw

    logEvent(
      `Route compared ${start} → ${goal} (α=${alpha}): fastest=${data.fastest.risk_band}, safest=${data.safest.risk_band}`,
      data.fastest.hazard_triggered ? "danger" : "ok",
    );
  } catch (e) {
    logEvent(`Route error: ${e.message}`, "danger");
    renderRouteError(e.message);
  } finally {
    _compareRoutesRunning = false;
    els.btnCompare.disabled = false;
    updatePinStatuses();
  }
}

/** Shown when routing fails (most commonly: the two points aren't
 * connected by the roads currently loaded). Must be visible in Simple
 * mode too — the event log where the raw error goes is Advanced-only,
 * so without this, a failure here looked like the button did nothing. */
function renderRouteError(rawMessage) {
  els.verdictCard.hidden = false;
  els.verdictCard.className = "verdict-card band-red";
  els.verdictIcon.textContent = "❓";
  els.verdictHeadline.textContent = "Couldn't find a route";
  els.verdictDetail.textContent =
    "We tried loading the roads between these two points, but still " +
    "couldn't connect them (common with real map data near dead-ends, " +
    "one-way streets, or a genuinely long trip). Try picking two spots " +
    'closer together, or pan the map along the route and tap "Load real ' +
    'roads in view" a few times to fill in the path yourself.';
  els.verdictEta.textContent = "—";
  els.verdictDist.textContent = "—";
  els.verdictTechBody.innerHTML = `<div class="verdict-tech-row"><span>Server said</span><span>${rawMessage}</span></div>`;
  els.btnStartOver.hidden = false;
}

async function checkReroute() {
  if (!lastFastRoute) return;
  const currentNode = lastFastRoute.node_ids[1] || lastFastRoute.node_ids[0];
  try {
    const data = await api("/reroute-check", {
      method: "POST",
      body: JSON.stringify({
        route_node_ids: lastFastRoute.node_ids,
        route_segment_ids: lastFastRoute.segment_ids,
        current_node: currentNode,
        goal_node: lastFastRoute.node_ids[lastFastRoute.node_ids.length - 1],
        alpha: parseFloat(els.alphaSlider.value),
      }),
    });
    logEvent(
      `Reroute check @ ${currentNode}: ${data.message}`,
      data.rerouted ? "warn" : "ok",
    );
    if (data.rerouted && data.new_route) {
      fillRouteCard("safe", data.new_route);
      routeLayer.clearLayers();
      drawRouteOnMap(data.new_route, "route-line--safe");
      drawRouteOnMap(lastFastRoute, "route-line--fast");
    }
  } catch (e) {
    logEvent(`Reroute-check error: ${e.message}`, "danger");
  }
}

async function pollWeather() {
  try {
    const data = await api("/weather/refresh", { method: "POST" });
    await refreshAll();
    logEvent(data.message, data.used_fallback ? "warn" : "ok");
  } catch (e) {
    logEvent(`Weather refresh failed: ${e.message}`, "danger");
  }
}

async function toggleOutage() {
  outageActive = !outageActive;
  els.btnOutage.textContent = outageActive ? "Restore feed" : "Simulate outage";
  if (outageActive) {
    stormActive = false;
    els.btnStorm.textContent = "Simulate storm";
  }
  await api("/weather/outage", {
    method: "POST",
    body: JSON.stringify({ simulate_outage: outageActive, simulate_storm: stormActive }),
  });
  logEvent(
    outageActive ? "Weather API outage simulated." : "Weather API restored.",
    outageActive ? "danger" : "ok",
  );
  await pollWeather();
}

async function toggleStorm() {
  stormActive = !stormActive;
  els.btnStorm.textContent = stormActive ? "Clear storm" : "Simulate storm";
  if (stormActive) {
    outageActive = false;
    els.btnOutage.textContent = "Simulate outage";
  }
  await api("/weather/outage", {
    method: "POST",
    body: JSON.stringify({ simulate_outage: outageActive, simulate_storm: stormActive }),
  });
  logEvent(
    stormActive ? "Severe storm simulated across area." : "Storm cleared.",
    stormActive ? "danger" : "ok",
  );
  await pollWeather();
}

// -------------------------------------------------------------- scheduler (auto-poll)

async function pollHealth() {
  let h;
  try {
    h = await api("/health");
  } catch {
    return;
  }

  els.pollIntervalLabel.textContent = Math.round(h.poll_interval_seconds);
  els.autoPollToggle.checked = h.auto_poll_enabled;

  if (h.auto_poll_enabled && h.seconds_until_next_refresh != null) {
    const s = Math.max(0, Math.round(h.seconds_until_next_refresh));
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    els.nextRefresh.textContent = `${mm}:${ss}`;
    els.autoPollCountdown.textContent = `next in ${mm}:${ss}`;
  } else {
    els.nextRefresh.textContent = "paused";
    els.autoPollCountdown.textContent = "paused";
  }

  // Detect a refresh that happened on its own (background scheduler),
  // not from a button click, and quietly pull the updated data.
  if (
    lastKnownRefreshTs !== null &&
    h.last_weather_refresh &&
    h.last_weather_refresh > lastKnownRefreshTs
  ) {
    await refreshAll();
    logEvent(
      "Weather auto-refreshed in the background.",
      h.used_fallback_last_refresh ? "warn" : "ok",
    );
  }
  lastKnownRefreshTs = h.last_weather_refresh;
}
setInterval(pollHealth, 4000);

els.autoPollToggle.addEventListener("change", async () => {
  await api("/weather/auto-poll", {
    method: "POST",
    body: JSON.stringify({ enabled: els.autoPollToggle.checked }),
  });
  logEvent(
    els.autoPollToggle.checked
      ? "Auto-refresh resumed."
      : "Auto-refresh paused.",
  );
});

// -------------------------------------------------------------- wire up

els.alphaSlider.addEventListener("input", () => {
  els.alphaValue.textContent = parseFloat(els.alphaSlider.value).toFixed(1);
});
els.alphaSlider.addEventListener("change", () => {
  if (els.startNode.value && els.goalNode.value && els.startNode.value !== els.goalNode.value) {
    compareRoutes();
  }
});
if (els.advIgnoreOneway) {
  els.advIgnoreOneway.addEventListener("change", () => {
    if (els.startNode.value && els.goalNode.value && els.startNode.value !== els.goalNode.value) {
      compareRoutes();
    }
  });
}
if (els.btnIgnoreOneway) {
  els.btnIgnoreOneway.addEventListener("change", () => {
    if (els.startNode.value && els.goalNode.value && els.startNode.value !== els.goalNode.value) {
      compareRoutes();
    }
  });
}
els.btnCompare.addEventListener("click", compareRoutes);
els.btnFindRoute.addEventListener("click", compareRoutes);
els.btnReroute.addEventListener("click", checkReroute);
els.btnRefresh.addEventListener("click", pollWeather);
els.btnOutage.addEventListener("click", toggleOutage);
  els.btnStorm.addEventListener("click", toggleStorm);
els.btnLoadRoads.addEventListener("click", () => loadRoadsInView());
els.startNode.addEventListener("change", () => {
  renderPins();
  updatePinStatuses();
});
els.goalNode.addEventListener("change", () => {
  renderPins();
  updatePinStatuses();
});

(async function init() {
  try {
    await api("/health");
    setLinkStatus(true);
    await refreshAll();
    logEvent("Console connected. Loading real roads for this area…");
    pollHealth();
    await loadRoadsInView({ silent: true }); // best-effort real data for the default view
  } catch (e) {
    setLinkStatus(false);
    logEvent(
      `Cannot reach API at ${API_BASE || location.origin}. Is uvicorn running?`,
      "danger",
    );
  }
})();
