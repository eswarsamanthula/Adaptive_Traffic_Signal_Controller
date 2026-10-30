"""Actuator command/ack handshake: node applies commands, controller tracks acks.

Uses the fake paho client from the end-to-end tests (no real broker).
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.actuator.actuator_node import ActuatorNode
from src.common.schemas import SensorReading
from src.common.time_utils import ManualClock
from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import TimingConfig
from src.database.repository import TrafficRepository
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import load_topics
from src.service.controller_service import ControllerService
from tests.test_end_to_end import FakeBrokerClient

TS = "2026-10-12 07:00:00"


def command_payload(command_id, approach, state, duration_sec, cycle_no=1):
    return json.dumps(
        {
            "command_id": command_id,
            "cycle_no": cycle_no,
            "approach": approach,
            "state": state,
            "duration_sec": duration_sec,
            "issued_at": TS,
            "source": "controller",
        }
    )


class ActuatorNodeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBrokerClient()
        self.client = MqttClient(BrokerConfig(), client=self.fake)
        self.topics = load_topics()
        self.config = TimingConfig()
        self.node = ActuatorNode(self.client, self.topics, self.config)
        self.node.start()

    def tearDown(self):
        self.node.stop_heartbeat()

    def test_start_publishes_online_heartbeat(self):
        beats = self.fake.payloads_for(self.topics.status_topic)
        self.assertTrue(beats)
        beat = beats[0]
        self.assertEqual(beat["role"], "actuator")
        self.assertEqual(beat["status"], "online")
        self.assertIn("timestamp", beat)
        self.assertGreater(beat["interval_seconds"], 0)

    def test_heartbeat_reports_command_counters(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, command_payload("ok1", "North", "GREEN", 25))
        self.node.handle_message(topic, command_payload("bad1", "North", "BLUE", 25))
        self.node._publish_heartbeat("online")
        beat = self.fake.payloads_for(self.topics.status_topic)[-1]
        self.assertEqual(beat["commands_received"], 2)
        self.assertEqual(beat["commands_accepted"], 1)
        self.assertEqual(beat["commands_rejected"], 1)

    def test_stop_publishes_offline_heartbeat(self):
        self.node.stop()
        beat = self.fake.payloads_for(self.topics.status_topic)[-1]
        self.assertEqual(beat["status"], "offline")

    def test_start_subscribes_to_command_topics(self):
        expected = {self.topics.command_topic(a) for a in self.config.phase_order}
        self.assertTrue(expected.issubset(set(self.fake.subscriptions)))

    def test_ack_topic_is_not_under_commands(self):
        self.assertEqual(self.topics.ack_topic("North"), "team9/traffic/acks/north")

    def test_valid_command_is_acked_accepted(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, command_payload("abc123", "North", "GREEN", 25))
        acks = self.fake.payloads_for(self.topics.ack_topic("North"))
        self.assertEqual(len(acks), 1)
        ack = acks[0]
        self.assertEqual(ack["status"], "accepted")
        self.assertEqual(ack["applied_state"], "GREEN")
        self.assertEqual(ack["command_id"], "abc123")
        self.assertEqual(self.node.actuator.states["North"], "GREEN")

    def test_invalid_state_is_acked_rejected(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, command_payload("bad1", "North", "BLUE", 25))
        ack = self.fake.payloads_for(self.topics.ack_topic("North"))[-1]
        self.assertEqual(ack["status"], "rejected")
        self.assertTrue(ack["reason"])
        self.assertEqual(self.node.actuator.states["North"], "RED")

    def test_out_of_range_green_is_rejected(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, command_payload("bad2", "North", "GREEN", 999))
        ack = self.fake.payloads_for(self.topics.ack_topic("North"))[-1]
        self.assertEqual(ack["status"], "rejected")

    def test_unknown_approach_in_command_is_rejected(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, command_payload("bad3", "Up", "GREEN", 25))
        ack = self.fake.payloads_for(self.topics.ack_topic("North"))[-1]
        self.assertEqual(ack["status"], "rejected")

    def test_invalid_json_does_not_crash(self):
        topic = self.topics.command_topic("North")
        self.node.handle_message(topic, "{not json")
        self.assertEqual(self.fake.payloads_for(self.topics.ack_topic("North")), [])


class ControllerAckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TrafficRepository.open(db_path=Path(self.tmp.name) / "traffic.db")
        self.fake = FakeBrokerClient()
        self.client = MqttClient(BrokerConfig(), client=self.fake)
        self.topics = load_topics()
        self.config = TimingConfig(tick_seconds=3600)
        controller = AdaptiveController(self.config, ManualClock())
        self.service = ControllerService(
            self.client, self.topics, repository=self.repo, controller=controller, config=self.config
        )
        self.service.start()

    def tearDown(self):
        self.service.stop()
        self.repo.close()
        self.tmp.cleanup()

    def _trigger_phase_change(self):
        self.service.controller.apply_readings(
            {a: SensorReading(TS, a, 10, 5, 10, 5, "LOW", "custom") for a in self.config.phase_order},
            decision_time=TS,
        )
        self.service.controller.start(at=0.0)
        return self.service.tick_once(at=self.config.default_green_seconds)

    def _commands(self):
        found = []
        for approach in self.config.phase_order:
            found.extend(self.fake.payloads_for(self.topics.command_topic(approach)))
        return found

    def _row(self, command_id):
        return self.repo.conn.execute(
            "SELECT * FROM actuator_commands WHERE command_id=?", (command_id,)
        ).fetchone()

    def test_phase_change_publishes_command_and_pending_row(self):
        self.assertTrue(self._trigger_phase_change())
        commands = self._commands()
        self.assertTrue(commands)
        command = commands[0]
        for key in ("command_id", "cycle_no", "approach", "state", "duration_sec", "issued_at", "source"):
            self.assertIn(key, command)
        self.assertEqual(command["source"], "controller")
        row = self._row(command["command_id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")

    def test_accepted_ack_updates_status_and_state(self):
        self._trigger_phase_change()
        command = self._commands()[0]
        ack = {
            "command_id": command["command_id"],
            "approach": command["approach"],
            "status": "accepted",
            "applied_state": command["state"],
            "applied_at": TS,
        }
        self.service.handle_message(self.topics.ack_topic(command["approach"]), json.dumps(ack))
        row = self._row(command["command_id"])
        self.assertEqual(row["status"], "accepted")
        self.assertIsNone(row["reason"])
        snapshot = self.fake.payloads_for(self.topics.signal_state_topic)[-1]
        self.assertEqual(snapshot["actuator"][command["approach"]], command["state"])

    def test_rejected_ack_logs_and_keeps_last_state(self):
        self._trigger_phase_change()
        command = self._commands()[0]
        before = self.repo.count_system_events()
        ack = {
            "command_id": command["command_id"],
            "approach": command["approach"],
            "status": "rejected",
            "reason": "conflicting greens",
            "applied_at": TS,
        }
        self.service.handle_message(self.topics.ack_topic(command["approach"]), json.dumps(ack))
        row = self._row(command["command_id"])
        self.assertEqual(row["status"], "rejected")
        self.assertIn("conflicting", row["reason"])
        self.assertGreater(self.repo.count_system_events(), before)
        snapshot = self.fake.payloads_for(self.topics.signal_state_topic)[-1]
        self.assertEqual(snapshot["actuator"][command["approach"]], "RED")

    def test_timeout_marks_unacknowledged_without_state_change(self):
        self._trigger_phase_change()
        command = self._commands()[0]
        before = self.repo.count_system_events()
        timed_out = self.service.check_ack_timeouts(now=time.time() + 9999)
        self.assertIn(command["command_id"], timed_out)
        row = self._row(command["command_id"])
        self.assertEqual(row["status"], "unacknowledged")
        self.assertGreater(self.repo.count_system_events(), before)
        snapshot = self.fake.payloads_for(self.topics.signal_state_topic)[-1]
        self.assertEqual(snapshot["actuator"][command["approach"]], "RED")

    def test_snapshot_reports_ack_timeout_health(self):
        self._trigger_phase_change()
        command = self._commands()[0]
        self.service.check_ack_timeouts(now=time.time() + 9999)
        self.service._publish_snapshot()
        snapshot = self.fake.payloads_for(self.topics.signal_state_topic)[-1]
        self.assertEqual(snapshot["ack_timeouts"], 2)  # startup GREEN plus the transition command
        self.assertIn(snapshot["last_timeout"]["command_id"], [c["command_id"] for c in self._commands()])
        self.assertIn(snapshot["last_timeout"]["approach"], self.config.phase_order)
        self.assertEqual(snapshot["pending_commands"], 0)

    def test_stale_ack_is_ignored(self):
        before = self.repo.count_system_events()
        ack = {"command_id": "nope", "approach": "North", "status": "accepted", "applied_state": "GREEN"}
        self.service.handle_message(self.topics.ack_topic("North"), json.dumps(ack))
        self.assertGreater(self.repo.count_system_events(), before)


if __name__ == "__main__":
    unittest.main()
