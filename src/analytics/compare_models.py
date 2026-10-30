"""Offline, chronological comparison of congestion classifiers.

Run from the repository root with ``python -m src.analytics.compare_models``.
Artifacts are written separately from the live Logistic Regression model and
existing evaluation outputs.
"""
from __future__ import annotations

import csv
import json
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, confusion_matrix,
    f1_score, precision_recall_fscore_support, precision_score, recall_score)
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

from src.analytics.feature_engineering import (FEATURE_NAMES, LABELS, FeatureSet,
    build_features, leakage_safe_chronological_split, to_matrix)
from src.simulator.traffic_replay import TrafficReplay

PROJECT_ROOT = Path(__file__).resolve().parents[2]
METRICS_DIR = PROJECT_ROOT / "results" / "metrics"
FIGURES_DIR = PROJECT_ROOT / "results" / "figures"
MODEL_DIR = PROJECT_ROOT / "models" / "comparison"
SEED = 2026
TRAIN_FRACTION = 0.70
ROUND_DIGITS = 4

# One source of truth for names, configurations, and construction.
MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "Logistic Regression": {"artifact": "logistic_regression.joblib", "parameters": {"scaler": "StandardScaler", "max_iter": 1000, "random_state": SEED}},
    "Random Forest": {"artifact": "random_forest.joblib", "parameters": {"n_estimators": 300, "max_features": "sqrt", "random_state": SEED, "n_jobs": 1, "class_weight": None}},
    "Gradient Boosting": {"artifact": "gradient_boosting.joblib", "parameters": {"n_estimators": 150, "learning_rate": 0.05, "max_depth": 2, "random_state": SEED}},
    "MLP Neural Network": {"artifact": "mlp_neural_network.joblib", "parameters": {"scaler": "StandardScaler", "hidden_layer_sizes": (64, 32), "alpha": 0.0001, "max_iter": 500, "random_state": SEED}},
}


def build_estimators() -> dict[str, Any]:
    """Build fresh deterministic candidates from MODEL_CONFIGS."""
    return {
        "Logistic Regression": Pipeline([("scale", StandardScaler()), ("clf", LogisticRegression(max_iter=1000, random_state=SEED))]),
        "Random Forest": RandomForestClassifier(n_estimators=300, max_features="sqrt", random_state=SEED, n_jobs=1, class_weight=None),
        "Gradient Boosting": GradientBoostingClassifier(n_estimators=150, learning_rate=0.05, max_depth=2, random_state=SEED),
        "MLP Neural Network": Pipeline([("scale", StandardScaler()), ("clf", MLPClassifier(hidden_layer_sizes=(64, 32), alpha=0.0001, max_iter=500, random_state=SEED))]),
    }


def validate_feature_sets(train: FeatureSet, test: FeatureSet) -> None:
    if len(train) < 2 or len(test) < 1:
        raise ValueError(f"insufficient data: need at least 2 training and 1 test feature rows (got {len(train)} and {len(test)})")
    if not train.rows or not test.rows or any(len(row) != len(FEATURE_NAMES) for row in train.rows + test.rows):
        raise ValueError("invalid feature matrix: expected finite rows with the configured feature width")
    if any(label not in range(len(LABELS)) for label in train.labels + test.labels):
        raise ValueError("invalid target labels; expected LOW, MEDIUM, HIGH class IDs")
    if set(train.timestamps) & set(test.timestamps):
        raise ValueError("feature timestamps appear in both partitions")
    if max(train.target_timestamps) >= min(test.timestamps):
        raise ValueError("training target timestamps overlap the held-out test period")


