"""Controller service: the glue between MQTT, the adaptive controller and SQLite.

Responsibilities:

* subscribe to every sensor topic plus the Testing-Lab input topic;
* validate inbound readings (trust boundary) and feed them to the controller;
* drive the phase clock on a background tick thread and publish every signal
  transition and a live snapshot;
* answer Testing-Lab submissions by recomputing the plan and publishing the
  old vs. new timings with the decision reason;
* persist readings, cycle decisions, phase events and actuator commands.

Every collaborator is injectable (``client``, ``topics``, ``repository``,
``controller``) so the whole loop can be exercised with a fake broker and a
:class:`~src.common.time_utils.ManualClock`, with no real network or sleeping.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import yaml

from src.common.logging_config import configure_logging, get_logger
from src.common.schemas import APPROACHES, CONGESTION_LEVELS, SensorReading
from src.common.time_utils import SystemClock, now_timestamp
from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import SignalState, TimingConfig
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import TopicSet, load_topics
from src.database.repository import InvalidReadingError, reading_from_payload

logger = get_logger(__name__)


#: Testing-Lab numeric fields the live dashboard always sends and the decision
#: engine actually uses. Missing -> the whole submission is rejected.
REQUIRED_TEST_FIELDS = ("queue_length", "arrival_rate")
#: Numeric fields the dashboard does not send; optional, default 0, validated
#: only when present.
OPTIONAL_TEST_FIELDS = ("vehicle_count", "avg_wait_time")


def _validate_field(name: str, value, *, required: bool) -> tuple:
    """Validate one Testing-Lab numeric field.

    Accepts only finite, integer-valued, non-negative numbers. ``bool`` is
    rejected even though it is an ``int`` subclass. Missing required fields are
    an error; missing optional fields default to 0. Returns (value, errors).
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            return None, [f"{name} is required"]
        return 0, []
    if isinstance(value, bool):
        return None, [f"{name} must be a number, got bool"]
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, [f"{name} must be a finite number, got {value!r}"]
    if float(value) != int(value):
        return None, [f"{name} must be an integer, got {value!r}"]
    if int(value) < 0:
        return None, [f"{name} must be >= 0, got {int(value)}"]
    return int(value), []


def validate_test_reading(
    approach: str, fields, *, timestamp: str, scenario: str = "custom"
) -> tuple:
    """Validate one approach's Testing-Lab fields into a SensorReading.

    Returns ``(reading_or_None, errors)``. A non-empty error list means the whole
    submission must be rejected: the caller must not apply, persist or publish.
    """
    if approach not in APPROACHES:
        return None, [f"unknown approach: {approach!r}"]
    if not isinstance(fields, dict):
        return None, [f"{approach}: readings must be an object"]

    errors: list = []
    values: Dict[str, int] = {}
    for name in REQUIRED_TEST_FIELDS:
        value, field_errors = _validate_field(name, fields.get(name), required=True)
        errors.extend(f"{approach}.{e}" for e in field_errors)
        values[name] = value if value is not None else 0
    for name in OPTIONAL_TEST_FIELDS:
        value, field_errors = _validate_field(name, fields.get(name), required=False)
        errors.extend(f"{approach}.{e}" for e in field_errors)
        values[name] = value if value is not None else 0

    congestion = fields.get("congestion_level")
    if congestion is None:
        congestion = "LOW"
    else:
        congestion = str(congestion).strip().upper()
        if congestion not in CONGESTION_LEVELS:
            errors.append(
                f"{approach}.congestion_level: unknown level {fields.get('congestion_level')!r}"
            )

    if errors:
        return None, errors
    return (
        SensorReading(
            timestamp=timestamp,
            approach=approach,
            vehicle_count=values["vehicle_count"],
            queue_length=values["queue_length"],
            avg_wait_time=values["avg_wait_time"],
            arrival_rate=values["arrival_rate"],
            congestion_level=congestion,
            scenario=scenario,
        ),
        [],
    )


