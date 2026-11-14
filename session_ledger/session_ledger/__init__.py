"""session_ledger: business-time sessions over corrected product events.

The package persists three facts in SQLite:

* current event revisions (including retracted headers);
* maximal connected sessions per user for a fixed idle gap G;
* the membership of every active event in its session.

Patches are applied atomically with local neighbourhood maintenance; readers
always see either the complete pre-patch or complete post-patch state.
"""

from __future__ import annotations

from .errors import (
    PatchError,
    RevisionConflictError,
    SessionLedgerError,
    StaleRevisionError,
    StoreError,
)
from .naive import RefEvent, RefSession, segment_all
from .patch import ParsedOp, parse_patch
from .query import (
    EventRow,
    Session,
    all_sessions,
    counts,
    event_header,
    events_in_interval,
    idle_gap,
    session_events,
    sessions_in_interval,
)
from .schema import initialise
from .store import EXIT_POINT_CODE, IngestStats, ingest

__all__ = [
    "EventRow",
    "EXIT_POINT_CODE",
    "IngestStats",
    "ParsedOp",
    "PatchError",
    "RefEvent",
    "RefSession",
    "RevisionConflictError",
    "Session",
    "SessionLedgerError",
    "StaleRevisionError",
    "StoreError",
    "all_sessions",
    "counts",
    "event_header",
    "events_in_interval",
    "idle_gap",
    "ingest",
    "initialise",
    "parse_patch",
    "segment_all",
    "session_events",
    "sessions_in_interval",
]
