"""Evaluate the congestion classifier against a naive majority-class baseline.

Reports accuracy, macro precision/recall/F1, per-class support and a confusion
matrix. The baseline predicts the most frequent training label; a model is only
described as useful if it beats that baseline on the held-out (future) test set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)

from src.analytics.feature_engineering import LABELS, FeatureSet, to_matrix
from src.analytics.train_model import TrainedModel


@dataclass
class EvaluationReport:
    train_size: int
    test_size: int
    labels: Sequence[str]
    accuracy: float
    precision_macro: float
    recall_macro: float
    f1_macro: float
    baseline_accuracy: float
    baseline_label: str
    confusion: List[List[int]] = field(default_factory=list)
    per_class: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def beats_baseline(self) -> bool:
        return self.accuracy > self.baseline_accuracy

    def as_dict(self) -> dict:
        return {
            "train_size": self.train_size,
            "test_size": self.test_size,
            "labels": list(self.labels),
            "accuracy": round(self.accuracy, 4),
            "precision_macro": round(self.precision_macro, 4),
            "recall_macro": round(self.recall_macro, 4),
            "f1_macro": round(self.f1_macro, 4),
            "baseline_accuracy": round(self.baseline_accuracy, 4),
            "baseline_label": self.baseline_label,
            "beats_baseline": self.beats_baseline,
            "confusion": self.confusion,
            "per_class": self.per_class,
        }


def _majority(labels: Sequence[int]) -> int:
    counts: Dict[int, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return max(counts, key=counts.get)


def evaluate(model: TrainedModel, train_set: FeatureSet, test_set: FeatureSet) -> EvaluationReport:
    if len(test_set) == 0:
        raise ValueError("empty test set")
    classes = list(range(len(LABELS)))
    y_true = list(test_set.labels)
    y_pred = list(model.pipeline.predict(to_matrix(test_set)))

    baseline_label = _majority(train_set.labels)
    y_base = [baseline_label] * len(y_true)

    confusion = confusion_matrix(y_true, y_pred, labels=classes).tolist()
    per_class: Dict[str, Dict[str, float]] = {}
    for i, name in enumerate(LABELS):
        support = sum(1 for t in y_true if t == i)
        per_class[name] = {
            "support": support,
            "precision": round(
                precision_score(y_true, y_pred, labels=[i], average="micro", zero_division=0), 4
            ),
            "recall": round(
                recall_score(y_true, y_pred, labels=[i], average="micro", zero_division=0), 4
            ),
            "f1": round(f1_score(y_true, y_pred, labels=[i], average="micro", zero_division=0), 4),
        }

    return EvaluationReport(
        train_size=len(train_set),
        test_size=len(test_set),
        labels=LABELS,
        accuracy=accuracy_score(y_true, y_pred),
        precision_macro=precision_score(y_true, y_pred, labels=classes, average="macro", zero_division=0),
        recall_macro=recall_score(y_true, y_pred, labels=classes, average="macro", zero_division=0),
        f1_macro=f1_score(y_true, y_pred, labels=classes, average="macro", zero_division=0),
        baseline_accuracy=accuracy_score(y_true, y_base),
        baseline_label=LABELS[baseline_label],
        confusion=confusion,
        per_class=per_class,
    )