def dataset_temporal_diagnostics(readings: list[Any]) -> dict[str, Any]:
    """Describe timestamp cadence and approach coverage without altering data."""
    per_time: dict[str, set[str]] = defaultdict(set)
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    for reading in readings:
        per_time[reading.timestamp].add(reading.approach)
        pairs[(reading.timestamp, reading.approach)] += 1
    timestamps = sorted(per_time)
    gaps = []
    for left, right in zip(timestamps, timestamps[1:]):
        seconds = (datetime.strptime(right, "%Y-%m-%d %H:%M:%S") - datetime.strptime(left, "%Y-%m-%d %H:%M:%S")).total_seconds()
        if seconds != 60:
            gaps.append({"from": left, "to": right, "seconds": seconds})
    expected = {"North", "South", "East", "West"}
    return {
        "unique_reading_timestamps": len(timestamps),
        "approach_coverage_per_timestamp": {"expected_approaches": sorted(expected),
            "timestamps_missing_approaches": sum(bool(expected - approaches) for approaches in per_time.values()),
            "missing_approach_reading_slots": sum(len(expected - approaches) for approaches in per_time.values()),
            "duplicate_timestamp_approach_pairs": sum(count - 1 for count in pairs.values() if count > 1)},
        "one_minute_cadence_deviations": len(gaps),
        "cadence_deviation_examples": gaps[:10],
        "handling": "irregular intervals are preserved and next-target timestamps come from actual next readings per approach; timestamp grouping and target-boundary purging remain in force",
    }


def calculate_metrics(y_true: list[int], y_pred: list[int], train_labels: list[int]) -> dict[str, Any]:
    """Return metrics with explicit LOW/MEDIUM/HIGH ordering and zero division."""
    if not y_true or len(y_true) != len(y_pred) or not train_labels:
        raise ValueError("metric inputs must contain matching non-empty predictions and training labels")
    classes = list(range(len(LABELS)))
    majority = max(classes, key=lambda c: (train_labels.count(c), -c))
    acc = accuracy_score(y_true, y_pred)
    p, r, f, support = precision_recall_fscore_support(y_true, y_pred, labels=classes, zero_division=0)
    return {
        "accuracy": round(float(acc), ROUND_DIGITS),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), ROUND_DIGITS),
        "macro_precision": round(float(precision_score(y_true, y_pred, labels=classes, average="macro", zero_division=0)), ROUND_DIGITS),
        "macro_recall": round(float(recall_score(y_true, y_pred, labels=classes, average="macro", zero_division=0)), ROUND_DIGITS),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=classes, average="macro", zero_division=0)), ROUND_DIGITS),
        "weighted_f1": round(float(f1_score(y_true, y_pred, labels=classes, average="weighted", zero_division=0)), ROUND_DIGITS),
        "majority_baseline_accuracy": round(float(accuracy_score(y_true, [majority] * len(y_true))), ROUND_DIGITS),
        "majority_label": LABELS[majority],
        "difference_from_majority_pp": round(float((acc - accuracy_score(y_true, [majority] * len(y_true))) * 100), ROUND_DIGITS),
        "beats_majority_baseline": bool(acc > accuracy_score(y_true, [majority] * len(y_true))),
        "per_class": {name: {"precision": round(float(p[i]), ROUND_DIGITS), "recall": round(float(r[i]), ROUND_DIGITS), "f1": round(float(f[i]), ROUND_DIGITS), "support": int(support[i])} for i, name in enumerate(LABELS)},
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=classes).tolist(),
    }


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"cannot write empty CSV: {path}")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_comparison_figures(results: dict[str, dict[str, Any]], output_dir: Path = FIGURES_DIR) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        raise RuntimeError(f"matplotlib is required to generate comparison charts: {exc}") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    names = list(MODEL_CONFIGS)
    colors = ["#2364aa", "#3da35d", "#f4a261"]
    outputs: list[Path] = []
    fig, ax = plt.subplots(figsize=(10, 5.5))
    x = list(range(len(names)))
    width = 0.24
    for j, metric in enumerate(("accuracy", "balanced_accuracy", "macro_f1")):
        vals = [results[n][metric] for n in names]
        bars = ax.bar([i + (j - 1) * width for i in x], vals, width, label=metric.replace("_", " ").title(), color=colors[j])
        ax.bar_label(bars, fmt="%.3f", fontsize=8, padding=2)
    ax.set_ylim(0, 1.12); ax.set_ylabel("Score (0–1)"); ax.set_xticks(x, names, rotation=12, ha="right"); ax.set_title("Offline model comparison"); ax.legend(); fig.tight_layout()
    p = output_dir / "model_comparison_metrics.png"; fig.savefig(p, dpi=150); plt.close(fig); outputs.append(p)
    fig, ax = plt.subplots(figsize=(10, 5.5))
    width = 0.2
    for j, label in enumerate(LABELS):
        vals = [results[n]["per_class"][label]["f1"] for n in names]
        ax.bar([i + (j - 1) * width for i in x], vals, width, label=label)
    ax.set_ylim(0, 1); ax.set_ylabel("F1 score (0–1)"); ax.set_xticks(x, names, rotation=12, ha="right"); ax.set_title("Per-class F1"); ax.legend(); fig.tight_layout()
    p = output_dir / "model_comparison_per_class_f1.png"; fig.savefig(p, dpi=150); plt.close(fig); outputs.append(p)
    fig, axes = plt.subplots(2, 2, figsize=(9, 8))
    for ax, name in zip(axes.flat, names):
        ax.imshow(results[name]["confusion_matrix"], cmap="Blues", vmin=0)
        ax.set_title(name); ax.set_xticks(range(3), LABELS); ax.set_yticks(range(3), LABELS)
        ax.set_xlabel("Predicted"); ax.set_ylabel("Actual")
        for i in range(3):
            for j in range(3): ax.text(j, i, str(results[name]["confusion_matrix"][i][j]), ha="center", va="center")
    fig.suptitle("Confusion matrices (actual rows, predicted columns)"); fig.tight_layout()
    p = output_dir / "model_comparison_confusion_matrices.png"; fig.savefig(p, dpi=150); plt.close(fig); outputs.append(p)
    return outputs


