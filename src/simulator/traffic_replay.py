"""Chronological replay of dataset/traffic_data.csv with playback controls.

The simulator is deliberately independent of MQTT. A clean callback interface
(``start(callback=...)``) lets a caller such as the MQTT publisher consume the
records without the simulator importing any MQTT code.
"""
from __future__ import annotations

import argparse
import csv
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import yaml

from src.common.schemas import TRAFFIC_COLUMNS, SensorReading
from src.simulator.scenario_manager import ScenarioManager
from src.simulator.sensor_generator import ReadingValidationError, SensorGenerator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_PATH = PROJECT_ROOT / "dataset" / "traffic_data.csv"
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

logger = logging.getLogger(__name__)

ReadingCallback = Callable[[SensorReading], None]


class ReplayError(RuntimeError):
    """Base error for replay problems."""


class DatasetValidationError(ReplayError):
    """Raised when the dataset file or its columns are unusable."""


@dataclass
class ValidationReport:
    path: str
    columns_ok: bool
    missing_columns: List[str]
    total_rows: int
    valid_rows: int
    invalid_rows: int
    invalid: List[tuple] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.columns_ok and self.invalid_rows == 0


class TrafficReplay:
    """Loads a traffic CSV and replays its records chronologically."""

    def __init__(
        self,
        csv_path: Path = DEFAULT_CSV_PATH,
        *,
        speed: float = 1.0,
        interval_seconds: float = 60.0,
        loop: bool = False,
        invalid_row_policy: str = "skip",
        scenario: Optional[str] = None,
        generator: Optional[SensorGenerator] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")
        if invalid_row_policy not in ("skip", "fail"):
            raise ValueError("invalid_row_policy must be 'skip' or 'fail'")

        self.csv_path = Path(csv_path)
        self.speed = float(speed)
        self.interval_seconds = float(interval_seconds)
        self.loop = bool(loop)
        self.invalid_row_policy = invalid_row_policy
        self.scenario_filter = scenario
        self.generator = generator or SensorGenerator()
        self.log = log or logger

        self._readings: List[SensorReading] = []
        self._invalid: List[tuple] = []
        self._missing_columns: List[str] = []
        self._total_rows = 0
        self._loaded = False

        self._position = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[BaseException] = None

    # ------------------------------------------------------------------ config
    @classmethod
    def from_settings(
        cls, settings_path: Optional[Path] = None, **overrides: object
    ) -> "TrafficReplay":
        settings_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        if not settings_path.exists():
            raise ReplayError(f"settings not found: {settings_path}")
        with settings_path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        simulator = data.get("simulator", {}) or {}
        paths = data.get("paths", {}) or {}

        csv_path = overrides.pop("csv_path", None)
        if csv_path is None:
            csv_path = PROJECT_ROOT / paths.get("dataset_csv", "dataset/traffic_data.csv")

        kwargs = {
            "speed": simulator.get("speed", 1.0),
            "interval_seconds": simulator.get("interval_seconds", 60.0),
            "loop": simulator.get("loop", False),
            "invalid_row_policy": simulator.get("invalid_row_policy", "skip"),
            "scenario": simulator.get("scenario"),
        }
        kwargs.update(overrides)
        return cls(csv_path, **kwargs)

    # ---------------------------------------------------------- load / validate
    def load(self) -> List[SensorReading]:
        if not self.csv_path.exists():
            raise DatasetValidationError(f"dataset not found: {self.csv_path}")

        readings: List[SensorReading] = []
        invalid: List[tuple] = []
        total = 0

        with self.csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            self._missing_columns = [c for c in TRAFFIC_COLUMNS if c not in fieldnames]
            if self._missing_columns:
                raise DatasetValidationError(
                    f"missing columns in {self.csv_path.name}: {self._missing_columns}"
                )

            for line, row in enumerate(reader, start=2):
                total += 1
                try:
                    reading = self.generator.build(row, line=line)
                except ReadingValidationError as exc:
                    if self.invalid_row_policy == "fail":
                        raise DatasetValidationError(f"invalid row: {exc}") from exc
                    invalid.append((line, str(exc)))
                    continue
                if self.scenario_filter is not None and reading.scenario != self.scenario_filter:
                    continue
                readings.append(reading)

        self._readings = readings
        self._invalid = invalid
        self._total_rows = total
        self._loaded = True
        if invalid:
            self.log.warning("%d invalid row(s) skipped in %s", len(invalid), self.csv_path.name)
        return list(self._readings)

    def validate(self) -> ValidationReport:
        self.load()
        return ValidationReport(
            path=str(self.csv_path),
            columns_ok=not self._missing_columns,
            missing_columns=list(self._missing_columns),
            total_rows=self._total_rows,
            valid_rows=len(self._readings),
            invalid_rows=len(self._invalid),
            invalid=list(self._invalid),
            scenarios=self.scenarios,
        )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------- read access
    @property
    def readings(self) -> List[SensorReading]:
        self._ensure_loaded()
        return list(self._readings)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._readings)

    @property
    def scenarios(self) -> List[str]:
        self._ensure_loaded()
        return ScenarioManager(self._readings).scenarios()

    def iter_readings(self) -> Iterator[SensorReading]:
        self._ensure_loaded()
        return iter(list(self._readings))

    # ------------------------------------------------------------ playback API
    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def last_error(self) -> Optional[BaseException]:
        return self._last_error

    @property
    def status(self) -> str:
        if self._last_error is not None:
            return "error"
        if self.is_running:
            return "paused" if self.is_paused else "running"
        return "stopped"

    def start(self, callback: Optional[ReadingCallback] = None, block: bool = False) -> "TrafficReplay":
        self._ensure_loaded()
        if self.is_running:
            raise ReplayError("replay is already running")
        self._stop_event.clear()
        self._pause_event.clear()
        self._last_error = None
        if block:
            self._run(callback)
        else:
            self._thread = threading.Thread(
                target=self._run, args=(callback,), name="traffic-replay", daemon=True
            )
            self._thread.start()
        return self

    def _run(self, callback: Optional[ReadingCallback]) -> None:
        previous_timestamp: Optional[str] = None
        try:
            while True:
                if self._stop_event.is_set():
                    break
                if not self._wait_while_paused():
                    return

                with self._lock:
                    if self._position >= len(self._readings):
                        if self.loop:
                            self._position = 0
                            previous_timestamp = None
                        else:
                            break
                    reading = self._readings[self._position]

                if (
                    previous_timestamp is not None
                    and reading.timestamp != previous_timestamp
                    and self.interval_seconds > 0
                ):
                    if self._stop_event.wait(self.interval_seconds / self.speed):
                        break
                    if not self._wait_while_paused():
                        return

                if callback is not None:
                    callback(reading)

                with self._lock:
                    self._position += 1
                previous_timestamp = reading.timestamp
        except BaseException as exc:  # noqa: BLE001 - surface any callback error
            self._last_error = exc
            self.log.error("replay stopped after error: %s", exc)
        finally:
            self._stop_event.set()

    def _wait_while_paused(self) -> bool:
        """Block while paused; return False if playback should stop."""
        while self._pause_event.is_set():
            if self._stop_event.wait(0.05):
                return False
        return True

    def step(self) -> Optional[SensorReading]:
        """Emit the next reading once, without pacing (manual control)."""
        self._ensure_loaded()
        with self._lock:
            if self._position >= len(self._readings):
                return None
            reading = self._readings[self._position]
            self._position += 1
        return reading

    def pause(self) -> "TrafficReplay":
        if self.is_running:
            self._pause_event.set()
        return self

    def resume(self) -> "TrafficReplay":
        self._pause_event.clear()
        return self

    def stop(self, timeout: float = 5.0) -> "TrafficReplay":
        self._stop_event.set()
        self._pause_event.clear()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout)
        self._thread = None
        return self

    def reset(self) -> "TrafficReplay":
        self.stop()
        with self._lock:
            self._position = 0
        self._stop_event.clear()
        self._pause_event.clear()
        self._last_error = None
        return self


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the traffic dataset chronologically.")
    parser.add_argument("--settings", default=None, help="path to settings.yaml")
    parser.add_argument("--csv", default=None, help="override dataset CSV path")
    parser.add_argument("--speed", type=float, default=None, help="replay speed multiplier")
    parser.add_argument("--interval", type=float, default=None, help="seconds between timestamps at speed 1")
    parser.add_argument("--scenario", default=None, help="only replay this scenario")
    parser.add_argument("--limit", type=int, default=None, help="stop after N records")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    overrides = {
        key: value
        for key, value in {
            "csv_path": args.csv,
            "speed": args.speed,
            "interval_seconds": args.interval,
            "scenario": args.scenario,
        }.items()
        if value is not None
    }
    replay = TrafficReplay.from_settings(args.settings, **overrides)
    report = replay.validate()
    print(
        f"dataset={report.path} rows={report.total_rows} valid={report.valid_rows} "
        f"invalid={report.invalid_rows} ok={report.ok}"
    )
    print(f"scenarios={report.scenarios}")

    count = 0
    for reading in replay.iter_readings():
        print(
            f"{reading.timestamp} {reading.approach:<5} queue={reading.queue_length} "
            f"arrival={reading.arrival_rate} congestion={reading.congestion_level} "
            f"scenario={reading.scenario}"
        )
        count += 1
        if args.limit is not None and count >= args.limit:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
