"""Regression tests for leakage-safe comparison and advanced evaluations."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.analytics import benchmark_models, closed_loop_evaluation
from src.analytics.compare_models import PROJECT_ROOT, dataset_temporal_diagnostics, run_comparison
from src.analytics.feature_engineering import build_features, leakage_safe_chronological_split
from tests.test_analytics import synthetic_timeline


class AdvancedEvaluationTests(unittest.TestCase):
    def test_all_models_use_same_leakage_safe_split(self):
        live_model = PROJECT_ROOT / "models" / "congestion_model.joblib"
        before = hashlib.sha256(live_model.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_comparison(synthetic_timeline(60), metrics_dir=root/"metrics",
                                   model_dir=root/"models", figure_dir=root/"figures")
            boundary = result["payload"]["metadata"]["split_details"]
            self.assertLess(boundary["latest_train_target_timestamp"], boundary["first_test_feature_timestamp"])
            self.assertEqual(boundary["train_rows"], len(result["train_set"]))
            self.assertEqual(boundary["test_rows"], len(result["test_set"]))
            for bundle_path in result["artifacts"].values():
                import joblib
                bundle = joblib.load(bundle_path)
                self.assertEqual(bundle["split_metadata"], boundary)
        self.assertEqual(hashlib.sha256(live_model.read_bytes()).hexdigest(), before)

    def test_irregular_approach_reading_uses_actual_next_timestamp(self):
        rows = [r for r in synthetic_timeline(30)
                if not (r.approach == "North" and r.timestamp == "2026-10-12 07:05:00")]
        features = build_features(rows)
        index = next(i for i, (approach, timestamp) in enumerate(zip(features.approaches, features.timestamps))
                     if approach == "North" and timestamp == "2026-10-12 07:04:00")
        self.assertEqual(features.target_timestamps[index], "2026-10-12 07:06:00")
        diagnostic = dataset_temporal_diagnostics(rows)
        self.assertGreater(diagnostic["approach_coverage_per_timestamp"]["timestamps_missing_approaches"], 0)
        train, test, _ = leakage_safe_chronological_split(features)
        self.assertLess(max(train.target_timestamps), min(test.timestamps))

    def test_rss_and_cpu_collection_errors_are_reported(self):
        from sklearn.linear_model import LogisticRegression
        with patch("src.analytics.benchmark_models.psutil.Process", side_effect=RuntimeError("unavailable")):
            measurement = benchmark_models._measure_fit(LogisticRegression(), [[0.0], [1.0], [2.0]], [0, 0, 1])
        self.assertIsNone(measurement["process_cpu_seconds"])
        self.assertIn("unavailable", measurement["cpu_measurement_error"])
        self.assertIsNone(measurement["peak_process_rss_bytes"])
        self.assertIn("unavailable", measurement["rss_sampling_error"])

    def test_benchmark_measurements_and_saved_sizes_are_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = benchmark_models.run_benchmark(
                synthetic_timeline(60), repetitions=1, warmups=1,
                inference_repetitions=5, batch_repetitions=2,
                output_dir=root/"metrics", figure_dir=root/"figures", model_dir=root/"models")
            self.assertEqual(len(payload["models"]), 4)
            for name, model in payload["models"].items():
                inf = model["inference"]["single_row_ms"]
                self.assertLessEqual(inf["median"], inf["p95"])
                self.assertLessEqual(inf["p95"], inf["p99"])
                self.assertLessEqual(inf["p99"], inf["max"])
                self.assertGreater(model["inference"]["batch_samples_per_second"], 0)
                self.assertGreaterEqual(model["training"]["training_wall_seconds"]["mean"], 0)
                self.assertGreaterEqual(model["training"]["process_cpu_seconds"]["mean"], 0)
                self.assertGreater(model["artifact"]["size_bytes"], 0)
                self.assertEqual(Path(model["artifact"]["path"]).stat().st_size, model["artifact"]["size_bytes"])
            self.assertTrue((root/"metrics"/"model_benchmark_runs.csv").stat().st_size)

    def test_closed_loop_uses_isolated_outputs_and_records_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = closed_loop_evaluation.run_closed_loop(cycles=1, output_dir=root/"metrics", figure_dir=root/"figures")
            self.assertGreater(result["phase_events"]["actual_state_transitions"], 0)
            self.assertEqual(result["safety"]["timeline_violations"], 0)
            self.assertEqual(result["safety"]["stop_reset_actuator_violations"], 0)
            self.assertEqual(result["safety"]["stop_actuator_green_count"], 0)
            self.assertEqual(result["safety"]["successful_stop"]["red_acknowledgements"], 4)
            self.assertEqual(result["safety"]["successful_reset"]["red_acknowledgements"], 4)
            self.assertEqual(result["safety"]["fault_injection_stop_result"]["status"], "failed")
            self.assertEqual(result["safety"]["fault_injection_reset_result"]["status"], "failed")
            self.assertTrue(result["safety"]["all_negative_tests_rejected"])
            self.assertTrue(result["acknowledgements"]["controller_state_not_applied_before_ack"])
            self.assertTrue(result["acknowledgements"]["mismatched_ack_ignored_without_state_change"])
            self.assertFalse(result["environment"]["real_mqtt_network"])
            for name in ("closed_loop_summary.json", "closed_loop_summary.csv", "closed_loop_latency_samples.csv",
                         "closed_loop_phase_events.csv", "closed_loop_control_stability.csv",
                         "closed_loop_safety_checks.json", "closed_loop_fixed_vs_adaptive.csv"):
                self.assertGreater((root/"metrics"/name).stat().st_size, 0)
            json.loads((root/"metrics"/"closed_loop_safety_checks.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
