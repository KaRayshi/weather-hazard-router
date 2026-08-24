"""
risk_engine.py
--------------
Weather-Induced Road Hazard Assessment Engine.

Implements:
  FR-2.2  Geofencing & Segment Mapping (weather -> nearest segments, <=1km)
  FR-3.1  Hazard Parameter Categorization (aquaplaning, flooding,
          visibility impairment, vehicle stability)
  FR-3.2  Dynamic Risk Scoring  (Rsegment, real-time, 0-100)
  FR-3.3  Threshold Triggering  (hazard warning when Rsegment >= 70)
  FR-1.2  Risk Heatmap color banding
  NFR-2.1 Fallback to historical trends when live weather is unavailable

--------------------------------------------------------------------
Reconciling the spec's formula with FR-3.1's four hazard categories
--------------------------------------------------------------------
The proposal gives:

    Rsegment = w1*Prain + w2*Vvis + w3*Sslope + w4*Fhistory     (in [0,100])

but FR-3.1 separately calls out FOUR hazard types to evaluate, including
"Vehicle Stability Risk (driven by sudden severe crosswinds)", which has
no term in the literal formula. To satisfy FR-3.1 without breaking the
spec's intent, this engine:

  1. Normalizes each raw weather/geometry input to a [0,1] sub-score
     (aquaplaning, flooding, visibility, wind) so they're comparable.
  2. Computes Rsegment using the spec's 4-term weighted sum, where:
        Prain      -> aquaplaning sub-score (rain intensity x friction)
        Vvis       -> visibility impairment sub-score
        Sslope     -> flooding sub-score (drainage rating x rain x history)
        Fhistory   -> historical flood index (raw, as given in spec)
  3. Adds an OPTIONAL 5th weighted term (w5 * Wwind) for crosswind /
     vehicle stability risk, defaulting to w5=0 so the engine exactly
     reproduces the spec's formula out of the box. Set w5 > 0 (and
     rebalance weights to sum to 1) to activate FR-3.1's wind hazard.

All weights are calibrated factors with 0 <= wi <= 1, per the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from models import RoadSegment, WeatherReading, Node, haversine_km

GEOFENCE_RADIUS_KM = 1.0          # FR-2.2
HAZARD_WARNING_THRESHOLD = 70.0    # FR-3.3, Rsegment in [0,100]

# FR-1.2 color banding, expressed on the [0,100] scale used by FR-3.2/3.3
# (0.3 / 0.7 fractional thresholds from FR-1.2 * 100)
GREEN_MAX = 30.0
YELLOW_MAX = 70.0


@dataclass
class RiskWeights:
    """Calibrated weighting factors wi, each constrained to [0, 1]."""
    w_rain: float = 0.35        # w1 -> aquaplaning
    w_visibility: float = 0.20  # w2 -> visibility impairment
    w_drainage: float = 0.30    # w3 -> localized flooding
    w_history: float = 0.15     # w4 -> historical flood susceptibility
    w_wind: float = 0.0         # optional 5th term for FR-3.1 wind hazard

    def __post_init__(self):
        for name, val in vars(self).items():
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"weight {name}={val} outside [0,1]")

    def normalized(self) -> "RiskWeights":
        """Rescale so weights sum to 1 (keeps Rsegment bounded to [0,100])."""
        total = sum(vars(self).values())
        if total == 0:
            raise ValueError("weights cannot all be zero")
        scaled = {k: v / total for k, v in vars(self).items()}
        return RiskWeights(**scaled)


class RiskEngine:
    def __init__(self, weights: Optional[RiskWeights] = None):
        self.weights = (weights or RiskWeights()).normalized()

    # ---------------------------------------------------------------
    # FR-2.2 Geofencing: map weather stations onto nearby road segments
    # ---------------------------------------------------------------
    def geofence_weather_to_segments(
        self,
        segments: Iterable[RoadSegment],
        nodes: Dict[str, Node],
        readings: List[WeatherReading],
        radius_km: float = GEOFENCE_RADIUS_KM,
    ) -> None:
        """
        For each segment, find the nearest weather reading within
        `radius_km` of the segment midpoint and attach its parameters.
        Segments with no reading in range are flagged `weather_data_stale`
        so the fallback path (NFR-2.1) can act on them.
        """
        for seg in segments:
            mid = seg.midpoint(nodes)
            if mid is None:
                continue
            mlat, mlon = mid

            best, best_dist = None, radius_km
            for r in readings:
                d = haversine_km(mlat, mlon, r.lat, r.lon)
                if d <= best_dist:
                    best, best_dist = r, d

            if best is not None:
                seg.precipitation_mm_h = best.precipitation_mm_h
                seg.visibility_m = best.visibility_m
                seg.wind_speed_kmh = best.wind_speed_kmh
                seg.weather_data_stale = False
            else:
                seg.weather_data_stale = True  # no station within 1km

    # ---------------------------------------------------------------
    # Sub-score normalization (raw units -> [0,1])
    # ---------------------------------------------------------------
    @staticmethod
    def _aquaplaning_subscore(precip_mm_h: float, friction_index: float) -> float:
        # Heavier rain + lower pavement friction = higher risk.
        # 50mm/h treated as a saturating "extreme" rain rate.
        rain_component = min(precip_mm_h / 50.0, 1.0)
        friction_component = 1.0 - max(0.0, min(friction_index, 1.0))
        return min(1.0, 0.7 * rain_component + 0.3 * friction_component)

    @staticmethod
    def _visibility_subscore(visibility_m: float) -> float:
        # 1000m = clear-ish, 0m = whiteout. Linear falloff, clamped.
        if visibility_m >= 1000:
            return 0.0
        return max(0.0, min(1.0, (1000.0 - visibility_m) / 1000.0))

    @staticmethod
    def _flooding_subscore(
        precip_mm_h: float, drainage_rating: float, historical_flood_index: float
    ) -> float:
        rain_component = min(precip_mm_h / 50.0, 1.0)
        # Poor drainage (high drainage_rating) amplifies flooding from rain,
        # and prior flood history raises the baseline risk even in light rain.
        base = 0.6 * rain_component * max(0.0, min(drainage_rating, 1.0))
        history_boost = 0.4 * max(0.0, min(historical_flood_index, 1.0))
        return min(1.0, base + history_boost)

    @staticmethod
    def _wind_subscore(wind_kmh: float, is_elevated_or_bridge: bool) -> float:
        # Crosswinds matter most on elevated roads/bridges (FR-3.1 #4).
        base = min(wind_kmh / 90.0, 1.0)  # 90 km/h treated as extreme
        return min(1.0, base * (1.4 if is_elevated_or_bridge else 0.6))

    # ---------------------------------------------------------------
    # FR-3.2 Dynamic Risk Scoring
    # ---------------------------------------------------------------
    def score_segment(self, seg: RoadSegment) -> float:
        """
        Compute Rsegment in [0, 100] using:
            Rsegment = w1*Prain + w2*Vvis + w3*Sslope + w4*Fhistory (+ w5*Wwind)
        where each term is first normalized to [0,1] above.
        """
        prain = self._aquaplaning_subscore(seg.precipitation_mm_h, seg.friction_index)
        vvis = self._visibility_subscore(seg.visibility_m)
        sslope = self._flooding_subscore(
            seg.precipitation_mm_h, seg.drainage_rating, seg.historical_flood_index
        )
        fhistory = max(0.0, min(seg.historical_flood_index, 1.0))
        wwind = self._wind_subscore(seg.wind_speed_kmh, seg.is_elevated_or_bridge)

        w = self.weights
        r_fraction = (
            w.w_rain * prain
            + w.w_visibility * vvis
            + w.w_drainage * sslope
            + w.w_history * fhistory
            + w.w_wind * wwind
        )
        seg.risk_score = round(min(1.0, max(0.0, r_fraction)) * 100.0, 2)
        return seg.risk_score

    def score_all(self, segments: Iterable[RoadSegment]) -> Dict[str, float]:
        return {seg.id: self.score_segment(seg) for seg in segments}

    # ---------------------------------------------------------------
    # FR-1.2 / FR-3.3
    # ---------------------------------------------------------------
    @staticmethod
    def risk_band(score: float) -> str:
        if score < GREEN_MAX:
            return "green"
        if score < YELLOW_MAX:
            return "yellow"
        return "red"

    @staticmethod
    def is_hazard_triggered(score: float) -> bool:
        return score >= HAZARD_WARNING_THRESHOLD

    # ---------------------------------------------------------------
    # NFR-2.1 Fallback routing when live weather API is unavailable
    # ---------------------------------------------------------------
    def apply_historical_fallback(self, segments: Iterable[RoadSegment]) -> None:
        """
        For any segment with stale/missing live weather data, fall back to
        a conservative estimate driven purely by historical_flood_index and
        drainage_rating (no live rain/visibility/wind signal). This keeps
        the system operational (does not crash / block routing) per
        NFR-2.1, at the cost of reduced precision until data returns.
        """
        for seg in segments:
            if not seg.weather_data_stale:
                continue
            # Assume moderate-to-adverse ambient conditions so fallback
            # scores are visibly elevated (conservative / safety-first),
            # not indistinguishable from clear-sky live readings.
            # History-heavy segments get proportionally worse conditions.
            seg.precipitation_mm_h = 8.0 + 30.0 * seg.historical_flood_index
            seg.visibility_m = max(200.0, 3000.0 * (1.0 - seg.drainage_rating))
            seg.wind_speed_kmh = 15.0 + 40.0 * (1.0 if seg.is_elevated_or_bridge else 0.3)
