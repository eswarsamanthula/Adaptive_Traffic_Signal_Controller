"""Adaptive signal phase state machine with a live countdown.

Model (matches the frozen reference dataset):

* deterministic phase order, default North -> South -> East -> West;
* every approach runs GREEN -> YELLOW -> ALL_RED, then the next approach;
* a cycle = one GREEN+YELLOW+ALL_RED for each approach;
* exactly one approach is non-RED at any instant, so two conflicting approaches
  can never be GREEN together;
* when a phase timer reaches zero the controller advances automatically on the
  next :meth:`tick`;
* GREEN durations come from :mod:`src.controller.decision_engine`; missing or
  stale readings fall back to the safe default green.

The controller is driven by an injected :class:`~src.common.time_utils.Clock`,
so tests advance a :class:`~src.common.time_utils.ManualClock` instead of
sleeping. It performs no I/O and never calls ``time`` or MQTT itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from src.common.schemas import APPROACHES, SensorReading
from src.common.time_utils import Clock, SystemClock, seconds_between
from src.controller.decision_engine import AllocationResult, allocate_greens
from src.controller.timing_constraints import SignalState, TimingConfig


@dataclass
class PhaseEvent:
    """One signal-state transition produced by :meth:`AdaptiveController.tick`."""

    cycle_no: int
    approach: str
    state: SignalState
    duration_sec: int
    at: float  # monotonic seconds from the injected clock
    timestamp: Optional[str] = None  # wall/dataset time stamp, set by the caller

    def as_dict(self) -> dict:
        return {
            "cycle_no": self.cycle_no,
            "approach": self.approach,
            "signal_state": self.state.value,
            "duration_sec": self.duration_sec,
            "timestamp": self.timestamp,
        }


class AdaptiveController:
    def __init__(
        self,
        config: Optional[TimingConfig] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.config = config or TimingConfig()
        self.clock = clock or SystemClock()
        order = self.config.phase_order

        self.running = False
        self.cycle_no = 0
        self.active_index = 0
        self.state = SignalState.RED
        self.phase_started_at = 0.0
        self.phase_duration = 0.0

        self.readings: Dict[str, Optional[SensorReading]] = {a: None for a in order}
        self.decision_time: Optional[str] = None
        self.previous_green: Dict[str, int] = {a: self.config.default_green_seconds for a in order}
        self._paused: Optional[dict] = None

        # Latest decision (for display / old-vs-new) and the schedule in force.
        self.plan: AllocationResult = allocate_greens(
            self.readings, self.previous_green, self.config
        )
        self.schedule: Dict[str, int] = self._schedule_from(self.plan)

    # --------------------------------------------------------------- helpers
    def _schedule_from(self, plan: AllocationResult) -> Dict[str, int]:
        by = plan.by_approach()
        return {a: by[a].applied_green_time for a in self.config.phase_order if a in by}

    def _stale_set(self) -> Set[str]:
        """Approaches whose stored reading is older than the staleness limit."""
        if not self.decision_time:
            return set()
        stale: Set[str] = set()
        for approach, reading in self.readings.items():
            if reading is None:
                stale.add(approach)
                continue
            age = seconds_between(self.decision_time, reading.timestamp)
            if age > self.config.stale_after_seconds:
                stale.add(approach)
        return stale

    def _recompute_plan(self) -> AllocationResult:
        self.plan = allocate_greens(
            self.readings, self.previous_green, self.config, stale=self._stale_set()
        )
        return self.plan

    # ------------------------------------------------------------ data intake
    def apply_reading(self, reading: SensorReading, *, decision_time: Optional[str] = None) -> None:
        if reading.approach not in self.readings:
            raise ValueError(f"unknown approach: {reading.approach!r}")
        self.readings[reading.approach] = reading
        if decision_time is not None:
            self.decision_time = decision_time
        self._recompute_plan()
        if not self.running:
            self.schedule = self._schedule_from(self.plan)

    def apply_readings(
        self,
        readings: Dict[str, SensorReading],
        *,
        decision_time: Optional[str] = None,
    ) -> AllocationResult:
        """Replace the stored readings and recompute the plan; returns the plan."""
        for approach, reading in readings.items():
            if approach not in self.readings or reading is None:
                continue
            self.readings[approach] = reading
        if decision_time is not None:
            self.decision_time = decision_time
        plan = self._recompute_plan()
        if not self.running:
            self.schedule = self._schedule_from(plan)
        return plan

    # ------------------------------------------------------------- lifecycle
    def start(self, *, at: Optional[float] = None) -> List[PhaseEvent]:
        if self.running:
            return []
        self._paused = None
        self.running = True
        return self._begin_cycle(self._now(at))

    def pause(self, *, at: Optional[float] = None) -> None:
        """Freeze the countdown, remembering how much of the phase is left."""
        if not self.running:
            return
        self._paused = {
            "state": self.state,
            "active_index": self.active_index,
            "duration": self.phase_duration,
            "remaining": self.phase_remaining,
        }
        self.running = False

    def resume(self, *, at: Optional[float] = None) -> None:
        if self.running or self._paused is None:
            return
        saved = self._paused
        self.state = saved["state"]
        self.active_index = saved["active_index"]
        self.phase_duration = saved["duration"]
        self.phase_started_at = self._now(at) - (saved["duration"] - saved["remaining"])
        self.running = True
        self._paused = None

    def stop(self, *, at: Optional[float] = None) -> PhaseEvent:
        self.running = False
        self._paused = None
        now = self._now(at)
        self.state = SignalState.RED
        self.phase_started_at = now
        self.phase_duration = 0.0
        return PhaseEvent(self.cycle_no, self.active_approach or self.config.phase_order[0],
                          SignalState.RED, 0, now)

    def reset(self) -> None:
        self.stop()
        self.cycle_no = 0
        self.active_index = 0
        self._paused = None
        for approach in self.config.phase_order:
            self.previous_green[approach] = self.config.default_green_seconds
            self.readings[approach] = None
        self.decision_time = None
        self.plan = allocate_greens(self.readings, self.previous_green, self.config)
        self.schedule = self._schedule_from(self.plan)

    def _now(self, at: Optional[float]) -> float:
        return self.clock.now() if at is None else float(at)

    def _begin_cycle(self, now: float) -> List[PhaseEvent]:
        self.cycle_no += 1
        # Freeze the latest plan into the schedule for this cycle.
        self.schedule = self._schedule_from(self.plan)
        self.previous_green = dict(self.schedule)
        self.active_index = 0
        self.state = SignalState.GREEN
        self.phase_started_at = now
        approach = self.config.phase_order[0]
        self.phase_duration = float(self.schedule.get(approach, self.config.default_green_seconds))
        return [PhaseEvent(self.cycle_no, approach, SignalState.GREEN, int(self.phase_duration), now)]

    # ----------------------------------------------------------------- ticking
    def tick(self, *, at: Optional[float] = None) -> List[PhaseEvent]:
        """Advance the clock; emit every phase transition that is now due."""
        now = self._now(at)
        events: List[PhaseEvent] = []
        if not self.running:
            return events

        while now >= self.phase_started_at + self.phase_duration:
            boundary = self.phase_started_at + self.phase_duration
            if self.state == SignalState.GREEN:
                self.state = SignalState.YELLOW
                self.phase_duration = float(self.config.yellow_seconds)
            elif self.state == SignalState.YELLOW:
                self.state = SignalState.ALL_RED
                self.phase_duration = float(self.config.all_red_seconds)
            else:  # ALL_RED -> next approach GREEN (or a new cycle)
                nxt = (self.active_index + 1) % len(self.config.phase_order)
                if nxt == 0:
                    events.extend(self._begin_cycle(boundary))
                    continue
                self.active_index = nxt
                self.state = SignalState.GREEN
                approach = self.config.phase_order[nxt]
                self.phase_duration = float(
                    self.schedule.get(approach, self.config.default_green_seconds)
                )
            self.phase_started_at = boundary
            approach = self.config.phase_order[self.active_index]
            events.append(
                PhaseEvent(self.cycle_no, approach, self.state, int(self.phase_duration), boundary)
            )
        return events

    # ---------------------------------------------------------------- readouts
    @property
    def active_approach(self) -> Optional[str]:
        if self.state == SignalState.RED:
            return None
        return self.config.phase_order[self.active_index]

    @property
    def phase_elapsed(self) -> float:
        if self._paused is not None:
            return max(0.0, self._paused["duration"] - self._paused["remaining"])
        if not self.running or self.phase_duration <= 0:
            return 0.0
        return max(0.0, min(self.phase_duration, self.clock.now() - self.phase_started_at))

    @property
    def phase_remaining(self) -> float:
        if self._paused is not None:
            return max(0.0, self._paused["remaining"])
        if not self.running or self.phase_duration <= 0:
            return 0.0
        return max(0.0, self.phase_duration - (self.clock.now() - self.phase_started_at))

    def next_phase(self) -> Dict[str, str]:
        order = self.config.phase_order
        if self.state == SignalState.GREEN:
            return {"approach": order[self.active_index], "state": SignalState.YELLOW.value}
        if self.state == SignalState.YELLOW:
            return {"approach": order[self.active_index], "state": SignalState.ALL_RED.value}
        nxt = (self.active_index + 1) % len(order)
        return {"approach": order[nxt], "state": SignalState.GREEN.value}

    def signal_for(self, approach: str) -> SignalState:
        if self.running and self.state != SignalState.RED and approach == self.active_approach:
            return self.state
        return SignalState.RED

    def _reading_view(self, approach: str) -> dict:
        reading = self.readings.get(approach)
        if reading is None:
            return {"last_update": None, "live": False, "age_seconds": None}
        age = None
        if self.decision_time:
            age = seconds_between(self.decision_time, reading.timestamp)
        live = age is not None and 0 <= age <= self.config.stale_after_seconds
        return {"last_update": reading.timestamp, "live": live, "age_seconds": age}

    def snapshot(self) -> dict:
        order = self.config.phase_order
        signals = {}
        approaches = {}
        for approach in order:
            state = self.signal_for(approach)
            remaining = int(round(self.phase_remaining)) if state != SignalState.RED else 0
            signals[approach] = {"state": state.value, "remaining": remaining}
            reading = self.readings.get(approach)
            view = self._reading_view(approach)
            approaches[approach] = {
                "signal": state.value,
                "remaining": remaining,
                "vehicle_count": getattr(reading, "vehicle_count", None),
                "queue_length": getattr(reading, "queue_length", None),
                "arrival_rate": getattr(reading, "arrival_rate", None),
                "avg_wait_time": getattr(reading, "avg_wait_time", None),
                "congestion_level": getattr(reading, "congestion_level", None),
                **view,
            }
        plan = self.plan.by_approach()
        return {
            "running": self.running,
            "paused": self._paused is not None,
            "cycle_no": self.cycle_no,
            "active_approach": self.active_approach,
            "state": self.state.value if self.running else SignalState.RED.value,
            "phase_elapsed": int(round(self.phase_elapsed)),
            "phase_remaining": int(round(self.phase_remaining)),
            "next_phase": self.next_phase(),
            "all_red": self.state == SignalState.ALL_RED,
            "decision_time": self.decision_time,
            "schedule": dict(self.schedule),
            "cycle_length_sec": self.plan.cycle_length_sec,
            "average_score": round(self.plan.average_score, 3),
            "fallback_used": self.plan.fallback_used,
            "decisions": {a: plan[a].as_dict() for a in plan},
            "signals": signals,
            "approaches": approaches,
        }
