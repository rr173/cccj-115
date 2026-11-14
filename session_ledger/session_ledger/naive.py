"""Naive full-rescan segmenter -- test-only reference implementation.

This module deliberately contains no call to the production neighbourhood
machinery in :mod:`session_ledger.store`.  Given an in-memory timeline it
re-derives every session from scratch with the same business rules:

* sort each user's active events by ``(event_ms, event_id)``;
* consecutive events are linked while the time gap is ``<= G``;
* a session is a maximal linked run; its id is the lexicographically smallest
  event id of the run; ``span_ms = end_ms - start_ms``.

The randomised tests run many patch rounds against the real SQLite store and
against this reference, then compare the complete session fields and the
per-event membership, not just row counts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefEvent:
    event_id: str
    user_id: str
    event_ms: int


@dataclass(frozen=True)
class RefSession:
    user_id: str
    session_id: str
    start_ms: int
    end_ms: int
    event_count: int
    span_ms: int


def segment_all(
    events: list[RefEvent], gap: int
) -> tuple[dict[str, RefSession], dict[str, str]]:
    """Return (sessions by id, event_id -> session_id) for a full timeline."""
    by_user: dict[str, list[RefEvent]] = {}
    for event in events:
        by_user.setdefault(event.user_id, []).append(event)

    sessions: dict[str, RefSession] = {}
    membership: dict[str, str] = {}
    for user_id, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.event_ms, e.event_id))
        run: list[RefEvent] = []
        for event in user_events:
            if run and event.event_ms - run[-1].event_ms > gap:
                _emit(run, user_id, sessions, membership)
                run = []
            run.append(event)
        if run:
            _emit(run, user_id, sessions, membership)
    return sessions, membership


def _emit(
    run: list[RefEvent],
    user_id: str,
    sessions: dict[str, RefSession],
    membership: dict[str, str],
) -> None:
    session_id = min(e.event_id for e in run)
    start = run[0].event_ms
    end = run[-1].event_ms
    sessions[session_id] = RefSession(
        user_id=user_id,
        session_id=session_id,
        start_ms=start,
        end_ms=end,
        event_count=len(run),
        span_ms=end - start,
    )
    for event in run:
        membership[event.event_id] = session_id
