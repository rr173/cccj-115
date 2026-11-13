"""SQLite schema for an imported DwC-A subset database.

The design keeps every row (valid or quarantined) with its full provenance:
member name, table identity and logical record number.  Attribution between
extension rows and core rows is resolved *relationally* -- never by holding
all record texts in Python memory.
"""

from __future__ import annotations

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE descriptor_xml (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    xml BLOB NOT NULL
);

CREATE TABLE tables (
    table_id            TEXT PRIMARY KEY,
    ordinal             INTEGER NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('core', 'extension')),
    row_type            TEXT NOT NULL,
    member              TEXT NOT NULL,
    encoding            TEXT NOT NULL,
    fields_terminated_by TEXT NOT NULL,
    lines_terminated_by TEXT NOT NULL,
    fields_enclosed_by  TEXT NOT NULL,
    ignore_header_lines INTEGER NOT NULL,
    input_count        INTEGER NOT NULL DEFAULT 0,
    valid_count        INTEGER NOT NULL DEFAULT 0,
    quarantined_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE field_maps (
    table_id     TEXT NOT NULL REFERENCES tables(table_id),
    column_order INTEGER NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('id', 'coreid', 'field')),
    term         TEXT NOT NULL DEFAULT '',
    col_index    INTEGER,
    default_value TEXT
);

CREATE TABLE core_rows (
    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id      TEXT NOT NULL,
    member        TEXT NOT NULL,
    record_number INTEGER NOT NULL,
    core_id       TEXT NOT NULL,
    raw_cells_json TEXT NOT NULL DEFAULT '[]',
    is_valid      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (table_id, record_number)
);

CREATE TABLE core_cells (
    core_row_id INTEGER NOT NULL REFERENCES core_rows(row_id),
    term        TEXT NOT NULL,
    value       TEXT NOT NULL,
    origin      TEXT NOT NULL
);

CREATE TABLE ext_rows (
    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id      TEXT NOT NULL,
    member        TEXT NOT NULL,
    record_number INTEGER NOT NULL,
    coreid_value  TEXT NOT NULL,
    raw_cells_json TEXT NOT NULL DEFAULT '[]',
    core_row_id   INTEGER REFERENCES core_rows(row_id),
    is_valid      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (table_id, record_number)
);

CREATE TABLE ext_cells (
    ext_row_id INTEGER NOT NULL REFERENCES ext_rows(row_id),
    term       TEXT NOT NULL,
    value      TEXT NOT NULL,
    origin     TEXT NOT NULL
);

CREATE TABLE quarantine (
    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id      TEXT NOT NULL,
    member        TEXT NOT NULL,
    record_number INTEGER NOT NULL,
    key_value     TEXT,
    reasons_json  TEXT NOT NULL,
    raw_cells_json TEXT NOT NULL,
    data_row_pk   INTEGER
);

CREATE INDEX idx_core_rows_core_id ON core_rows(core_id);
CREATE INDEX idx_core_rows_valid ON core_rows(is_valid, record_number);
CREATE INDEX idx_ext_rows_core ON ext_rows(table_id, core_row_id);
CREATE INDEX idx_ext_rows_coreid ON ext_rows(table_id, coreid_value);
CREATE INDEX idx_ext_rows_valid ON ext_rows(is_valid, table_id, record_number);
CREATE INDEX idx_core_cells_row ON core_cells(core_row_id);
CREATE INDEX idx_ext_cells_row ON ext_cells(ext_row_id);
CREATE INDEX idx_quarantine_table ON quarantine(table_id);
"""
