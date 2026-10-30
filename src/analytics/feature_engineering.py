"""Feature engineering for the congestion-prediction model.

Turns a chronologically ordered list of :class:`SensorReading` into a design
matrix. Everything here is causal: a row only uses the current interval and
*earlier* intervals of the same approach (lag/rolling features), and the target
is the *next* interval's congestion level. That ordering is what prevents
target leakage — no future information is ever used as an input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    target_timestamps: List[str] = field(default_factory=list)

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
    target_timestamps: List[str] = []

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
            target_timestamps.append(nxt.timestamp)

    return FeatureSet(rows=rows, labels=labels, timestamps=timestamps, approaches=approaches,
                      target_timestamps=target_timestamps)


@dataclass
class ChronologicalSplitMetadata:
    boundary_timestamp: str
    unique_timestamp_count: int
    train_feature_timestamps: List[str]
    test_feature_timestamps: List[str]
    earliest_train_target_timestamp: str
    latest_train_target_timestamp: str
    first_test_feature_timestamp: str
    train_rows: int
    test_rows: int
    excluded_gap_rows: int
    excluded_gap_reason: str
    actual_train_fraction: float

    def as_dict(self) -> dict:
        return {
            "boundary_timestamp": self.boundary_timestamp,
            "unique_timestamp_count": self.unique_timestamp_count,
            "train_feature_timestamps": sorted(set(self.train_feature_timestamps)),
            "test_feature_timestamps": sorted(set(self.test_feature_timestamps)),
            "earliest_train_target_timestamp": self.earliest_train_target_timestamp,
            "latest_train_target_timestamp": self.latest_train_target_timestamp,
            "first_test_feature_timestamp": self.first_test_feature_timestamp,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "excluded_gap_rows": self.excluded_gap_rows,
            "excluded_gap_reason": self.excluded_gap_reason,
            "actual_train_fraction": round(self.actual_train_fraction, 6),
        }


def leakage_safe_chronological_split(
    features: FeatureSet, train_fraction: float = 0.7
) -> Tuple[FeatureSet, FeatureSet, ChronologicalSplitMetadata]:
    """Group equal feature times and purge rows whose next target crosses the boundary."""
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be in (0, 1)")
    n = len(features)
    if n < 2 or len(features.target_timestamps) != n:
        raise ValueError("insufficient or invalid feature set: target timestamps are required")
    unique_times = sorted(set(features.timestamps))
    if len(unique_times) < 2:
        raise ValueError("insufficient unique feature timestamps for a chronological split")
    cut = int(len(unique_times) * train_fraction)
    if cut <= 0 or cut >= len(unique_times):
        raise ValueError("train fraction leaves an empty temporal partition")
    boundary = unique_times[cut]
    train_indices = [i for i, (feature_time, target_time) in enumerate(zip(features.timestamps, features.target_timestamps))
                     if feature_time < boundary and target_time < boundary]
    test_indices = [i for i, feature_time in enumerate(features.timestamps) if feature_time >= boundary]
    excluded = [i for i, feature_time in enumerate(features.timestamps)
                if feature_time < boundary and features.target_timestamps[i] >= boundary]
    if not train_indices or not test_indices:
        raise ValueError("leakage-safe chronological split produced an empty train or test set")

    def subset(indices: List[int]) -> FeatureSet:
        ordered = sorted(indices, key=lambda i: (features.timestamps[i], features.approaches[i]))
        return FeatureSet(
            rows=[features.rows[i] for i in ordered], labels=[features.labels[i] for i in ordered],
            timestamps=[features.timestamps[i] for i in ordered], approaches=[features.approaches[i] for i in ordered],
            target_timestamps=[features.target_timestamps[i] for i in ordered],
        )

    train, test = subset(train_indices), subset(test_indices)
    meta = ChronologicalSplitMetadata(
        boundary_timestamp=boundary, unique_timestamp_count=len(unique_times),
        train_feature_timestamps=train.timestamps, test_feature_timestamps=test.timestamps,
        earliest_train_target_timestamp=min(train.target_timestamps),
        latest_train_target_timestamp=max(train.target_timestamps),
        first_test_feature_timestamp=min(test.timestamps), train_rows=len(train), test_rows=len(test),
        excluded_gap_rows=len(excluded), excluded_gap_reason="feature before boundary but next-reading target at or after boundary",
        actual_train_fraction=len(train) / (len(train) + len(test)),
    )
    if not max(train.target_timestamps) < min(test.timestamps):
        raise ValueError("temporal leakage check failed: training target timestamps overlap the test period")
    if set(train.timestamps) & set(test.timestamps):
        raise ValueError("temporal leakage check failed: feature timestamp appears in both partitions")
    return train, test, meta


def chronological_split(
    features: FeatureSet, train_fraction: float = 0.7
) -> Tuple[FeatureSet, FeatureSet]:
    """Backward-compatible two-set API backed by the leakage-safe split."""
    train, test, _ = leakage_safe_chronological_split(features, train_fraction)
    return train, test


def to_matrix(feature_set: FeatureSet) -> List[List[float]]:
    """Plain list-of-lists matrix (scikit-learn accepts this directly)."""
    return [list(row) for row in feature_set.rows]
