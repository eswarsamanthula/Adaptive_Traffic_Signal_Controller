"""SQLite connection handling for the traffic database.

The database path is read from ``config/settings.yaml`` under ``database.path``
(relative paths are resolved against the project root). ``schema.sql`` lives
next to this module and is applied idempotently.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "traffic.db"

#: Seconds a writer waits for a competing SQLite writer before raising. Passed
#: as ``sqlite3.connect``'s ``timeout``, which sets SQLite's ``busy_timeout``.
#: The Node-RED SQLite node defaults to 1 s, so both writers wait rather than
#: raising ``SQLITE_BUSY`` on a brief overlap.
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0


class DatabaseError(RuntimeError):
    """Raised for database configuration or connection problems."""


@dataclass
class DatabaseConfig:
    path: str = str(DEFAULT_DB_PATH)

    @classmethod
    def from_dict(cls, data: Optional[dict] = None) -> "DatabaseConfig":
        return cls(path=str((data or {}).get("path", DEFAULT_DB_PATH)))

    @classmethod
    def from_settings(cls, settings_path: Optional[Path] = None) -> "DatabaseConfig":
        path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        if not path.exists():
            raise DatabaseError(f"settings not found: {path}")
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls.from_dict(data.get("database", {}))

    @property
    def resolved_path(self) -> Path:
        candidate = Path(self.path)
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (creating the parent directory) a SQLite connection with dict rows.

    The connection uses WAL mode and an explicit busy timeout so the Python
    service and the Node-RED SQLite node -- two separate connections to the same
    file -- do not fail with ``SQLITE_BUSY`` when they write at the same instant.

    WAL is a persistent database property, so the Node-RED connection inherits
    it without any flow change; it also removes the rollback-journal lock paths
    that return ``SQLITE_BUSY`` even when a busy timeout is set. The busy timeout
    makes any remaining writer-vs-writer overlap wait instead of raising.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseError(f"cannot create database directory {path.parent}: {exc}") from exc
    conn = sqlite3.connect(
        str(path), check_same_thread=False, timeout=DEFAULT_BUSY_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Best-effort switch to WAL mode (a non-file database such as ``:memory:``
    cannot use it). WAL persists in the database file, so every later connection
    -- including Node-RED's -- uses it too."""
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
    except sqlite3.Error as exc:
        logger.warning("could not enable WAL mode: %s", exc)


def init_db(conn: sqlite3.Connection, schema_path: Optional[Path] = None) -> None:
    """Apply schema.sql to the connection (safe to call repeatedly)."""
    schema = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
    if not schema.exists():
        raise DatabaseError(f"schema not found: {schema}")
    conn.executescript(schema.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` never
#: alters an existing table, so we add missing columns here instead of dropping
#: and recreating it (which would lose data).
_MIGRATIONS = {
    "actuator_commands": {"reason": "TEXT", "ack_at": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def create_database(db_path: Optional[Path] = None) -> Path:
    """Create/initialise the database file and return its path."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = connect(path)
    try:
        init_db(conn)
    finally:
        conn.close()
    return path


def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Create/initialise the traffic SQLite database.")
    parser.add_argument("--settings", default=None, help="path to settings.yaml")
    args = parser.parse_args(argv)

    config = DatabaseConfig.from_settings(args.settings)
    path = create_database(config.resolved_path)
    conn = connect(path)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
    finally:
        conn.close()
    print(f"database ready: {path}")
    print(f"tables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
