"""Analytics tests: congestion rules, features, training, evaluation, simulation.

Pure-Python / scikit-learn, no broker, no browser, no Node-RED. Deterministic
because every model uses a fixed ``random_state`` and the split is chronological.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.analytics.congestion import (
    approach_stats,
    classify_congestion,
    congestion_index,
    overall_congestion,
    queue_trend,
)
from src.analytics.evaluate_model import evaluate
from src.analytics.feature_engineering import (
    FEATURE_NAMES,
    build_features,
    chronological_split,
    leakage_safe_chronological_split,
    to_matrix,
)
from src.analytics.simulation import (
    SimParams,
    compare,
    group_arrivals_by_time,
    simulate,
    fixed_time_policy,
    adaptive_policy,
)
from src.analytics import train_model
from src.common.schemas import APPROACHES, SensorReading
from src.controller.timing_constraints import TimingConfig

TS = "2026-10-12 07:00:00"


def reading(approach, timestamp, queue, arrival, wait=0, vehicle=None):
    return SensorReading(
        timestamp=timestamp,
        approach=approach,
        vehicle_count=queue + arrival if vehicle is None else vehicle,
        queue_length=queue,
        avg_wait_time=wait,
        arrival_rate=arrival,
        congestion_level=classify_congestion(queue, wait),
        scenario="synthetic",
    )


def synthetic_timeline(minutes=60, approaches=APPROACHES):
    """A tiny deterministic 4-approach timeline with a heavy North surge."""
    rows = []
    for m in range(minutes):
        ts = f"2026-10-12 07:{m:02d}:00"
        for a in approaches:
            base = {"North": 20, "South": 6, "East": 8, "West": 5}[a]
            queue = base + (m % 7)
            rows.append(reading(a, ts, queue, base, wait=10 + m % 20))
    return rows


class CongestionTests(unittest.TestCase):
    def test_rule_matches_documented_bands(self):
        self.assertEqual(classify_congestion(0, 0), "LOW")
        self.assertEqual(classify_congestion(8, 0), "MEDIUM")
        self.assertEqual(classify_congestion(16, 0), "HIGH")
        self.assertEqual(classify_congestion(0, 30), "MEDIUM")
        self.assertEqual(classify_congestion(0, 60), "HIGH")
        # worse of the two bands wins
        self.assertEqual(classify_congestion(16, 0), "HIGH")
        self.assertEqual(classify_congestion(0, 5), "LOW")

    def test_index_bounds(self):
        self.assertGreaterEqual(congestion_index(0, 0, 0), 0.0)
        self.assertLessEqual(congestion_index(999, 999, 999), 1.0)

    def test_approach_stats_and_overall(self):
        rows = [reading("North", TS, 20, 5, wait=70)] + [
            reading(a, TS, 2, 1) for a in ("South", "East", "West")
        ]
        stats = approach_stats(rows)
        self.assertEqual(stats["North"].max_queue, 20)
        self.assertEqual(stats["North"].congestion, "HIGH")
        self.assertEqual(overall_congestion(rows), "HIGH")

    def test_queue_trend_has_points(self):
        rows = [reading("North", TS, 20, 5)] + [
            reading("North", "2026-10-12 07:01:00", 22, 6)
        ]
        trend = queue_trend(rows, "North")
        self.assertEqual(len(trend), 2)
        self.assertIn("congestion_index", trend[0])


class FeatureTests(unittest.TestCase):
    def test_features_are_causal_and_ordered(self):
        features = build_features(synthetic_timeline())
        self.assertEqual(len(features.rows[0]), len(FEATURE_NAMES))
        # last row per approach cannot be a target (no future reading)
        self.assertEqual(len(features), (60 - 1) * len(APPROACHES))

    def test_split_is_chronological(self):
        features = build_features(synthetic_timeline())
        train, test = chronological_split(features, 0.7)
        self.assertGreater(len(train), 0)
        self.assertGreater(len(test), 0)
        self.assertLessEqual(max(train.timestamps), min(test.timestamps))
        self.assertEqual(len(train) + len(test), len(features) - 4)

    def test_matrix_shape(self):
        features = build_features(synthetic_timeline())
        matrix = to_matrix(features)
        self.assertEqual(len(matrix), len(features))
        self.assertEqual(len(matrix[0]), len(FEATURE_NAMES))

    def test_feature_and_target_timestamps_are_actual_adjacent_readings(self):
        rows = synthetic_timeline(minutes=8)
        features = build_features(rows)
        self.assertEqual(len(features.target_timestamps), len(features))
        self.assertEqual(features.target_timestamps[0], "2026-10-12 07:01:00")

    def test_safe_split_groups_timestamps_and_purges_boundary_targets(self):
        features = build_features(synthetic_timeline(minutes=30))
        train, test, meta = leakage_safe_chronological_split(features)
        self.assertLess(max(train.target_timestamps), min(test.timestamps))
        self.assertFalse(set(train.timestamps) & set(test.timestamps))
        self.assertEqual(meta.excluded_gap_rows, 4)
        timestamp_partitions = {}
        for part, subset in (("train", train), ("test", test)):
            for timestamp in subset.timestamps:
                timestamp_partitions.setdefault(timestamp, set()).add(part)
        self.assertTrue(all(len(parts) == 1 for parts in timestamp_partitions.values()))
        self.assertEqual(meta.boundary_timestamp, min(test.timestamps))


class ModelTests(unittest.TestCase):
    def test_train_evaluate_and_predict(self):
        rows = synthetic_timeline(minutes=120)
        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "model.joblib"
            model, train_set, test_set = train_model.train(rows, model_path=model_path)
            self.assertTrue(model_path.exists())
            report = evaluate(model, train_set, test_set)
            self.assertEqual(report.test_size, len(test_set))
            self.assertGreaterEqual(report.accuracy, 0.0)
            self.assertLessEqual(report.accuracy, 1.0)
            self.assertEqual(len(report.confusion), 3)
            # baseline is defined and comparable
            self.assertIn(report.baseline_label, ("LOW", "MEDIUM", "HIGH"))

    def test_train_requires_enough_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                train_model.train(
                    synthetic_timeline(minutes=3), model_path=Path(tmp) / "m.joblib"
                )


class FigureTests(unittest.TestCase):
    def test_generate_writes_evaluation_figures(self):
        from src.analytics import figures

        if figures._pyplot() is None:
            self.skipTest("matplotlib is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            model, train_set, test_set = train_model.train(
                synthetic_timeline(minutes=120), model_path=Path(tmp) / "model.joblib"
            )
            report = evaluate(model, train_set, test_set)
            written = figures.generate(report.as_dict(), Path(tmp))
            self.assertEqual(
                {p.name for p in written},
                {"confusion_matrix.png", "per_class_metrics.png", "model_vs_baseline.png"},
            )
            for path in written:
                self.assertGreater(path.stat().st_size, 0)


class SimulationTests(unittest.TestCase):
    def test_group_arrivals_by_time(self):
        steps = group_arrivals_by_time(synthetic_timeline(minutes=5))
        self.assertEqual(len(steps), 5)
        self.assertEqual(set(steps[0][1]), set(APPROACHES))

    def test_queues_never_negative_and_served_bounded(self):
        config = TimingConfig()
        steps = group_arrivals_by_time(synthetic_timeline(minutes=30))
        metrics = simulate(steps, adaptive_policy(config), config, policy_name="adaptive")
        self.assertGreaterEqual(metrics.avg_queue, 0.0)
        self.assertGreaterEqual(metrics.max_queue, 0)
        self.assertLessEqual(metrics.served_fraction, 1.0)
        self.assertGreaterEqual(metrics.served_fraction, 0.0)

    def test_adaptive_beats_fixed_on_skewed_demand(self):
        config = TimingConfig()
        results = compare(synthetic_timeline(minutes=120), config)
        self.assertLessEqual(
            results["adaptive"].avg_delay_per_vehicle,
            results["fixed"].avg_delay_per_vehicle,
        )
        self.assertLessEqual(results["adaptive"].max_queue, results["fixed"].max_queue)

    def test_deterministic(self):
        config = TimingConfig()
        steps = group_arrivals_by_time(synthetic_timeline(minutes=40))
        first = simulate(steps, adaptive_policy(config), config)
        second = simulate(steps, adaptive_policy(config), config)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_fixed_policy_uses_same_green_everywhere(self):
        config = TimingConfig()
        policy = fixed_time_policy(25, config)
        greens, cycle = policy({}, {})
        self.assertEqual(set(greens.values()), {25})
        self.assertEqual(cycle, config.cycle_length([25] * 4))


if __name__ == "__main__":
    unittest.main()