class ControllerService:
    def __init__(
        self,
        client: MqttClient,
        topics: TopicSet,
        *,
        repository=None,
        controller: Optional[AdaptiveController] = None,
        config: Optional[TimingConfig] = None,
        ack_timeout_seconds: float = 2.0,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.topics = topics
        self.repo = repository
        self.config = config or TimingConfig()
        self.controller = controller or AdaptiveController(self.config, SystemClock())
        self.log = log or logger
        self.ack_timeout_seconds = float(ack_timeout_seconds)

        self._sensor_topics = {
            self.topics.sensor_topic(a): a for a in self.config.phase_order
        }
        self._ack_topics = {
            self.topics.ack_topic(a): a
            for a in self.config.phase_order
            if self.topics.ack is not None
        }
        # Last state each approach was *acknowledged* to be in. Never advanced
        # without an actuator ack, so it can lag the intended controller state.
        self._acked_states: Dict[str, str] = {a: "RED" for a in self.config.phase_order}
        self._pending_acks: Dict[str, dict] = {}
        # Ack health for the dashboard: how many commands have timed out and the
        # most recent one, so a "not responding" actuator is distinguishable
        # from one that is simply idle.
        self._ack_timeouts = 0
        self._last_timeout: Optional[dict] = None
        self._last_persisted_cycle = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.client.is_connected:
            self.client.connect()
        for topic in self._sensor_topics:
            self.client.subscribe(topic, self.handle_message)
        self.client.subscribe(self.topics.test_input_topic, self.handle_message)
        for topic in self._ack_topics:
            self.client.subscribe(topic, self.handle_message)
        self.controller.start()
        self._announce_cycle()
        self._publish_snapshot()
        self._start_tick_thread()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.controller.stop()
        self._publish_snapshot()

    def _start_tick_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick_loop, name="phase-tick", daemon=True)
        self._thread.start()

    def _tick_loop(self) -> None:
        interval = max(0.05, self.config.tick_seconds)
        while not self._stop.wait(interval):
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self.log.error("tick failed: %s", exc)

    # ------------------------------------------------------------- message path
    def handle_message(self, topic: str, payload: str) -> None:
        if topic == self.topics.test_input_topic:
            self._handle_test_input(payload)
        elif topic in self._sensor_topics:
            self._handle_sensor(self._sensor_topics[topic], payload)
        elif topic in self._ack_topics:
            self._handle_ack(self._ack_topics[topic], payload)
        else:
            self.log.warning("message on unhandled topic %s", topic)

    def _handle_sensor(self, approach: str, payload: str) -> None:
        try:
            data = json.loads(payload)
            reading = reading_from_payload(data)
        except (ValueError, json.JSONDecodeError) as exc:
            self._log_event("warning", "service", "invalid-reading", f"{approach}: {exc}")
            return
        self.controller.apply_reading(reading, decision_time=reading.timestamp)
        if self.repo is not None:
            sequence = data.get("sequence") if isinstance(data, dict) else None
            self.repo.insert_reading(reading, sequence=sequence)
        self._publish_snapshot()

    def _handle_test_input(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("test input must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._log_event("warning", "service", "invalid-test-input", str(exc))
            return

        command = str(data.get("command", "")).lower()
        if command:
            self._handle_control(command)
            return

        result = self.run_test_case(data)
        self._publish(self.topics.test_result_topic, result)

    def _handle_control(self, command: str) -> None:
        if command == "start":
            self.controller.start()
            self._announce_cycle()
        elif command == "reset":
            self.controller.reset()
        elif command in ("stop", "pause", "resume"):
            getattr(self.controller, command)()
        else:
            self._log_event("warning", "service", "invalid-command", command)
            return
        self._log_event("info", "service", "control", command)
        self._publish_snapshot()

    # --------------------------------------------------------- actuator handshake
    def _dispatch_command(self, event) -> None:
        """Publish an actuator command for a phase change and track its ack."""
        approach = event.approach
        command = {
            "command_id": uuid.uuid4().hex,
            "cycle_no": event.cycle_no,
            "approach": approach,
            "state": event.state.value,
            "duration_sec": event.duration_sec,
            "issued_at": event.timestamp or now_timestamp(),
            "source": "controller",
        }
        if self.repo is not None:
            self.repo.insert_actuator_command(
                approach=approach,
                command=event.state.value,
                command_id=command["command_id"],
                green_time_sec=event.duration_sec if event.state == SignalState.GREEN else None,
                cycle_no=event.cycle_no,
                status="pending",
                detail=json.dumps(command, separators=(",", ":")),
            )
        self._pending_acks[command["command_id"]] = {
            "approach": approach,
            "state": event.state.value,
            "issued_epoch": time.time(),
        }
        self._publish(self.topics.command_topic(approach), command)

    def _handle_ack(self, approach: str, payload: str) -> None:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("ack must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._log_event("warning", "service", "invalid-ack", f"{approach}: {exc}")
            return

        command_id = data.get("command_id")
        pending = self._pending_acks.pop(command_id, None)
        if pending is None:
            self._log_event("warning", "service", "stale-ack", f"no pending command {command_id!r}")
            return

        status = str(data.get("status", "")).lower()
        ack_at = data.get("applied_at") or now_timestamp()
        if status == "accepted":
            state = data.get("applied_state") or pending["state"]
            self._acked_states[approach] = state
            self._update_ack(command_id, "accepted", None, ack_at, state)
        else:
            reason = data.get("reason") or "rejected"
            self._update_ack(command_id, "rejected", reason, ack_at, None)
            self._log_event(
                "warning", "service", "actuator-rejected",
                f"{approach} {pending['state']}: {reason}",
            )
        self._publish_snapshot()

    def check_ack_timeouts(self, *, now: Optional[float] = None, timeout: Optional[float] = None) -> list:
        """Mark commands with no ack within the timeout as unacknowledged."""
        now = time.time() if now is None else now
        timeout = self.ack_timeout_seconds if timeout is None else timeout
        timed_out = []
        for command_id, pending in list(self._pending_acks.items()):
            if now - pending["issued_epoch"] <= timeout:
                continue
            del self._pending_acks[command_id]
            self._update_ack(command_id, "unacknowledged", None, now_timestamp(), None)
            self._ack_timeouts += 1
            self._last_timeout = {
                "command_id": command_id,
                "approach": pending["approach"],
                "state": pending["state"],
                "timeout_s": timeout,
                "at": now_timestamp(),
            }
            self._log_event(
                "warning", "service", "actuator-timeout",
                f"{pending['approach']} {pending['state']} unacknowledged after {timeout}s",
            )
            timed_out.append(command_id)
        return timed_out

    def _update_ack(self, command_id, status, reason, ack_at, applied_state) -> None:
        if self.repo is not None:
            self.repo.update_actuator_ack(
                command_id, status=status, reason=reason, ack_at=ack_at, applied_state=applied_state
            )

    def run_test_case(self, data: dict) -> dict:
        """Validate submitted readings, recompute the plan; return old vs. new.

        Any invalid approach rejects the **whole** submission: nothing is
        applied, persisted or published, the current plan is left unchanged and
        a ``warning`` system event is logged.
        """
        timestamp = str(data.get("timestamp") or now_timestamp())
        scenario = str(data.get("scenario") or "custom")
        raw = data.get("readings")
        if not isinstance(raw, dict):
            raw = {}
            if data.get("approach"):
                raw = {str(data["approach"]): data}

        if not raw:
            return {"ok": False, "errors": ["no readings supplied"], "timestamp": timestamp}

        readings: Dict[str, SensorReading] = {}
        errors = []
        for approach, fields in raw.items():
            reading, field_errors = validate_test_reading(
                approach, fields, timestamp=timestamp, scenario=scenario
            )
            errors.extend(field_errors)
            if reading is not None:
                readings[approach] = reading

        if errors:
            self._log_event("warning", "service", "invalid-test-input", "; ".join(errors))
            return {"ok": False, "errors": errors, "timestamp": timestamp}

        new = self.controller.apply_readings(readings, decision_time=timestamp)
        self._persist_readings(readings, scenario)
        self._publish(self.topics.timing_decision_topic, self._plan_payload(new, timestamp))

        new_by = new.by_approach()
        detail = {}
        for approach in self.config.phase_order:
            decision = new_by[approach]
            detail[approach] = {
                **decision.as_dict(),
                "old_green_time": decision.previous_green_time,
                "new_green_time": decision.applied_green_time,
            }

        return {
            "ok": True,
            "errors": errors,
            "timestamp": timestamp,
            "scenario": scenario,
            "selected_approach": self.controller.active_approach,
            "selected_state": self.controller.state.value,
            "countdown": int(round(self.controller.phase_remaining)),
            "running": self.controller.running,
            "average_score": round(new.average_score, 3),
            "cycle_length_sec": new.cycle_length_sec,
            "approaches": detail,
        }

    # ------------------------------------------------------------------- timing
    def tick_once(self, *, at: Optional[float] = None) -> list:
        events = self.controller.tick(at=at)
        stamp = now_timestamp()
        for event in events:
            event.timestamp = stamp
            self._record_event(event)
        if self.controller.cycle_no > self._last_persisted_cycle:
            self._announce_cycle()
        self.check_ack_timeouts()
        self._publish_snapshot()
        return events

    def _announce_cycle(self) -> None:
        cycle = self.controller.cycle_no
        if cycle <= 0 or cycle == self._last_persisted_cycle:
            return
        self._last_persisted_cycle = cycle
        decision_time = self.controller.decision_time or now_timestamp()
        if self.repo is not None:
            for decision in self.controller.plan.decisions:
                self.repo.insert_decision(
                    cycle_no=cycle,
                    decision_time=decision_time,
                    approach=decision.approach,
                    traffic_timestamp_used=decision.traffic_timestamp_used,
                    queue_length=decision.queue_length,
                    arrival_rate=decision.arrival_rate,
                    previous_green_time=decision.previous_green_time,
                    requested_green_time=decision.requested_green_time,
                    applied_green_time=decision.applied_green_time,
                    cycle_length_sec=self.controller.plan.cycle_length_sec,
                    decision_reason=decision.decision_reason,
                )
        self._publish(
            self.topics.timing_decision_topic,
            self._plan_payload(self.controller.plan, decision_time, cycle_no=cycle),
        )

    def _record_event(self, event) -> None:
        if self.repo is not None:
            self.repo.insert_signal_event(
                cycle_no=event.cycle_no,
                approach=event.approach,
                start_time=event.timestamp or now_timestamp(),
                signal_state=event.state.value,
                duration_sec=event.duration_sec,
            )
        self._dispatch_command(event)
        self._publish(
            self.topics.signal_phase_topic,
            {
                "timestamp": event.timestamp,
                "cycle_no": event.cycle_no,
                "approach": event.approach,
                "signal_state": event.state.value,
                "duration_sec": event.duration_sec,
            },
        )

    # ------------------------------------------------------------- persistence
    def _persist_readings(self, readings: Dict[str, SensorReading], scenario: str) -> None:
        if self.repo is None:
            return
        for reading in readings.values():
            self.repo.insert_reading(reading)

    def _log_event(self, level: str, source: str, event_type: str, message: str) -> None:
        if self.repo is not None:
            self.repo.log_system_event(level, source, event_type, message)
        getattr(self.log, level if level in ("info", "warning", "error") else "info")(
            "%s: %s", event_type, message
        )

    # --------------------------------------------------------------- publishing
    def _plan_payload(self, plan, timestamp: str, *, cycle_no: Optional[int] = None) -> dict:
        return {
            "timestamp": timestamp,
            "cycle_no": cycle_no if cycle_no is not None else self.controller.cycle_no,
            "cycle_length_sec": plan.cycle_length_sec,
            "average_score": round(plan.average_score, 3),
            "fallback_used": plan.fallback_used,
            "decisions": {a: d.as_dict() for a, d in plan.by_approach().items()},
        }

    def _publish_snapshot(self) -> None:
        snapshot = self.controller.snapshot()
        snapshot["timestamp"] = now_timestamp()
        # ``intended`` is what the controller wants; ``actuator`` is the last
        # state each approach was acknowledged to be in. They differ until an
        # ack arrives, so the dashboard can show both.
        snapshot["intended"] = dict(snapshot["signals"])
        snapshot["actuator"] = dict(self._acked_states)
        snapshot["pending_commands"] = len(self._pending_acks)
        # Ack health, so the dashboard can show timed-out commands (and a
        # "not responding" actuator) instead of leaving rows pending forever.
        snapshot["ack_timeouts"] = self._ack_timeouts
        snapshot["last_timeout"] = self._last_timeout
        self._publish(self.topics.signal_state_topic, snapshot)

    def _publish(self, topic: str, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        try:
            self.client.publish(topic, data)
        except Exception as exc:  # noqa: BLE001 - publishing must not crash control
            self.log.error("publish to %s failed: %s", topic, exc)


def build_service(
    settings_path: Optional[Path] = None,
    topics_path: Optional[Path] = None,
    *,
    with_database: bool = True,
    log: Optional[logging.Logger] = None,
) -> ControllerService:
    config = TimingConfig.from_settings(settings_path)
    broker = BrokerConfig.from_settings(settings_path).with_role("controller")
    client = MqttClient(broker, log=log)
    topics = load_topics(topics_path)
    repository = None
    if with_database:
        from src.database.repository import TrafficRepository

        repository = TrafficRepository.open(settings_path=settings_path)
    return ControllerService(
        client, topics, repository=repository, config=config,
        ack_timeout_seconds=_ack_timeout(settings_path), log=log,
    )


def _ack_timeout(settings_path: Optional[Path]) -> float:
    from src.database.db import DEFAULT_SETTINGS_PATH

    path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return 2.0
    return float((data.get("actuator") or {}).get("ack_timeout_seconds", 2.0))


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the adaptive traffic controller service.")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--topics", default=None)
    parser.add_argument("--no-database", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()
    service = build_service(
        args.settings, args.topics, with_database=not args.no_database
    )
    service.start()
    print("controller service running; press Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        service.stop()
        if service.repo is not None:
            service.repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
