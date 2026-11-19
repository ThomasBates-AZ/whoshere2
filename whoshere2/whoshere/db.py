"""SQLite storage. One small file in the /data volume."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

# Columns added after the first release: (table, column, type). Added to existing databases on start.
ADDED_COLUMNS = [
    ("devices", "last_ssid", "TEXT"),
    ("presence", "ssid", "TEXT"),
    ("events", "ssid", "TEXT"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

-- Every client UniFi has shown us. person_id set = this device is tracked for that person.
CREATE TABLE IF NOT EXISTS devices (
    mac         TEXT PRIMARY KEY,
    person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    client_name TEXT,
    first_seen  REAL,
    last_seen   REAL,
    last_uplink TEXT,
    connected   INTEGER NOT NULL DEFAULT 0,
    ignored     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS devices_person ON devices(person_id);

-- Access points and switches; their names are what the board shows as "where".
CREATE TABLE IF NOT EXISTS locations (
    mac       TEXT PRIMARY KEY,
    name      TEXT,
    alias     TEXT,
    kind      TEXT,
    last_seen REAL
);

CREATE TABLE IF NOT EXISTS presence (
    person_id     INTEGER PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    state         TEXT NOT NULL CHECK (state IN ('here', 'away')),
    since         REAL,
    last_seen     REAL,
    uplink        TEXT,
    missing_since REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('arrive', 'depart')),
    at         REAL NOT NULL,
    uplink     TEXT,
    device_mac TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE INDEX IF NOT EXISTS events_person ON events(person_id, at);

CREATE TABLE IF NOT EXISTS poll_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_attempt REAL,
    last_success REAL,
    last_error   TEXT,
    client_count INTEGER
);
INSERT OR IGNORE INTO poll_status (id) VALUES (1);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, kind in ADDED_COLUMNS:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Small helpers so callers don't need a connection for one-off queries.
    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple | dict = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid if cur.lastrowid else cur.rowcount
