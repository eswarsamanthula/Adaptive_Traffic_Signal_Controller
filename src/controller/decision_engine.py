"""Adaptive green-time decision logic (pure functions, no I/O).

Given the latest reading per approach (or ``None`` when missing/stale) and the
previous applied green per approach, :func:`allocate_greens` produces one
:class:`TimingDecision` per approach:

* a *demand score* = queue*Wq + arrival*Wa + wait*Ww summarises each approach;
* an approach scores above the intersection average -> **extend**, below ->
  **shorten**, within ``hold_band_seconds`` -> **hold**;
* the requested green is ``previous * score / average`` (so a busier approach
  gets proportionally more time) and is then clamped to ``[min, max]``;
* missing or stale readings fall back to the default/fixed green, so a faulty
  sensor can never produce an unsafe (too long or too short) green.

The reason strings mirror the frozen reference dataset
(``dataset/adaptive_decisions.csv``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Set

from src.common.schemas import SensorReading
from src.controller.timing_constraints import TimingConfig

HOLD_REASON = "Demand near average: hold green"
EXTEND_REASON = "Queue/arrival score above average: extend green"
SHORTEN_REASON = "Queue/arrival score below average: shorten green"
NO_DEMAND_REASON = "No measurable demand: hold green"
FALLBACK_REASON = "Fallback (stale or missing reading): fixed default green"
STARTUP_REASON = "Startup default: no completed traffic reading yet"


@dataclass
class TimingDecision:
    approach: str
    previous_green_time: int
    requested_green_time: int
    applied_green_time: int
    decision_reason: str
    demand_score: float = 0.0
    queue_length: Optional[int] = None
    arrival_rate: Optional[int] = None
    traffic_timestamp_used: Optional[str] = None
    used_fallback: bool = False

    def as_dict(self) -> dict:
        return {
            "approach": self.approach,
            "previous_green_time": self.previous_green_time,
            "requested_green_time": self.requested_green_time,
            "applied_green_time": self.applied_green_time,
            "decision_reason": self.decision_reason,
            "demand_score": round(self.demand_score, 3),
            "queue_length": self.queue_length,
            "arrival_rate": self.arrival_rate,
            "traffic_timestamp_used": self.traffic_timestamp_used,
            "used_fallback": self.used_fallback,
        }


@dataclass
class AllocationResult:
    decisions: List[TimingDecision] = field(default_factory=list)
    average_score: float = 0.0
    cycle_length_sec: int = 0
    fallback_used: bool = False

    def by_approach(self) -> Dict[str, TimingDecision]:
        return {d.approach: d for d in self.decisions}


def demand_score(reading: SensorReading, config: TimingConfig) -> float:
    """Weighted demand score; higher means more green is warranted."""
    return (
        config.score_queue_weight * float(reading.queue_length)
        + config.score_arrival_weight * float(reading.arrival_rate)
        + config.score_wait_weight * float(reading.avg_wait_time)
    )


def _classify_reason(requested: float, previous: int, config: TimingConfig) -> str:
    band = config.hold_band_seconds
    rounded = round(requested)
    if rounded - previous >= band:
        return EXTEND_REASON
    if previous - rounded >= band:
        return SHORTEN_REASON
    return HOLD_REASON


def allocate_greens(
    readings: Mapping[str, Optional[SensorReading]],
    previous: Mapping[str, int],
    config: TimingConfig,
    *,
    stale: Optional[Set[str]] = None,
) -> AllocationResult:
    """Compute the next green time for every approach in the configured order."""
    stale = stale or set()
    order = config.phase_order
    result = AllocationResult()

    valid: Dict[str, float] = {}
    for approach in order:
        reading = readings.get(approach)
        if reading is None or approach in stale:
            continue
        valid[approach] = demand_score(reading, config)

    if not valid:
        # No trustworthy data at all: hold the fixed default on every approach.
        for approach in order:
            prev = int(previous.get(approach, config.default_green_seconds))
            applied = config.clamp_green(config.default_green_seconds)
            result.decisions.append(
                TimingDecision(
                    approach=approach,
                    previous_green_time=prev,
                    requested_green_time=config.default_green_seconds,
                    applied_green_time=applied,
                    decision_reason=STARTUP_REASON,
                    demand_score=0.0,
                    used_fallback=True,
                )
            )
        result.cycle_length_sec = config.cycle_length([d.applied_green_time for d in result.decisions])
        result.fallback_used = True
        return result

    average = sum(valid.values()) / len(valid)
    result.average_score = average

    for approach in order:
        prev = int(previous.get(approach, config.default_green_seconds))
        reading = readings.get(approach)

        if approach not in valid:
            # Missing or stale reading for this approach only.
            result.fallback_used = True
            result.decisions.append(
                TimingDecision(
                    approach=approach,
                    previous_green_time=prev,
                    requested_green_time=config.default_green_seconds,
                    applied_green_time=config.clamp_green(config.default_green_seconds),
                    decision_reason=FALLBACK_REASON,
                    used_fallback=True,
                    traffic_timestamp_used=getattr(reading, "timestamp", None),
                )
            )
            continue

        score = valid[approach]
        if average <= 0:
            requested = float(prev)
            reason = NO_DEMAND_REASON
        else:
            requested = prev * (score / average)
            reason = _classify_reason(requested, prev, config)

        applied = config.clamp_green(requested)
        if round(requested) < config.min_green_seconds:
            reason = f"Requested below limit: clamped to min green ({config.min_green_seconds} s)"
        elif round(requested) > config.max_green_seconds:
            reason = f"Requested above limit: clamped to max green ({config.max_green_seconds} s)"

        result.decisions.append(
            TimingDecision(
                approach=approach,
                previous_green_time=prev,
                requested_green_time=round(requested),
                applied_green_time=applied,
                decision_reason=reason,
                demand_score=score,
                queue_length=reading.queue_length,
                arrival_rate=reading.arrival_rate,
                traffic_timestamp_used=reading.timestamp,
            )
        )

    result.cycle_length_sec = config.cycle_length([d.applied_green_time for d in result.decisions])
    return result
