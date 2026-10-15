"""Deterministic congestion metrics derived from validated sensor readings.

These helpers add no new state: they re-express what a reading already contains
using the *same* threshold rule documented for the frozen dataset
(``dataset/data_dictionary.md``): the congestion level is the worse of the queue
and waiting-time bands. They are used by the analytics/evaluation pipeline and
by tests, and are deliberately free of I/O or model dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from src.common.schemas import SensorReading

# Thresholds documented for the synthetic dataset.
QUEUE_MEDIUM = 8
QUEUE_HIGH = 16
WAIT_MEDIUM = 30
WAIT_HIGH = 60

_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_REVERSE = {v: k for k, v in _RANK.items()}


def _band(value: int, medium: int, high: int) -> str:
    if value >= high:
        return "HIGH"
    if value >= medium:
        return "MEDIUM"
    return "LOW"


def classify_congestion(queue_length: int, avg_wait_time: int) -> str:
    """Worse of the queue and waiting-time bands (LOW < MEDIUM < HIGH)."""
    queue_band = _band(int(queue_length), QUEUE_MEDIUM, QUEUE_HIGH)
    wait_band = _band(int(avg_wait_time), WAIT_MEDIUM, WAIT_HIGH)
    return queue_band if _RANK[queue_band] >= _RANK[wait_band] else wait_band


def congestion_index(queue_length: int, arrival_rate: int, avg_wait_time: int) -> float:
    """A continuous 0-1 congestion index (normalised, saturating) for trends."""
    q = min(1.0, int(queue_length) / float(QUEUE_HIGH))
    a = min(1.0, int(arrival_rate) / 30.0)
    w = min(1.0, int(avg_wait_time) / float(WAIT_HIGH))
    return round((q + a + w) / 3.0, 4)


@dataclass
class ApproachStats:
    approach: str
    samples: int
    avg_queue: float
    max_queue: int
    avg_arrival: float
    avg_wait: float
    max_wait: int
    congestion: str

    def as_dict(self) -> dict:
        return {
            "approach": self.approach,
            "samples": self.samples,
            "avg_queue": round(self.avg_queue, 3),
            "max_queue": self.max_queue,
            "avg_arrival": round(self.avg_arrival, 3),
            "avg_wait": round(self.avg_wait, 3),
            "max_wait": self.max_wait,
            "congestion": self.congestion,
        }


def approach_stats(readings: Sequence[SensorReading]) -> Dict[str, ApproachStats]:
    """Per-approach summary of a chronologically ordered reading sequence."""
    grouped: Dict[str, List[SensorReading]] = {}
    for reading in readings:
        grouped.setdefault(reading.approach, []).append(reading)
    stats: Dict[str, ApproachStats] = {}
    for approach, rows in grouped.items():
        n = len(rows)
        queues = [r.queue_length for r in rows]
        waits = [r.avg_wait_time for r in rows]
        arrivals = [r.arrival_rate for r in rows]
        worst = max((classify_congestion(r.queue_length, r.avg_wait_time) for r in rows), key=_RANK.get)
        stats[approach] = ApproachStats(
            approach=approach,
            samples=n,
            avg_queue=sum(queues) / n,
            max_queue=max(queues),
            avg_arrival=sum(arrivals) / n,
            avg_wait=sum(waits) / n,
            max_wait=max(waits),
            congestion=worst,
        )
    return stats


def queue_trend(readings: Sequence[SensorReading], approach: str) -> List[dict]:
    """(timestamp, queue, wait, congestion_index) points for one approach."""
    return [
        {
            "timestamp": r.timestamp,
            "queue_length": r.queue_length,
            "avg_wait_time": r.avg_wait_time,
            "congestion_index": congestion_index(r.queue_length, r.arrival_rate, r.avg_wait_time),
        }
        for r in readings
        if r.approach == approach
    ]


def overall_congestion(readings: Iterable[SensorReading]) -> str:
    """Worst congestion level across the supplied readings."""
    worst = "LOW"
    for reading in readings:
        level = classify_congestion(reading.queue_length, reading.avg_wait_time)
        if _RANK[level] > _RANK[worst]:
            worst = level
    return worst
