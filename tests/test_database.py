"""Database storage tests (SQLite) and Node-RED flow validation.

Use a temporary database file, so the real data/traffic.db is never touched.

Run without pytest:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.schemas import SensorReading
from src.database.db import DatabaseConfig, init_db
from src.database.repository import (
    InvalidReadingError,
    TrafficRepository,
    reading_from_payload,
)
from src.simulator.traffic_replay import DEFAULT_CSV_PATH, TrafficReplay

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLOWS_PATH = PROJECT_ROOT / "node_red" / "flows.json"

TABLES = {
    "sensor_readings",
    "signal_decisions",
    "signal_events",
    "actuator_commands",
    "system_events",
}


def make_reading(approach="North", timestamp="2026-10-12 03:00:00", queue=3):
    return SensorReading(timestamp, approach, 5, queue, 20, 2, "LOW", "Rush hour")


def payload(**overrides):
    data = {
        "timestamp": "2026-10-12 03:00:00",
        "approach": "North",
        "vehicle_count": 5,
        "queue_length": 3,
        "avg_wait_time": 20,
        "arrival_rate": 2,
        "congestion_level": "LOW",
        "scenario": "Rush hour",
    }
    data.update(overrides)
    return data


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = TrafficRepository.open(self.db_path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.repo.close)


class SchemaTests(DatabaseTestCase):
    def test_all_tables_created(self):
        names = {
            row[0]
            for row in self.repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(TABLES.issubset(names), TABLES - names)

    def test_sensor_unique_constraint_present(self):
        indexes = self.repo.conn.execute("PRAGMA index_list('sensor_readings')").fetchall()
        # an automatic index backs the UNIQUE(timestamp, approach) constraint
        self.assertTrue(any(row["unique"] == 1 for row in indexes))

    def test_init_db_is_idempotent(self):
        init_db(self.repo.conn)  # must not raise on re-run
        init_db(self.repo.conn)


class ReadingTests(DatabaseTestCase):
    def test_insert_reading_stores_one(self):
        self.assertTrue(self.repo.insert_reading(make_reading()))
        self.assertEqual(self.repo.count_readings(), 1)

    def test_duplicate_reading_is_ignored(self):
        self.assertTrue(self.repo.insert_reading(make_reading()))
        self.assertFalse(self.repo.insert_reading(make_reading()))  # same ts+approach
        self.assertEqual(self.repo.count_readings(), 1)

    def test_different_approach_same_timestamp_is_new(self):
        self.assertTrue(self.repo.insert_reading(make_reading("North")))
        self.assertTrue(self.repo.insert_reading(make_reading("South")))
        self.assertEqual(self.repo.count_readings(), 2)

    def test_fetch_readings_history_order(self):
        self.repo.insert_reading(make_reading(timestamp="2026-10-12 03:00:00"))
        self.repo.insert_reading(make_reading(timestamp="2026-10-12 03:02:00"))
        self.repo.insert_reading(make_reading(timestamp="2026-10-12 03:01:00"))
        newest = self.repo.fetch_readings(limit=10)
        self.assertEqual(
            [r["timestamp"] for r in newest],
            ["2026-10-12 03:02:00", "2026-10-12 03:01:00", "2026-10-12 03:00:00"],
        )
        oldest = self.repo.fetch_readings(limit=1, newest_first=False)
        self.assertEqual(oldest[0]["timestamp"], "2026-10-12 03:00:00")

    def test_fetch_readings_filter_by_approach_and_since(self):
        self.repo.insert_reading(make_reading("East", "2026-10-12 03:00:00"))
        self.repo.insert_reading(make_reading("East", "2026-10-12 03:05:00"))
        self.repo.insert_reading(make_reading("West", "2026-10-12 03:05:00"))
        east = self.repo.fetch_readings(approach="East")
        self.assertEqual(len(east), 2)
        self.assertTrue(all(r["approach"] == "East" for r in east))
        recent = self.repo.fetch_readings(since="2026-10-12 03:04:00")
        self.assertEqual(len(recent), 2)

    def test_latest_per_approach(self):
        self.repo.insert_reading(make_reading("North", "2026-10-12 03:00:00", queue=1))
        self.repo.insert_reading(make_reading("North", "2026-10-12 03:10:00", queue=9))
        self.repo.insert_reading(make_reading("South", "2026-10-12 03:00:00", queue=4))
        latest = {r["approach"]: r["queue_length"] for r in self.repo.latest_per_approach()}
        self.assertEqual(latest, {"North": 9, "South": 4})

    def test_sequence_is_stored(self):
        self.repo.insert_reading(make_reading(), sequence=42)
        row = self.repo.fetch_readings()[0]
        self.assertEqual(row["sequence"], 42)


class PayloadTests(DatabaseTestCase):
    def test_valid_payload_inserted(self):
        self.assertTrue(self.repo.insert_payload(payload(), sequence=1))
        self.assertEqual(self.repo.count_readings(), 1)

    def test_payload_accepts_numeric_strings(self):
        reading = reading_from_payload(payload(queue_length="7"))
        self.assertEqual(reading.queue_length, 7)

    def test_duplicate_payload_ignored(self):
        self.assertTrue(self.repo.insert_payload(payload()))
        self.assertFalse(self.repo.insert_payload(payload()))
        self.assertEqual(self.repo.count_readings(), 1)

    def test_non_object_payload_rejected(self):
        for bad in ("not json", 5, [1, 2, 3], None):
            with self.assertRaises(InvalidReadingError):
                reading_from_payload(bad)

    def test_missing_field_rejected(self):
        data = payload()
        del data["queue_length"]
        with self.assertRaises(InvalidReadingError):
            reading_from_payload(data)

    def test_unknown_approach_rejected(self):
        with self.assertRaises(InvalidReadingError):
            reading_from_payload(payload(approach="Up"))

    def test_negative_number_rejected(self):
        with self.assertRaises(InvalidReadingError):
            reading_from_payload(payload(queue_length=-1))

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(InvalidReadingError):
            reading_from_payload(payload(timestamp="not-a-time"))

    def test_invalid_payload_stores_nothing(self):
        with self.assertRaises(InvalidReadingError):
            self.repo.insert_payload(payload(approach="Up"))
        self.assertEqual(self.repo.count_readings(), 0)


class OtherTableTests(DatabaseTestCase):
    def test_decision_event_and_command_inserts(self):
        self.assertTrue(
            self.repo.insert_decision(
                cycle_no=1, decision_time="2026-10-12 03:00:00", approach="North", decision_id="D0001"
            )
        )
        self.assertFalse(  # same cycle + approach is a duplicate
            self.repo.insert_decision(
                cycle_no=1, decision_time="2026-10-12 03:00:00", approach="North", decision_id="D0001"
            )
        )
        self.assertTrue(
            self.repo.insert_signal_event(
                cycle_no=1, approach="North", start_time="2026-10-12 03:00:00",
                signal_state="GREEN", duration_sec=25, event_id="E000001",
            )
        )
        self.assertTrue(
            self.repo.insert_actuator_command(
                approach="North", command="SET_GREEN", green_time_sec=25, cycle_no=1, command_id="C1"
            )
        )

    def test_system_event_logged(self):
        event_id = self.repo.log_system_event("error", "node-red", "db-error", "database locked")
        self.assertGreater(event_id, 0)
        self.assertEqual(self.repo.count_system_events(), 1)


class ConfigTests(unittest.TestCase):
    def test_database_path_from_settings(self):
        config = DatabaseConfig.from_settings()
        self.assertEqual(config.path, "data/traffic.db")
        self.assertEqual(config.resolved_path, PROJECT_ROOT / "data" / "traffic.db")

    def test_absolute_path_kept(self):
        config = DatabaseConfig(path=str(PROJECT_ROOT / "custom.db"))
        self.assertEqual(config.resolved_path, PROJECT_ROOT / "custom.db")


class ReplayIntegrationTests(DatabaseTestCase):
    def test_replay_bulk_insert_and_duplicate_rereplay(self):
        replay = TrafficReplay(DEFAULT_CSV_PATH, interval_seconds=0)
        replay.load()
        inserted = self.repo.insert_readings(replay.readings)
        self.assertEqual(inserted, len(replay))
        self.assertEqual(self.repo.count_readings(), len(replay))
        # replaying the same data again must not create duplicates
        self.assertEqual(self.repo.insert_readings(replay.readings), 0)
        self.assertEqual(self.repo.count_readings(), len(replay))


class FlowsJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flow = json.loads(FLOWS_PATH.read_text(encoding="utf-8"))
        cls.by_id = {node["id"]: node for node in cls.flow}

    def test_flows_json_is_valid_and_flat_list(self):
        self.assertIsInstance(self.flow, list)
        self.assertTrue(all(isinstance(n, dict) and "id" in n and "type" in n for n in self.flow))

    def test_has_tab_and_all_wires_resolve(self):
        self.assertTrue(any(n["type"] == "tab" for n in self.flow))
        ids = set(self.by_id)
        for node in self.flow:
            for output in node.get("wires", []):
                for target in output:
                    self.assertIn(target, ids, f"{node['id']} -> missing {target}")

    def test_subscribes_to_configured_sensor_topics(self):
        mqtt = next(n for n in self.flow if n["type"] == "mqtt in")
        self.assertEqual(mqtt["topic"], "team9/traffic/sensors/#")

    def test_sqlite_insert_targets_sensor_readings(self):
        inserts = [n for n in self.flow if n["type"] == "sqlite" and "sensor_readings" in n.get("sql", "")]
        self.assertEqual(len(inserts), 1)
        self.assertIn("INSERT OR IGNORE", inserts[0]["sql"])

    def test_has_malformed_and_error_handling(self):
        self.assertTrue(any(n["type"] == "catch" for n in self.flow))
        validate = next(n for n in self.flow if n.get("name") == "validate reading")
        self.assertEqual(validate["outputs"], 2)
        self.assertIn("invalid-json", validate["func"])

    def test_dashboard_and_mqtt_status_nodes_present(self):
        self.assertTrue(any(n["type"] == "status" for n in self.flow))
        self.assertTrue(any(n["type"] == "ui_text" for n in self.flow))
        self.assertTrue(any(n["type"] == "ui_chart" for n in self.flow))

    def test_no_node_id_equals_its_type(self):
        # Regression: a ui_base node whose id == its type ("ui_base") makes the
        # runtime's config-dependency check treat node["type"] as a self
        # reference and abort the deploy with
        # "Circular config node dependency detected: ui_base".
        collisions = [(n["id"], n["type"]) for n in self.flow if n["id"] == n["type"]]
        self.assertEqual(collisions, [])

    def test_single_ui_base_has_distinct_id(self):
        bases = [n for n in self.flow if n["type"] == "ui_base"]
        self.assertEqual(len(bases), 1)
        self.assertNotEqual(bases[0]["id"], "ui_base")

    def test_dashboard_board_is_template_wired_from_dash(self):
        boards = [n for n in self.flow if n["type"] == "ui_template"]
        self.assertTrue(boards, "expected a ui_template live board")
        board_ids = {n["id"] for n in boards}
        dash = self.by_id["fn_dash"]
        self.assertTrue(board_ids.intersection(dash["wires"][0]))

    def test_signal_state_and_timing_topics_subscribed(self):
        topics = {n.get("topic") for n in self.flow if n["type"] == "mqtt in"}
        self.assertIn("team9/traffic/signal/state", topics)
        self.assertIn("team9/traffic/timing/decisions", topics)
        self.assertIn("team9/traffic/test/result", topics)

    def test_testing_lab_publishes_to_test_input(self):
        outs = {n.get("topic") for n in self.flow if n["type"] == "mqtt out"}
        self.assertIn("team9/traffic/test/input", outs)
        lab = self.by_id["ui_lab"]
        self.assertIn("mqtt_out_test", lab["wires"][0])

    def test_signal_panel_is_fed_by_signal_state_parser(self):
        panel = self.by_id["ui_signal_panel"]
        panel_ids = {n["id"] for n in self.flow if n["type"] == "ui_template"}
        self.assertIn("ui_signal_panel", panel_ids)
        self.assertIn("ui_signal_panel", self.by_id["fn_signal_parse"]["wires"][0])
        self.assertEqual(panel["group"], "ui_group_phase")


class SharedConnectionThreadTests(unittest.TestCase):
    """The service writes readings from paho's network thread and events from
    the tick thread, so the repository connection must work across threads."""

    def test_write_from_another_thread_is_stored(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            repo = TrafficRepository.open(Path(tmp) / "threads.db")
            errors = []

            def worker():
                try:
                    repo.insert_reading(make_reading(timestamp="2026-10-12 05:00:00"))
                except Exception as exc:  # noqa: BLE001 - surface the real error
                    errors.append(exc)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(repo.count_readings(), 1)
            repo.close()


class ConcurrentWriterTests(unittest.TestCase):
    """Two connections to one database file must not raise SQLITE_BUSY when they
    write at the same instant. This is the Python service + Node-RED SQLite node
    contention the WAL mode / busy timeout fix addresses."""

    def test_connection_uses_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = TrafficRepository.open(Path(tmp) / "wal.db")
            try:
                mode = repo.conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
            finally:
                repo.close()

    def test_two_connections_write_concurrently_without_lock_errors(self):
        import sqlite3
        import threading

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "concurrent.db"
            repo = TrafficRepository.open(db_path)
            errors = []

            def python_writer():
                for i in range(150):
                    try:
                        repo.insert_reading(
                            make_reading(
                                approach="North",
                                timestamp=f"2026-10-12 06:00:{i:02d}",
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - surface the real error
                        errors.append(("python", repr(exc)))
                        return

            def node_red_writer():
                # Mimic the Node-RED SQLite node: its own connection (1 s timeout)
                # opened and used on its own thread.
                other = sqlite3.connect(str(db_path), timeout=1.0)
                try:
                    for i in range(150):
                        try:
                            with other:
                                other.execute(
                                    "INSERT OR IGNORE INTO sensor_readings "
                                    "(timestamp, approach, vehicle_count, queue_length, "
                                    "avg_wait_time, arrival_rate, congestion_level, scenario) "
                                    "VALUES (?,?,?,?,?,?,?,?)",
                                    (
                                        f"2026-10-12 07:00:{i:02d}", "East",
                                        5, 3, 20, 2, "LOW", "Rush hour",
                                    ),
                                )
                        except Exception as exc:  # noqa: BLE001 - surface the real error
                            errors.append(("node-red", repr(exc)))
                            return
                finally:
                    other.close()

            threads = [
                threading.Thread(target=python_writer),
                threading.Thread(target=node_red_writer),
            ]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
                self.assertEqual(repo.count_readings(), 300)
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
