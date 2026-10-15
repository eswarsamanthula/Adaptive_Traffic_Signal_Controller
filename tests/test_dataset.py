"""Dataset + simulator tests.

Run without pytest:  python -m unittest discover -s tests -v
"""
import csv
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.schemas import APPROACHES, TRAFFIC_COLUMNS
from src.simulator.scenario_manager import ScenarioManager
from src.simulator.traffic_replay import (
    DEFAULT_CSV_PATH,
    DatasetValidationError,
    TrafficReplay,
)


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(TRAFFIC_COLUMNS))
        for row in rows:
            writer.writerow(row)


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replay = TrafficReplay(DEFAULT_CSV_PATH)
        cls.report = cls.replay.validate()

    def test_dataset_exists(self):
        self.assertTrue(DEFAULT_CSV_PATH.exists(), f"missing {DEFAULT_CSV_PATH}")

    def test_columns_and_row_counts(self):
        self.assertTrue(self.report.columns_ok)
        self.assertEqual(self.report.missing_columns, [])
        self.assertEqual(self.report.total_rows, 5000)
        self.assertEqual(self.report.valid_rows, 5000)
        self.assertEqual(self.report.invalid_rows, 0)

    def test_readings_are_chronological_with_four_approaches(self):
        readings = self.replay.readings
        self.assertEqual([r.approach for r in readings[:4]], list(APPROACHES))
        timestamps = [r.timestamp for r in readings]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_scenarios_present(self):
        expected = {"Low traffic", "Normal", "Rush hour", "Sudden congestion", "Uneven load"}
        self.assertTrue(expected.issubset(set(self.report.scenarios)))

    def test_scenario_manager_filters(self):
        manager = ScenarioManager(self.replay.readings)
        rush = manager.filter("Rush hour")
        self.assertTrue(rush)
        self.assertTrue(all(r.scenario == "Rush hour" for r in rush))
        self.assertIn("Rush hour", manager.scenarios())

    def test_replay_controls_start_stop_reset(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0)
        seen = []
        replay.start(callback=seen.append)
        replay.stop()
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual(replay.status, "stopped")
        replay.reset()
        self.assertEqual(replay.position, 0)

    def test_pause_holds_position_and_resume_continues(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0.05, speed=1.0)
        seen = []
        replay.start(callback=lambda r: seen.append(r.timestamp))
        time.sleep(0.12)
        replay.pause()
        self.assertTrue(replay.is_paused)
        paused_at = replay.position
        time.sleep(0.15)
        self.assertLessEqual(replay.position - paused_at, 1)
        replay.resume()
        self.assertFalse(replay.is_paused)
        time.sleep(0.12)
        replay.stop()
        self.assertEqual(seen, sorted(seen))

    def test_invalid_rows_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.csv")
            _write_csv(
                path,
                [
                    ["2026-10-12 03:00:00", "North", "0", "0", "0", "0", "LOW", "Low traffic"],
                    ["not-a-timestamp", "South", "0", "0", "0", "0", "LOW", "Low traffic"],
                    ["2026-10-12 03:00:00", "Elsewhere", "0", "0", "0", "0", "LOW", "Low traffic"],
                ],
            )
            report = TrafficReplay(path).validate()
            self.assertEqual(report.valid_rows, 1)
            self.assertEqual(report.invalid_rows, 2)
            self.assertFalse(report.ok)

    def test_invalid_rows_fail_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.csv")
            _write_csv(
                path,
                [["bad", "North", "0", "0", "0", "0", "LOW", "Low traffic"]],
            )
            with self.assertRaises(DatasetValidationError):
                TrafficReplay(path, invalid_row_policy="fail").load()

    def test_missing_columns_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "narrow.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write("Timestamp,Approach\n2026-10-12 03:00:00,North\n")
            with self.assertRaises(DatasetValidationError):
                TrafficReplay(path).load()

    def test_missing_values_are_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "blank.csv")
            _write_csv(
                path,
                [["2026-10-12 03:00:00", "North", "", "0", "0", "0", "LOW", "Low traffic"]],
            )
            report = TrafficReplay(path).validate()
            self.assertEqual(report.valid_rows, 0)
            self.assertEqual(report.invalid_rows, 1)


if __name__ == "__main__":
    unittest.main()
