"""End-to-end safe Stop/Reset acknowledgement regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from src.actuator.actuator_node import ActuatorNode
from src.common.time_utils import ManualClock
from src.controller.adaptive_controller import AdaptiveController
from src.controller.timing_constraints import TimingConfig
from src.database.repository import TrafficRepository
from src.mqtt.mqtt_client import BrokerConfig, MqttClient
from src.mqtt.topics import load_topics
from src.service.controller_service import ControllerService


class SynchronousBus:
    """Routes controller commands through a real ActuatorNode and ACK handler."""
    def __init__(self):
        self.subscriptions = {}
        self.published = []
        self.connected = False
        self.drop_acks = set()
        self.reject = set()
        self.mismatch = set()
        self.hold_acks = []
        self.service = None
        self.node = None

    def connect_async(self, *_args):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, {}, 0, None)
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): self.connected = False
    def reconnect_delay_set(self, *_args, **_kwargs): pass
    def subscribe(self, topic, qos=0): return (0, 1)
    def publish(self, topic, payload, qos=0, retain=False):
        data = json.loads(payload) if isinstance(payload, str) else payload
        self.published.append((topic, data))
        if self.node and topic in self.node._command_topics:
            if data["approach"] in self.reject:
                data = dict(data, state="BLUE")
            self.node.handle_message(topic, json.dumps(data))
        elif self.service and topic in {self.service.topics.ack_topic(a) for a in self.service.config.phase_order}:
            ack = dict(data)
            approach = ack.get("approach")
            if approach in self.drop_acks:
                return type("Info", (), {"rc": 0, "mid": len(self.published)})()
            if approach in self.mismatch:
                ack["approach"] = "South" if approach != "South" else "North"
            if self.hold_acks:
                self.hold_acks.append((topic, ack))
            else:
                self.service.handle_message(topic, json.dumps(ack))
        return type("Info", (), {"rc": 0, "mid": len(self.published)})()


class SafeStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TrafficRepository.open(db_path=Path(self.tmp.name) / "traffic.db")
        self.bus = SynchronousBus()
        self.topics = load_topics()
        self.config = TimingConfig(tick_seconds=3600)
        self.client = MqttClient(BrokerConfig(), client=self.bus)
        self.controller = AdaptiveController(self.config, ManualClock())
        self.service = ControllerService(self.client, self.topics, repository=self.repo,
                                         controller=self.controller, config=self.config,
                                         ack_timeout_seconds=1)
        self.node = ActuatorNode(self.client, self.topics, self.config)
        self.bus.node = self.node
        self.bus.service = self.service
        self.service.start()

    def tearDown(self):
        self.service.stop()
        self.node.stop_heartbeat()
        self.repo.close()
        self.tmp.cleanup()

    def _make_green(self, approach="North"):
        # Exercise the actual simulator and its accepted acknowledgement.
        self.node.handle_message(self.topics.command_topic(approach), json.dumps({
            "command_id": "seed-" + approach, "approach": approach,
            "state": "GREEN", "duration_sec": 30, "cycle_no": 1,
        }))
        # A matching pending command is needed for service correlation.
        cid = "seed-" + approach
        self.service._pending_acks[cid] = {"approach": approach, "state": "GREEN", "issued_epoch": 0}
        ack = {"command_id": cid, "approach": approach, "status": "accepted", "applied_state": "GREEN"}
        self.service.handle_message(self.topics.ack_topic(approach), json.dumps(ack))

    def _command(self, operation):
        self.service.handle_message(self.topics.test_input_topic, json.dumps({"command": operation}))

    def test_stop_issues_four_unique_red_commands_and_confirms_all(self):
        self._make_green("North")
        self._command("stop")
        op = self.service._safe_operation_view()
        commands = [p for t, p in self.bus.published if "/commands/" in t and p.get("source") == "safe_stop"]
        self.assertEqual(len(commands), 4)
        self.assertEqual(len({c["command_id"] for c in commands}), 4)
        self.assertTrue(all(c["state"] == "RED" and c["duration_sec"] == 0 for c in commands))
        self.assertEqual(op["status"], "stop_confirmed")
        self.assertEqual(op["red_acknowledgements"], 4)
        self.assertEqual(set(op["actual_actuator_states"].values()), {"RED"})

    def test_stop_from_each_green_approach_and_stopped_state(self):
        for approach in self.config.phase_order:
            with self.subTest(approach=approach):
                self.service._safe_operation = None
                self.service._green_inhibited = False
                self._make_green(approach)
                self._command("stop")
                self.assertEqual(self.service._safe_operation_view()["status"], "stop_confirmed")
                self.assertEqual(self.service._safe_operation_view()["red_acknowledgements"], 4)
        self.service._safe_operation = None
        self.service._green_inhibited = False
        self.controller.stop()
        self._command("stop")
        self.assertEqual(self.service._safe_operation_view()["status"], "stop_confirmed")

    def test_reset_only_confirms_after_four_red_acknowledgements(self):
        self._make_green("West")
        self._command("reset")
        op = self.service._safe_operation_view()
        self.assertEqual(op["status"], "reset_confirmed")
        self.assertEqual(op["red_acknowledgements"], 4)
        self.assertEqual(self.controller.cycle_no, 0)

    def test_reset_while_already_stopped_confirms_without_deleting_history(self):
        before = self.repo.count_system_events()
        self.controller.stop()
        self._command("reset")
        self.assertEqual(self.service._safe_operation_view()["status"], "reset_confirmed")
        self.assertEqual(self.service._safe_operation_view()["red_acknowledgements"], 4)
        self.assertEqual(self.repo.count_system_events(), before)

    def test_confirmed_stop_allows_normal_phases_after_safe_restart(self):
        self._command("stop")
        self.assertEqual(self.service._safe_operation_view()["status"], "stop_confirmed")
        self._command("start")
        self.assertTrue(self.controller.running)
        self.assertEqual(self.node.actuator.states["North"], "GREEN")
        events = self.service.tick_once(at=self.controller.phase_started_at + self.controller.phase_duration)
        self.assertEqual(events[0].state.value, "YELLOW")
        self.assertEqual(self.node.actuator.states["North"], "YELLOW")

    def test_actuator_applied_red_with_lost_ack_stays_unconfirmed(self):
        self._make_green("East")
        self.bus.drop_acks.add("East")
        self._command("stop")
        op = self.service._safe_operation_view()
        self.assertEqual(op["status"], "stopping")
        self.assertEqual(op["red_acknowledgements"], 3)
        self.assertEqual(op["actual_actuator_states"]["East"], "GREEN")
        self.assertEqual(self.node.actuator.states["East"], "RED")
        self.assertEqual(op["pending_commands"], 1)
        snapshot = self.bus.published
        state = [p for t, p in snapshot if t == self.topics.signal_state_topic][-1]
        self.assertEqual(state["safe_operation"]["status"], "stopping")
        self.assertEqual(state["actuator"]["East"], "GREEN")
        self.assertEqual(state["pending_commands"], 1)
        cid = next(c for c, p in self.service._pending_acks.items() if p["approach"] == "East")
        self.service.check_ack_timeouts(now=self.service._pending_acks[cid]["issued_epoch"] + 2)
        op = self.service._safe_operation_view()
        self.assertEqual(op["status"], "failed")
        self.assertEqual(op["timed_out"][0]["approach"], "East")
        self.assertTrue(self.service._green_inhibited)
        self.service.handle_message(self.topics.ack_topic("East"), json.dumps({
            "command_id": cid, "approach": "East", "status": "accepted", "applied_state": "RED"
        }))
        self.assertEqual(self.service._safe_operation_view()["status"], "failed")

    def test_stale_ack_from_failed_operation_cannot_complete_retry(self):
        self.bus.drop_acks.add("South")
        self._command("stop")
        old_id = next(cid for cid, p in self.service._pending_acks.items() if p["approach"] == "South")
        self.service.check_ack_timeouts(now=max(p["issued_epoch"] for p in self.service._pending_acks.values()) + 2)
        self.assertEqual(self.service._safe_operation_view()["status"], "failed")
        self.bus.drop_acks.clear()
        self._command("reset")
        self.assertEqual(self.service._safe_operation_view()["status"], "reset_confirmed")
        before = dict(self.service._acked_states)
        self.service.handle_message(self.topics.ack_topic("South"), json.dumps({
            "command_id": old_id, "approach": "South", "status": "accepted", "applied_state": "GREEN"
        }))
        self.assertEqual(self.service._safe_operation_view()["status"], "reset_confirmed")
        self.assertEqual(self.service._acked_states, before)

    def test_rejected_red_ack_fails_safe_operation_and_keeps_state_authoritative(self):
        self._make_green("South")
        self.bus.reject.add("West")
        self._command("stop")
        op = self.service._safe_operation_view()
        self.assertEqual(op["status"], "failed")
        self.assertEqual(op["actual_actuator_states"]["West"], "RED")
        self.assertEqual(len(op["rejected"]), 1)

    def test_restart_is_blocked_after_missing_ack(self):
        self.bus.drop_acks.add("North")
        self._command("stop")
        before = len([1 for t, p in self.bus.published if "/commands/" in t and p.get("state") == "GREEN"])
        self.assertEqual(self.service.tick_once(at=10_000), [])
        self.assertEqual(len([1 for t, p in self.bus.published if "/commands/" in t and p.get("state") == "GREEN"]), before)
        self.service.check_ack_timeouts(now=max(p["issued_epoch"] for p in self.service._pending_acks.values()) + 2)
        self._command("start")
        after = len([1 for t, p in self.bus.published if "/commands/" in t and p.get("state") == "GREEN"])
        self.assertEqual(after, before)

    def test_mismatched_approach_ack_does_not_change_acknowledged_state(self):
        self._make_green("North")
        self.bus.mismatch.add("East")
        self._command("stop")
        op = self.service._safe_operation_view()
        self.assertEqual(op["red_acknowledgements"], 3)
        self.assertEqual(op["pending_commands"], 1)
        self.assertEqual(op["actual_actuator_states"]["East"], "RED")

    def test_nonzero_red_duration_is_rejected_without_changing_actuator(self):
        self._make_green("North")
        before = self.node.actuator.snapshot()
        self.node.handle_message(self.topics.command_topic("North"), json.dumps({
            "command_id": "bad-red-duration", "approach": "North",
            "state": "RED", "duration_sec": 1, "cycle_no": 1,
        }))
        self.assertEqual(self.node.actuator.snapshot(), before)
        ack = [p for t, p in self.bus.published if t == self.topics.ack_topic("North")][-1]
        self.assertEqual(ack["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
