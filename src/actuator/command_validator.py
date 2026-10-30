"""Validation of actuator commands before they reach the (simulated) lights.

A command is a dict ``{approach, state, duration_sec, cycle_no}``. Validation
rejects unknown approaches, unknown states, durations outside the safe window
for that state, and any command set that would light two approaches GREEN at
once. Validation is pure and has no I/O so it can be called on every command.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from src.controller.timing_constraints import SignalState, TimingConfig


@dataclass
class CommandResult:
    ok: bool
    command: Optional[dict] = None
    errors: List[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def validate_command(command: dict, config: TimingConfig) -> CommandResult:
    errors: List[str] = []
    approach = command.get("approach")
    state = command.get("state")
    duration = command.get("duration_sec")

    if approach not in config.phase_order:
        errors.append(f"unknown approach: {approach!r}")
    try:
        state = SignalState(state)
    except ValueError:
        errors.append(f"unknown state: {command.get('state')!r}")
        return CommandResult(False, None, errors)

    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        errors.append(f"duration_sec must be numeric, got {duration!r}")
    else:
        if state == SignalState.GREEN:
            if not (config.min_green_seconds <= duration <= config.max_green_seconds):
                errors.append(
                    f"green duration {duration} outside [{config.min_green_seconds},"
                    f" {config.max_green_seconds}]"
                )
        elif state == SignalState.YELLOW and duration != config.yellow_seconds:
            errors.append(f"yellow duration must be {config.yellow_seconds}, got {duration}")
        elif state == SignalState.ALL_RED and duration != config.all_red_seconds:
            errors.append(f"all-red duration must be {config.all_red_seconds}, got {duration}")
        elif state == SignalState.RED and duration != 0:
            errors.append(f"red duration must be 0, got {duration}")

    if errors:
        return CommandResult(False, None, errors)
    return CommandResult(
        True,
        {"approach": approach, "state": state.value, "duration_sec": int(duration),
         "cycle_no": command.get("cycle_no")},
    )


def validate_no_conflict(commands: Iterable[dict], config: TimingConfig) -> List[str]:
    """Return error strings if more than one approach is GREEN simultaneously."""
    greens = [
        c.get("approach")
        for c in commands
        if str(c.get("state")) == SignalState.GREEN.value
    ]
    if len(greens) > 1:
        return [f"conflicting greens: {greens}"]
    return []
