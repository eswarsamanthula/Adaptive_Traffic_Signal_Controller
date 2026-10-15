-- SQLite schema for the Adaptive Traffic Signal Controller.
--
-- Idempotent: every statement uses IF NOT EXISTS, so it can run on each startup.
-- Approach identifiers use the canonical names (North/South/East/West).
-- Cycle identifiers use cycle_no where a decision/event belongs to a cycle.

-- Sensor readings published by the simulator over MQTT.
-- (timestamp, approach) is the natural key: the dataset has 5000 unique pairs,
-- so it doubles as the duplicate guard when a replay is published twice.
CREATE TABLE IF NOT EXISTS sensor_readings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    approach         TEXT    NOT NULL,
    vehicle_count    INTEGER NOT NULL,
    queue_length     INTEGER NOT NULL,
    avg_wait_time    INTEGER NOT NULL,
    arrival_rate     INTEGER NOT NULL,
    congestion_level TEXT    NOT NULL,
    scenario         TEXT    NOT NULL,
    sequence         INTEGER,
    received_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (timestamp, approach)
);

CREATE INDEX IF NOT EXISTS idx_sensor_readings_approach_time
    ON sensor_readings (approach, timestamp);

-- Signal decisions (one row per approach per decision cycle).
CREATE TABLE IF NOT EXISTS signal_decisions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id            TEXT,
    cycle_no               INTEGER NOT NULL,
    decision_time          TEXT    NOT NULL,
    approach               TEXT    NOT NULL,
    traffic_timestamp_used TEXT,
    queue_length           INTEGER,
    arrival_rate           INTEGER,
    previous_green_time    INTEGER,
    requested_green_time   INTEGER,
    applied_green_time     INTEGER,
    cycle_length_sec       INTEGER,
    decision_reason        TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cycle_no, approach)
);

-- Signal phase events (GREEN/YELLOW/RED transitions).
CREATE TABLE IF NOT EXISTS signal_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    UNIQUE,
    cycle_no     INTEGER NOT NULL,
    approach     TEXT    NOT NULL,
    start_time   TEXT    NOT NULL,
    signal_state TEXT    NOT NULL,
    duration_sec INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Commands sent to the (simulated) signal actuator.
CREATE TABLE IF NOT EXISTS actuator_commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id    TEXT    UNIQUE,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    approach      TEXT    NOT NULL,
    command       TEXT    NOT NULL,
    green_time_sec INTEGER,
    cycle_no      INTEGER,
    status        TEXT    NOT NULL DEFAULT 'pending',
    detail        TEXT,
    reason        TEXT,
    ack_at        TEXT,
    applied_at    TEXT
);

-- Operational/system events: errors, warnings and status messages.
CREATE TABLE IF NOT EXISTS system_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    level      TEXT NOT NULL,
    source     TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message    TEXT
);
