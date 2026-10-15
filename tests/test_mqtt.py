"""MQTT + simulator/publisher integration tests.

These use an injected fake client, so they need neither a broker nor the
paho-mqtt package.

Run without pytest:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.schemas import SensorReading
from src.mqtt.mqtt_client import (
    DEFAULT_SETTINGS_PATH,
    BrokerConfig,
    MqttClient,
    MqttConnectionError,
    MqttPublishError,
    client_id_for_role,
)
from src.mqtt.publisher import MqttPublisher, build_publisher
from src.mqtt.topics import DEFAULT_TOPICS_PATH, TopicError, load_topics
from src.simulator.traffic_replay import DEFAULT_CSV_PATH, TrafficReplay


class FakePublishInfo:
    def __init__(self, rc=0, mid=0):
        self.rc = rc
        self.mid = mid


class FakeClient:
    """Minimal stand-in for paho.mqtt.client.Client."""

    def __init__(self):
        self.published = []
        self.connected_to = None
        self.loops = 0
        self.disconnected = False
        self.reconnects = 0
        self.on_connect = None
        self.on_disconnect = None
        self.on_publish = None
        self.fail_on_call = None
        self.auto_connect = True   # fire on_connect (rc=0) on connect_async
        self.auto_ack = True       # fire on_publish for QoS>0 publishes
        self.connect_rc = 0        # reason code reported by on_connect
        self._next_mid = 1

    # paho has connect_async; the wrapper prefers it.
    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)
        self._fire_connect()
        return 0

    def connect(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)
        self._fire_connect()
        return 0

    def _fire_connect(self):
        if self.auto_connect and self.on_connect:
            self.on_connect(self, None, {}, self.connect_rc, None)

    def loop_start(self):
        self.loops += 1

    def loop_stop(self):
        self.loops -= 1

    def disconnect(self):
        self.disconnected = True
        return 0

    def reconnect(self):
        self.reconnects += 1
        self._fire_connect()
        return 0

    def reconnect_delay_set(self, min_delay=1, max_delay=60):
        return None

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail_on_call is not None and len(self.published) == self.fail_on_call:
            raise OSError("simulated publish failure")
        mid = self._next_mid
        self._next_mid += 1
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain, "mid": mid}
        )
        info = FakePublishInfo(0, mid)
        if qos > 0 and self.auto_ack and self.on_publish:
            self.on_publish(self, None, mid, 0, None)
        return info


def make_reading(approach="North", timestamp="2026-10-12 07:32:00"):
    return SensorReading(timestamp, approach, 5, 3, 20, 2, "LOW", "Rush hour")


class TopicTests(unittest.TestCase):
    def test_sensor_topics_per_approach(self):
        topics = load_topics(DEFAULT_TOPICS_PATH)
        self.assertEqual(topics.sensor_topic("North"), "team9/traffic/sensors/north")
        self.assertEqual(topics.sensor_topic("West"), "team9/traffic/sensors/west")

    def test_status_and_command_topics(self):
        topics = load_topics()
        self.assertEqual(topics.status_topic, "team9/traffic/status")
        self.assertEqual(topics.command_topic("East"), "team9/traffic/commands/east")

    def test_unknown_approach_rejected(self):
        with self.assertRaises(TopicError):
            load_topics().sensor_topic("Up")


class ClientTests(unittest.TestCase):
    def test_connect_and_disconnect_with_injected_client(self):
        fake = FakeClient()
        client = MqttClient(BrokerConfig(), client=fake)
        client.connect()
        self.assertEqual(fake.connected_to, ("localhost", 1883, 60))
        self.assertTrue(client.is_connected)
        self.assertEqual(fake.loops, 1)
        client.disconnect()
        self.assertTrue(fake.disconnected)
        self.assertFalse(client.is_connected)

    def test_broker_config_from_settings(self):
        config = BrokerConfig.from_settings()
        self.assertTrue(config.host)
        self.assertEqual(config.port, 1883)
        self.assertEqual(config.qos, 1)

    def test_publish_acknowledgement_is_tracked(self):
        fake = FakeClient()
        client = MqttClient(BrokerConfig(), client=fake)
        self.assertIsNotNone(fake.on_publish, "on_publish callback must be registered")
        self.assertEqual(client.acknowledged_count, 0)
        fake.on_publish(fake, None, 1)          # paho 1.x form
        fake.on_publish(fake, None, 2, 0, None)  # paho 2.x form
        self.assertEqual(client.acknowledged_count, 2)

    def test_unexpected_disconnect_schedules_reconnect(self):
        fake = FakeClient()
        client = MqttClient(
            BrokerConfig(reconnect_delay_seconds=0, connect_timeout_seconds=0.1),
            client=fake,
        )
        client.connect()
        fake.auto_connect = False  # keep it disconnected so we observe retries
        client._on_disconnect(fake, None, 1)  # unexpected drop
        time.sleep(0.3)
        client.disconnect()
        self.assertGreaterEqual(fake.reconnects, 1)


class ConnectionLifecycleTests(unittest.TestCase):
    def test_connect_succeeds_via_callback(self):
        fake = FakeClient()
        client = MqttClient(BrokerConfig(), client=fake)
        client.connect()
        self.assertTrue(client.is_connected)
        client.disconnect()

    def test_connect_times_out_and_fails_clearly(self):
        fake = FakeClient()
        fake.auto_connect = False  # never delivers the on_connect callback
        client = MqttClient(BrokerConfig(connect_timeout_seconds=0.15), client=fake)
        started = time.time()
        with self.assertRaises(MqttConnectionError) as ctx:
            client.connect()
        elapsed = time.time() - started
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(elapsed, 2.0)          # bounded by the configured timeout
        self.assertFalse(client.is_connected)
        self.assertEqual(fake.loops, 0)        # loop stopped during cleanup
        self.assertTrue(fake.disconnected)

    def test_connect_rejection_reported(self):
        fake = FakeClient()
        fake.connect_rc = 5  # CONNACK failure
        client = MqttClient(BrokerConfig(connect_timeout_seconds=0.5), client=fake)
        with self.assertRaises(MqttConnectionError) as ctx:
            client.connect()
        self.assertIn("rejected", str(ctx.exception))
        self.assertFalse(client.is_connected)

    def test_reconnect_waits_for_callback_not_rc_zero(self):
        fake = FakeClient()
        client = MqttClient(
            BrokerConfig(reconnect_delay_seconds=0, connect_timeout_seconds=0.15),
            client=fake,
        )
        client.connect()
        fake.auto_connect = False  # reconnect() returns 0 but never confirms
        client._on_disconnect(fake, None, 1)
        time.sleep(0.5)
        client.disconnect()
        self.assertGreaterEqual(fake.reconnects, 2)  # kept retrying, not fooled by rc==0
        self.assertFalse(client.is_connected)

    def test_reconnect_completes_when_callback_fires(self):
        fake = FakeClient()
        client = MqttClient(
            BrokerConfig(reconnect_delay_seconds=0, connect_timeout_seconds=1.0),
            client=fake,
        )
        client.connect()
        client._on_disconnect(fake, None, 1)
        time.sleep(0.3)
        self.assertTrue(client.is_connected)
        self.assertGreaterEqual(fake.reconnects, 1)
        client.disconnect()


class PublisherAckTests(unittest.TestCase):
    def test_publish_reading_waits_for_ack(self):
        fake = FakeClient()
        client = MqttClient(BrokerConfig(), client=fake)
        publisher = MqttPublisher(client, load_topics())
        publisher.publish_reading(make_reading())
        self.assertEqual(client.acknowledged_count, 1)
        self.assertEqual(publisher.published_count, 1)

    def test_ack_timeout_raises_and_keeps_sequence(self):
        fake = FakeClient()
        fake.auto_ack = False
        client = MqttClient(BrokerConfig(ack_timeout_seconds=0.1), client=fake)
        publisher = MqttPublisher(client, load_topics())
        with self.assertRaises(MqttPublishError) as ctx:
            publisher.publish_reading(make_reading())
        self.assertIn("acknowledgement", str(ctx.exception))
        self.assertEqual(publisher.published_count, 0)
        self.assertEqual(publisher.last_sequence, 0)

    def test_qos0_does_not_wait_for_ack(self):
        fake = FakeClient()
        fake.auto_ack = False
        client = MqttClient(BrokerConfig(qos=0, ack_timeout_seconds=0.05), client=fake)
        publisher = MqttPublisher(client, load_topics())
        publisher.publish_reading(make_reading())  # must not block or raise
        self.assertEqual(publisher.published_count, 1)
        self.assertEqual(client.acknowledged_count, 0)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.client = MqttClient(BrokerConfig(), client=self.fake)
        self.publisher = MqttPublisher(self.client, load_topics())

    def test_publish_reading_topic_and_payload(self):
        result = self.publisher.publish_reading(make_reading("East"))
        self.assertEqual(result["topic"], "team9/traffic/sensors/east")
        self.assertEqual(len(self.fake.published), 1)
        body = json.loads(self.fake.published[0]["payload"])
        self.assertEqual(body["approach"], "East")
        self.assertEqual(body["sequence"], 1)
        self.assertEqual(body["queue_length"], 3)

    def test_sequence_increments_without_duplicates(self):
        for _ in range(3):
            self.publisher.publish_reading(make_reading())
        sequences = [json.loads(p["payload"])["sequence"] for p in self.fake.published]
        self.assertEqual(sequences, [1, 2, 3])
        self.assertEqual(self.publisher.published_count, 3)

    def test_failed_publish_does_not_gap_sequence(self):
        self.fake.fail_on_call = 1
        self.publisher.publish_reading(make_reading())  # seq 1 -> ok
        with self.assertRaises(MqttPublishError):
            self.publisher.publish_reading(make_reading())  # fails, seq not consumed
        self.fake.fail_on_call = None  # recover after the single injected failure
        self.publisher.publish_reading(make_reading())  # should still be seq 2
        sequences = [json.loads(p["payload"])["sequence"] for p in self.fake.published]
        self.assertEqual(sequences, [1, 2])
        self.assertEqual(self.publisher.published_count, 2)


class ConnectCallbackTests(unittest.TestCase):
    """Regression: paho 2.x passes a ReasonCode enum, not an int rc."""

    class Paho2Reason:
        def __init__(self, failure):
            self.is_failure = failure

    class ReasonWithValueOnly:
        def __init__(self, value):
            self.value = value

    def test_paho1_int_rc(self):
        self.assertTrue(MqttClient._connect_succeeded(0))
        self.assertFalse(MqttClient._connect_succeeded(1))
        self.assertFalse(MqttClient._connect_succeeded(5))

    def test_paho2_reason_code_attribute(self):
        self.assertTrue(MqttClient._connect_succeeded(self.Paho2Reason(False)))
        self.assertFalse(MqttClient._connect_succeeded(self.Paho2Reason(True)))

    def test_reason_code_value_fallback(self):
        self.assertTrue(MqttClient._connect_succeeded(self.ReasonWithValueOnly(0)))
        self.assertFalse(MqttClient._connect_succeeded(self.ReasonWithValueOnly(135)))

    def test_on_connect_does_not_raise_and_tracks_state(self):
        client = MqttClient(BrokerConfig(), client=FakeClient())
        client._on_connect(None, None, {}, self.Paho2Reason(False))
        self.assertTrue(client.is_connected)
        client._on_connect(None, None, {}, self.Paho2Reason(True))
        self.assertFalse(client.is_connected)
        client._on_connect(None, None, {}, 0)  # paho 1.x 4-arg form
        self.assertTrue(client.is_connected)


class ConfigConsistencyTests(unittest.TestCase):
    def test_broker_and_topics_line_up(self):
        config = BrokerConfig.from_settings()
        self.assertIsInstance(config.port, int)
        self.assertEqual(config.port, 1883)
        self.assertIn(config.qos, (0, 1, 2))

        topics = load_topics()
        for approach in ("North", "South", "East", "West"):
            topic = topics.sensor_topic(approach)
            self.assertTrue(topic.startswith(topics.base + "/"), topic)
        self.assertEqual(topics.status_topic, topics.base + "/status")

        publisher = MqttPublisher(MqttClient(config, client=FakeClient()), topics)
        self.assertEqual(publisher.qos, config.qos)
        self.assertEqual(publisher.retain, config.retain)

    def test_simulator_approaches_match_topic_approaches(self):
        import yaml

        with DEFAULT_SETTINGS_PATH.open(encoding="utf-8") as handle:
            settings = yaml.safe_load(handle)
        topics = load_topics(DEFAULT_TOPICS_PATH)
        self.assertEqual(set(settings["simulator"]["approaches"]), set(topics.approaches))


class ReplayPublisherIntegrationTests(unittest.TestCase):
    def test_no_skipped_or_duplicated_records(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0)
        replay.load()
        fake = FakeClient()
        publisher = MqttPublisher(MqttClient(BrokerConfig(), client=fake), load_topics())

        replay.start(callback=publisher.publish_reading, block=True)

        self.assertEqual(publisher.published_count, len(replay))
        self.assertEqual(len(replay), 5000)
        sequences = [json.loads(p["payload"])["sequence"] for p in fake.published]
        self.assertEqual(sequences, list(range(1, len(replay) + 1)))
        self.assertEqual(len(set(sequences)), len(sequences))
        topics = {p["topic"] for p in fake.published}
        self.assertEqual(topics, {f"team9/traffic/sensors/{name}" for name in ("north", "south", "east", "west")})

        # published order must equal source order, with no row dropped or repeated
        published = [(json.loads(p["payload"])["timestamp"], json.loads(p["payload"])["approach"]) for p in fake.published]
        expected = [(r.timestamp, r.approach) for r in replay.readings]
        self.assertEqual(published, expected)

    def test_failed_publish_does_not_advance_position_or_lose_record(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0)
        replay.load()
        fake = FakeClient()
        fake.fail_on_call = 5  # 6th publish attempt fails
        publisher = MqttPublisher(MqttClient(BrokerConfig(), client=fake), load_topics())

        replay.start(callback=publisher.publish_reading, block=True)

        self.assertIsInstance(replay.last_error, MqttPublishError)
        self.assertEqual(replay.status, "error")
        self.assertEqual(publisher.published_count, 5)
        self.assertEqual(replay.position, 5)  # did not advance past the failed record
        self.assertEqual(publisher.last_sequence, 5)  # sequence not consumed by the failure
        # the failed record is still queued at the current position, not lost
        recovered = replay.step()
        self.assertEqual((recovered.timestamp, recovered.approach),
                         (replay.readings[5].timestamp, replay.readings[5].approach))

    def test_ack_timeout_does_not_advance_position_or_lose_record(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0)
        replay.load()
        fake = FakeClient()
        fake.auto_ack = False  # broker never confirms delivery
        publisher = MqttPublisher(
            MqttClient(BrokerConfig(ack_timeout_seconds=0.1), client=fake), load_topics()
        )

        replay.start(callback=publisher.publish_reading, block=True)

        self.assertIsInstance(replay.last_error, MqttPublishError)
        self.assertEqual(publisher.published_count, 0)
        self.assertEqual(publisher.last_sequence, 0)
        self.assertEqual(replay.position, 0)  # stopped at the un-acked record, not lost


class ClientIdRoleTests(unittest.TestCase):
    """Each process role must get its own client id to avoid a broker takeover."""

    def test_roles_produce_distinct_ids(self):
        base = "team9-traffic-sim"
        ids = {
            client_id_for_role(base, role)
            for role in ("publisher", "controller", "actuator")
        }
        self.assertEqual(len(ids), 3)
        self.assertEqual(client_id_for_role(base, "publisher"), "team9-traffic-sim-publisher")
        self.assertEqual(client_id_for_role(base, "controller"), "team9-traffic-sim-controller")
        self.assertEqual(client_id_for_role(base, "actuator"), "team9-traffic-sim-actuator")

    def test_same_role_is_stable(self):
        self.assertEqual(
            client_id_for_role("base", "publisher"),
            client_id_for_role("base", "publisher"),
        )

    def test_empty_or_none_base_falls_back_to_default(self):
        self.assertEqual(client_id_for_role(None, "controller"), "team9-traffic-sim-controller")
        self.assertEqual(client_id_for_role("", "controller"), "team9-traffic-sim-controller")
        self.assertEqual(client_id_for_role("   ", "controller"), "team9-traffic-sim-controller")

    def test_empty_role_returns_base(self):
        self.assertEqual(client_id_for_role("base", None), "base")
        self.assertEqual(client_id_for_role("base", ""), "base")

    def test_broker_config_with_role_copies_and_suffixes(self):
        config = BrokerConfig(client_id="team9-traffic-sim")
        other = config.with_role("actuator")
        self.assertEqual(other.client_id, "team9-traffic-sim-actuator")
        self.assertEqual(config.client_id, "team9-traffic-sim")  # original untouched

    def test_build_publisher_applies_publisher_role(self):
        publisher = build_publisher()
        self.assertTrue(publisher.client.broker.client_id.endswith("-publisher"))


if __name__ == "__main__":
    unittest.main()
