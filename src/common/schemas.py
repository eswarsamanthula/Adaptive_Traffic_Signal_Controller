"""Shared data schemas used by the simulator and the MQTT layer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

APPROACHES = ("North", "South", "East", "West")
CONGESTION_LEVELS = ("LOW", "MEDIUM", "HIGH")

TRAFFIC_COLUMNS = (
    "Timestamp",
    "Approach",
    "Vehicle_Count",
    "Queue_Length",
    "Avg_Wait_Time",
    "Arrival_Rate",
    "Congestion_Level",
    "Scenario",
)

NUMERIC_COLUMNS = ("Vehicle_Count", "Queue_Length", "Avg_Wait_Time", "Arrival_Rate")


@dataclass(frozen=True)
class SensorReading:
    """One validated traffic reading for a single approach at one timestamp."""

    timestamp: str
    approach: str
    vehicle_count: int
    queue_length: int
    avg_wait_time: int
    arrival_rate: int
    congestion_level: str
    scenario: str

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable representation used for MQTT publishing."""
        return {
            "timestamp": self.timestamp,
            "approach": self.approach,
            "vehicle_count": self.vehicle_count,
            "queue_length": self.queue_length,
            "avg_wait_time": self.avg_wait_time,
            "arrival_rate": self.arrival_rate,
            "congestion_level": self.congestion_level,
            "scenario": self.scenario,
        }
