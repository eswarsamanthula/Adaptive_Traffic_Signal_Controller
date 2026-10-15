"""MQTT topic definitions loaded from config/mqtt_topics.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOPICS_PATH = PROJECT_ROOT / "config" / "mqtt_topics.yaml"


class TopicError(ValueError):
    """Raised for invalid topic configuration or unknown approaches."""


@dataclass(frozen=True)
class TopicSet:
    base: str
    sensor: str
    status: str
    command: Optional[str] = None
    ack: Optional[str] = None
    signal_state: Optional[str] = None
    signal_phase: Optional[str] = None
    timing_decision: Optional[str] = None
    test_input: Optional[str] = None
    test_result: Optional[str] = None
    approaches: Dict[str, str] = field(default_factory=dict)

    def _slug(self, approach: str) -> str:
        if approach not in self.approaches:
            raise TopicError(f"unknown approach: {approach!r}")
        return self.approaches[approach]

    def sensor_topic(self, approach: str) -> str:
        return self.sensor.format(base=self.base, approach=self._slug(approach))

    def command_topic(self, approach: str) -> str:
        if self.command is None:
            raise TopicError("no command topic configured")
        return self.command.format(base=self.base, approach=self._slug(approach))

    def ack_topic(self, approach: str) -> str:
        if self.ack is None:
            raise TopicError("no ack topic configured")
        return self.ack.format(base=self.base, approach=self._slug(approach))

    @property
    def status_topic(self) -> str:
        return self.status.format(base=self.base)

    def _flat(self, template: Optional[str], name: str) -> str:
        if template is None:
            raise TopicError(f"no {name} topic configured")
        return template.format(base=self.base)

    @property
    def signal_state_topic(self) -> str:
        return self._flat(self.signal_state, "signal/state")

    @property
    def signal_phase_topic(self) -> str:
        return self._flat(self.signal_phase, "signal/phase")

    @property
    def timing_decision_topic(self) -> str:
        return self._flat(self.timing_decision, "timing/decisions")

    @property
    def test_input_topic(self) -> str:
        return self._flat(self.test_input, "test/input")

    @property
    def test_result_topic(self) -> str:
        return self._flat(self.test_result, "test/result")


def load_topics(path: Optional[Path] = None) -> TopicSet:
    path = Path(path) if path else DEFAULT_TOPICS_PATH
    if not path.exists():
        raise TopicError(f"topics config not found: {path}")

    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    base = str(data.get("base", "traffic")).strip("/")
    topics = data.get("topics", {}) or {}
    approaches = data.get("approaches", {}) or {}

    sensor = topics.get("sensor", "{base}/sensors/{approach}")
    if "{approach}" not in sensor:
        raise TopicError("sensor topic template must contain '{approach}'")

    status = topics.get("status", "{base}/status")
    command = topics.get("command")

    def _opt(key: str) -> Optional[str]:
        value = topics.get(key)
        return str(value) if value else None

    topic_set = TopicSet(
        base=base,
        sensor=sensor,
        status=status,
        command=str(command) if command else None,
        ack=_opt("ack"),
        signal_state=_opt("signal_state"),
        signal_phase=_opt("signal_phase"),
        timing_decision=_opt("timing_decision"),
        test_input=_opt("test_input"),
        test_result=_opt("test_result"),
        approaches={str(key): str(value) for key, value in approaches.items()},
    )

    if not topic_set.approaches:
        raise TopicError("no approaches configured in topics file")
    for approach in topic_set.approaches:
        topic_set.sensor_topic(approach)
    return topic_set
