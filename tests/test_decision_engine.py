"""Tests for the adaptive green-time decision engine."""
from __future__ import annotations

import unittest

from src.common.schemas import SensorReading
from src.controller.decision_engine import (
    EXTEND_REASON,
    FALLBACK_REASON,
    HOLD_REASON,
    SHORTEN_REASON,
    STARTUP_REASON,
    allocate_greens,
    demand_score,
)
from src.controller.timing_constraints import TimingConfig

TS = "2026-10-12 07:00:00"


def reading(approach, queue, arrival, wait=10, ts=TS):
    return SensorReading(ts, approach, queue + arrival, queue, wait, arrival, "LOW", "custom")


def previous(config):
    return {a: config.default_green_seconds for a in config.phase_order}


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = TimingConfig()

    def test_busy_approach_extends_and_quiet_one_shortens(self):
        readings = {
            "North": reading("North", 25, 12),
            "South": reading("South", 3, 1),
            "East": reading("East", 20, 10),
            "West": reading("West", 4, 2),
        }
        result = allocate_greens(readings, previous(self.config), self.config)
        by = result.by_approach()
        self.assertGreater(by["North"].applied_green_time, self.config.default_green_seconds)
        self.assertLessEqual(by["South"].requested_green_time, self.config.default_green_seconds)
        self.assertIn("extend", by["North"].decision_reason.lower())

    def test_requested_below_min_clamped_with_reason(self):
        readings = {
            "North": reading("North", 0, 0),
            "South": reading("South", 40, 20),
            "East": reading("East", 40, 20),
            "West": reading("West", 40, 20),
        }
        result = allocate_greens(readings, previous(self.config), self.config)
        north = result.by_approach()["North"]
        self.assertEqual(north.applied_green_time, self.config.min_green_seconds)
        self.assertIn("clamped to min green", north.decision_reason)

    def test_requested_above_max_clamped_with_reason(self):
        readings = {
            "North": reading("North", 500, 300),
            "South": reading("South", 1, 1),
            "East": reading("East", 1, 1),
            "West": reading("West", 1, 1),
        }
        result = allocate_greens(readings, previous(self.config), self.config)
        north = result.by_approach()["North"]
        self.assertEqual(north.applied_green_time, self.config.max_green_seconds)
        self.assertIn("clamped to max green", north.decision_reason)

    def test_all_equal_demand_holds(self):
        readings = {a: reading(a, 10, 5) for a in self.config.phase_order}
        result = allocate_greens(readings, previous(self.config), self.config)
        for decision in result.decisions:
            self.assertEqual(decision.applied_green_time, self.config.default_green_seconds)
            self.assertEqual(decision.decision_reason, HOLD_REASON)

    def test_equal_demand_holds_any_previous_green(self):
        readings = {a: reading(a, 2, 1) for a in self.config.phase_order}
        for value in (self.config.default_green_seconds, 50):
            prior = {a: value for a in self.config.phase_order}
            result = allocate_greens(readings, prior, self.config)
            for decision in result.decisions:
                self.assertEqual(decision.requested_green_time, value)
                self.assertEqual(decision.applied_green_time, value)
                self.assertEqual(decision.decision_reason, HOLD_REASON)

    def test_no_readings_uses_startup_default(self):
        result = allocate_greens({}, previous(self.config), self.config)
        self.assertTrue(result.fallback_used)
        for decision in result.decisions:
            self.assertEqual(decision.applied_green_time, self.config.default_green_seconds)
            self.assertEqual(decision.decision_reason, STARTUP_REASON)

    def test_missing_or_stale_reading_falls_back(self):
        readings = {
            "North": reading("North", 25, 12),
            "South": None,
            "East": reading("East", 20, 10),
            "West": reading("West", 4, 2),
        }
        result = allocate_greens(
            readings, previous(self.config), self.config, stale={"West"}
        )
        by = result.by_approach()
        self.assertTrue(result.fallback_used)
        self.assertEqual(by["South"].decision_reason, FALLBACK_REASON)
        self.assertEqual(by["West"].decision_reason, FALLBACK_REASON)
        self.assertEqual(by["South"].applied_green_time, self.config.default_green_seconds)

    def test_cycle_length_is_sum_of_greens_plus_clearance(self):
        readings = {a: reading(a, 10, 5) for a in self.config.phase_order}
        result = allocate_greens(readings, previous(self.config), self.config)
        greens = [d.applied_green_time for d in result.decisions]
        expected = sum(greens) + len(self.config.phase_order) * (
            self.config.yellow_seconds + self.config.all_red_seconds
        )
        self.assertEqual(result.cycle_length_sec, expected)
        self.assertEqual(expected, 4 * self.config.default_green_seconds + 20)

    def test_demand_score_uses_configured_weights(self):
        config = TimingConfig(score_queue_weight=2.0, score_arrival_weight=1.0, score_wait_weight=0.0)
        self.assertEqual(demand_score(reading("North", 10, 4), config), 10 * 2.0 + 4)

    def test_applied_green_always_within_bounds(self):
        for queue in (0, 1, 5, 50, 500):
            readings = {a: reading(a, queue, queue) for a in self.config.phase_order}
            readings["North"] = reading("North", queue, queue)
            result = allocate_greens(readings, previous(self.config), self.config)
            for decision in result.decisions:
                self.assertGreaterEqual(decision.applied_green_time, self.config.min_green_seconds)
                self.assertLessEqual(decision.applied_green_time, self.config.max_green_seconds)


if __name__ == "__main__":
    unittest.main()
