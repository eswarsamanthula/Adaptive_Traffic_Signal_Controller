"""Testing-Lab input validation: bad submissions are rejected whole.

Nothing invalid may reach the controller: no plan change, no reading persisted,
no timing decision published, and a ``warning`` system event is logged.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import TimingConfig
from src.common.time_utils import ManualClock
from src.database.repository import TrafficRepository
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import load_topics
from src.service.controller_service import ControllerService
from tests.test_end_to_end import FakeBrokerClient

TS = "2026-10-12 07:00:00"


class TestingLabValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "traffic.db"
        self.repo = TrafficRepository.open(db_path=db_path)
        self.fake = FakeBrokerClient()
        self.client = MqttClient(BrokerConfig(), client=self.fake)
        self.topics = load_topics()
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

    def submit(self, readings):
        payload = json.dumps({"timestamp": TS, "scenario": "custom", "readings": readings})
        self.service.handle_message(self.topics.test_input_topic, payload)
        return self.fake.payloads_for(self.topics.test_result_topic)[-1]

    def greens(self):
        return {a: d.applied_green_time for a, d in self.service.controller.plan.by_approach().items()}

    def assert_rejected(self, readings):
        before_greens = self.greens()
        before_readings = self.repo.count_readings()
        before_decisions = len(self.fake.payloads_for(self.topics.timing_decision_topic))
        result = self.submit(readings)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        self.assertEqual(self.greens(), before_greens)
        self.assertEqual(self.repo.count_readings(), before_readings)
        self.assertEqual(len(self.fake.payloads_for(self.topics.timing_decision_topic)), before_decisions)
        self.assertGreaterEqual(self.repo.count_system_events(), 1)
        return result

    def test_negative_value_rejected(self):
        self.assert_rejected({"North": {"queue_length": -3, "arrival_rate": 4}})

    def test_nonnumeric_text_rejected(self):
        self.assert_rejected({"North": {"queue_length": "abc", "arrival_rate": 4}})

    def test_missing_required_field_rejected(self):
        self.assert_rejected({"North": {"queue_length": 5}})

    def test_nan_rejected(self):
        self.assert_rejected({"North": {"queue_length": float("nan"), "arrival_rate": 4}})

    def test_infinite_rejected(self):
        self.assert_rejected({"North": {"queue_length": float("inf"), "arrival_rate": 4}})

    def test_bool_rejected(self):
        self.assert_rejected({"North": {"queue_length": True, "arrival_rate": 4}})

    def test_fractional_value_rejected(self):
        self.assert_rejected({"North": {"queue_length": 3.5, "arrival_rate": 4}})

    def test_unknown_approach_rejected(self):
        self.assert_rejected({"North": {"queue_length": 5, "arrival_rate": 4}, "Up": {"queue_length": 5, "arrival_rate": 4}})

    def test_unknown_congestion_level_rejected(self):
        self.assert_rejected({"North": {"queue_length": 5, "arrival_rate": 4, "congestion_level": "EXTREME"}})

    def test_one_bad_approach_rejects_whole_submission(self):
        result = self.assert_rejected(
            {
                "North": {"queue_length": 5, "arrival_rate": 4},
                "South": {"queue_length": -1, "arrival_rate": 4},
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(self.repo.count_readings(), 0)

    def test_valid_input_applied_and_persisted(self):
        result = self.submit({"North": {"queue_length": 30, "arrival_rate": 9}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.repo.count_readings(), 1)
        self.assertEqual(self.service.controller.readings["North"].queue_length, 30)

    def test_optional_fields_validated_when_present(self):
        self.assert_rejected({"North": {"queue_length": 5, "arrival_rate": 4, "avg_wait_time": -2}})

    def test_partial_submission_updates_only_given_approach(self):
        self.submit({"North": {"queue_length": 30, "arrival_rate": 9}})
        result = self.submit({"South": {"queue_length": 1, "arrival_rate": 1}})
        self.assertTrue(result["ok"])
        saved = self.service.controller.readings
        self.assertEqual(saved["North"].queue_length, 30)
        self.assertEqual(saved["South"].queue_length, 1)


if __name__ == "__main__":
    unittest.main()
