"""Fixed-time (non-adaptive) baseline for comparison with the adaptive plan.

Reuses the :class:`AdaptiveController` state machine but ignores live demand and
always assigns the same default green to every approach, matching the notion of
a conventional pre-timed signal used in ``dataset/fixed_baseline_signal.csv``.
"""
from __future__ import annotations

from typing import Optional

from src.common.time_utils import Clock
from src.controller.adaptive_controller import AdaptiveController
from src.controller.decision_engine import AllocationResult, TimingDecision
from src.controller.timing_constraints import TimingConfig

FIXED_REASON = "Fixed-time plan: equal default green for every approach"


class FixedTimeController(AdaptiveController):
    def __init__(self, config: Optional[TimingConfig] = None, clock: Optional[Clock] = None) -> None:
        super().__init__(config, clock)
        self.plan = self._fixed_plan()
        self.schedule = self._schedule_from(self.plan)

    def _fixed_plan(self) -> AllocationResult:
        result = AllocationResult()
        green = self.config.default_green_seconds
        for approach in self.config.phase_order:
            result.decisions.append(
                TimingDecision(
                    approach=approach,
                    previous_green_time=green,
                    requested_green_time=green,
                    applied_green_time=green,
                    decision_reason=FIXED_REASON,
                )
            )
        result.cycle_length_sec = self.config.cycle_length(
            [d.applied_green_time for d in result.decisions]
        )
        return result

    def _recompute_plan(self) -> AllocationResult:
        self.plan = self._fixed_plan()
        return self.plan

    def reset(self) -> None:
        super().reset()
        self.plan = self._fixed_plan()
        self.schedule = self._schedule_from(self.plan)
