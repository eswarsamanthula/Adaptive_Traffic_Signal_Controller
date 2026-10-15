"""Standalone simulated actuator node.

Runs as its own process with its own MQTT client (role ``actuator``). It
subscribes to the per-approach ``commands/{approach}`` topics, validates each
command, applies it to a simulated signal head, and publishes an
acknowledgement on ``acks/{approach}``.

The acknowledgement is how the controller learns what was actually applied:
``accepted`` carries the resulting ``applied_state``; ``rejected`` carries a
``reason`` and leaves the light unchanged. No hardware is involved.

Run it with::

    python -m src.actuator.actuator_node
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import yaml

from src.actuator.actuator_simulator import ActuatorSimulator
from src.common.logging_config import configure_logging, get_logger
from src.common.time_utils import now_timestamp
from src.controller.timing_constraints import TimingConfig
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import TopicSet, load_topics

logger = get_logger(__name__)

#: Default seconds between liveness heartbeats published on the status topic.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0


class ActuatorNode:
    def __init__(
        self,
        client,
        topics: TopicSet,
        config: TimingConfig,
        log: Optional[logging.Logger] = None,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        self.topics = topics
        self.config = config
        self.actuator = ActuatorSimulator(config)
        self.log = log or logger
        self.heartbeat_interval = max(0.5, float(heartbeat_interval))
        self._command_topics: Dict[str, str] = {
            topics.command_topic(a): a for a in config.phase_order
        }
        # Real liveness state: counters + a background heartbeat so a dashboard
        # can tell "alive" from "last ack was a while ago".
        self._commands_received = 0
        self._commands_accepted = 0
        self._commands_rejected = 0
        self._started_epoch = time.monotonic()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.client.is_connected:
            self.client.connect()
        for topic in self._command_topics:
            self.client.subscribe(topic, self.handle_message)
        self._publish_heartbeat("online")
        self._start_heartbeat_thread()
        self.log.info("actuator node listening on %s", ", ".join(self._command_topics))

    def stop(self) -> None:
        self.stop_heartbeat()
        # Retained "offline" so a dashboard knows this was a clean shutdown, not
        # a silent crash (which would leave a stale "online" beat).
        self._publish_heartbeat("offline")
        self.client.disconnect()

    # ------------------------------------------------------------- heartbeat
    def _start_heartbeat_thread(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="actuator-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval):
            try:
                self._publish_heartbeat("online")
            except Exception as exc:  # noqa: BLE001 - a bad heartbeat must not kill the node
                self.log.warning("heartbeat publish failed: %s", exc)

    def _publish_heartbeat(self, status: str) -> None:
        payload = {
            "role": "actuator",
            "status": status,
            "timestamp": now_timestamp(),
            "interval_seconds": self.heartbeat_interval,
            "uptime_seconds": round(time.monotonic() - self._started_epoch, 1),
            "commands_received": self._commands_received,
            "commands_accepted": self._commands_accepted,
            "commands_rejected": self._commands_rejected,
        }
        data = json.dumps(payload, separators=(",", ":"))
        # Retained so a dashboard opened after the last beat still sees it (and
        # can compute staleness from the timestamp) instead of showing nothing.
        try:
            self.client.publish(self.topics.status_topic, data, retain=True)
        except Exception as exc:  # noqa: BLE001 - a failed heartbeat must not crash the node
            self.log.warning("heartbeat publish failed: %s", exc)

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat thread without disconnecting (for tests)."""
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def handle_message(self, topic: str, payload: str) -> None:
        approach = self._command_topics.get(topic)
        try:
            command = json.loads(payload)
            if not isinstance(command, dict):
                raise ValueError("command must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            self.log.warning("invalid command on %s: %s", topic, exc)
            return

        self._commands_received += 1
        result = self.actuator.apply(command)
        applied = bool(result.get("applied"))
        if applied:
            self._commands_accepted += 1
        else:
            self._commands_rejected += 1
        ack = {
            "command_id": command.get("command_id"),
            "approach": command.get("approach", approach),
            "status": "accepted" if applied else "rejected",
            "applied_state": result["command"]["state"] if applied else None,
            "applied_at": now_timestamp(),
        }
        if not applied:
            ack["reason"] = "; ".join(result.get("errors") or ["rejected"])
        self._publish_ack(approach, command.get("approach"), ack)

    def _publish_ack(self, topic_approach: Optional[str], command_approach, ack: dict) -> None:
        approach = topic_approach if topic_approach in self.config.phase_order else command_approach
        if self.topics.ack is None:
            self.log.warning("no ack topic configured; dropping ack %s", ack.get("command_id"))
            return
        if approach not in self.config.phase_order:
            self.log.warning("cannot ack unknown approach %r", approach)
            return
        data = json.dumps(ack, separators=(",", ":"))
        try:
            self.client.publish(self.topics.ack_topic(approach), data)
        except Exception as exc:  # noqa: BLE001 - a failed ack must not crash the node
            self.log.error("ack publish failed: %s", exc)


def _heartbeat_interval(settings_path: Optional[Path]) -> float:
    from src.database.db import DEFAULT_SETTINGS_PATH

    path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    return float((data.get("actuator") or {}).get(
        "heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    ))


def build_node(
    settings_path: Optional[Path] = None,
    topics_path: Optional[Path] = None,
    *,
    log: Optional[logging.Logger] = None,
) -> ActuatorNode:
    config = TimingConfig.from_settings(settings_path)
    broker = BrokerConfig.from_settings(settings_path).with_role("actuator")
    client = MqttClient(broker, log=log)
    return ActuatorNode(
        client, load_topics(topics_path), config, log=log,
        heartbeat_interval=_heartbeat_interval(settings_path),
    )


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the simulated actuator node.")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--topics", default=None)
    args = parser.parse_args(argv)

    configure_logging()
    node = build_node(args.settings, args.topics)
    node.start()
    print("actuator node running; press Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        node.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
