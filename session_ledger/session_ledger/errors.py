"""Errors raised by the session ledger.

All errors carry enough context (patch line numbers, field names) to point at
the rejected record.  A rejected patch leaves every persisted table untouched
because :mod:`session_ledger.store` applies whole patches inside one SQLite
transaction.
"""

from __future__ import annotations


class SessionLedgerError(Exception):
    """Base class for every error raised by this package."""


class StoreError(SessionLedgerError):
    """The on-disk store is missing, uninitialised or inconsistent."""


class PatchError(SessionLedgerError):
    """A patch file is structurally invalid.

    ``lines`` lists the physical 1-based JSONL line(s) involved (usually one).
    The whole patch -- not just the offending line -- must be refused.
    """

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        if line is not None:
            message = f"line {line}: {message}"
        super().__init__(message)


class StaleRevisionError(SessionLedgerError):
    """A patch line carries a revision older than the stored one."""

    def __init__(self, event_id: str, revision: int, stored: int, line: int) -> None:
        self.event_id = event_id
        self.revision = revision
        self.stored_revision = stored
        self.line = line
        super().__init__(
            f"line {line}: stale revision {revision} for event {event_id!r}; "
            f"stored revision is {stored}"
        )


class RevisionConflictError(SessionLedgerError):
    """Same revision as stored but a different business payload."""

    def __init__(self, event_id: str, revision: int, line: int) -> None:
        self.event_id = event_id
        self.revision = revision
        self.line = line
        super().__init__(
            f"line {line}: revision {revision} of event {event_id!r} already "
            "exists with different content"
        )
