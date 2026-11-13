"""Disk-backed checklist store with exact identity deduplication.

The checklist text is never kept in memory as a whole.  Each line is parsed
and its decoded identity ``(uuid_bytes, fingerprint_bytes)`` is inserted into
a SQLite table keyed by the identifier; duplicates are discarded by the
database's uniqueness constraint, not by a Python ``set``.

The primary key on ``uuid`` alone also makes *identity conflicts* detectable:
one identifier carrying two different fingerprints inside a single checklist.
When that happens the store rolls back and reports the two input line numbers.
"""

from __future__ import annotations

import os
import sqlite3

from .config import fingerprint_bytes, uuid_bytes
from .errors import ChecklistConflictError, ChecklistError

#: Hex fields are fixed width; anything else is rejected before decoding.
UUID_HEX_LEN = 32
FINGERPRINT_HEX_LEN = 64

SCHEMA = (
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE elements (
        uuid        BLOB PRIMARY KEY,
        fingerprint BLOB NOT NULL,
        first_line  INTEGER NOT NULL
    ) WITHOUT ROWID;
    """,
)

#: How many identifiers are looked up per SQL statement when checking a batch
#: against already committed rows.  Keeps the variable count far below
#: SQLite's default limit of 999.
LOOKUP_CHUNK = 400


class ChecklistStore:
    """A transient or on-disk SQLite store of checklist identities."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._conn = sqlite3.connect(path if path is not None else ":memory:")
        if path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in SCHEMA:
            self._conn.execute(statement)
        self._conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [("source_lines", 0), ("unique_elements", 0), ("duplicate_lines", 0)],
        )
        self._conn.commit()

    # -- lifecycle -------------------------------------------------------
    @classmethod
    def open_existing(cls, path: str) -> "ChecklistStore":
        if not os.path.exists(path):
            raise ChecklistError(f"checklist store does not exist: {path}")
        store = cls.__new__(cls)
        store.path = path
        store._conn = sqlite3.connect(path)
        return store

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "ChecklistStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ChecklistError("checklist store is closed")
        return self._conn

    # -- ingest ----------------------------------------------------------
    def ingest_lines(self, lines, *, batch_size: int = 2000) -> dict:
        """Read checklist lines, obey uniqueness, then commit once.

        Each accepted line contributes exactly one element.  A repeated
        ``(uuid, fingerprint)`` is counted as a duplicate.  A repeated
        ``uuid`` with a *different* fingerprint raises
        :class:`ChecklistConflictError` naming both line numbers; the
        transaction rolls back, so no digest can be built from a conflicting
        checklist.

        ``lines`` may be any iterable of strings (typically an open file or
        stdout produced by another command).  At most one batch of parsed
        identities exists in Python at a time.
        """
        conn = self.connection
        conn.execute("BEGIN")
        seen_lines = 0
        unique = 0
        pending: list[tuple[bytes, bytes, int]] = []
        try:
            for line_no, raw in enumerate(lines, start=1):
                seen_lines += 1
                uuid_hex, fp_hex = _parse_line(raw, line_no)
                pending.append(
                    (uuid_bytes(uuid_hex), fingerprint_bytes(fp_hex), line_no)
                )
                if len(pending) >= batch_size:
                    unique += self._flush(pending)
                    pending.clear()
            unique += self._flush(pending)
            duplicates = seen_lines - unique
            values = {
                "source_lines": seen_lines,
                "unique_elements": unique,
                "duplicate_lines": duplicates,
            }
            for key, value in values.items():
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = ?", (value, key)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "source_lines": seen_lines,
            "unique_elements": unique,
            "duplicate_lines": duplicates,
        }

    def _flush(self, pending: list[tuple[bytes, bytes, int]]) -> int:
        """Insert one batch, resolving duplicates and conflicts in SQL.

        Returns the number of rows that were genuinely new.  Conflicts are
        checked against earlier rows in the same batch and against rows
        committed by previous batches.
        """
        conn = self.connection
        overlay: dict[bytes, tuple[bytes, int]] = {}
        for uuid_b, fp_b, line_no in pending:
            previous = overlay.get(uuid_b)
            if previous is not None and previous[0] != fp_b:
                raise ChecklistConflictError(
                    "checklist conflict: identifier "
                    f"{uuid_b.hex()} has fingerprint {previous[0].hex()} "
                    f"at line {previous[1]} and fingerprint {fp_b.hex()} "
                    f"at line {line_no}; refusing to build a digest"
                )
            overlay.setdefault(uuid_b, (fp_b, line_no))

        # One indexed lookup per chunk, never one per line.
        existing = self._lookup([uuid_b for uuid_b, _, _ in pending])
        for uuid_b, fp_b, line_no in pending:
            row = existing.get(uuid_b)
            if row is not None and row[0] != fp_b:
                raise ChecklistConflictError(
                    "checklist conflict: identifier "
                    f"{uuid_b.hex()} has fingerprint {row[0].hex()} "
                    f"at line {row[1]} and fingerprint {fp_b.hex()} "
                    f"at line {line_no}; refusing to build a digest"
                )

        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO elements(uuid, fingerprint, first_line) "
            "VALUES (?, ?, ?)",
            pending,
        )
        return conn.total_changes - before

    def _lookup(self, uuids: list[bytes]) -> dict[bytes, tuple[bytes, int]]:
        """Fetch ``uuid -> (fingerprint, first_line)`` for one batch."""
        found: dict[bytes, tuple[bytes, int]] = {}
        conn = self.connection
        for start in range(0, len(uuids), LOOKUP_CHUNK):
            chunk = uuids[start:start + LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT uuid, fingerprint, first_line FROM elements "
                f"WHERE uuid IN ({placeholders})",
                chunk,
            )
            for uuid_b, fp_b, line_no in rows:
                found[bytes(uuid_b)] = (bytes(fp_b), line_no)
        return found

    # -- reads -----------------------------------------------------------
    def meta(self) -> dict:
        rows = self.connection.execute("SELECT key, value FROM meta")
        return {key: value for key, value in rows}

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM elements").fetchone()[0]

    def iter_elements(self, batch_size: int = 1000):
        """Yield ``(uuid_bytes, fingerprint_bytes)`` in deterministic order."""
        cursor = self.connection.execute(
            "SELECT uuid, fingerprint FROM elements ORDER BY uuid"
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield from rows


def _parse_line(raw: str, line_no: int) -> tuple[str, str]:
    """Split one checklist line into ``(uuid_hex, fingerprint_hex)``.

    Accepted separators: a comma or whitespace.  Blank lines and lines
    starting with ``#`` are rejected: silently skipping them would let a
    malformed input look complete.  Both fields are validated as fixed-width
    hexadecimal *before* any byte comparison happens.
    """
    text = raw.strip()
    if not text or text.startswith("#"):
        raise ChecklistError(f"line {line_no}: not a checklist entry: {raw!r}")
    fields = [part.strip() for part in text.split(",")] if "," in text else text.split()
    if len(fields) != 2:
        raise ChecklistError(
            f"line {line_no}: expected UUID and fingerprint, got {len(fields)} fields"
        )
    uuid_hex, fp_hex = fields
    if len(uuid_hex) != UUID_HEX_LEN:
        raise ChecklistError(
            f"line {line_no}: UUID must be {UUID_HEX_LEN} hex characters, "
            f"got {len(uuid_hex)}"
        )
    if len(fp_hex) != FINGERPRINT_HEX_LEN:
        raise ChecklistError(
            f"line {line_no}: fingerprint must be {FINGERPRINT_HEX_LEN} hex "
            f"characters, got {len(fp_hex)}"
        )
    try:
        bytes.fromhex(uuid_hex)
    except ValueError:
        raise ChecklistError(f"line {line_no}: UUID is not valid hexadecimal") from None
    try:
        bytes.fromhex(fp_hex)
    except ValueError:
        raise ChecklistError(
            f"line {line_no}: fingerprint is not valid hexadecimal"
        ) from None
    return uuid_hex.lower(), fp_hex.lower()
