"""Persisted event/session maintenance with local neighbourhood updates.

The engine never re-segments a whole user, let alone the whole database.  For
each patch it:

1. validates every revision decision against stored headers *before writing
   anything*, so a stale/conflicting/unknown-retract line aborts the whole
   patch with no table touched;
2. writes event headers (inserts, replacements, retractions) in one
   ``BEGIN IMMEDIATE`` transaction;
3. for every affected user, locates the old sessions overlapping the changed
   business times, expands that neighbourhood only while adjacent sessions
   sit within the idle gap ``G``, streams the active events of that range
   through a cursor and re-segments just them;
4. verifies the two outer neighbours, then commits.

A crash (or the deliberate :func:`os._exit` test hook) between steps 2 and 4
rolls the transaction back: reopening the database shows the pre-patch state
and the same patch can be ingested again.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    PatchError,
    RevisionConflictError,
    StaleRevisionError,
    StoreError,
)
from .patch import ParsedOp
from .schema import INT64_MAX, INT64_MIN, connect, get_idle_gap

#: Rows per cursor fetch / per executemany batch.  Memory is therefore bounded
#: by this constant, not by the size of the rebuilt segment.
BATCH = 512

#: Exit code used by the deliberate os._exit test hook.
EXIT_POINT_CODE = 42


@dataclass
class UserStats:
    """Read accounting for one affected user."""

    anchors: int = 0
    resegment_rows_read: int = 0
    boundary_probes: int = 0
    index_lookups: int = 0
    sessions_removed: int = 0
    sessions_created: int = 0


@dataclass
class IngestStats:
    """Auditable counters for one patch ingestion.

    Counter meanings (database reads, not just final writes):

    effective_changes
        patch lines that actually changed stored state (new insert, higher
        revision replacement, higher revision retraction or restore).
    noops
        lines equal to the stored revision with identical content.
    resegment_rows_read
        active event rows streamed through cursors while re-segmenting
        affected neighbourhoods -- the full historical rows actually read,
        including events that end up unchanged in the rebuilt sessions.
    boundary_probes
        queries that probe a session/event immediately outside the current
        neighbourhood to decide whether expansion continues or stops, plus
        the two final outer-neighbour verifications per affected user.
    index_lookups
        every indexed point/range lookup used to locate headers, overlapping
        sessions and neighbours (this includes the boundary probes).
    sessions_before / sessions_after
        total session rows in the database immediately before and after the
        patch.
    """

    effective_changes: int = 0
    noops: int = 0
    resegment_rows_read: int = 0
    boundary_probes: int = 0
    index_lookups: int = 0
    sessions_before: int = 0
    sessions_after: int = 0
    per_user: dict[str, UserStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "effective_changes": self.effective_changes,
            "noops": self.noops,
            "resegment_rows_read": self.resegment_rows_read,
            "boundary_probes": self.boundary_probes,
            "index_lookups": self.index_lookups,
            "sessions_before": self.sessions_before,
            "sessions_after": self.sessions_after,
            "per_user": {u: vars(s) for u, s in sorted(self.per_user.items())},
        }


@dataclass(frozen=True)
class _Header:
    revision: int
    user_id: str | None
    event_ms: int | None
    is_retracted: bool


def _ensure_staging(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _seg_staging ("
        "event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, event_ms INTEGER NOT NULL)"
    )
    conn.execute("DELETE FROM _seg_staging")


def ingest(
    db_path: str | Path,
    ops: list[ParsedOp],
    *,
    exit_after_event_writes: bool = False,
) -> IngestStats:
    """Apply one validated patch atomically and return processing stats."""
    conn = connect(db_path)
    stats = IngestStats()
    try:
        gap = get_idle_gap(conn)
        _ensure_staging(conn)

        # ---- revision decisions, all reads, before any write -------------
        decisions: list[tuple[ParsedOp, _Header | None]] = []
        for op in ops:
            row = conn.execute(
                "SELECT revision, user_id, event_ms, is_retracted "
                "FROM events WHERE event_id = ?",
                (op.event_id,),
            ).fetchone()
            stats.index_lookups += 1
            stored = (
                _Header(row[0], row[1], row[2], bool(row[3]))
                if row is not None
                else None
            )
            _decide(op, stored)
            decisions.append((op, stored))

        stats.sessions_before = conn.execute(
            "SELECT count(*) FROM sessions"
        ).fetchone()[0]

        # ---- single transaction for every mutation -----------------------
        conn.execute("BEGIN IMMEDIATE")
        anchors: dict[str, list[int]] = {}
        displaced: list[str] = []
        for op, stored in decisions:
            if _is_noop(op, stored):
                stats.noops += 1
                continue
            stats.effective_changes += 1
            if stored is not None and not stored.is_retracted:
                anchors.setdefault(stored.user_id, []).append(stored.event_ms)
                # A moved or retracted event leaves its old membership behind;
                # its old session can lie far outside any time-based rebuild
                # window, so remove the row by identity here rather than
                # relying on a session cascade.  A moved event also vacates its
                # old session's name space.
                if op.op == "retract" or (
                    op.user_id != stored.user_id or op.event_ms != stored.event_ms
                ):
                    displaced.append(op.event_id)
            _write_header(conn, op)
            if op.op == "put":
                anchors.setdefault(op.user_id, []).append(op.event_ms)

        for i in range(0, len(displaced), BATCH):
            chunk = displaced[i : i + BATCH]
            conn.execute(
                f"DELETE FROM members WHERE event_id IN "
                f"({','.join('?' for _ in chunk)})",
                chunk,
            )

        if exit_after_event_writes:
            # Deliberate crash point: headers are changed inside the open
            # transaction, session maintenance has not run.  No commit means
            # a reopened database still shows the complete pre-patch state.
            os._exit(EXIT_POINT_CODE)

        # Users are processed in sorted order for determinism.  For a
        # cross-user move, the event's old and new users are both in
        # ``anchors``; the source user's rebuild deletes its old session (and
        # cascades the membership) before the destination user's rebuild may
        # reuse the same event-derived session_id.
        for user_id in sorted(anchors):
            user_anchors = anchors[user_id]
            ustats = stats.per_user.setdefault(user_id, UserStats())
            ustats.anchors = len(user_anchors)
            _rebuild_neighbourhood(conn, user_id, user_anchors, gap, stats, ustats)

        # Roll per-user read accounting into the auditable patch-level totals.
        stats.resegment_rows_read = sum(
            u.resegment_rows_read for u in stats.per_user.values()
        )
        stats.boundary_probes = sum(
            u.boundary_probes for u in stats.per_user.values()
        )
        stats.index_lookups += sum(
            u.index_lookups for u in stats.per_user.values()
        )

        stats.sessions_after = conn.execute(
            "SELECT count(*) FROM sessions"
        ).fetchone()[0]
        conn.execute("DELETE FROM _seg_staging")
        conn.commit()
        return stats
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decide(op: ParsedOp, stored: _Header | None) -> None:
    if stored is None:
        if op.op == "retract":
            raise PatchError(
                f"cannot retract unknown event_id {op.event_id!r}", op.line
            )
        return
    if op.revision < stored.revision:
        raise StaleRevisionError(
            op.event_id, op.revision, stored.revision, op.line
        )
    if op.revision == stored.revision and not _same_payload(op, stored):
        raise RevisionConflictError(op.event_id, op.revision, op.line)


def _same_payload(op: ParsedOp, stored: _Header) -> bool:
    if op.op == "retract":
        return stored.is_retracted
    return (
        not stored.is_retracted
        and stored.user_id == op.user_id
        and stored.event_ms == op.event_ms
    )


def _is_noop(op: ParsedOp, stored: _Header | None) -> bool:
    if stored is None:
        return False
    if op.revision != stored.revision:
        return False
    return _same_payload(op, stored)


def _write_header(conn: sqlite3.Connection, op: ParsedOp) -> None:
    if op.op == "retract":
        conn.execute(
            "INSERT INTO events(event_id, revision, user_id, event_ms, "
            "is_retracted) VALUES (?, ?, NULL, NULL, 1) "
            "ON CONFLICT(event_id) DO UPDATE SET "
            "revision=excluded.revision, user_id=NULL, event_ms=NULL, "
            "is_retracted=1",
            (op.event_id, op.revision),
        )
    else:
        conn.execute(
            "INSERT INTO events(event_id, revision, user_id, event_ms, "
            "is_retracted) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(event_id) DO UPDATE SET "
            "revision=excluded.revision, user_id=excluded.user_id, "
            "event_ms=excluded.event_ms, is_retracted=0",
            (op.event_id, op.revision, op.user_id, op.event_ms),
        )


# ---------------------------------------------------------------------------
# Neighbourhood maintenance
# ---------------------------------------------------------------------------


def _rebuild_neighbourhood(
    conn: sqlite3.Connection,
    user_id: str,
    user_anchors: list[int],
    gap: int,
    stats: IngestStats,
    ustats: UserStats,
) -> None:
    """Re-segment exactly the affected neighbourhood of one user.

    Closure argument (why this is the right and complete neighbourhood):

    1. Seed window ``[min(anchors)-G, max(anchors)+G]``.  Every active event
       outside it is more than G away from every anchor, hence every edge
       incident to it keeps its old status.
    2. Take every old session intersecting the window.  A session is one
       connected component, so its membership can only change as a whole: all
       of its members enter the rebuild set and the window extends to the
       session's full extent (without adding another G margin here -- the
       margin belongs to seed/intersection tests, not to component extents).
    3. The extended window may intersect further sessions (this is how one
       batch can jointly bridge a whole chain, and how a bridge deletion can
       split into fragments that still border neighbours).  Iterate until no
       new session intersects.
    4. Termination leaves two outer neighbour sessions whose gaps to the
       window are strictly greater than G; the explicit probes below assert
       that, guaranteeing untouched sessions stay maximal.
    """
    lo = max(INT64_MIN, min(user_anchors) - gap)
    hi = min(INT64_MAX, max(user_anchors) + gap)
    affected: set[str] = set()

    while True:
        rows = conn.execute(
            "SELECT session_id, start_ms, end_ms FROM sessions "
            "WHERE user_id = ? AND start_ms <= ? AND end_ms >= ?",
            (user_id, hi, lo),
        ).fetchall()
        ustats.index_lookups += 1
        grew = False
        for session_id, start_ms, end_ms in rows:
            if session_id in affected:
                continue
            affected.add(session_id)
            grew = True
            lo = min(lo, start_ms)
            hi = max(hi, end_ms)
        if not grew:
            break

    if affected:
        # Chunked deletes; ON DELETE CASCADE removes membership rows of the
        # deleted sessions, including rows for events whose headers have just
        # moved to another user in this same patch.
        ids = list(affected)
        ustats.sessions_removed = len(ids)
        for i in range(0, len(ids), BATCH):
            chunk = ids[i : i + BATCH]
            conn.execute(
                f"DELETE FROM sessions WHERE session_id IN "
                f"({','.join('?' for _ in chunk)})",
                chunk,
            )

    # The staging table is connection-scoped and shared across the users of
    # one patch; clear it before this user's streaming rebuild so no foreign
    # rows leak into the set-based materialisation below.
    conn.execute("DELETE FROM _seg_staging")

    # Stream the active events of the neighbourhood through a cursor.  This is
    # the counted historical read: rows are fetched in BATCH-sized lists, so
    # even a giant connected segment never materialises in Python memory.
    cursor = conn.execute(
        "SELECT event_id, event_ms FROM events "
        "WHERE user_id = ? AND is_retracted = 0 "
        "AND event_ms BETWEEN ? AND ? "
        "ORDER BY event_ms, event_id",
        (user_id, lo, hi),
    )
    ustats.index_lookups += 1
    _stream_resegment(conn, cursor, user_id, gap, ustats)

    # Final verification of the two outer neighbours against the freshly
    # written active events.  These probes must confirm gaps strictly greater
    # than G; otherwise the expansion stop condition was wrong.
    _verify_outer_edges(conn, user_id, lo, hi, gap, stats, ustats)


def _stream_resegment(
    conn: sqlite3.Connection,
    cursor: sqlite3.Cursor,
    user_id: str,
    gap: int,
    ustats: UserStats,
) -> None:
    """Fetch event rows in batches and split them into maximal segments.

    Only the current run (session) is held in memory; finished runs are pushed
    to the staging table in batches.  Same-timestamp events stay in insertion
    order (event_id tie-break) and never get deduplicated.

    ``session_id`` is globally unique (it is an event id, and event ids are
    unique across users).  If a freshly computed name already belongs to a
    session of *another* user -- e.g. an event moved across users -- that
    foreign row must be removed first; its own events are rebuilt when (or
    already were rebuilt when) that user's neighbourhood is processed in the
    same patch transaction.
    """
    pending: list[tuple[str, str, str, int]] = []
    claimed_names: set[str] = set()

    def flush(run: list[tuple[str, str, int]]) -> None:
        if not run:
            return
        session_id = min(event_id for event_id, _ in run)
        claimed_names.add(session_id)
        ustats.sessions_created += 1
        for event_id, event_ms in run:
            pending.append((event_id, session_id, user_id, event_ms))
            if len(pending) >= BATCH:
                conn.executemany(
                    "INSERT INTO _seg_staging VALUES (?, ?, ?, ?)", pending
                )
                pending.clear()

    run: list[tuple[str, str, int]] = []
    last_ms: int | None = None
    while True:
        chunk = cursor.fetchmany(BATCH)
        if not chunk:
            break
        for event_id, event_ms in chunk:
            ustats.resegment_rows_read += 1
            if run and event_ms - last_ms > gap:
                flush(run)
                run = []
            run.append((event_id, event_ms))
            last_ms = event_ms
    flush(run)
    if pending:
        conn.executemany(
            "INSERT INTO _seg_staging VALUES (?, ?, ?, ?)", pending
        )

    if claimed_names:
        # Remove foreign-user rows carrying a name we are about to reuse.
        # Membership rows are deleted explicitly: ON DELETE CASCADE cannot run
        # because the moving event's membership is about to be re-inserted for
        # this user, and the source user's rebuild already re-homed its other
        # members.
        placeholders = ",".join("?" for _ in claimed_names)
        foreign = conn.execute(
            f"SELECT session_id FROM sessions "
            f"WHERE session_id IN ({placeholders}) AND user_id <> ?",
            (*claimed_names, user_id),
        ).fetchall()
        ustats.index_lookups += 1
        for (session_id,) in foreign:
            conn.execute(
                "DELETE FROM members WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ? AND user_id <> ?",
                (session_id, user_id),
            )

    # Materialise sessions and memberships from the staged runs with set-based
    # SQL so no full segment is held in Python.
    conn.execute(
        "INSERT INTO sessions(session_id, user_id, start_ms, end_ms, "
        "event_count, span_ms) "
        "SELECT session_id, ?, min(event_ms), max(event_ms), count(*), "
        "max(event_ms) - min(event_ms) FROM _seg_staging WHERE user_id = ? "
        "GROUP BY session_id",
        (user_id, user_id),
    )
    conn.execute(
        "INSERT INTO members(event_id, session_id, user_id, event_ms) "
        "SELECT event_id, session_id, user_id, event_ms FROM _seg_staging "
        "WHERE user_id = ?",
        (user_id,),
    )
    conn.execute("DELETE FROM _seg_staging WHERE user_id = ?", (user_id,))


def _verify_outer_edges(
    conn: sqlite3.Connection,
    user_id: str,
    lo: int,
    hi: int,
    gap: int,
    stats: IngestStats,
    ustats: UserStats,
) -> None:
    left = conn.execute(
        "SELECT event_ms FROM events "
        "WHERE user_id = ? AND is_retracted = 0 AND event_ms < ? "
        "ORDER BY event_ms DESC, event_id DESC LIMIT 1",
        (user_id, lo),
    ).fetchone()
    right = conn.execute(
        "SELECT event_ms FROM events "
        "WHERE user_id = ? AND is_retracted = 0 AND event_ms > ? "
        "ORDER BY event_ms ASC, event_id ASC LIMIT 1",
        (user_id, hi),
    ).fetchone()
    ustats.boundary_probes += 2
    ustats.index_lookups += 2

    first = conn.execute(
        "SELECT min(event_ms) FROM events "
        "WHERE user_id = ? AND is_retracted = 0 AND event_ms BETWEEN ? AND ?",
        (user_id, lo, hi),
    ).fetchone()[0]
    last = conn.execute(
        "SELECT max(event_ms) FROM events "
        "WHERE user_id = ? AND is_retracted = 0 AND event_ms BETWEEN ? AND ?",
        (user_id, lo, hi),
    ).fetchone()[0]

    if left is not None and first is not None and first - left[0] <= gap:
        raise StoreError(
            f"neighbourhood invariant violated on the left of user {user_id!r}"
        )
    if right is not None and last is not None and right[0] - last <= gap:
        raise StoreError(
            f"neighbourhood invariant violated on the right of user {user_id!r}"
        )
