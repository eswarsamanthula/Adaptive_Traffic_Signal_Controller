"""Build validated SensorReading objects from raw dataset rows."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Iterator, Mapping, Optional

from src.common.schemas import (
    APPROACHES,
    CONGESTION_LEVELS,
    NUMERIC_COLUMNS,
    TRAFFIC_COLUMNS,
    SensorReading,
)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class ReadingValidationError(ValueError):
    """Raised when a raw row cannot be turned into a valid SensorReading."""


def _parse_non_negative_int(value: object, column: str) -> int:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ReadingValidationError(f"missing value in column '{column}'")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ReadingValidationError(f"non-numeric value in column '{column}': {value!r}")
    if number != int(number):
        raise ReadingValidationError(f"non-integer value in column '{column}': {value!r}")
    integer = int(number)
    if integer < 0:
        raise ReadingValidationError(f"negative value in column '{column}': {integer}")
    return integer


class SensorGenerator:
    """Turns raw CSV rows into validated SensorReading objects."""

    def __init__(self, approaches: Iterable[str] = APPROACHES) -> None:
        self.approaches = tuple(approaches)

    def build(self, row: Mapping[str, object], line: Optional[int] = None) -> SensorReading:
        where = f" (line {line})" if line is not None else ""

        missing = [column for column in TRAFFIC_COLUMNS if column not in row]
        if missing:
            raise ReadingValidationError(f"missing columns{where}: {missing}")

        timestamp = str(row["Timestamp"]).strip()
        try:
            datetime.strptime(timestamp, TIMESTAMP_FORMAT)
        except ValueError:
            raise ReadingValidationError(f"invalid timestamp{where}: {timestamp!r}")

        approach = str(row["Approach"]).strip()
        if approach not in self.approaches:
            raise ReadingValidationError(f"unknown approach{where}: {approach!r}")

        numbers = {column: _parse_non_negative_int(row[column], column) for column in NUMERIC_COLUMNS}

        congestion = str(row["Congestion_Level"]).strip().upper()
        if congestion not in CONGESTION_LEVELS:
            raise ReadingValidationError(
                f"invalid congestion level{where}: {row['Congestion_Level']!r}"
            )

        scenario = str(row["Scenario"]).strip()
        if not scenario:
            raise ReadingValidationError(f"empty scenario{where}")

        return SensorReading(
            timestamp=timestamp,
            approach=approach,
            vehicle_count=numbers["Vehicle_Count"],
            queue_length=numbers["Queue_Length"],
            avg_wait_time=numbers["Avg_Wait_Time"],
            arrival_rate=numbers["Arrival_Rate"],
            congestion_level=congestion,
            scenario=scenario,
        )

    def iter_build(
        self, rows: Iterable[Mapping[str, object]], start_line: int = 2
    ) -> Iterator[SensorReading]:
        for offset, row in enumerate(rows):
            yield self.build(row, line=start_line + offset)
