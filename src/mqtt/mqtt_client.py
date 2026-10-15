"""Paho MQTT client wrapper with reconnection and graceful shutdown.

The broker settings are read from config/settings.yaml. A client instance can
be injected (``client=...``) so the wrapper is testable without a real broker
or the paho package installed.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

logger = logging.getLogger(__name__)

#: Fallback base client id when the configured one is empty/None.
DEFAULT_CLIENT_ID = "team9-traffic-sim"


def client_id_for_role(base: Optional[str], role: Optional[str], default: str = DEFAULT_CLIENT_ID) -> str:
    """Return a unique MQTT client id for a process role.

    The configured id is a *base prefix*; each process appends its role so
    several processes can share one broker without a client-id takeover
    ("already connected, closing old connection"). An empty/None base falls
    back to ``default``; an empty role returns the base unchanged.
    """
    prefix = (base or "").strip() or default
    role = (role or "").strip()
    return f"{prefix}-{role}" if role else prefix


class MqttError(RuntimeError):
    """Base MQTT error."""


class MqttConnectionError(MqttError):
    """Raised when the broker connection cannot be established."""


class MqttPublishError(MqttError):
    """Raised when a publish call fails."""


@dataclass
class BrokerConfig:
    host: str = "localhost"
    port: int = 1883
    keepalive: int = 60
    client_id: str = "team9-traffic-sim"
    username: Optional[str] = None
    password: Optional[str] = None
    clean_session: bool = True
    qos: int = 1
    retain: bool = False
    reconnect_delay_seconds: float = 5.0
    reconnect_max_seconds: float = 60.0
    connect_timeout_seconds: float = 10.0
    ack_timeout_seconds: float = 5.0

    @classmethod
    def from_dict(cls, data: Optional[dict] = None) -> "BrokerConfig":
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})

    @classmethod
    def from_settings(cls, path: Optional[Path] = None) -> "BrokerConfig":
        path = Path(path) if path else DEFAULT_SETTINGS_PATH
        if not path.exists():
            raise MqttConnectionError(f"settings not found: {path}")
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls.from_dict(data.get("mqtt", {}))

    def with_role(self, role: str) -> "BrokerConfig":
        """Copy of this config whose client_id is suffixed with ``role``."""
        return replace(self, client_id=client_id_for_role(self.client_id, role))


class MqttClient:
    def __init__(
        self,
        broker: Optional[BrokerConfig] = None,
        *,
        client: object = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.broker = broker or BrokerConfig()
        self.log = log or logger
        self._client = client
        self._connected = threading.Event()
        self._connect_done = threading.Event()
        self._last_connect_error = None
        self._shutting_down = False
        self._reconnect_thread: Optional[threading.Thread] = None
        self._acked = 0
        self._pending: dict = {}   # mid -> Event for in-flight QoS>0 publishes
        self._results: dict = {}   # mid -> bool (True acked / False failed) arriving early
        self._ack_lock = threading.Lock()

        if self._client is None:
            self._client = self._build_client()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        try:
            self._client.on_message = self._on_message
        except (AttributeError, TypeError):  # injected clients may be read-only
            pass
        self._handlers: dict = {}

    # -------------------------------------------------------------- construct
    def _build_client(self):
        try:
            from paho.mqtt import client as mqtt
        except ImportError as exc:
            raise MqttConnectionError(
                "paho-mqtt is not installed. Install it with: pip install paho-mqtt"
            ) from exc

        kwargs = {
            "client_id": self.broker.client_id,
            "clean_session": self.broker.clean_session,
        }
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
        else:
            client = mqtt.Client(**kwargs)

        if self.broker.username:
            client.username_pw_set(self.broker.username, self.broker.password)
        client.reconnect_delay_set(min_delay=1, max_delay=self.broker.reconnect_max_seconds)
        return client

    # --------------------------------------------------------------- callbacks
    @staticmethod
    def _connect_succeeded(reason_code) -> bool:
        """Normalise paho 1.x `rc` (int) and paho 2.x `ReasonCode`.

        paho 2.x exposes `ReasonCode.is_failure` and has no reliable `int()`
        conversion, so falling back to `int(reason_code)` would raise inside
        the callback thread.
        """
        if reason_code is None:
            return True
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None:
            return not (is_failure() if callable(is_failure) else is_failure)
        value = getattr(reason_code, "value", reason_code)
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            return False

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if self._connect_succeeded(reason_code):
            self._connected.set()
            self.log.info("connected to broker %s:%s", self.broker.host, self.broker.port)
        else:
            self._connected.clear()
            self._last_connect_error = reason_code
            self.log.error("broker rejected connection: %s", reason_code)
        self._connect_done.set()

    def _on_disconnect(self, client, userdata, *args):
        self._connected.clear()
        if self._shutting_down:
            return
        self.log.warning("disconnected from broker; scheduling reconnect")
        self._schedule_reconnect()

    @staticmethod
    def _ack_failed(reason_code) -> bool:
        """True if an on_publish reason code reports a failed delivery."""
        if reason_code is None:
            return False
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None:
            return is_failure() if callable(is_failure) else bool(is_failure)
        value = getattr(reason_code, "value", reason_code)
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return False

    def _on_publish(self, client, userdata, mid, reason_code=None, *args):
        """Called by paho when the broker acknowledges a QoS 1/2 publish."""
        result = not self._ack_failed(reason_code)
        with self._ack_lock:
            if result:
                self._acked += 1
            event = self._pending.pop(mid, None)
            self._results[mid] = result
            if event is not None:
                event.set()

    def _on_message(self, client, userdata, message):
        """Dispatch an inbound message to the callback registered for its topic."""
        topic = getattr(message, "topic", "")
        handler = self._handlers.get(topic)
        if handler is None:
            return
        payload = getattr(message, "payload", b"")
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", errors="replace")
        try:
            handler(topic, payload)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the loop
            self.log.error("message handler for %s failed: %s", topic, exc)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="mqtt-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        while not self._shutting_down and not self._connected.is_set():
            if self._sleep(self.broker.reconnect_delay_seconds):
                return
            if self._shutting_down:
                return
            try:
                self._client.reconnect()
            except Exception as exc:  # noqa: BLE001 - keep retrying
                self.log.warning("reconnect attempt failed: %s", exc)
                continue
            # reconnect() only *starts* the attempt; the connection is proven
            # by the on_connect callback (which sets _connected), not by rc == 0.
            if self._connected.wait(self.broker.connect_timeout_seconds):
                self.log.info("reconnected to broker")
                return
            self.log.warning(
                "reconnect attempt to %s:%s did not complete within %ss; retrying",
                self.broker.host,
                self.broker.port,
                self.broker.connect_timeout_seconds,
            )

    def _sleep(self, seconds: float) -> bool:
        """Sleep in small steps; returns True if shutdown was requested."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._shutting_down:
                return True
            time.sleep(0.1)
        return self._shutting_down

    # ------------------------------------------------------------------ public
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def acknowledged_count(self) -> int:
        with self._ack_lock:
            return self._acked

    def connect(self) -> "MqttClient":
        """Connect within ``connect_timeout_seconds`` or raise clearly.

        Uses the async connect + callback model so the timeout bounds the wait
        for the actual CONNACK (via on_connect), not just the TCP connect call.
        """
        self._connect_done.clear()
        self._last_connect_error = None
        client = self._client

        if hasattr(client, "connect_async"):
            try:
                client.connect_async(
                    self.broker.host, self.broker.port, self.broker.keepalive
                )
            except Exception as exc:
                raise MqttConnectionError(
                    f"could not start connection to {self.broker.host}:{self.broker.port}: {exc}"
                ) from exc
            client.loop_start()
            if not self._connect_done.wait(self.broker.connect_timeout_seconds):
                self._cleanup_after_failed_connect()
                raise MqttConnectionError(
                    f"timed out connecting to {self.broker.host}:{self.broker.port} "
                    f"after {self.broker.connect_timeout_seconds}s"
                )
            if not self._connected.is_set():
                self._cleanup_after_failed_connect()
                raise MqttConnectionError(
                    f"connection to {self.broker.host}:{self.broker.port} rejected: "
                    f"{self._last_connect_error}"
                )
            return self

        # Fallback for injected clients without connect_async.
        try:
            return_code = client.connect(
                self.broker.host, self.broker.port, self.broker.keepalive
            )
        except Exception as exc:
            raise MqttConnectionError(
                f"could not connect to {self.broker.host}:{self.broker.port}: {exc}"
            ) from exc
        if return_code != 0:
            raise MqttConnectionError(
                f"connection to {self.broker.host}:{self.broker.port} failed (rc={return_code})"
            )
        self._connected.set()
        client.loop_start()
        return self

    def _cleanup_after_failed_connect(self) -> None:
        self._shutting_down = True
        try:
            self._client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._connected.clear()
        self._shutting_down = False

    def wait_for_ack(self, mid: int, timeout: float) -> bool:
        """Block until the broker acknowledges publish ``mid``.

        Returns True when acknowledged, False on timeout or a failed delivery
        reason code. Robust to the ack arriving before the wait is registered.
        """
        with self._ack_lock:
            if mid in self._results:
                return self._results.pop(mid)
            event = self._pending.get(mid)
            if event is None:
                event = threading.Event()
                self._pending[mid] = event
        if event.wait(timeout):
            with self._ack_lock:
                return self._results.pop(mid, True)
        with self._ack_lock:
            self._pending.pop(mid, None)
            self._results.pop(mid, None)
        return False

    def subscribe(self, topic: str, callback, qos: Optional[int] = None) -> None:
        """Register ``callback(topic, payload_str)`` and subscribe on the broker."""
        self._handlers[topic] = callback
        subscribe = getattr(self._client, "subscribe", None)
        if subscribe is None:
            return
        resolved = self.broker.qos if qos is None else qos
        result = subscribe(topic, qos=resolved)
        code = result[0] if isinstance(result, tuple) else getattr(result, "rc", 0)
        if code not in (0, None):
            raise MqttError(f"subscribe to {topic} rejected (rc={code})")

    def publish(self, topic: str, payload: str, qos: Optional[int] = None, retain: Optional[bool] = None):
        qos = self.broker.qos if qos is None else qos
        retain = self.broker.retain if retain is None else retain
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            raise MqttPublishError(f"publish to {topic} failed: {exc}") from exc
        return_code = getattr(info, "rc", 0)
        if return_code != 0:
            raise MqttPublishError(f"publish to {topic} rejected (rc={return_code})")
        return info

    def disconnect(self) -> None:
        self._shutting_down = True
        try:
            self._client.loop_stop()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("error stopping network loop: %s", exc)
        try:
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("error during disconnect: %s", exc)
        self._connected.clear()
        thread = self._reconnect_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
