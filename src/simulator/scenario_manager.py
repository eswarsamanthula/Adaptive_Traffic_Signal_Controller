"""Scenario handling for readings that come from the project dataset."""
from __future__ import annotations

from typing import List, Sequence

from src.common.schemas import SensorReading


class ScenarioManager:
    """Read-only helpers over the scenarios already present in the dataset.

    Assumes readings are supplied in chronological order.
    """

    def __init__(self, readings: Sequence[SensorReading]) -> None:
        self._readings: List[SensorReading] = list(readings)

    def scenarios(self) -> List[str]:
        """Distinct scenario names, in order of first appearance."""
        ordered: List[str] = []
        for reading in self._readings:
            if reading.scenario not in ordered:
                ordered.append(reading.scenario)
        return ordered

    def filter(self, scenario: str) -> List[SensorReading]:
        return [reading for reading in self._readings if reading.scenario == scenario]
