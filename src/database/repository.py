"""Data access for the traffic database.

``TrafficRepository`` wraps a connection created by :mod:`src.database.db` and
offers idempotent inserts (duplicates are ignored, not raised) plus historical
queries. Sensor payloads arriving as JSON (e.g. from Node-RED) are validated
with the same rules the simulator uses, so a malformed message is rejected
rather than stored.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, TypeVar

_T = TypeVar("_T")

from src.common.schemas import SensorReading
from src.database.db import DatabaseConfig, connect, init_db
from src.simulator.sensor_generator import ReadingValidationError, SensorGenerator

logger = logging.getLogger(__name__)

#: Bounded retry for transient SQLite lock errors. WAL mode plus the busy
#: timeout already make a competing writer wait; this is the final guard so a
#: brief overlap that outran the timeout never silently drops a write. Retrying
#: is safe because every write is idempotent (INSERT OR IGNORE / keyed UPDATE).
_WRITE_MAX_RETRIES = 4
_WRITE_RETRY_BASE_DELAY = 0.05


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text

# MQTT/JSON keys -> the column names the shared SensorGenerator validates.
_PAYLOAD_TO_COLUMN = {
    "timestamp": "Timestamp",
    "approach": "Approach",
    "vehicle_count": "Vehicle_Count",
    "queue_length": "Queue_Length",
    "avg_wait_time": "Avg_Wait_Time",
    "arrival_rate": "Arrival_Rate",
    "congestion_level": "Congestion_Level",
    "scenario": "Scenario",
}

_INSERT_READING = (
    "INSERT OR IGNORE INTO sensor_readings "
    "(timestamp, approach, vehicle_count, queue_length, avg_wait_time, "
    "arrival_rate, congestion_level, scenario, sequence) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class InvalidReadingError(ValueError):
    """Raised when an incoming payload is not a valid sensor reading."""


def reading_from_payload(payload: object, generator: Optional[SensorGenerator] = None) -> SensorReading:
    """Validate a JSON payload into a SensorReading, or raise InvalidReadingError."""
    if not isinstance(payload, dict):
        raise InvalidReadingError("payload must be a JSON object")
    row = {
        column: payload.get(key)
        for key, column in _PAYLOAD_TO_COLUMN.items()
    }
    try:
        return (generator or SensorGenerator()).build(row)
    except ReadingValidationError as exc:
        raise InvalidReadingError(str(exc)) from exc


class TrafficRepository:
    def __init__(self, conn: sqlite3.Connection, *, log: Optional[logging.Logger] = None) -> None:
        self.conn = conn
        self.log = log or logger
        # The connection is shared across threads (paho network thread writes
        # readings while the phase-tick thread writes events), so serialise
        # access on this connection.
        self._lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def open(
        cls,
        db_path: Optional[Path] = None,
        *,
        settings_path: Optional[Path] = None,
        log: Optional[logging.Logger] = None,
    ) -> "TrafficRepository":
        """Open a repository at ``db_path`` (else the path from settings.yaml)."""
        if db_path is None:
            db_path = DatabaseConfig.from_settings(settings_path).resolved_path
        conn = connect(db_path)
        init_db(conn)
        return cls(conn, log=log)

    def close(self) -> None:
        self.conn.close()

    def _retry(self, operation: Callable[[], _T]) -> _T:
        """Run a write callable, retrying transient lock errors a bounded number
        of times. The retry unit includes the commit, because that is where the
        writer lock is taken. On a lock failure the transaction is rolled back
        first, so a retry starts clean and cannot duplicate a partially applied
        write (most writes are also idempotent by construction)."""
        attempt = 0
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if attempt >= _WRITE_MAX_RETRIES or not _is_lock_error(exc):
                    raise
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                time.sleep(_WRITE_RETRY_BASE_DELAY * (attempt + 1))
                attempt += 1

    # --------------------------------------------------------------- readings
    def insert_reading(self, reading: SensorReading, *, sequence: Optional[int] = None) -> bool:
        """Insert a reading; return True if stored, False if it was a duplicate."""
        with self._lock:
            def run():
                cursor = self.conn.execute(
                    _INSERT_READING,
                    (
                        reading.timestamp,
                        reading.approach,
                        reading.vehicle_count,
                        reading.queue_length,
                        reading.avg_wait_time,
                        reading.arrival_rate,
                        reading.congestion_level,
                        reading.scenario,
                        sequence,
                    ),
                )
                self.conn.commit()
                return cursor.rowcount == 1
            return self._retry(run)

    def insert_payload(self, payload: object, *, sequence: Optional[int] = None) -> bool:
        """Validate and insert a JSON-shaped reading payload."""
        return self.insert_reading(reading_from_payload(payload), sequence=sequence)

    def insert_readings(self, readings: Iterable[SensorReading]) -> int:
        """Bulk-insert readings; return the number actually stored (dupes skipped)."""
        rows = [
            (
                r.timestamp,
                r.approach,
                r.vehicle_count,
                r.queue_length,
                r.avg_wait_time,
                r.arrival_rate,
                r.congestion_level,
                r.scenario,
                None,
            )
            for r in readings
        ]
        with self._lock:
            def run():
                before = self.conn.total_changes
                self.conn.executemany(_INSERT_READING, rows)
                self.conn.commit()
                return self.conn.total_changes - before
            return self._retry(run)

    def count_readings(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0])

    def fetch_readings(
        self,
        *,
        approach: Optional[str] = None,
        limit: Optional[int] = 100,
        newest_first: bool = True,
        since: Optional[str] = None,
    ) -> List[dict]:
        """Return historical readings, optionally filtered by approach/time."""
        clauses: List[str] = []
        params: List[object] = []
        if approach is not None:
            clauses.append("approach = ?")
            params.append(approach)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        query = "SELECT * FROM sensor_readings"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in self.conn.execute(query, params).fetchall()]

    def latest_per_approach(self) -> List[dict]:
        """Most recent reading for each approach, newest approach first."""
        query = """
            SELECT * FROM sensor_readings AS r
            WHERE r.id = (
                SELECT r2.id FROM sensor_readings AS r2
                WHERE r2.approach = r.approach
                ORDER BY r2.timestamp DESC, r2.id DESC
                LIMIT 1
            )
            ORDER BY r.approach
        """
        return [dict(row) for row in self.conn.execute(query).fetchall()]

    # -------------------------------------------------------------- decisions
    def _write(self, sql: str, params: tuple):
        """Execute a writing statement under the shared-connection lock."""
        with self._lock:
            def run():
                cursor = self.conn.execute(sql, params)
                self.conn.commit()
                return cursor
            return self._retry(run)

    def insert_decision(
        self,
        *,
        cycle_no: int,
        decision_time: str,
        approach: str,
        decision_id: Optional[str] = None,
        traffic_timestamp_used: Optional[str] = None,
        queue_length: Optional[int] = None,
        arrival_rate: Optional[int] = None,
        previous_green_time: Optional[int] = None,
        requested_green_time: Optional[int] = None,
        applied_green_time: Optional[int] = None,
        cycle_length_sec: Optional[int] = None,
        decision_reason: Optional[str] = None,
    ) -> bool:
        cursor = self._write(
            "INSERT OR IGNORE INTO signal_decisions "
            "(decision_id, cycle_no, decision_time, approach, traffic_timestamp_used, "
            "queue_length, arrival_rate, previous_green_time, requested_green_time, "
            "applied_green_time, cycle_length_sec, decision_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id, cycle_no, decision_time, approach, traffic_timestamp_used,
                queue_length, arrival_rate, previous_green_time, requested_green_time,
                applied_green_time, cycle_length_sec, decision_reason,
            ),
        )
        return cursor.rowcount == 1

    def insert_signal_event(
        self,
        *,
        cycle_no: int,
        approach: str,
        start_time: str,
        signal_state: str,
        duration_sec: int,
        event_id: Optional[str] = None,
    ) -> bool:
        cursor = self._write(
            "INSERT OR IGNORE INTO signal_events "
            "(event_id, cycle_no, approach, start_time, signal_state, duration_sec) "
            "VALUES (?,?,?,?,?,?)",
            (event_id, cycle_no, approach, start_time, signal_state, duration_sec),
        )
        return cursor.rowcount == 1

    def insert_actuator_command(
        self,
        *,
        approach: str,
        command: str,
        command_id: Optional[str] = None,
        green_time_sec: Optional[int] = None,
        cycle_no: Optional[int] = None,
        status: str = "pending",
        detail: Optional[str] = None,
    ) -> bool:
        cursor = self._write(
            "INSERT OR IGNORE INTO actuator_commands "
            "(command_id, approach, command, green_time_sec, cycle_no, status, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (command_id, approach, command, green_time_sec, cycle_no, status, detail),
        )
        return cursor.rowcount == 1

    def update_actuator_ack(
        self,
        command_id: str,
        *,
        status: str,
        reason: Optional[str] = None,
        ack_at: Optional[str] = None,
        applied_state: Optional[str] = None,
    ) -> int:
        """Record the actuator's acknowledgement for a command."""
        cursor = self._write(
            "UPDATE actuator_commands SET status=?, reason=?, ack_at=?, "
            "applied_at=COALESCE(?, applied_at), detail=COALESCE(?, detail) "
            "WHERE command_id=?",
            (status, reason, ack_at, ack_at, applied_state, command_id),
        )
        return cursor.rowcount

    # ------------------------------------------------------------ system events
    def log_system_event(self, level: str, source: str, event_type: str, message: str = "") -> int:
        cursor = self._write(
            "INSERT INTO system_events (level, source, event_type, message) VALUES (?,?,?,?)",
            (level, source, event_type, message),
        )
        return int(cursor.lastrowid)

    def count_system_events(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0])
