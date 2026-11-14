"""Read-only query API over the maintained facts.

Every query reads persisted rows directly; nothing is re-segmented at query
time.  Sessions are intersected against closed time intervals, so a segment
that merely overlaps the window (it starts before and ends inside it) is
returned as well.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .schema import connect, get_idle_gap


@dataclass(frozen=True)
class Session:
    user_id: str
    session_id: str
    start_ms: int
    end_ms: int
    event_count: int
    span_ms: int


@dataclass(frozen=True)
class EventRow:
    event_id: str
    revision: int
    user_id: str
    event_ms: int
    session_id: str
    is_retracted: bool


def idle_gap(db_path: str | Path) -> int:
    """Return the fixed idle gap G of the store."""
    conn = connect(db_path)
    try:
        return get_idle_gap(conn)
    finally:
        conn.close()


def sessions_in_interval(
    db_path: str | Path,
    user_id: str,
    start_ms: int,
    end_ms: int,
) -> list[Session]:
    """Sessions of ``user_id`` intersecting the closed interval.

    Intersection means ``session.start_ms <= end_ms AND session.end_ms >=
    start_ms`` -- sessions starting before the window are included.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT user_id, session_id, start_ms, end_ms, event_count, span_ms "
            "FROM sessions WHERE user_id = ? AND start_ms <= ? AND end_ms >= ? "
            "ORDER BY start_ms, session_id",
            (user_id, end_ms, start_ms),
        ).fetchall()
        return [Session(*row) for row in rows]
    finally:
        conn.close()


def session_events(
    db_path: str | Path, session_id: str
) -> list[EventRow]:
    """All active events of one session in business-time order."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT e.event_id, e.revision, m.user_id, m.event_ms, "
            "m.session_id, e.is_retracted "
            "FROM members m JOIN events e ON e.event_id = m.event_id "
            "WHERE m.session_id = ? ORDER BY m.event_ms, m.event_id",
            (session_id,),
        ).fetchall()
        return [
            EventRow(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows
        ]
    finally:
        conn.close()


def events_in_interval(
    db_path: str | Path,
    user_id: str,
    start_ms: int,
    end_ms: int,
) -> list[EventRow]:
    """Active events of a user inside a closed interval, time ordered."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT e.event_id, e.revision, m.user_id, m.event_ms, "
            "m.session_id, e.is_retracted "
            "FROM members m JOIN events e ON e.event_id = m.event_id "
            "WHERE m.user_id = ? AND m.event_ms BETWEEN ? AND ? "
            "ORDER BY m.event_ms, m.event_id",
            (user_id, start_ms, end_ms),
        ).fetchall()
        return [
            EventRow(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows
        ]
    finally:
        conn.close()


def all_sessions(db_path: str | Path) -> list[Session]:
    """Full session snapshot, ordered by user and business time."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT user_id, session_id, start_ms, end_ms, event_count, span_ms "
            "FROM sessions ORDER BY user_id, start_ms, session_id"
        ).fetchall()
        return [Session(*row) for row in rows]
    finally:
        conn.close()


def event_header(db_path: str | Path, event_id: str) -> EventRow | None:
    """Current header of one identity, including retracted headers."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT event_id, revision, user_id, event_ms, NULL, is_retracted "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return EventRow(row[0], row[1], row[2], row[3], row[4], bool(row[5]))
    finally:
        conn.close()


def counts(db_path: str | Path) -> dict[str, int]:
    """Snapshot row counts for tests and audits."""
    conn = connect(db_path)
    try:
        out: dict[str, int] = {}
        for table in ("events", "sessions", "members"):
            out[table] = conn.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
        out["retracted_events"] = conn.execute(
            "SELECT count(*) FROM events WHERE is_retracted = 1"
        ).fetchone()[0]
        return out
    finally:
        conn.close()


def explain_index_usage(
    db_path: str | Path, sql: str, params: tuple[object, ...]
) -> list[sqlite3.Row]:
    """EXPLAIN QUERY PLAN helper used by the locality benchmark."""
    conn = connect(db_path)
    try:
        return conn.execute(
            "EXPLAIN QUERY PLAN " + sql, params
        ).fetchall()
    finally:
        conn.close()
