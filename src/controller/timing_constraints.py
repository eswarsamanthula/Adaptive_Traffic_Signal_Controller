"""Signal timing constraints and their configuration.

Single source of truth for the safety limits every controller must respect:
minimum/maximum green, the fixed yellow and all-red clearance intervals, the
startup default green, the phase order and the staleness limit. The defaults
match the frozen reference dataset (``dataset/data_dictionary.md``): green
10-60 s, yellow 3 s, all-red 2 s, cycle overhead 4 x (3 + 2) = 20 s.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class SignalState(str, Enum):
    """The states a single approach's signal can be in."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ALL_RED = "ALL_RED"
    RED = "RED"  # implicit: any approach not currently GREEN/YELLOW/ALL_RED


#: Which *approach* currently holds the active (non-red) interval.
PHASE_STATES = (SignalState.GREEN, SignalState.YELLOW, SignalState.ALL_RED)


@dataclass(frozen=True)
class TimingConfig:
    phase_order: Tuple[str, ...] = ("North", "South", "East", "West")
    min_green_seconds: int = 10
    max_green_seconds: int = 60
    default_green_seconds: int = 25
    yellow_seconds: int = 3
    all_red_seconds: int = 2
    stale_after_seconds: int = 180
    score_queue_weight: float = 1.0
    score_arrival_weight: float = 1.0
    score_wait_weight: float = 0.0
    hold_band_seconds: int = 3
    tick_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.min_green_seconds <= 0:
            raise ValueError("min_green_seconds must be > 0")
        if self.max_green_seconds < self.min_green_seconds:
            raise ValueError("max_green_seconds must be >= min_green_seconds")
        if not (self.min_green_seconds <= self.default_green_seconds <= self.max_green_seconds):
            raise ValueError("default_green_seconds must lie within [min, max]")
        if not self.phase_order:
            raise ValueError("phase_order must not be empty")
        if len(set(self.phase_order)) != len(self.phase_order):
            raise ValueError("phase_order must not contain duplicates")

    # -------------------------------------------------------------- helpers
    def clamp_green(self, seconds: float) -> int:
        """Clamp a requested green time into the safe [min, max] window."""
        return int(max(self.min_green_seconds, min(self.max_green_seconds, round(seconds))))

    @property
    def clearance_seconds(self) -> int:
        return self.yellow_seconds + self.all_red_seconds

    def cycle_length(self, applied_greens: Sequence[int]) -> int:
        """Sum of the applied greens plus one clearance interval per phase."""
        return int(sum(applied_greens) + len(self.phase_order) * self.clearance_seconds)

    # --------------------------------------------------------------- loading
    @classmethod
    def from_dict(cls, data: Optional[dict] = None) -> "TimingConfig":
        data = data or {}
        order = data.get("phase_order")
        kwargs = {
            "min_green_seconds": int(data.get("min_green_seconds", 10)),
            "max_green_seconds": int(data.get("max_green_seconds", 60)),
            "default_green_seconds": int(data.get("default_green_seconds", 25)),
            "yellow_seconds": int(data.get("yellow_seconds", 3)),
            "all_red_seconds": int(data.get("all_red_seconds", 2)),
            "stale_after_seconds": int(data.get("stale_after_seconds", 180)),
            "score_queue_weight": float(data.get("score_queue_weight", 1.0)),
            "score_arrival_weight": float(data.get("score_arrival_weight", 1.0)),
            "score_wait_weight": float(data.get("score_wait_weight", 0.0)),
            "hold_band_seconds": int(data.get("hold_band_seconds", 3)),
            "tick_seconds": float(data.get("tick_seconds", 0.5)),
        }
        if order:
            kwargs["phase_order"] = tuple(str(a) for a in order)
        return cls(**kwargs)

    @classmethod
    def from_settings(cls, path: Optional[Path] = None) -> "TimingConfig":
        path = Path(path) if path else DEFAULT_SETTINGS_PATH
        if not path.exists():
            raise FileNotFoundError(f"settings not found: {path}")
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls.from_dict(data.get("controller", {}))
