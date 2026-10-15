"""Load the trained congestion-model bundle."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import joblib

from src.analytics.feature_engineering import FEATURE_NAMES, LABELS
from src.analytics.train_model import DEFAULT_MODEL_PATH


@dataclass
class Predictor:
    pipeline: object
    features: Sequence[str]
    labels: Sequence[str]
    model_path: Path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Predictor":
        path = Path(path) if path else DEFAULT_MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"model not found: {path}. Train it with "
                f"'python -m src.analytics.run_evaluation --train'."
            )
        bundle = joblib.load(path)
        return cls(
            pipeline=bundle["pipeline"],
            features=bundle.get("features", FEATURE_NAMES),
            labels=bundle.get("labels", LABELS),
            model_path=path,
        )
