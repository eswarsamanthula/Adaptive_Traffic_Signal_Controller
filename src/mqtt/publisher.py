"""Publish simulator readings as JSON over MQTT.

``MqttPublisher.publish_reading`` has the same signature the simulator's
``TrafficReplay.start(callback=...)`` expects, so the two compose directly:

    replay.start(callback=publisher.publish_reading)
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

from src.common.schemas import SensorReading
from src.mqtt.mqtt_client import BrokerConfig, MqttClient, MqttPublishError
from src.mqtt.topics import TopicSet, load_topics

logger = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(
        self,
        client: MqttClient,
        topics: TopicSet,
        *,
        qos: Optional[int] = None,
        retain: Optional[bool] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.topics = topics
        self.log = log or logger
        self.qos = client.broker.qos if qos is None else qos
        self.retain = client.broker.retain if retain is None else retain
        self.ack_timeout = client.broker.ack_timeout_seconds
        self._sequence = 0
        self._published = 0

    @property
    def published_count(self) -> int:
        return self._published

    @property
    def last_sequence(self) -> int:
        return self._sequence

    def build_payload(self, reading: SensorReading, sequence: int) -> dict:
        payload = reading.to_payload()
        payload["sequence"] = sequence
        return payload

    def publish_reading(self, reading: SensorReading, *, wait_for_ack: Optional[bool] = None) -> dict:
        """Publish one reading to its approach-specific sensor topic.

        For QoS 1/2 the broker acknowledgement is awaited (up to
        ``ack_timeout``). The sequence number only advances after a successful
        publish + ack, so a failure never creates a gap and the record is not
        silently dropped: the caller (the replay) stops with the record still
        pending at its current position.
        """
        query_ack = self.qos > 0 if wait_for_ack is None else (wait_for_ack and self.qos > 0)
        sequence = self._sequence + 1
        topic = self.topics.sensor_topic(reading.approach)
        payload = self.build_payload(reading, sequence)
        data = json.dumps(payload, separators=(",", ":"))
        info = self.client.publish(topic, data, qos=self.qos, retain=self.retain)
        if query_ack:
            mid = getattr(info, "mid", None)
            if mid:
                if not self.client.wait_for_ack(mid, self.ack_timeout):
                    raise MqttPublishError(
                        f"no acknowledgement for {topic} (mid={mid}) within {self.ack_timeout}s"
                    )
        self._sequence = sequence
        self._published += 1
        return {"topic": topic, "payload": payload, "bytes": len(data)}


def build_publisher(
    settings_path: Optional[Path] = None,
    topics_path: Optional[Path] = None,
    log: Optional[logging.Logger] = None,
) -> MqttPublisher:
    broker = BrokerConfig.from_settings(settings_path).with_role("publisher")
    client = MqttClient(broker, log=log)
    topics = load_topics(topics_path)
    return MqttPublisher(client, topics, log=log)


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Publish simulator readings to MQTT.")
    parser.add_argument("--settings", default=None, help="path to settings.yaml")
    parser.add_argument("--topics", default=None, help="path to mqtt_topics.yaml")
    parser.add_argument("--speed", type=float, default=None, help="replay speed multiplier")
    parser.add_argument("--limit", type=int, default=8, help="stop after N records")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from src.simulator.traffic_replay import TrafficReplay

    overrides = {"speed": args.speed} if args.speed is not None else {}
    replay = TrafficReplay.from_settings(args.settings, **overrides)
    publisher = build_publisher(args.settings, args.topics)

    publisher.client.connect()
    state = {"count": 0}

    def on_reading(reading: SensorReading) -> None:
        result = publisher.publish_reading(reading)
        state["count"] += 1
        print(f"published {result['topic']} seq={result['payload']['sequence']} {result['payload']}")
        if args.limit and state["count"] >= args.limit:
            replay.stop()

    try:
        replay.start(callback=on_reading, block=True)
    finally:
        publisher.client.disconnect()

    if replay.last_error is not None:
        print(
            f"replay stopped after publish error at position "
            f"{replay.position}: {replay.last_error}"
        )
        return 1

    print(f"published {publisher.published_count} reading(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
