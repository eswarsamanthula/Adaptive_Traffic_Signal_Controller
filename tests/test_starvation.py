"""Starvation / bounded-wait proof for the adaptive controller.

Even under extreme skewed demand, every approach must (a) get at least the
minimum green and (b) never wait on red longer than the worst-case cycle.
"""
import unittest

from src.analytics.simulation import adaptive_policy, group_arrivals_by_time, simulate
from src.common.schemas import SensorReading
from src.controller.decision_engine import allocate_greens
from src.controller.timing_constraints import TimingConfig

CFG = TimingConfig()
APPROACHES = CFG.phase_order


def _reading(approach, queue, arrival, wait=0):
    return SensorReading(
        timestamp="2026-01-01T08:00:00", approach=approach, vehicle_count=queue,
        queue_length=queue, avg_wait_time=wait, arrival_rate=arrival,
        congestion_level="HIGH", scenario="test",
    )


class StarvationTests(unittest.TestCase):
    def test_worst_case_cycle_bound(self):
        # 4 x max green + 4 x (yellow + all-red) = 260 s
        self.assertEqual(CFG.cycle_length([CFG.max_green_seconds] * 4), 260)

    def test_idle_approach_still_gets_min_green(self):
        readings = {a: _reading(a, 0, 0) for a in APPROACHES}
        readings["North"] = _reading("North", 500, 200, 300)
        prev = {a: CFG.default_green_seconds for a in APPROACHES}
        for _ in range(50):  # repeated cycles, North always saturated
            plan = allocate_greens(readings, prev, CFG)
            greens = {a: d.applied_green_time for a, d in plan.by_approach().items()}
            for a, g in greens.items():
                self.assertGreaterEqual(g, CFG.min_green_seconds, a)
                self.assertLessEqual(g, CFG.max_green_seconds, a)
            prev = greens

    def test_max_red_wait_bounded_under_extreme_skew(self):
        steps = [
            ("t%03d" % i, {"North": 60, "South": 0, "East": 0, "West": 0})
            for i in range(200)
        ]
        # worst-case red for any approach = full cycle minus its own min green
        bound = CFG.cycle_length([CFG.max_green_seconds] * 4) - CFG.min_green_seconds
        seen = 0
        prev = {a: CFG.default_green_seconds for a in APPROACHES}
        policy = adaptive_policy(CFG)
        for ts, arr in steps:
            readings = {a: _reading(a, 300 if a == "North" else 0, arr[a]) for a in APPROACHES}
            greens, cycle = policy(readings, prev)
            for a in APPROACHES:
                seen = max(seen, cycle - greens[a])
                self.assertGreaterEqual(greens[a], CFG.min_green_seconds)
            prev = greens
        self.assertLessEqual(seen, bound)

    def test_simulation_reports_bounded_red_wait(self):
        steps = [("t%03d" % i, {"North": 40, "South": 1, "East": 1, "West": 1}) for i in range(100)]
        m = simulate(steps, adaptive_policy(CFG), CFG, policy_name="adaptive")
        self.assertLessEqual(m.max_red_wait_sec, 260 - CFG.min_green_seconds)
        self.assertGreater(m.max_red_wait_sec, 0)


if __name__ == "__main__":
    unittest.main()