def run_comparison(readings: list[Any], *, metrics_dir: Path = METRICS_DIR, model_dir: Path = MODEL_DIR, figure_dir: Path = FIGURES_DIR) -> dict[str, Any]:
    features = build_features(readings)
    if len(features) < 20:
        raise ValueError(f"dataset too small: need at least 20 feature rows, got {len(features)}")
    train_set, test_set, split_metadata = leakage_safe_chronological_split(features, TRAIN_FRACTION)
    validate_feature_sets(train_set, test_set)
    x_train, x_test = to_matrix(train_set), to_matrix(test_set)
    estimators = build_estimators()
    results: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, Path] = {}
    for name, estimator in estimators.items():
        caught: list[str] = []
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", ConvergenceWarning)
            estimator.fit(x_train, train_set.labels)
        caught = [str(w.message) for w in recorded if issubclass(w.category, ConvergenceWarning)]
        for warning_text in caught:
            print(f"Convergence warning for {name}: {warning_text}")
        prediction = [int(v) for v in estimator.predict(x_test)]
        metric = calculate_metrics(list(test_set.labels), prediction, list(train_set.labels))
        if name == "MLP Neural Network":
            fitted_mlp = estimator.named_steps["clf"]
            metric["convergence"] = {"converged": not bool(caught), "iterations": int(fitted_mlp.n_iter_), "warnings": caught}
        results[name] = metric
        artifact_path = model_dir / MODEL_CONFIGS[name]["artifact"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model_name": name, "configuration": MODEL_CONFIGS[name]["parameters"], "features": list(FEATURE_NAMES), "labels": list(LABELS), "label_to_id": {v: i for i, v in enumerate(LABELS)}, "train_size": len(train_set), "test_size": len(test_set), "random_seed": SEED, "split_metadata": split_metadata.as_dict(), "estimator": estimator, "test_predictions": prediction}, artifact_path)
        loaded = joblib.load(artifact_path)
        if list(map(int, loaded["estimator"].predict(x_test))) != prediction:
            raise RuntimeError(f"saved model failed prediction round-trip: {artifact_path}")
        artifacts[name] = artifact_path

    baseline_label = max(range(len(LABELS)), key=lambda c: (train_set.labels.count(c), -c))
    baseline = calculate_metrics(list(test_set.labels), [baseline_label] * len(test_set), list(train_set.labels))
    metadata = {"dataset_path": "dataset/traffic_data.csv", "reading_count": len(readings), "feature_rows": len(features), "train_size": len(train_set), "test_size": len(test_set), "train_fraction": TRAIN_FRACTION, "split": "timestamp-grouped leakage-safe chronological split with target-boundary purge", "split_details": split_metadata.as_dict(), "dataset_temporal_diagnostics": dataset_temporal_diagnostics(readings), "class_support": {name: {"train": train_set.labels.count(i), "test": test_set.labels.count(i)} for i, name in enumerate(LABELS)}, "features": list(FEATURE_NAMES), "labels": list(LABELS), "target": "next interval congestion label", "rounding_digits": ROUND_DIGITS, "class_weighting": "none (Random Forest is unweighted)", "notes": ["FeatureSet is constructed once and split once for every model.", "Test scores were not used for parameter selection.", "All rows sharing a feature timestamp are assigned to the same partition; target-crossing boundary rows are purged.", "Models are offline decision-support experiments and do not control live signal timings.", "Dataset imbalance can depress minority MEDIUM/HIGH recall; interpret macro F1, balanced accuracy and HIGH recall alongside accuracy."]}
    payload = {"metadata": metadata, "model_configurations": MODEL_CONFIGS, "models": results, "majority_baseline": baseline, "baseline_label": LABELS[baseline_label]}
    _save_json(metrics_dir / "model_comparison.json", payload)
    _save_json(metrics_dir / "model_comparison_confusion_matrices.json", {"labels": list(LABELS), "matrices": {k: v["confusion_matrix"] for k, v in results.items()}})
    overall_rows = [{"model": name, **{k: v for k, v in metric.items() if k not in ("per_class", "confusion_matrix", "convergence")}, "high_recall": metric["per_class"]["HIGH"]["recall"], "convergence_status": ("converged" if metric.get("convergence", {}).get("converged") else "not_applicable" if "convergence" not in metric else "not_converged")} for name, metric in results.items()]
    overall_rows.append({"model": "Majority-class baseline", **{k: baseline[k] for k in ("accuracy", "balanced_accuracy", "macro_precision", "macro_recall", "macro_f1", "weighted_f1", "majority_baseline_accuracy", "majority_label", "difference_from_majority_pp", "beats_majority_baseline")}, "high_recall": baseline["per_class"]["HIGH"]["recall"], "convergence_status": "not_applicable"})
    _write_csv(metrics_dir / "model_comparison.csv", overall_rows)
    per_rows = [{"model": name, "label": label, **values} for name, metric in results.items() for label, values in metric["per_class"].items()]
    _write_csv(metrics_dir / "model_comparison_per_class.csv", per_rows)
    charts = generate_comparison_figures(results, figure_dir)
    winner = max(results, key=lambda n: results[n]["macro_f1"])
    payload["winner_by_macro_f1"] = winner
    _save_json(metrics_dir / "model_comparison.json", payload)
    print(f"{'Model':<24} {'Accuracy':>9} {'Balanced':>9} {'Macro F1':>9} {'HIGH recall':>12} {'vs majority pp':>15}")
    for row in overall_rows[:-1]: print(f"{row['model']:<24} {row['accuracy']:>9.4f} {row['balanced_accuracy']:>9.4f} {row['macro_f1']:>9.4f} {row['high_recall']:>12.4f} {row['difference_from_majority_pp']:>15.4f}")
    print(f"Winner by macro F1: {winner}. Statistical selection does not authorize replacing the deterministic live controller.")
    print(f"Metrics: {metrics_dir}; models: {model_dir}; charts: {figure_dir}")
    return {"payload": payload, "artifacts": artifacts, "charts": charts, "train_set": train_set, "test_set": test_set}


def main() -> int:
    try:
        readings = TrafficReplay.from_settings().readings
        if not readings:
            raise ValueError("dataset contains no valid traffic readings")
        run_comparison(readings,
            metrics_dir=METRICS_DIR / "temporal_corrected",
            model_dir=MODEL_DIR / "temporal_corrected",
            figure_dir=FIGURES_DIR / "temporal_corrected")
        return 0
    except Exception as exc:
        print(f"Model comparison failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
