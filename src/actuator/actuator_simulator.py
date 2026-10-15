"""Simulated actuator: the software stand-in for the physical signal heads.

Holds the last known state per approach and applies validated commands,
rejecting anything unsafe. No hardware is involved; this only tracks state so
the dashboard and database can show what the controller commanded.
"""
from __future__ import annotations

from typing import Dict, List

from src.actuator.command_validator import validate_command, validate_no_conflict
from src.controller.timing_constraints import SignalState, TimingConfig


class ActuatorSimulator:
    def __init__(self, config: TimingConfig) -> None:
        self.config = config
        self.states: Dict[str, str] = {a: SignalState.RED.value for a in config.phase_order}
        self.history: List[dict] = []

    def apply(self, command: dict) -> dict:
        """Validate and apply one command; returns a result dict."""
        result = validate_command(command, self.config)
        if not result.ok:
            return {"applied": False, "errors": result.errors}
        self.states[result.command["approach"]] = result.command["state"]
        self.history.append(result.command)
        return {"applied": True, "command": result.command, "errors": []}

    def apply_snapshot(self, commands: List[dict]) -> dict:
        """Apply a whole-intersection snapshot, refusing conflicting greens."""
        conflicts = validate_no_conflict(commands, self.config)
        if conflicts:
            return {"applied": False, "errors": conflicts}
        results = [self.apply(c) for c in commands]
        errors = [e for r in results for e in r.get("errors", [])]
        return {"applied": not errors, "errors": errors, "results": results}

    def snapshot(self) -> Dict[str, str]:
        return dict(self.states)
