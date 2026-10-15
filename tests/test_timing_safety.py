"""Safety tests for the signal phase state machine.

These guarantee the properties a traffic controller must never violate:
no two conflicting greens, mandatory yellow then all-red clearance before the
next green, green times inside [min, max], and safe fallback on stale data.
"""
from __future__ import annotations

import unittest

from src.common.schemas import SensorReading
from src.common.time_utils import ManualClock
from src.controller.adaptive_controller import AdaptiveController
from src.controller.fixed_time_controller import FixedTimeController
from src.controller.timing_constraints import SignalState, TimingConfig, PHASE_STATES


def reading(approach, queue=10, arrival=5, ts="2026-10-12 07:00:00"):
    return SensorReading(ts, approach, queue + arrival, queue, 10, arrival, "LOW", "custom")


class TimingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.config = TimingConfig()
        self.clock = ManualClock()
        self.controller = AdaptiveController(self.config, self.clock)
        self.controller.apply_readings(
            {a: reading(a) for a in self.config.phase_order}, decision_time="2026-10-12 07:00:00"
        )

    def _non_red(self):
        return [a for a in self.config.phase_order if self.controller.signal_for(a) != SignalState.RED]

    def test_green_then_yellow_then_all_red_then_next_green(self):
        self.controller.start()
        self.assertEqual(self.controller.state, SignalState.GREEN)
        self.assertEqual(self.controller.active_approach, "North")

        self.controller.tick(at=self.config.default_green_seconds)
        self.assertEqual(self.controller.state, SignalState.YELLOW)
        self.assertEqual(self.controller.active_approach, "North")

        self.controller.tick(at=self.config.default_green_seconds + self.config.yellow_seconds)
        self.assertEqual(self.controller.state, SignalState.ALL_RED)

        self.controller.tick(
            at=self.config.default_green_seconds
            + self.config.yellow_seconds
            + self.config.all_red_seconds
        )
        self.assertEqual(self.controller.state, SignalState.GREEN)
        self.assertEqual(self.controller.active_approach, "South")

    def test_never_two_conflicting_greens_across_a_whole_cycle(self):
        self.controller.start()
        t = 0.0
        step = 0.5
        greens_seen = set()
        while self.controller.cycle_no < 2:
            t += step
            self.controller.tick(at=t)
            non_red = self._non_red()
            self.assertLessEqual(len(non_red), 1, f"conflicting signals: {non_red}")
            greens = [a for a in self.config.phase_order if self.controller.signal_for(a) == SignalState.GREEN]
            self.assertLessEqual(len(greens), 1)
            if greens:
                greens_seen.add(greens[0])
        self.assertEqual(greens_seen, set(self.config.phase_order))

    def test_yellow_and_all_red_are_fixed_lengths(self):
        self.controller.start()
        self.controller.tick(at=self.config.default_green_seconds)  # -> yellow
        self.assertEqual(self.controller.phase_duration, self.config.yellow_seconds)
        self.controller.tick(at=self.config.default_green_seconds + self.config.yellow_seconds)
        self.assertEqual(self.controller.phase_duration, self.config.all_red_seconds)

    def test_green_durations_stay_within_bounds(self):
        # Extreme demand must still be clamped by the safety window.
        self.controller.apply_readings(
            {
                "North": reading("North", 999, 999),
                "South": reading("South", 0, 0),
                "East": reading("East", 0, 0),
                "West": reading("West", 0, 0),
            },
            decision_time="2026-10-12 07:00:00",
        )
        self.controller.start()
        for approach, greens in self.controller.schedule.items():
            self.assertGreaterEqual(greens, self.config.min_green_seconds)
            self.assertLessEqual(greens, self.config.max_green_seconds)

    def test_countdown_decreases_then_transitions(self):
        self.controller.start()
        self.clock.advance(5)
        remaining = self.controller.phase_remaining
        self.assertAlmostEqual(
            remaining, self.config.default_green_seconds - 5, delta=0.01
        )

    def test_stop_clears_all_signals(self):
        self.controller.start()
        self.controller.stop()
        self.assertEqual(self._non_red(), [])
        self.assertEqual(self.controller.state, SignalState.RED)

    def test_pause_and_resume_preserve_remaining(self):
        self.controller.start()
        self.clock.advance(4)
        self.controller.pause()
        remaining = self.controller.phase_remaining
        self.assertFalse(self.controller.running)
        self.clock.advance(100)  # time passes while paused; must not advance
        self.assertEqual(self.controller.tick(at=self.clock.now()), [])
        self.controller.resume()
        self.assertTrue(self.controller.running)
        self.assertAlmostEqual(self.controller.phase_remaining, remaining, delta=0.01)

    def test_stale_readings_fall_back_without_crashing(self):
        old = reading("North", queue=50, arrival=25, ts="2026-10-12 06:00:00")
        self.controller.apply_readings(
            {"North": old}, decision_time="2026-10-12 07:00:00"
        )
        self.controller.start()
        north = self.controller.snapshot()["decisions"]["North"]
        self.assertTrue(north["used_fallback"])
        self.assertEqual(north["applied_green_time"], self.config.default_green_seconds)

    def test_fixed_time_controller_uses_equal_default_greens(self):
        fixed = FixedTimeController(self.config, ManualClock())
        self.assertEqual(set(fixed.schedule.values()), {self.config.default_green_seconds})

    def test_next_phase_reports_clearance_follows_green(self):
        self.controller.start()
        self.assertEqual(self.controller.next_phase()["state"], SignalState.YELLOW.value)


if __name__ == "__main__":
    unittest.main()
