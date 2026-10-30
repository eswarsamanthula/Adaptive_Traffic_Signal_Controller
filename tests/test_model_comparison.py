"""Focused tests for the isolated offline model-comparison workflow."""
import json
import tempfile
import unittest
from pathlib import Path

from src.analytics import compare_models as comparison
from src.analytics.feature_engineering import LABELS
from tests.test_analytics import synthetic_timeline


class ModelComparisonTests(unittest.TestCase):
    def test_four_configurations_and_metrics_include_every_class(self):
        self.assertEqual(set(comparison.build_estimators()), set(comparison.MODEL_CONFIGS))
        metrics = comparison.calculate_metrics([0, 1, 2], [0, 0, 0], [0, 0, 1])
        for key in ("accuracy", "balanced_accuracy", "macro_precision", "macro_recall", "macro_f1"):
            self.assertIn(key, metrics)
        self.assertEqual(list(metrics["per_class"]), list(LABELS))
        self.assertEqual(metrics["per_class"]["HIGH"]["recall"], 0)
        self.assertEqual(len(metrics["confusion_matrix"]), 3)

    def test_chronological_run_saves_separate_models_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = comparison.run_comparison(synthetic_timeline(45), metrics_dir=root / "metrics", model_dir=root / "models", figure_dir=root / "figures")
            train, test = result["train_set"], result["test_set"]
            self.assertLessEqual(max(train.timestamps), min(test.timestamps))
            bundles = [__import__("joblib").load(path) for path in result["artifacts"].values()]
            self.assertEqual({b["test_size"] for b in bundles}, {len(test)})
            self.assertEqual({b["train_size"] for b in bundles}, {len(train)})
            self.assertEqual(len(result["artifacts"]), 4)
            self.assertTrue(all(p.exists() and p.stat().st_size for p in result["artifacts"].values()))
            self.assertTrue(all(p.exists() and p.stat().st_size for p in result["charts"]))
            for name in ("model_comparison.json", "model_comparison.csv", "model_comparison_per_class.csv", "model_comparison_confusion_matrices.json"):
                self.assertTrue((root / "metrics" / name).stat().st_size)
            json.loads((root / "metrics" / "model_comparison.json").read_text(encoding="utf-8"))

    def test_invalid_metrics_and_too_small_dataset_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "metric inputs"):
            comparison.calculate_metrics([], [], [0])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "too small"):
            comparison.run_comparison(synthetic_timeline(2), metrics_dir=Path(tmp) / "m", model_dir=Path(tmp) / "a", figure_dir=Path(tmp) / "f")


if __name__ == "__main__":
    unittest.main()
