"""Train a lightweight congestion-level classifier.

Task: given the current and recent traffic state for one approach, predict the
**next** interval's congestion level (LOW / MEDIUM / HIGH). This is a small,
well-defined, defensible task that adds decision support; the live controller
never depends on it (it keeps its deterministic timing rules and fallback).

Design choices that keep the result honest:

* features are causal (see :mod:`src.analytics.feature_engineering`);
* the split is chronological (train on the past, test on the future);
* the scaler is fit on the training split only;
* nothing is claimed beyond the test-set metrics reported by
  :mod:`src.analytics.evaluate_model`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.analytics.feature_engineering import (
    FEATURE_NAMES,
    LABELS,
    FeatureSet,
    build_features,
    chronological_split,
    to_matrix,
)
from src.common.schemas import SensorReading

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "congestion_model.joblib"


def build_pipeline() -> Pipeline:
    """Deterministic, fast, low-variance classifier for a small tabular task."""
    return Pipeline(
        steps=[
            ("scale", StandardScaler()),
            (
                "clf",
                LogisticRegression(max_iter=1000, random_state=2026),
            ),
        ]
    )


@dataclass
class TrainedModel:
    pipeline: Pipeline
    features: Sequence[str]
    labels: Sequence[str]
    train_size: int
    test_size: int

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path) if path else DEFAULT_MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "pipeline": self.pipeline,
                "features": list(self.features),
                "labels": list(self.labels),
            },
            path,
        )
        return path


def train(
    readings: Sequence[SensorReading],
    *,
    train_fraction: float = 0.7,
    model_path: Optional[Path] = None,
) -> tuple[TrainedModel, FeatureSet, FeatureSet]:
    """Train on ``readings`` and persist the model; returns the model + splits."""
    features = build_features(readings)
    if len(features) < 20:
        raise ValueError(f"not enough feature rows to train: {len(features)}")

    train_set, test_set = chronological_split(features, train_fraction)
    pipeline = build_pipeline()
    pipeline.fit(to_matrix(train_set), train_set.labels)

    model = TrainedModel(
        pipeline=pipeline,
        features=FEATURE_NAMES,
        labels=LABELS,
        train_size=len(train_set),
        test_size=len(test_set),
    )
    model.save(model_path)
    return model, train_set, test_set
