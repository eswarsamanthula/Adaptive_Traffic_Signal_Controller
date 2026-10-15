"""End-to-end tests: MQTT message path -> controller -> SQLite, no real broker.

A fake paho client and a manual clock keep the whole loop deterministic. The
service is exercised exactly as it runs in production, minus the network.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import TimingConfig
from src.common.schemas import SensorReading
from src.common.time_utils import ManualClock
from src.database.repository import TrafficRepository
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import load_topics
from src.service.controller_service import ControllerService

TS = "2026-10-12 07:00:00"


class FakePublishInfo:
    def __init__(self, rc, mid):
        self.rc = rc
        self.mid = mid


class FakeBrokerClient:
    """Minimal paho stand-in that records subscriptions and publishes."""

    def __init__(self):
        self.subscriptions = []
        self.published = []
        self.connected = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_publish = None
        self.on_message = None
        self._mid = 0

    def connect_async(self, host, port, keepalive):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, {}, 0, None)
        return 0

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.connected = False
        return 0

    def reconnect(self):
        return 0

    def reconnect_delay_set(self, *args, **kwargs):
        return None

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        return (0, len(self.subscriptions))

    def publish(self, topic, payload, qos=0, retain=False):
        self._mid += 1
        self.published.append({"topic": topic, "payload": payload, "qos": qos})
        return FakePublishInfo(0, self._mid)

    def payloads_for(self, topic):
        return [json.loads(p["payload"]) for p in self.published if p["topic"] == topic]


def sensor_payload(approach, queue, arrival, ts=TS, sequence=1):
    return json.dumps(
        {
            "timestamp": ts,
            "approach": approach,
            "vehicle_count": queue + arrival,
            "queue_length": queue,
            "avg_wait_time": 10,
            "arrival_rate": arrival,
            "congestion_level": "LOW",
            "scenario": "custom",
            "sequence": sequence,
        }
    )


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "traffic.db"
        self.repo = TrafficRepository.open(db_path=db_path)
        self.fake = FakeBrokerClient()
        self.client = MqttClient(BrokerConfig(), client=self.fake)
        self.topics = load_topics()
        # Large tick keeps the background thread idle during the test.
        self.config = TimingConfig(tick_seconds=3600)
        self.clock = ManualClock()
        controller = AdaptiveController(self.config, self.clock)
        self.service = ControllerService(
            self.client, self.topics, repository=self.repo, controller=controller, config=self.config
        )
        self.service.start()

    def tearDown(self):
        self.service.stop()
        self.repo.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- startup
    def test_start_subscribes_to_sensors_and_test_input(self):
        expected = {self.topics.sensor_topic(a) for a in self.config.phase_order}
        expected.add(self.topics.test_input_topic)
        self.assertTrue(expected.issubset(set(self.fake.subscriptions)))

    def test_start_persists_first_cycle_decisions(self):
        count = self.repo.conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0]
        self.assertEqual(count, len(self.config.phase_order))

    # ------------------------------------------------------------- sensor path
    def test_sensor_message_updates_snapshot_and_persists(self):
        topic = self.topics.sensor_topic("North")
        self.service.handle_message(topic, sensor_payload("North", queue=20, arrival=9))
        self.assertEqual(self.repo.count_readings(), 1)
        snaps = self.fake.payloads_for(self.topics.signal_state_topic)
        self.assertTrue(snaps)
        self.assertEqual(snaps[-1]["approaches"]["North"]["queue_length"], 20)

    def test_invalid_reading_is_logged_not_stored(self):
        self.service.handle_message(self.topics.sensor_topic("North"), "{not json")
        self.assertEqual(self.repo.count_readings(), 0)
        self.assertEqual(self.repo.count_system_events(), 1)

    def test_duplicate_reading_is_ignored(self):
        topic = self.topics.sensor_topic("North")
        payload = sensor_payload("North", 20, 9)
        self.service.handle_message(topic, payload)
        self.service.handle_message(topic, payload)
        self.assertEqual(self.repo.count_readings(), 1)

    # ---------------------------------------------------------------- test lab
    def test_test_input_returns_old_and_new_timinng(self):
        payload = json.dumps(
            {
                "timestamp": "2026-10-12 07:05:00",
                "scenario": "heavy",
                "readings": {
                    "North": {"queue_length": 7, "arrival_rate": 5},
                    "South": {"queue_length": 5, "arrival_rate": 3},
                    "East": {"queue_length": 5, "arrival_rate": 3},
                    "West": {"queue_length": 5, "arrival_rate": 3},
                },
            }
        )
        self.service.handle_message(self.topics.test_input_topic, payload)
        results = self.fake.payloads_for(self.topics.test_result_topic)
        self.assertTrue(results)
        result = results[-1]
        self.assertTrue(result["ok"])
        north = result["approaches"]["North"]
        self.assertGreater(north["new_green_time"], north["old_green_time"])
        self.assertIn("extend", north["decision_reason"].lower())
        self.assertIn("selected_approach", result)

    def test_old_green_is_previous_not_pending_plan(self):
        # Skew the live plan so it diverges from the engine's previous_green.
        for approach, queue, arrival in (
            ("North", 20, 9),
            ("South", 20, 9),
            ("East", 2, 1),
            ("West", 2, 1),
        ):
            self.service.handle_message(
                self.topics.sensor_topic(approach), sensor_payload(approach, queue, arrival)
            )
        controller = self.service.controller
        self.assertNotEqual(
            controller.plan.by_approach()["North"].applied_green_time,
            controller.previous_green["North"],
        )

        # Identical readings for every approach -> score == average -> hold.
        payload = json.dumps(
            {
                "timestamp": "2026-10-12 07:07:00",
                "readings": {
                    a: {"queue_length": 2, "arrival_rate": 1} for a in self.config.phase_order
                },
            }
        )
        self.service.handle_message(self.topics.test_input_topic, payload)
        result = self.fake.payloads_for(self.topics.test_result_topic)[-1]
        self.assertTrue(result["ok"])
        for approach in self.config.phase_order:
            detail = result["approaches"][approach]
            self.assertEqual(detail["old_green_time"], detail["previous_green_time"])
            self.assertEqual(detail["new_green_time"], detail["old_green_time"])
            self.assertIn("hold", detail["decision_reason"].lower())

    def test_test_input_persists_readings(self):
        payload = json.dumps(
            {"readings": {"North": {"queue_length": 5, "arrival_rate": 3}}, "timestamp": "2026-10-12 07:06:00"}
        )
        self.service.handle_message(self.topics.test_input_topic, payload)
        self.assertEqual(self.repo.count_readings(), 1)

    def test_control_commands_start_stop_reset(self):
        self.service.handle_message(self.topics.test_input_topic, json.dumps({"command": "stop"}))
        self.assertFalse(self.service.controller.running)
        self.service.handle_message(self.topics.test_input_topic, json.dumps({"command": "start"}))
        self.assertTrue(self.service.controller.running)
        self.service.handle_message(self.topics.test_input_topic, json.dumps({"command": "reset"}))
        self.assertEqual(self.service.controller.cycle_no, 0)

    # --------------------------------------------------------------- tick path
    def test_tick_publishes_transitions_and_persists_events(self):
        self.service.controller.apply_readings(
            {
                a: SensorReading(TS, a, 10, 5, 10, 5, "LOW", "custom")
                for a in self.config.phase_order
            },
            decision_time=TS,
        )
        self.service.controller.start(at=0.0)
        events = self.service.tick_once(at=self.config.default_green_seconds)
        self.assertTrue(events)
        phases = self.fake.payloads_for(self.topics.signal_phase_topic)
        self.assertTrue(any(p["signal_state"] == "YELLOW" for p in phases))
        count = self.repo.conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
        self.assertGreaterEqual(count, 1)
        commands = self.repo.conn.execute("SELECT COUNT(*) FROM actuator_commands").fetchone()[0]
        self.assertGreaterEqual(commands, 1)

    def test_service_never_reports_two_greens(self):
        self.service.controller.start(at=0.0)
        t = 0.0
        while self.service.controller.cycle_no < 2:
            t += 1.0
            self.service.tick_once(at=t)
            snapshot = self.service.controller.snapshot()
            greens = [a for a, s in snapshot["signals"].items() if s["state"] == "GREEN"]
            self.assertLessEqual(len(greens), 1)


if __name__ == "__main__":
    unittest.main()
