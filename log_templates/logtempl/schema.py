"""SQLite schema and connection setup.

One library file holds all durable state:

``meta``             configuration, global revision/template counters.
``sources``          per-source next expected position and accepted count.
``templates``        current template head (word count, revision, wildcards).
``template_items``   current items, one row per word position.
``template_revs``    immutable snapshot of every template revision.
``rev_items``        items of each revision (versioned structure).
``messages``         accepted records; never rewritten after acceptance.
``message_words``    per-position classified item + raw parameter word.
``message_gaps``     preserved whitespace before/between/after words.
``template_stats``   message counters (also derivable from ``messages``).
``index_nodes``      materialized candidate-index trie nodes.
``index_leaves``     template ids attached to trie nodes.
``batches``          accepted batches, for observability.

Pragmas: rollback journal with ``synchronous=FULL``.  A transaction either
commits fully (durable even if the process is killed immediately after) or
rolls back leaving no trace.  This is what the fault-point test relies on.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sources (
    source        TEXT PRIMARY KEY,
    next_seq      INTEGER NOT NULL CHECK (next_seq >= 0),
    message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0)
);

CREATE TABLE templates (
    template_id INTEGER PRIMARY KEY,
    word_count  INTEGER NOT NULL CHECK (word_count >= 0),
    revision    INTEGER NOT NULL CHECK (revision >= 1),
    wildcards   INTEGER NOT NULL CHECK (wildcards >= 0),
    created_tx  INTEGER NOT NULL,
    render      TEXT NOT NULL
);

CREATE TABLE template_items (
    template_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    item_kind   TEXT NOT NULL CHECK (item_kind IN ('literal','mask','wildcard')),
    item_value  TEXT NOT NULL,
    PRIMARY KEY (template_id, position)
);

CREATE TABLE template_revs (
    template_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    word_count  INTEGER NOT NULL,
    wildcards   INTEGER NOT NULL,
    tx_id       INTEGER NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('created','generalised')),
    render      TEXT NOT NULL,
    PRIMARY KEY (template_id, revision)
);

CREATE TABLE rev_items (
    template_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    item_kind   TEXT NOT NULL CHECK (item_kind IN ('literal','mask','wildcard')),
    item_value  TEXT NOT NULL,
    PRIMARY KEY (template_id, revision, position)
);

CREATE TABLE template_stats (
    template_id  INTEGER PRIMARY KEY,
    member_count INTEGER NOT NULL DEFAULT 0,
    total_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE messages (
    source       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    template_id  INTEGER NOT NULL,
    template_rev INTEGER NOT NULL,
    word_count   INTEGER NOT NULL,
    message      TEXT NOT NULL,
    PRIMARY KEY (source, seq)
);

CREATE INDEX messages_by_template ON messages(template_id, template_rev);

CREATE TABLE message_words (
    source     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    item_kind  TEXT NOT NULL CHECK (item_kind IN ('literal','mask')),
    item_value TEXT NOT NULL,
    raw_word   TEXT NOT NULL,
    PRIMARY KEY (source, seq, position)
);

CREATE TABLE message_gaps (
    source   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    position INTEGER NOT NULL,
    gap_text TEXT NOT NULL,
    PRIMARY KEY (source, seq, position)
);

CREATE TABLE index_nodes (
    node_id    INTEGER PRIMARY KEY,
    level      INTEGER NOT NULL CHECK (level >= 0),
    parent_id  INTEGER,
    slot_kind  TEXT NOT NULL,
    slot_value TEXT NOT NULL
);

CREATE UNIQUE INDEX index_root_unique
    ON index_nodes(slot_value) WHERE parent_id IS NULL;

CREATE UNIQUE INDEX index_child_unique
    ON index_nodes(parent_id, slot_kind, slot_value)
    WHERE parent_id IS NOT NULL;

CREATE TABLE index_leaves (
    node_id     INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    PRIMARY KEY (node_id, template_id)
);

CREATE INDEX index_leaves_by_template ON index_leaves(template_id);

CREATE TABLE batches (
    batch_id     INTEGER PRIMARY KEY,
    tx_id        INTEGER NOT NULL,
    token        TEXT,
    source_list  TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    first_seq    INTEGER NOT NULL,
    last_seq     INTEGER NOT NULL
);
"""

INITIAL_META = {
    "engine": "logtempl",
    "schema_version": SCHEMA_VERSION,
    "revision_counter": "0",
    "tx_counter": "0",
    "template_next_id": "1",
}


def open_connection(path: str) -> sqlite3.Connection:
    """Open with crash-safe settings; recovery of a hot journal is automatic."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
