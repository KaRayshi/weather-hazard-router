"""
routing.py
----------
Alternative Routing & Path Optimization.

Implements:
  FR-4.1  Risk-Averse Route Calculation (modified A* with hazard-weighted
          edge costs: Wcost = Distance * (1 + alpha * Rsegment/100))
  FR-4.2  Automated Rerouting (mid-transit re-check + reroute trigger)
  FR-4.3  Delay vs. Safety Evaluation (trade-off summary between routes)
  NFR-1.1 Route Recalculation performance (<2.5s) - reports wall-clock time
  FR-3.3  Threshold triggering hook, reused from risk_engine
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from models import Node, RoadSegment, haversine_km
from risk_engine import RiskEngine, HAZARD_WARNING_THRESHOLD


@dataclass
class RouteResult:
    node_ids: List[str]
    segment_ids: List[str]
    total_distance_km: float
    total_travel_time_min: float
    aggregate_risk: float          # mean Rsegment across the path, 0-100
    max_segment_risk: float
    worst_segment_id: Optional[str]
    compute_seconds: float
    
    # Weather conditions encountered
    max_rain_mm_h: float = 0.0
    min_vis_m: float = 10000.0
    max_wind_kmh: float = 0.0
    
    # True if this route had to travel one or more segments "backwards"
    # relative to how they were loaded (see find_route's allow_wrong_way).
    # Only ever set by the wrong-way fallback pass, never by a normal
    # direction-respecting search.
    approximate: bool = False
    wrong_way_segment_ids: List[str] = field(default_factory=list)

    def risk_band(self) -> str:
        return RiskEngine.risk_band(self.aggregate_risk)

    def has_hazard(self) -> bool:
        return self.max_segment_risk >= HAZARD_WARNING_THRESHOLD


# Multiplier applied to wrong-way edges during the fallback search, on top
# of the normal risk-weighted cost. Large enough that the search always
# prefers a legitimate direction-respecting path when one exists, and only
# resorts to a wrong-way edge when it's genuinely the only way through.
WRONG_WAY_PENALTY = 5.0


class RoadNetwork:
    """
    Simple directed graph over RoadSegments, indexed by node id.
    Assumes segments are added in the direction they're traversable;
    call add_segment(..., bidirectional=True) for two-way roads.
    """

    DEFAULT_SPEED_KMH = 50.0  # used to derive ETA when segment has no speed limit

    def __init__(self):
        self.nodes: Dict[str, Node] = {}
        self.segments: Dict[str, RoadSegment] = {}
        self.adjacency: Dict[str, List[str]] = {}  # node_id -> [segment_id, ...]
        # Mirror of adjacency, keyed by to_node instead of from_node. Used
        # only by the wrong-way fallback search in find_route(..., allow_wrong_way=True)
        # to consider traveling a one-way segment in reverse as a last resort,
        # without needing to store a duplicate RoadSegment for it.
        self.reverse_adjacency: Dict[str, List[str]] = {}

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node
        self.adjacency.setdefault(node.id, [])
        self.reverse_adjacency.setdefault(node.id, [])

    def add_segment(self, segment: RoadSegment, bidirectional: bool = True) -> None:
        self.segments[segment.id] = segment
        self.adjacency.setdefault(segment.from_node, []).append(segment.id)
        self.reverse_adjacency.setdefault(segment.to_node, []).append(segment.id)
        if bidirectional:
            rev = RoadSegment(
                id=segment.id + "_rev",
                from_node=segment.to_node,
                to_node=segment.from_node,
                distance_km=segment.distance_km,
                drainage_rating=segment.drainage_rating,
                historical_flood_index=segment.historical_flood_index,
                is_elevated_or_bridge=segment.is_elevated_or_bridge,
                friction_index=segment.friction_index,
            )
            self.segments[rev.id] = rev
            self.adjacency.setdefault(segment.to_node, []).append(rev.id)
            self.reverse_adjacency.setdefault(segment.from_node, []).append(rev.id)

    def all_segments(self):
        return self.segments.values()

    def evaluate_rankings(
        self, routes: dict[str, RouteResult]
    ) -> list[str]:
        """Rank the routes and provide human-readable reasons."""
        valid_routes = {k: v for k, v in routes.items() if v is not None}
        
        # Deduplicate paths that ended up being exactly identical
        unique_paths = {}
        for k, v in valid_routes.items():
            path_str = ",".join(v.node_ids)
            if path_str not in unique_paths:
                unique_paths[path_str] = (k, v)
        
        # Rank by a combined score: primarily risk, secondarily time
        def score_route(r: RouteResult):
            is_hazardous = r.max_segment_risk >= 70
            return r.total_travel_time_min + (r.aggregate_risk * 0.5) + (100 if is_hazardous else 0)

        def safety_word(r: RouteResult) -> str:
            band = RiskEngine.risk_band(r.aggregate_risk)
            if band == 'green':
                return 'safe'
            elif band == 'yellow':
                return 'moderate risk'
            return 'high risk'

        ranked = sorted(unique_paths.values(), key=lambda x: score_route(x[1]))
        
        medals = ['🥇', '🥈', '🥉']
        reasons = []
        for i, (name, r) in enumerate(ranked):
            medal = medals[i] if i < len(medals) else f'#{i+1}'
            label_map = {'fastest': 'Fastest', 'balanced': 'Balanced', 'safest': 'Safest'}
            friendly_name = label_map.get(name, name.capitalize())
            safety = safety_word(r)
            
            if name == 'fastest':
                desc = f"{r.total_travel_time_min} min · {r.total_distance_km} km — Gets you there quickest. Evaluated as {safety} based on live Open-Meteo data."
            elif name == 'safest':
                desc = f"{r.total_travel_time_min} min · {r.total_distance_km} km — Avoids dangerous weather zones. Evaluated as {safety} based on live Open-Meteo data."
            else:
                desc = f"{r.total_travel_time_min} min · {r.total_distance_km} km — Good balance of speed and safety. Evaluated as {safety} based on live Open-Meteo data."
                
            reasons.append(f"{medal} {friendly_name} Route: {desc}")
            
        return reasons

    # -----------------------------------------------------------
    # FR-4.1 Risk-Averse Route Calculation
    # -----------------------------------------------------------
    def find_route(
        self,
        start_id: str,
        goal_id: str,
        alpha: float = 1.0,
        allow_wrong_way: bool = False,
        edge_penalties: Optional[Dict[str, float]] = None,
    ) -> Optional[RouteResult]:
        """
        Modified A* search. Edge cost:
            Wcost = Distance * (1 + alpha * Rsegment / 100)
        alpha (the user's risk-avoidance preference factor):
            0.0  -> pure shortest-distance routing (ignores risk)
            1.0  -> balanced risk aversion (default)
            >2.0 -> strongly avoids high-risk segments even at a large
                    time cost

        allow_wrong_way:
            False (default) -> only travels segments in the direction they
                were loaded (a one-way street stays one-way). This is the
                only mode that should ever be presented as a normal route.
            True  -> as a last-resort fallback, also allows traversing a
                loaded segment against its direction, at a steep cost
                penalty (WRONG_WAY_PENALTY), so it's only used when
                there's truly no legitimate path. The result is flagged
                RouteResult.approximate=True so callers (and the UI) can
                warn the driver instead of presenting it as a normal route.
                This does NOT invent roads that were never loaded -- it
                only reconsiders roads that ARE on the map but were
                excluded purely by assumed direction (e.g. an incorrectly
                tagged oneway, or a road you could still walk/turn around
                on). Two areas with genuinely no loaded road between them
                at all still correctly return None.
        """
        t0 = time.perf_counter()

        if start_id not in self.nodes or goal_id not in self.nodes:
            return None

        goal_node = self.nodes[goal_id]

        def heuristic(node_id: str) -> float:
            n = self.nodes[node_id]
            # Admissible: true edge cost >= straight-line distance always,
            # since Wcost = Distance * (1 + alpha*R/100) >= Distance.
            return haversine_km(n.lat, n.lon, goal_node.lat, goal_node.lon)

        # priority queue of (f_score, node_id)
        open_heap: List[Tuple[float, str]] = [(heuristic(start_id), start_id)]
        g_score: Dict[str, float] = {start_id: 0.0}
        came_from_seg: Dict[str, str] = {}
        came_from_node: Dict[str, str] = {}
        came_from_wrong_way: Dict[str, bool] = {}
        visited = set()

        def relax(current: str, seg_id: str, neighbor: str, wrong_way: bool) -> None:
            seg = self.segments[seg_id]
            penalty = edge_penalties.get(seg_id, 1.0) if edge_penalties else 1.0
            w_cost = (seg.distance_km * penalty) * (1.0 + alpha * seg.risk_score / 100.0)
            if wrong_way:
                w_cost *= WRONG_WAY_PENALTY
            tentative_g = g_score[current] + w_cost

            if tentative_g < g_score.get(neighbor, float("inf")):
                g_score[neighbor] = tentative_g
                came_from_seg[neighbor] = seg_id
                came_from_node[neighbor] = current
                came_from_wrong_way[neighbor] = wrong_way
                f = tentative_g + heuristic(neighbor)
                heapq.heappush(open_heap, (f, neighbor))

        while open_heap:
            _, current = heapq.heappop(open_heap)
            if current in visited:
                continue
            visited.add(current)

            if current == goal_id:
                break

            for seg_id in self.adjacency.get(current, []):
                seg = self.segments[seg_id]
                relax(current, seg_id, seg.to_node, wrong_way=False)

            if allow_wrong_way:
                for seg_id in self.reverse_adjacency.get(current, []):
                    seg = self.segments[seg_id]
                    if seg.from_node == current:
                        continue  # same edge already handled forward above
                    relax(current, seg_id, seg.from_node, wrong_way=True)

        if goal_id not in g_score:
            return None  # no path found, even with wrong-way segments allowed

        # reconstruct path
        node_path = [goal_id]
        seg_path = []
        wrong_way_segments: List[str] = []
        cur = goal_id
        while cur != start_id:
            seg_id = came_from_seg[cur]
            seg_path.append(seg_id)
            if came_from_wrong_way.get(cur):
                wrong_way_segments.append(seg_id)
            cur = came_from_node[cur]
            node_path.append(cur)
        node_path.reverse()
        seg_path.reverse()
        wrong_way_segments.reverse()

        segs = [self.segments[s] for s in seg_path]
        total_distance = sum(s.distance_km for s in segs)
        total_time_min = sum(
            (s.distance_km / max(1.0, getattr(s, 'traffic_speed_kmh', self.DEFAULT_SPEED_KMH))) * 60.0 for s in segs
        )
        risks = [s.risk_score for s in segs] or [0.0]
        worst = max(segs, key=lambda s: s.risk_score) if segs else None

        return RouteResult(
            node_ids=node_path,
            segment_ids=seg_path,
            total_distance_km=round(total_distance, 3),
            total_travel_time_min=round(total_time_min, 1),
            aggregate_risk=round(sum(risks) / len(risks), 2),
            max_segment_risk=max(risks),
            worst_segment_id=worst.id if worst else None,
            compute_seconds=round(time.perf_counter() - t0, 4),
            max_rain_mm_h=round(max((s.precipitation_mm_h for s in segs), default=0.0), 1),
            min_vis_m=round(min((s.visibility_m for s in segs), default=10000.0), 0),
            max_wind_kmh=round(max((s.wind_speed_kmh for s in segs), default=0.0), 1),
            approximate=bool(wrong_way_segments),
            wrong_way_segment_ids=wrong_way_segments,
        )

    def find_alternative_routes(
        self,
        start_id: str,
        goal_id: str,
        alpha: float = 1.0,
        allow_wrong_way: bool = False,
        num_alternatives: int = 3,
    ) -> List[RouteResult]:
        """
        Find up to `num_alternatives` distinct routes using edge-penalization.
        Thread-safe: penalties are passed into the search without mutating the
        shared road network in-memory.
        """
        PENALTY_FACTOR = 3.0  # multiply cost of used segments
        results: List[RouteResult] = []
        seen_paths = set()
        penalty_map: Dict[str, float] = {}

        alphas_to_try = [
            0.0,                          # fastest
            max(1.0, alpha / 2.0),        # balanced  
            max(alpha, 2.5),              # safest
        ]

        for i in range(num_alternatives):
            a = alphas_to_try[i] if i < len(alphas_to_try) else alpha
            route = self.find_route(start_id, goal_id, a, allow_wrong_way, edge_penalties=penalty_map)
            if route is None:
                continue

            path_key = ",".join(route.node_ids)
            if path_key in seen_paths:
                # Same path found — penalize harder and retry with a slightly higher alpha
                for seg_id in route.segment_ids:
                    penalty_map[seg_id] = penalty_map.get(seg_id, 1.0) * PENALTY_FACTOR

                retry_route = self.find_route(
                    start_id, goal_id, a + 0.5, allow_wrong_way, edge_penalties=penalty_map
                )
                if retry_route:
                    retry_key = ",".join(retry_route.node_ids)
                    if retry_key not in seen_paths:
                        seen_paths.add(retry_key)
                        results.append(retry_route)
                        for seg_id in retry_route.segment_ids:
                            penalty_map[seg_id] = penalty_map.get(seg_id, 1.0) * PENALTY_FACTOR
                continue

            seen_paths.add(path_key)
            results.append(route)

            # Penalize the segments of this route for subsequent iterations
            for seg_id in route.segment_ids:
                penalty_map[seg_id] = penalty_map.get(seg_id, 1.0) * PENALTY_FACTOR

        return results

    # -----------------------------------------------------------
    # FR-4.2 Automated Rerouting
    # -----------------------------------------------------------
    def check_and_reroute(
        self,
        current_route: RouteResult,
        current_node_id: str,
        goal_id: str,
        alpha: float = 1.0,
    ) -> Tuple[bool, Optional[RouteResult]]:
        """
        Call this periodically (e.g. every weather refresh cycle) while a
        route is active. If any *remaining* (not-yet-traveled) segment of
        the current route now exceeds the hazard threshold, compute a
        fresh route from the driver's current position and return it.

        Tries a normal direction-respecting reroute first; if that finds
        nothing, retries allowing wrong-way segments as a last resort
        (flagged via RouteResult.approximate) rather than stranding a
        driver mid-trip with "no alternative route exists" when a
        last-resort option actually does.

        Returns (rerouted: bool, new_route_or_None).
        """
        def reroute_from(node_id: str) -> Optional[RouteResult]:
            route = self.find_route(node_id, goal_id, alpha)
            if route is not None:
                return route
            return self.find_route(node_id, goal_id, alpha, allow_wrong_way=True)

        if current_node_id not in current_route.node_ids:
            # driver has diverged from the planned path; just recompute
            return True, reroute_from(current_node_id)

        idx = current_route.node_ids.index(current_node_id)
        remaining_seg_ids = current_route.segment_ids[idx:]
        remaining_segs = [self.segments[s] for s in remaining_seg_ids]

        if any(RiskEngine.is_hazard_triggered(s.risk_score) for s in remaining_segs):
            return True, reroute_from(current_node_id)

        return False, None

    # -----------------------------------------------------------
    # FR-4.3 Delay vs. Safety Evaluation
    # -----------------------------------------------------------
    def describe_tradeoff(
        self, safe_route: RouteResult, fast_route: RouteResult
    ) -> str:
        """
        Human-readable trade-off summary, e.g.:
        'Alternative route adds 4 minutes but avoids high flood risk
        segment on Main Street.'
        """
        delay = round(safe_route.total_travel_time_min - fast_route.total_travel_time_min, 1)
        worst_seg = self.segments.get(fast_route.worst_segment_id) if fast_route.worst_segment_id else None
        worst_label = worst_seg.id if worst_seg else "a high-risk segment"
        risk_desc = RiskEngine.risk_band(fast_route.max_segment_risk)

        if delay <= 0:
            return (
                f"The safer route is also faster or equal in time "
                f"(risk {risk_desc} avoided on {worst_label})."
            )
        return (
            f"Alternative route adds {delay} min but avoids a {risk_desc}-risk "
            f"segment ({worst_label}, R={fast_route.max_segment_risk})."
        )