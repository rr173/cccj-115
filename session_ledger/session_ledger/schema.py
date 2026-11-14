"""SQLite schema, initialisation and low-level connection handling.

Tables
------
meta
    Singleton key/value table holding the fixed idle gap ``G`` (milliseconds)
    and the schema version.

events
    Current revision of every known event identity, including retracted ones
    (``is_retracted = 1`` keeps the header so a newer ``put`` can restore it).
    Only non-retracted rows participate in segmentation.

sessions
    One row per maximal connected segment.  ``session_id`` is the
    lexicographically smallest ``event_id`` among the segment's members.

members
    Every *active* event's membership row, ordered by (user, business time,
    event_id) through an index that also serves interval and expansion queries.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import StoreError

SCHEMA_VERSION = 1
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    revision      INTEGER NOT NULL CHECK (revision > 0),
    user_id       TEXT,
    event_ms      INTEGER,
    is_retracted  INTEGER NOT NULL CHECK (is_retracted IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count > 0),
    span_ms     INTEGER NOT NULL CHECK (span_ms = end_ms - start_ms)
);

CREATE TABLE IF NOT EXISTS members (
    event_id   TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    event_ms   INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_range
    ON sessions(user_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS idx_members_user_time
    ON members(user_id, event_ms, event_id);
CREATE INDEX IF NOT EXISTS idx_members_session
    ON members(session_id, event_ms, event_id);
CREATE INDEX IF NOT EXISTS idx_events_user_time
    ON events(user_id, event_ms, event_id) WHERE is_retracted = 0;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database in WAL mode with foreign keys enforced.

    The connection runs in autocommit mode (``isolation_level=None``) so the
    maintenance code owns the transaction boundaries explicitly; read-only
    callers are unaffected.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialise(db_path: str | Path, idle_gap_ms: int) -> None:
    """Create a fresh store with a fixed idle gap ``G``.

    ``idle_gap_ms`` must be a positive integer (booleans are rejected because
    they are not integers in the domain sense).  Re-initialising an existing
    store with a different gap is refused; the gap is fixed for the life of
    the database.
    """
    if isinstance(idle_gap_ms, bool) or not isinstance(idle_gap_ms, int):
        raise StoreError("idle_gap_ms must be an integer")
    if idle_gap_ms <= 0:
        raise StoreError("idle_gap_ms must be a positive integer (milliseconds)")

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(_DDL)
        row = conn.execute("SELECT value FROM meta WHERE key = 'idle_gap_ms'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('idle_gap_ms', ?)",
                (str(idle_gap_ms),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        elif int(row[0]) != idle_gap_ms:
            raise StoreError(
                f"store already initialised with idle_gap_ms={row[0]}; "
                f"refusing to change it to {idle_gap_ms}"
            )
    finally:
        conn.close()


def get_idle_gap(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'idle_gap_ms'"
    ).fetchone()
    if row is None:
        raise StoreError("store is not initialised (no idle gap in meta)")
    return int(row[0])
