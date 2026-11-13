"""SQLite schema, connection management and on-disk layout.

The library lives in a single SQLite database file (plus its ``-wal`` and
``-shm`` sidecars while open).  Point coordinates are stored as their raw
signed 32-bit integers together with the exact decimal text of the source
scale/offset, so every spatial comparison can be re-done exactly.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .errors import LibraryError

APPLICATION_ID = 0x50434C41  # "PCLA"
SCHEMA_VERSION = 1
META_APP_ID = "application_id"
META_SCHEMA = "schema_version"
META_RTREE = "rtree_available"
META_REF_POLICY = "reference_policy"  # "none" | "strict"
META_REF_DESC = "reference_description"  # NULL until first referenced source

_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sources (
    src_id              INTEGER PRIMARY KEY,
    source_sha256       TEXT NOT NULL UNIQUE,
    original_path       TEXT NOT NULL,
    point_format        INTEGER NOT NULL,
    record_length       INTEGER NOT NULL,
    extra_bytes_len     INTEGER NOT NULL,
    n_points            INTEGER NOT NULL,
    scale_x             TEXT NOT NULL,
    scale_y             TEXT NOT NULL,
    scale_z             TEXT NOT NULL,
    offset_x            TEXT NOT NULL,
    offset_y            TEXT NOT NULL,
    offset_z            TEXT NOT NULL,
    has_gps             INTEGER NOT NULL CHECK (has_gps IN (0, 1)),
    has_rgb             INTEGER NOT NULL CHECK (has_rgb IN (0, 1)),
    reference_desc      TEXT,
    -- Bounds as raw integer indices actually read from the point records:
    x_int_min           INTEGER,
    x_int_max           INTEGER,
    y_int_min           INTEGER,
    y_int_max           INTEGER,
    z_int_min           INTEGER,
    z_int_max           INTEGER,
    -- Bounds the header declared (physical doubles), and the physical bounds
    -- recomputed from the actually read integer points:
    declared_x_min      REAL,
    declared_x_max      REAL,
    declared_y_min      REAL,
    declared_y_max      REAL,
    declared_z_min      REAL,
    declared_z_max      REAL,
    actual_x_min        TEXT,
    actual_x_max        TEXT,
    actual_y_min        TEXT,
    actual_y_max        TEXT,
    actual_z_min        TEXT,
    actual_z_max        TEXT,
    file_size_bytes     INTEGER NOT NULL,
    ingested_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE points (
    src_id          INTEGER NOT NULL REFERENCES sources(src_id),
    point_index     INTEGER NOT NULL,
    x               INTEGER NOT NULL,
    y               INTEGER NOT NULL,
    z               INTEGER NOT NULL,
    intensity       INTEGER NOT NULL,
    classification  INTEGER NOT NULL,
    class_flags     INTEGER NOT NULL,
    return_number   INTEGER NOT NULL,
    number_of_returns INTEGER NOT NULL,
    gps_time        REAL,
    red             INTEGER,
    green           INTEGER,
    blue            INTEGER,
    extra_bytes     BLOB NOT NULL,
    PRIMARY KEY (src_id, point_index)
);

-- The primary key automatically provides an index on (src_id, point_index).
-- Classification filtering per source:
CREATE INDEX idx_points_src_class ON points(src_id, classification);
"""

_RTREE_SQL = """
CREATE VIRTUAL TABLE points_rtree USING rtree(
    id,
    min_x, max_x,
    min_y, max_y,
    min_z, max_z
);
"""


def _rtree_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _rtree_probe USING rtree("
            "id, min_x, max_x, min_y, max_y, min_z, max_z)"
        )
        conn.execute("DROP TABLE _rtree_probe")
        return True
    except sqlite3.DatabaseError:
        return False


def create_library(path: str, reference_policy: str) -> None:
    """Create a new empty library database.

    ``reference_policy`` is either ``"none"`` (an explicitly unreferenced
    library that only accepts files without a coordinate reference
    description) or ``"strict"`` (every file must carry the same reference
    description).
    """
    if reference_policy not in ("none", "strict"):
        raise LibraryError(f"unknown reference policy: {reference_policy!r}")
    if os.path.exists(path):
        raise LibraryError(f"library already exists: {path}")
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA_SQL)
        has_rtree = _rtree_available(conn)
        if has_rtree:
            conn.executescript(_RTREE_SQL)
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                (META_APP_ID, str(APPLICATION_ID)),
                (META_SCHEMA, str(SCHEMA_VERSION)),
                (META_RTREE, "1" if has_rtree else "0"),
                (META_REF_POLICY, reference_policy),
            ],
        )
        conn.commit()
    except BaseException:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
        raise
    conn.close()


def connect(path: str) -> sqlite3.Connection:
    """Open an existing library and verify its identity."""
    if not os.path.exists(path):
        raise LibraryError(f"library does not exist: {path}")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise LibraryError(f"not a point-cloud library database: {path}") from exc
    if app_id != APPLICATION_ID:
        conn.close()
        raise LibraryError(f"not a point-cloud library database: {path}")
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise LibraryError(f"library metadata is corrupted: {path}") from exc
    if int(rows.get(META_APP_ID, "0")) != APPLICATION_ID:
        conn.close()
        raise LibraryError(f"library metadata mismatch: {path}")
    return conn


@contextmanager
def open_library(path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def library_stats(conn: sqlite3.Connection) -> dict:
    """Cheap on-disk footprint summary (see docs/space_overhead.rst)."""
    n_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    n_points = conn.execute("SELECT COALESCE(SUM(n_points), 0) FROM sources").fetchone()[0]
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    npages = conn.execute("PRAGMA page_count").fetchone()[0]
    return {
        "n_sources": n_sources,
        "n_points": n_points,
        "page_size_bytes": page,
        "page_count": npages,
        "database_bytes": page * npages,
    }
