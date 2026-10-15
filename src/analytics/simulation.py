"""Fixed-time vs adaptive comparison on identical traffic.

The frozen dataset's ``Traffic_Data`` sheet gives an *exogenous* arrival rate
(documented as independent of the signal). This module replays those arrivals
through a small deterministic queue model so both control policies see exactly
the same demand and the same initial conditions:

    queue(t+1) = max(0, queue(t) + arrivals(t) - min(queue(t), capacity(t)))

``capacity(t)`` is the approach's green seconds in the interval times a
saturation flow (vehicles/second). Waiting is accumulated as vehicle-seconds
(``queue_after * dt`` summed over the run), so ``avg_delay = vehicle_seconds /
total_arrivals`` is the mean seconds each arriving vehicle waited.

This is an explicitly *approximate* model — one saturation-flow constant, no
per-lane detail — used only to compare the two policies under equal demand. The
absolute numbers are not claimed to reproduce the dataset; only the fixed-vs-
adaptive comparison is meaningful.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.analytics.congestion import classify_congestion
from src.common.schemas import APPROACHES, SensorReading
from src.controller.decision_engine import allocate_greens
from src.controller.timing_constraints import TimingConfig

#: Saturation flow: vehicles discharged per second of green per approach.
#: 0.5 veh/s ~= 1800 veh/hour/lane, a conventional planning value.
DEFAULT_SATURATION_FLOW = 0.5

PolicyResult = Tuple[Dict[str, int], int]
GreenPolicy = Callable[[Dict[str, SensorReading], Dict[str, int]], PolicyResult]


@dataclass
class SimParams:
    interval_seconds: int = 60
    saturation_flow_per_second: float = DEFAULT_SATURATION_FLOW
    initial_queue: int = 0


@dataclass
class PolicyMetrics:
    policy: str
    total_steps: int
    total_arrivals: int
    total_served: int
    vehicle_seconds_delay: float
    avg_delay_per_vehicle: float
    avg_queue: float
    max_queue: int
    cycles: int
    phase_changes: int
    served_fraction: float
    duration_sec: int
    throughput_per_min: float = 0.0
    jain_fairness: float = 1.0
    worst_approach_delay: float = 0.0
    max_red_wait_sec: int = 0
    per_approach_delay: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "total_steps": self.total_steps,
            "total_arrivals": self.total_arrivals,
            "total_served": self.total_served,
            "vehicle_seconds_delay": round(self.vehicle_seconds_delay, 1),
            "avg_delay_per_vehicle": round(self.avg_delay_per_vehicle, 3),
            "avg_queue": round(self.avg_queue, 3),
            "max_queue": self.max_queue,
            "cycles": self.cycles,
            "phase_changes": self.phase_changes,
            "served_fraction": round(self.served_fraction, 4),
            "duration_sec": self.duration_sec,
            "throughput_per_min": round(self.throughput_per_min, 3),
            "jain_fairness": round(self.jain_fairness, 4),
            "worst_approach_delay": round(self.worst_approach_delay, 3),
            "max_red_wait_sec": self.max_red_wait_sec,
        }


# --------------------------------------------------------------- green policies
def fixed_time_policy(green_seconds: int, config: TimingConfig) -> GreenPolicy:
    """Every approach gets the same green every interval (the baseline)."""
    cycle = config.cycle_length([green_seconds] * len(config.phase_order))

    def policy(readings, previous) -> PolicyResult:
        return {a: green_seconds for a in config.phase_order}, cycle

    return policy


def adaptive_policy(config: TimingConfig) -> GreenPolicy:
    """Recompute greens from the simulated readings via the decision engine."""

    def policy(readings, previous) -> PolicyResult:
        plan = allocate_greens(readings, previous, config)
        greens = {a: d.applied_green_time for a, d in plan.by_approach().items()}
        return greens, plan.cycle_length_sec

    return policy


# ------------------------------------------------------------------- simulation
def group_arrivals_by_time(
    readings: Sequence[SensorReading],
) -> List[Tuple[str, Dict[str, int]]]:
    """Collapse readings into (timestamp, {approach: arrival_rate}) steps."""
    steps: Dict[str, Dict[str, int]] = {}
    order: List[str] = []
    for reading in readings:
        if reading.timestamp not in steps:
            steps[reading.timestamp] = {}
            order.append(reading.timestamp)
        steps[reading.timestamp][reading.approach] = reading.arrival_rate
    return [(ts, steps[ts]) for ts in order]


def _estimate_wait(delay_by_approach: Dict[str, float], arrivals_by_approach: Dict[str, int]) -> Dict[str, float]:
    return {
        a: delay_by_approach[a] / max(1, arrivals_by_approach[a])
        for a in delay_by_approach
    }


def simulate(
    steps: Sequence[Tuple[str, Dict[str, int]]],
    policy: GreenPolicy,
    config: TimingConfig,
    *,
    params: Optional[SimParams] = None,
    policy_name: str = "policy",
) -> PolicyMetrics:
    params = params or SimParams()
    dt = params.interval_seconds
    order = config.phase_order

    queues: Dict[str, int] = {a: params.initial_queue for a in order}
    previous: Dict[str, int] = {a: config.default_green_seconds for a in order}
    delay_by_approach: Dict[str, float] = {a: 0.0 for a in order}
    arrivals_by_approach: Dict[str, int] = {a: 0 for a in order}

    vehicle_seconds = 0.0
    total_arrivals = 0
    total_served = 0
    sum_queue = 0.0
    queue_samples = 0
    max_queue = 0
    cycle_lengths: List[int] = []
    max_red_wait = 0

    for timestamp, arrivals in steps:
        est = _estimate_wait(delay_by_approach, arrivals_by_approach)
        readings = {
            a: SensorReading(
                timestamp=timestamp,
                approach=a,
                vehicle_count=queues[a] + arrivals.get(a, 0),
                queue_length=queues[a],
                avg_wait_time=int(round(est[a])),
                arrival_rate=arrivals.get(a, 0),
                congestion_level=classify_congestion(queues[a], int(round(est[a]))),
                scenario="evaluation",
            )
            for a in order
        }
        greens, cycle = policy(readings, previous)
        cycle_lengths.append(cycle)
        for a in order:
            # Longest an approach can sit on red before its next green.
            max_red_wait = max(max_red_wait, cycle - greens.get(a, config.default_green_seconds))

        for a in order:
            arrivals_a = arrivals.get(a, 0)
            green_fraction = greens.get(a, config.default_green_seconds) / max(1, cycle)
            capacity = dt * green_fraction * params.saturation_flow_per_second
            served = min(queues[a], capacity)
            queues[a] = queues[a] + arrivals_a - int(round(served))
            if queues[a] < 0:
                queues[a] = 0

            delay_by_approach[a] += queues[a] * dt
            arrivals_by_approach[a] += arrivals_a
            vehicle_seconds += queues[a] * dt
            total_arrivals += arrivals_a
            total_served += int(round(served))
            max_queue = max(max_queue, queues[a])
            sum_queue += queues[a]
            queue_samples += 1

        previous = greens

    mean_cycle = sum(cycle_lengths) / len(cycle_lengths) if cycle_lengths else 1
    duration = len(steps) * dt
    per_approach = {a: delay_by_approach[a] / max(1, arrivals_by_approach[a]) for a in order}
    xs = list(per_approach.values())
    sq = sum(x * x for x in xs)
    jain = (sum(xs) ** 2) / (len(xs) * sq) if sq > 0 else 1.0
    cycles = int(round(duration / mean_cycle)) if mean_cycle else 0
    return PolicyMetrics(
        policy=policy_name,
        total_steps=len(steps),
        total_arrivals=total_arrivals,
        total_served=total_served,
        vehicle_seconds_delay=vehicle_seconds,
        avg_delay_per_vehicle=vehicle_seconds / total_arrivals if total_arrivals else 0.0,
        avg_queue=sum_queue / queue_samples if queue_samples else 0.0,
        max_queue=max_queue,
        cycles=cycles,
        phase_changes=cycles * len(order),
        served_fraction=total_served / total_arrivals if total_arrivals else 0.0,
        duration_sec=duration,
        throughput_per_min=total_served / (duration / 60) if duration else 0.0,
        jain_fairness=jain,
        worst_approach_delay=max(xs) if xs else 0.0,
        max_red_wait_sec=int(max_red_wait),
        per_approach_delay=per_approach,
    )


def compare(
    readings: Sequence[SensorReading],
    config: Optional[TimingConfig] = None,
    *,
    params: Optional[SimParams] = None,
) -> Dict[str, PolicyMetrics]:
    """Run fixed-time and adaptive on the same arrivals; return both metric sets."""
    config = config or TimingConfig()
    steps = group_arrivals_by_time(readings)
    fixed = simulate(
        steps,
        fixed_time_policy(config.default_green_seconds, config),
        config,
        params=params,
        policy_name="fixed",
    )
    adaptive = simulate(steps, adaptive_policy(config), config, params=params, policy_name="adaptive")
    return {"fixed": fixed, "adaptive": adaptive}


def comparison_table(metrics: Mapping[str, PolicyMetrics]) -> List[dict]:
    """Flatten the comparison into rows with the adaptive-vs-fixed delta."""
    fixed = metrics["fixed"].as_dict()
    adaptive = metrics["adaptive"].as_dict()
    rows = []
    for key in fixed:
        if key == "policy":
            continue
        rows.append(
            {
                "metric": key,
                "fixed": fixed[key],
                "adaptive": adaptive[key],
                "delta": (
                    round(adaptive[key] - fixed[key], 4)
                    if isinstance(fixed[key], (int, float))
                    else None
                ),
            }
        )
    return rows
