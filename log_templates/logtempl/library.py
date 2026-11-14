"""Library: batch ingestion, template learning, revisions, queries.

One :class:`Library` is one SQLite file.  Every mutation happens in a single
SQLite transaction that covers, for the whole incoming batch:

1. template creation / generalisation,
2. template revision snapshots and global revision counter,
3. index node and leaf updates,
4. message registration (classified words + raw words + gaps),
5. per-source next positions and message counters.

If anything fails or the process dies before COMMIT, all five roll back
together; after COMMIT all five are durable, even across ``os._exit``.

Replay / gap protocol (per ``source``)
--------------------------------------
``sources.next_seq`` is the next position this library expects.

* ``seq > next_seq``            -> the batch is refused, nothing is written,
                                   ``sources`` and all tables are unchanged.
* ``seq == next_seq``           -> new record, accepted.
* ``seq <  next_seq``           -> a replay of an already accepted position:
      - identical message text   -> ignored (no writes, no revision bump);
      - different message text   -> conflict, batch refused.

The "ignore replay" rule is what makes re-feeding an entire source after a
crash idempotent.  Refusal happens as a pre-flight scan before the write
transaction starts, and the position check is repeated inside the write
transaction against the now-current state.

Fault injection
---------------
:func:`_fault_point` is called after the first template mutation in a batch
but before its message is registered (and before COMMIT).  When
``LOGTEMPL_FAULT=source:seq`` names that record it calls ``os._exit(23)``:
templates and index changes were staged but never committed, so a later run
sees the pre-batch state and produces identical ids, revisions and members.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field

from . import masks
from .cache import CappedCache
from .config import Config, ConfigError, build_config
from .index import Candidate, IndexWriter, load_items, lookup
from .schema import INITIAL_META, create_schema, open_connection
from .templates import Item, LITERAL, MASK, WILDCARD, render_items
from .textproc import Tokenised

ENGINE = "logtempl"

#: Set to ``"<source>:<seq>"`` to trigger the test-only crash point.
FAULT_ENV = "LOGTEMPL_FAULT_BEFORE_MESSAGE"


class LogTemplateError(Exception):
    """Base class for library-level refusals."""


class ConflictError(LogTemplateError):
    pass


class GapError(LogTemplateError):
    pass


class InputFormatError(LogTemplateError):
    pass


class LibraryExists(LogTemplateError):
    pass


class UnknownTemplate(LogTemplateError):
    pass


def _fault_point(source: str, seq: int) -> None:
    spec = os.environ.get(FAULT_ENV)
    if spec and spec == f"{source}:{seq}":
        os._exit(23)


@dataclass(frozen=True)
class Record:
    source: str
    seq: int
    message: str


@dataclass
class BatchOutcome:
    accepted: list[tuple[str, int]] = field(default_factory=list)
    ignored_replays: list[tuple[str, int]] = field(default_factory=list)
    templates_created: list[int] = field(default_factory=list)
    templates_revised: list[int] = field(default_factory=list)
    index_nodes_visited: int = 0
    candidates_examined: int = 0
    branches_expanded: int = 0
    branches_pruned: int = 0
    tx_id: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.accepted)


@dataclass(frozen=True)
class MessageRow:
    source: str
    seq: int
    template_id: int
    template_rev: int
    word_count: int
    message: str


@dataclass(frozen=True)
class RevisionInfo:
    template_id: int
    revision: int
    word_count: int
    wildcards: int
    tx_id: int
    origin: str
    render: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class MemberRow:
    source: str
    seq: int
    template_rev: int
    message: str


class Library:
    def __init__(self, path: str, conn: sqlite3.Connection, config: Config):
        self.path = path
        self.conn = conn
        self.config = config
        self.enabled = masks.validate_enabled(list(config.enabled_masks))
        self.threshold = config.threshold
        self.template_cache = CappedCache(config.template_cache)
        self.node_cache = CappedCache(config.node_cache)
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @classmethod
    def create(cls, path: str, **config_kwargs) -> "Library":
        if os.path.exists(path):
            raise LibraryExists(f"{path} already exists")
        config = build_config(**config_kwargs)
        conn = open_connection(path)
        try:
            create_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            meta = dict(INITIAL_META)
            meta["config"] = config.to_json()
            meta["enabled_masks"] = masks.enabled_text(
                masks.validate_enabled(list(config.enabled_masks))
            )
            for key, value in sorted(meta.items()):
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)", (key, value)
                )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            raise
        return cls(path, conn, config)

    @classmethod
    def open(
        cls,
        path: str,
        *,
        template_cache: int | None = None,
        node_cache: int | None = None,
        depth: int | None = None,
        enabled_masks=None,
    ) -> "Library":
        """Re-open a library.

        Cache limits and ``depth`` are initialisation parameters and may be
        changed (changing depth rebuilds the derived index).  The mask rule
        set and the threshold are fixed at creation: supplying a different
        mask set is refused, because existing mask items would change meaning.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        conn = open_connection(path)
        try:
            stored = cls._read_config(conn)
        except BaseException:
            conn.close()
            raise
        if enabled_masks is not None:
            requested = masks.validate_enabled(enabled_masks)
            if requested != masks.validate_enabled(list(stored.enabled_masks)):
                conn.close()
                raise ConfigError(
                    "mask rule set is fixed for a library: stored "
                    f"{masks.enabled_text(masks.validate_enabled(list(stored.enabled_masks)))}"
                    f", requested {masks.enabled_text(requested)}"
                )
        config = build_config(
            threshold=stored.threshold_text,
            depth=stored.depth if depth is None else depth,
            template_cache=(
                stored.template_cache if template_cache is None
                else template_cache
            ),
            node_cache=(
                stored.node_cache if node_cache is None else node_cache
            ),
            enabled_masks=list(stored.enabled_masks),
        )
        lib = cls(path, conn, config)
        if config.depth != stored.depth:
            lib._persist_config(reindex_from_depth_change=True)
        elif (config.template_cache != stored.template_cache
              or config.node_cache != stored.node_cache):
            lib._persist_config()
        return lib

    @staticmethod
    def _read_config(conn: sqlite3.Connection) -> Config:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'config'"
        ).fetchone()
        if row is None:
            raise LogTemplateError("not a logtempl library (missing config)")
        return Config.from_json(row[0])

    def _persist_config(self, reindex_from_depth_change: bool = False) -> None:
        """Update stored config; depth changes rebuild the (derived) index."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'config'",
                (self.config.to_json(),),
            )
            if reindex_from_depth_change:
                IndexWriter(self.conn, self.config.depth).rebuild()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.template_cache.clear()
        self.node_cache.clear()

    def close(self) -> None:
        if not self._closed:
            self.conn.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------
    # meta helpers
    # ------------------------------------------------------------------
    def _meta(self, key: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise LogTemplateError(f"missing meta key {key!r}")
        return row[0]

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = ?", (value, key)
        )

    def _meta_int(self, key: str) -> int:
        return int(self._meta(key))

    def next_seq(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT next_seq FROM sources WHERE source = ?", (source,)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def config_report(self) -> dict:
        return {
            "threshold": self.config.threshold_text,
            "threshold_exact": str(self.threshold),
            "depth": self.config.depth,
            "template_cache": self.config.template_cache,
            "node_cache": self.config.node_cache,
            "enabled_masks": list(self.config.enabled_masks),
            "template_counter": self._meta_int("template_next_id"),
            "revision_counter": self._meta_int("revision_counter"),
            "tx_counter": self._meta_int("tx_counter"),
        }

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    def ingest_batch(self, records: list[Record], *, token: str | None = None) -> BatchOutcome:
        """Apply one batch atomically.  Raises GapError/ConflictError before
        any write, so a refused batch leaves the library untouched."""
        prepared = self._preflight(records)
        outcome = BatchOutcome()
        if not prepared[0]:
            # only replays; nothing to write
            outcome.ignored_replays = prepared[1]
            return outcome

        new_records = prepared[0]
        outcome.ignored_replays = prepared[1]
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            tx_id = self._bump_tx()
            outcome.tx_id = tx_id
            for rec in new_records:
                self._accept_record(rec, tx_id, outcome)
            self._record_batch(token, tx_id, outcome, new_records)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return outcome

    def _preflight(self, records: list[Record]):
        """Validate ordering, gaps and replays without writing anything."""
        # Local view of each source's next position while scanning the batch.
        local_next: dict[str, int] = {}
        # Text of records accepted earlier in *this* batch, keyed by position.
        seen_in_batch: dict[tuple[str, int], str] = {}
        accepted: list[Record] = []
        replays: list[tuple[str, int]] = []
        for rec in records:
            if not isinstance(rec, Record):
                raise InputFormatError("batch entries must be Record objects")
            if rec.seq < 0:
                raise InputFormatError(f"negative seq {rec.seq}")
            expected = local_next.get(rec.source, self.next_seq(rec.source))
            if rec.seq > expected:
                raise GapError(
                    f"seq gap for source {rec.source!r}: got {rec.seq}, "
                    f"expected {expected}"
                )
            if rec.seq == expected:
                accepted.append(rec)
                seen_in_batch[(rec.source, rec.seq)] = rec.message
                local_next[rec.source] = expected + 1
                continue
            # rec.seq < expected: this position was already taken, either by
            # an earlier record of this batch or by an earlier committed batch.
            prior = seen_in_batch.get((rec.source, rec.seq))
            if prior is None:
                prior = self._committed_text(rec.source, rec.seq)
            if prior is None:
                raise GapError(
                    f"seq {rec.seq} of source {rec.source!r} is below the "
                    f"expected {expected} but is not committed"
                )
            if prior != rec.message:
                raise ConflictError(
                    f"conflict at source {rec.source!r} seq {rec.seq}: "
                    "already accepted text differs from the replayed text"
                )
            replays.append((rec.source, rec.seq))
        # Deduplicate replays deterministically, preserving first-seen order.
        seen = set()
        unique_replays = []
        for item in replays:
            if item not in seen:
                seen.add(item)
                unique_replays.append(item)
        return accepted, unique_replays

    def _committed_text(self, source: str, seq: int) -> str | None:
        row = self.conn.execute(
            "SELECT message FROM messages WHERE source = ? AND seq = ?",
            (source, seq),
        ).fetchone()
        return None if row is None else row[0]

    def _bump_tx(self) -> int:
        tx_id = self._meta_int("tx_counter") + 1
        self._set_meta("tx_counter", str(tx_id))
        return tx_id

    def _accept_record(self, rec: Record, tx_id: int, outcome: BatchOutcome) -> None:
        # Re-check under the write lock: a concurrent writer could have moved
        # the source forward since the pre-flight scan.
        expected = self.next_seq(rec.source)
        if rec.seq != expected:
            raise GapError(
                f"seq {rec.seq} for source {rec.source!r} is no longer the "
                f"expected position {expected}"
            )
        tokens = Tokenised.from_message(rec.message)
        message_items = self._classify(tokens)
        wc = tokens.word_count
        report = lookup(
            self.conn,
            self.config.depth,
            wc,
            message_items,
            self.threshold,
            self.template_cache,
            self.node_cache,
        )
        outcome.index_nodes_visited += report.nodes_visited
        outcome.candidates_examined += report.candidates_examined
        outcome.branches_expanded += report.branches_expanded
        outcome.branches_pruned += report.branches_pruned

        best = self._choose(report.candidates)
        if best is None:
            template_id, revision = self._create_template(
                message_items, tx_id
            )
            outcome.templates_created.append(template_id)
        else:
            template_id = best.template_id
            old_revision = best.revision
            revision = self._generalise(template_id, message_items, tx_id)
            if revision != old_revision:
                outcome.templates_revised.append(template_id)

        # Test fault point: template + index changes for this record are
        # staged, its message row is not written yet.
        _fault_point(rec.source, rec.seq)

        self._register_message(
            rec, template_id, revision, tokens, message_items, wc
        )
        self._advance_source(rec.source, rec.seq)
        outcome.accepted.append((rec.source, rec.seq))

    def _classify(self, tokens: Tokenised) -> tuple[tuple[str, str], ...]:
        """Whole-word mask classification of every token, in order."""
        items = []
        for word in tokens.words:
            kind = masks.classify(word, self.enabled)
            if kind is None:
                items.append((LITERAL, word))
            else:
                items.append((MASK, kind))
        return tuple(items)

    def _choose(self, candidates: list[Candidate]) -> Candidate | None:
        """Deterministic selection among admissible candidates.

        Candidates arrive sorted by ``(-points, wildcards, template_id)``.
        That key implements the documented rule -- most matching fixed
        positions, then fewest wildcards, then the earliest created template
        -- using only persisted integers, never Python hash/set iteration
        order.  Zero-word candidates only compete with zero-word messages,
        which is already guaranteed by the word-count-partitioned index.
        """
        return candidates[0] if candidates else None

    # -- template writes ----------------------------------------------
    def _create_template(
        self, message_items: tuple[tuple[str, str], ...], tx_id: int
    ) -> tuple[int, int]:
        conn = self.conn
        template_id = self._meta_int("template_next_id")
        self._set_meta("template_next_id", str(template_id + 1))
        # A library-wide, strictly increasing revision counter.  Every
        # structural change (template creation or generalization) consumes
        # the next value, so revisions are globally ordered and never reused.
        revision = self._next_revision()
        items = tuple(
            Item(LITERAL, value) if kind == LITERAL else Item(MASK, value)
            for kind, value in message_items
        )
        wildcards = 0
        conn.execute(
            "INSERT INTO templates "
            "(template_id, word_count, revision, wildcards, created_tx, render) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (template_id, len(items), revision, wildcards, tx_id,
             render_items(items)),
        )
        conn.execute(
            "INSERT INTO template_stats (template_id, member_count, total_count) "
            "VALUES (?, 0, 0)",
            (template_id,),
        )
        self._save_revision(
            template_id, revision, items, tx_id, "created",
            len(items), wildcards, render_items(items),
        )
        self._write_current_items(template_id, items)
        IndexWriter(conn, self.config.depth).attach(
            template_id, len(items), items, revision
        )
        # Index node children/leaves changed: drop node-derived entries.  Other
        # templates' items are unaffected and may stay cached.
        self.node_cache.clear()
        return template_id, revision

    def _generalise(
        self, template_id: int, message_items: tuple[tuple[str, str], ...],
        tx_id: int,
    ) -> int:
        """Merge a message into a template; return the referencing revision."""
        conn = self.conn
        current = self._load_current_items(template_id)
        changed = False
        new_items: list[Item] = []
        for (m_kind, m_value), t_item in zip(message_items, current):
            if t_item.kind == WILDCARD:
                new_items.append(t_item)
            elif m_kind == t_item.kind and m_value == t_item.value:
                new_items.append(t_item)
            else:
                new_items.append(Item(WILDCARD, ""))
                changed = True
        items = tuple(new_items)
        if not changed:
            # Perfect agreement: message references the current revision and
            # the index/template rows stay untouched.
            return self._current_revision(template_id)
        revision = self._next_revision()
        wildcards = sum(1 for it in items if it.kind == WILDCARD)
        render = render_items(items)
        conn.execute(
            "UPDATE templates SET revision = ?, wildcards = ?, render = ? "
            "WHERE template_id = ?",
            (revision, wildcards, render, template_id),
        )
        self._save_revision(
            template_id, revision, items, tx_id, "generalised",
            len(items), wildcards, render,
        )
        self._write_current_items(template_id, items)
        IndexWriter(conn, self.config.depth).relink(
            template_id, len(items), items, revision
        )
        # Only the mutated template and the index derived from it are stale.
        self.template_cache.drop(("items", template_id))
        self.node_cache.clear()
        return revision

    def _next_revision(self) -> int:
        """Next value of the library-wide monotone revision counter.

        Called inside the batch transaction; a crash before COMMIT therefore
        does not consume a value, which is why the post-fault re-run produces
        the same revision numbers as the fault-free run.
        """
        revision = self._meta_int("revision_counter") + 1
        self._set_meta("revision_counter", str(revision))
        return revision

    def _save_revision(
        self, template_id: int, revision: int, items: tuple[Item, ...],
        tx_id: int, origin: str, word_count: int, wildcards: int, render: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO template_revs "
            "(template_id, revision, word_count, wildcards, tx_id, origin, render) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (template_id, revision, word_count, wildcards, tx_id, origin, render),
        )
        self.conn.executemany(
            "INSERT INTO rev_items "
            "(template_id, revision, position, item_kind, item_value) "
            "VALUES (?, ?, ?, ?, ?)",
            [(template_id, revision, pos, it.kind, it.value)
             for pos, it in enumerate(items)],
        )

    def _load_current_items(self, template_id: int) -> tuple[Item, ...]:
        key = ("items", template_id)
        return self.template_cache.get(
            key, lambda _k: load_items(self.conn, template_id)
        )

    def _write_current_items(self, template_id: int, items: tuple[Item, ...]) -> None:
        self.conn.execute(
            "DELETE FROM template_items WHERE template_id = ?", (template_id,)
        )
        self.conn.executemany(
            "INSERT INTO template_items "
            "(template_id, position, item_kind, item_value) "
            "VALUES (?, ?, ?, ?)",
            [(template_id, pos, it.kind, it.value)
             for pos, it in enumerate(items)],
        )

    def _current_revision(self, template_id: int) -> int:
        row = self.conn.execute(
            "SELECT revision FROM templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if row is None:
            raise UnknownTemplate(str(template_id))
        return row[0]

    def _register_message(
        self, rec: Record, template_id: int, revision: int,
        tokens: Tokenised, message_items: tuple[tuple[str, str], ...],
        wc: int,
    ) -> None:
        conn = self.conn
        conn.execute(
            "UPDATE template_stats SET member_count = member_count + 1, "
            "total_count = total_count + 1 WHERE template_id = ?",
            (template_id,),
        )
        conn.execute(
            "INSERT INTO messages "
            "(source, seq, template_id, template_rev, word_count, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rec.source, rec.seq, template_id, revision, wc, rec.message),
        )
        conn.executemany(
            "INSERT INTO message_words "
            "(source, seq, position, item_kind, item_value, raw_word) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(rec.source, rec.seq, pos, kind, value, tokens.words[pos])
             for pos, (kind, value) in enumerate(message_items)],
        )
        conn.executemany(
            "INSERT INTO message_gaps (source, seq, position, gap_text) "
            "VALUES (?, ?, ?, ?)",
            [(rec.source, rec.seq, pos, gap)
             for pos, gap in enumerate(tokens.gaps)],
        )

    def _advance_source(self, source: str, seq: int) -> None:
        row = self.conn.execute(
            "SELECT message_count FROM sources WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO sources (source, next_seq, message_count) "
                "VALUES (?, ?, 1)",
                (source, seq + 1),
            )
        else:
            self.conn.execute(
                "UPDATE sources SET next_seq = ?, message_count = message_count + 1 "
                "WHERE source = ?",
                (seq + 1, source),
            )

    def _record_batch(
        self, token: str | None, tx_id: int, outcome: BatchOutcome,
        records: list[Record],
    ) -> None:
        source_list = sorted({r.source for r in records})
        self.conn.execute(
            "INSERT INTO batches "
            "(tx_id, token, source_list, record_count, first_seq, last_seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                tx_id,
                token,
                json.dumps(source_list, ensure_ascii=False),
                len(records),
                min((r.seq for r in records), default=0),
                max((r.seq for r in records), default=0),
            ),
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get_message(self, source: str, seq: int) -> MessageRow | None:
        row = self.conn.execute(
            "SELECT source, seq, template_id, template_rev, word_count, message "
            "FROM messages WHERE source = ? AND seq = ?",
            (source, seq),
        ).fetchone()
        return None if row is None else MessageRow(*row)

    def message_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def reconstruct_message(self, source: str, seq: int) -> str:
        """Rebuild the original text from stored words and gaps.

        The reconstruction uses the *message's own* raw words and gaps, not
        the current template, so later generalisation cannot change it.
        """
        words = [
            raw for (raw,) in self.conn.execute(
                "SELECT raw_word FROM message_words "
                "WHERE source = ? AND seq = ? ORDER BY position",
                (source, seq),
            )
        ]
        gaps = [
            gap for (gap,) in self.conn.execute(
                "SELECT gap_text FROM message_gaps "
                "WHERE source = ? AND seq = ? ORDER BY position",
                (source, seq),
            )
        ]
        row = self.conn.execute(
            "SELECT word_count FROM messages WHERE source = ? AND seq = ?",
            (source, seq),
        ).fetchone()
        if row is None:
            raise KeyError(f"no message at {source!r}:{seq}")
        if len(words) != row[0] or len(gaps) != row[0] + 1:
            raise LogTemplateError(
                f"stored word/gap arity inconsistent at {source!r}:{seq}"
            )
        parts = [gaps[0]]
        for k, word in enumerate(words):
            parts.append(word)
            parts.append(gaps[k + 1])
        return "".join(parts)

    def message_parameters(
        self, source: str, seq: int
    ) -> list[tuple[int, str, str, str]]:
        """Per-position ``(position, item_kind, item_value, raw_word)``."""
        return [
            (pos, kind, value, raw)
            for pos, kind, value, raw in self.conn.execute(
                "SELECT position, item_kind, item_value, raw_word "
                "FROM message_words WHERE source = ? AND seq = ? "
                "ORDER BY position",
                (source, seq),
            )
        ]

    def template_head(self, template_id: int) -> dict:
        row = self.conn.execute(
            "SELECT template_id, word_count, revision, wildcards, created_tx, render "
            "FROM templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if row is None:
            raise UnknownTemplate(str(template_id))
        stats = self.conn.execute(
            "SELECT member_count, total_count FROM template_stats "
            "WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        return {
            "template_id": row[0],
            "word_count": row[1],
            "revision": row[2],
            "wildcards": row[3],
            "created_tx": row[4],
            "render": row[5],
            "member_count": stats[0] if stats else 0,
            "total_count": stats[1] if stats else 0,
        }

    def template_revision(
        self, template_id: int, revision: int
    ) -> RevisionInfo:
        row = self.conn.execute(
            "SELECT template_id, revision, word_count, wildcards, tx_id, "
            "origin, render FROM template_revs "
            "WHERE template_id = ? AND revision = ?",
            (template_id, revision),
        ).fetchone()
        if row is None:
            raise UnknownTemplate(f"template {template_id} revision {revision}")
        items = tuple(
            Item.from_row(kind, value)
            for kind, value in self.conn.execute(
                "SELECT item_kind, item_value FROM rev_items "
                "WHERE template_id = ? AND revision = ? ORDER BY position",
                (template_id, revision),
            )
        )
        return RevisionInfo(*row, items)

    def template_revisions(self, template_id: int) -> list[RevisionInfo]:
        revs = [
            rev for (rev,) in self.conn.execute(
                "SELECT revision FROM template_revs WHERE template_id = ? "
                "ORDER BY revision",
                (template_id,),
            )
        ]
        if not revs:
            raise UnknownTemplate(str(template_id))
        return [self.template_revision(template_id, r) for r in revs]

    def template_members(
        self, template_id: int, revision: int | None = None
    ) -> list[MemberRow]:
        if revision is None:
            rows = self.conn.execute(
                "SELECT source, seq, template_rev, message FROM messages "
                "WHERE template_id = ? ORDER BY source, seq",
                (template_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT source, seq, template_rev, message FROM messages "
                "WHERE template_id = ? AND template_rev = ? "
                "ORDER BY source, seq",
                (template_id, revision),
            ).fetchall()
        return [MemberRow(*r) for r in rows]

    def template_ids(self) -> list[int]:
        return [
            tid for (tid,) in self.conn.execute(
                "SELECT template_id FROM templates ORDER BY template_id"
            )
        ]

    def source_report(self) -> list[tuple[str, int, int]]:
        return self.conn.execute(
            "SELECT source, next_seq, message_count FROM sources ORDER BY source"
        ).fetchall()

    def cache_stats(self) -> dict:
        return {
            "templates": self.template_cache.stats(),
            "nodes": self.node_cache.stats(),
        }

    def verify_integrity(self) -> None:
        """Cheap consistency checks used by tests and the CLI."""
        conn = self.conn
        bad = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if bad != "ok":
            raise LogTemplateError(f"sqlite integrity check: {bad}")
        # each template has exactly word_count current items
        for template_id, wc in conn.execute(
            "SELECT template_id, word_count FROM templates"
        ):
            n = conn.execute(
                "SELECT COUNT(*) FROM template_items WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            if n != wc:
                raise LogTemplateError(
                    f"template {template_id}: {n} items for word_count {wc}"
                )
        # every message references an existing revision of its template
        for source, seq, tid, rev in conn.execute(
            "SELECT source, seq, template_id, template_rev FROM messages"
        ):
            n = conn.execute(
                "SELECT COUNT(*) FROM template_revs "
                "WHERE template_id = ? AND revision = ?",
                (tid, rev),
            ).fetchone()[0]
            if n != 1:
                raise LogTemplateError(
                    f"message {source!r}:{seq} references missing revision "
                    f"{tid}@{rev}"
                )
        # every template has exactly one leaf
        for (template_id,) in conn.execute("SELECT template_id FROM templates"):
            n = conn.execute(
                "SELECT COUNT(*) FROM index_leaves WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            if n != 1:
                raise LogTemplateError(
                    f"template {template_id} has {n} index leaves"
                )
        # Leaf nodes agree with the templates' *current* prefix paths.
        writer = IndexWriter(conn, self.config.depth)
        for template_id, tid, wc, level in conn.execute(
            "SELECT t.template_id, t.template_id, t.word_count, n.level "
            "FROM templates t "
            "JOIN index_leaves l ON l.template_id = t.template_id "
            "JOIN index_nodes n ON n.node_id = l.node_id"
        ):
            if level != min(self.config.depth, wc):
                raise LogTemplateError(
                    f"template {template_id} leaf at level {level}, expected "
                    f"{min(self.config.depth, wc)}"
                )
            items = load_items(conn, template_id, wc)
            expected = writer.existing_leaf_node(wc, items)
            actual = conn.execute(
                "SELECT node_id FROM index_leaves WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            if expected is None:
                raise LogTemplateError(
                    f"template {template_id} has no index path nodes (index "
                    "out of date)"
                )
            if expected != actual:
                raise LogTemplateError(
                    f"template {template_id} leaf node {actual} != relinked "
                    f"path node {expected} (index out of date)"
                )
        # Message word/gap arity must invert to the stored text.
        for source, seq, wc, text in conn.execute(
            "SELECT source, seq, word_count, message FROM messages"
        ).fetchall():
            words = conn.execute(
                "SELECT raw_word FROM message_words WHERE source=? AND seq=? "
                "ORDER BY position", (source, seq),
            ).fetchall()
            gaps = conn.execute(
                "SELECT gap_text FROM message_gaps WHERE source=? AND seq=? "
                "ORDER BY position", (source, seq),
            ).fetchall()
            if len(words) != wc or len(gaps) != wc + 1:
                raise LogTemplateError(
                    f"message {source!r}:{seq} has {len(words)} words / "
                    f"{len(gaps)} gaps for word_count {wc}"
                )
            rebuilt = gaps[0][0]
            for k, (word,) in enumerate(words):
                rebuilt += word + gaps[k + 1][0]
            if rebuilt != text:
                raise LogTemplateError(
                    f"message {source!r}:{seq} does not reconstruct exactly"
                )
        # Template statistics equal an independent count over messages.
        for template_id, member_count, total_count in conn.execute(
            "SELECT s.template_id, s.member_count, s.total_count "
            "FROM template_stats s"
        ):
            actual = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE template_id = ?",
                (template_id,),
            ).fetchone()[0]
            if member_count != actual or total_count != actual:
                raise LogTemplateError(
                    f"template {template_id} stats {member_count}/{total_count}"
                    f" != message count {actual}"
                )

    def rebuild_index(self) -> None:
        """Recompute derived index tables from templates (recovery/verify)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            IndexWriter(self.conn, self.config.depth).rebuild()
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.node_cache.clear()
