"""Feature engineering for the congestion-prediction model.

Turns a chronologically ordered list of :class:`SensorReading` into a design
matrix. Everything here is causal: a row only uses the current interval and
*earlier* intervals of the same approach (lag/rolling features), and the target
is the *next* interval's congestion level. That ordering is what prevents
target leakage — no future information is ever used as an input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from src.analytics.congestion import classify_congestion
from src.common.schemas import SensorReading

#: Feature columns in a fixed order (the model relies on this order).
FEATURE_NAMES: Tuple[str, ...] = (
    "queue_length",
    "vehicle_count",
    "arrival_rate",
    "avg_wait_time",
    "queue_lag1",
    "arrival_lag1",
    "queue_roll3",
    "arrival_roll3",
)

LABELS: Tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")
_LABEL_INDEX = {name: i for i, name in enumerate(LABELS)}


@dataclass
class FeatureSet:
    rows: List[Tuple[float, ...]]
    labels: List[int]          # integer class ids (index into LABELS)
    timestamps: List[str]
    approaches: List[str]

    def __len__(self) -> int:
        return len(self.rows)


def _hour(timestamp: str) -> int:
    # "YYYY-MM-DD HH:MM:SS" -> hour of day (used only as a passive sanity check)
    try:
        return int(timestamp[11:13])
    except (ValueError, IndexError):
        return 0


def build_features(readings: Sequence[SensorReading]) -> FeatureSet:
    """Build causal feature rows with the next-interval congestion as target."""
    by_approach: Dict[str, List[SensorReading]] = {}
    for reading in readings:
        by_approach.setdefault(reading.approach, []).append(reading)

    rows: List[Tuple[float, ...]] = []
    labels: List[int] = []
    timestamps: List[str] = []
    approaches: List[str] = []

    for approach, series in by_approach.items():
        series = sorted(series, key=lambda r: r.timestamp)
        for i in range(len(series) - 1):
            window = series[max(0, i - 2): i + 1]
            current = series[i]
            nxt = series[i + 1]
            prev = series[i - 1] if i >= 1 else current
            queue_roll3 = sum(r.queue_length for r in window) / len(window)
            arrival_roll3 = sum(r.arrival_rate for r in window) / len(window)
            rows.append(
                (
                    float(current.queue_length),
                    float(current.vehicle_count),
                    float(current.arrival_rate),
                    float(current.avg_wait_time),
                    float(prev.queue_length),
                    float(prev.arrival_rate),
                    queue_roll3,
                    arrival_roll3,
                )
            )
            labels.append(_LABEL_INDEX[classify_congestion(nxt.queue_length, nxt.avg_wait_time)])
            timestamps.append(current.timestamp)
            approaches.append(approach)

    return FeatureSet(rows=rows, labels=labels, timestamps=timestamps, approaches=approaches)


def chronological_split(
    features: FeatureSet, train_fraction: float = 0.7
) -> Tuple[FeatureSet, FeatureSet]:
    """Split by time order (earliest 70% train, latest 30% test).

    The split is by index across the whole (already chronological) timeline so
    the test set is strictly in the future relative to the training set.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be in (0, 1)")
    # Order rows by timestamp first so the split respects chronology globally.
    order = sorted(range(len(features.rows)), key=lambda i: (features.timestamps[i], features.approaches[i]))
    cut = int(len(order) * train_fraction)
    train_idx, test_idx = order[:cut], order[cut:]

    def _subset(indices: List[int]) -> FeatureSet:
        return FeatureSet(
            rows=[features.rows[i] for i in indices],
            labels=[features.labels[i] for i in indices],
            timestamps=[features.timestamps[i] for i in indices],
            approaches=[features.approaches[i] for i in indices],
        )

    return _subset(train_idx), _subset(test_idx)


def to_matrix(feature_set: FeatureSet) -> List[List[float]]:
    """Plain list-of-lists matrix (scikit-learn accepts this directly)."""
    return [list(row) for row in feature_set.rows]
