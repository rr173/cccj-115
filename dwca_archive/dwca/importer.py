"""Two-phase, disk-backed import of a DwC-A ZIP into a fresh SQLite file.

Memory boundary
---------------
The archive's record texts never accumulate in Python memory.  Every logical
record is parsed once, written to SQLite inside the import transaction and
released; Python keeps only small bookkeeping sets (core id strings and a set
of *ambiguous* keys), whose size is bounded by the number of distinct keys,
not by the volume or width of record text.  Extension attribution is resolved
by SQL against already stored core rows, so a duplicate core key appearing at
the very end of the core file still invalidates every row carrying that key
without rescanning or buffering extension data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .descriptor import parse_archive
from .errors import DescriptorError
from .models import (
    REASON_AMBIGUOUS_REF,
    REASON_BAD_CORE_REF,
    REASON_DUPLICATE_KEY,
    REASON_EMPTY_REF,
    REASON_MISSING_REF,
    REASON_SHORT_ROW,
    ArchiveSpec,
    TableSpec,
)
from .parser import iter_records
from .schema import SCHEMA_SQL, SCHEMA_VERSION

# Every table is inserted in small batches; one batch is the largest amount
# of record text ever held on the Python side at once.  Tuned so peak text
# buffering stays in the low MiBs even with wide rows.
INSERT_BATCH = 2000


@dataclass
class TableCount:
    table_id: str
    member: str
    row_type: str
    input_count: int
    valid_count: int
    quarantined_count: int


@dataclass
class ImportResult:
    db_path: str
    tables: list[TableCount]

    def format_report(self) -> str:
        lines = [
            "table_id                            input  valid  quarantined"
        ]
        for t in self.tables:
            lines.append(
                f"{t.table_id:<34} {t.input_count:>5} {t.valid_count:>6} "
                f"{t.quarantined_count:>12}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connection / schema setup
# ---------------------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    # Single-writer import; these pragmas keep the disk-backed staging work
    # fast without changing the memory boundary.
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")  # ~20 MiB page cache
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _store_metadata(conn: sqlite3.Connection, spec: ArchiveSpec,
                    source_zip: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("source_zip", os.path.basename(source_zip)),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("imported_at_utc", datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "INSERT INTO descriptor_xml(id, xml) VALUES (1, ?)",
        (spec.xml_bytes,),
    )
    for ordinal, table in enumerate(spec.all_tables()):
        conn.execute(
            "INSERT INTO tables "
            "(table_id, ordinal, kind, row_type, member, encoding, "
            "fields_terminated_by, lines_terminated_by, fields_enclosed_by, "
            "ignore_header_lines) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                table.table_id, ordinal,
                "core" if table.is_core else "extension", table.row_type,
                table.member, table.encoding, table.fields_terminated_by,
                table.lines_terminated_by, table.fields_enclosed_by,
                table.ignore_header_lines,
            ),
        )
        order = 0
        map_rows = []
        if table.id_field is not None:
            map_rows.append((
                table.table_id, order, "id", "", table.id_field.index, None,
            ))
            order += 1
        if table.coreid_field is not None:
            map_rows.append((
                table.table_id, order, "coreid", "",
                table.coreid_field.index, None,
            ))
            order += 1
        for f in table.fields:
            map_rows.append((
                table.table_id, order, "field", f.term, f.index, f.default,
            ))
            order += 1
        conn.executemany(
            "INSERT INTO field_maps "
            "(table_id, column_order, role, term, col_index, default_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            map_rows,
        )


# ---------------------------------------------------------------------------
# Phase 1: core table
# ---------------------------------------------------------------------------

def _import_core(conn: sqlite3.Connection, zf: zipfile.ZipFile,
                 spec: TableSpec) -> None:
    """Stream core rows; detect duplicate keys while rows flow to disk."""
    pending_rows: list[tuple] = []
    pending_cells: list[tuple] = []
    # Bookkeeping only: distinct key strings and row ids, never row texts.
    seen_keys: set[str] = set()
    ambiguous: set[str] = set()
    # (row_id, key or "") for rows flagged short; evidence stays on disk.
    short_rows: list[tuple[int, str]] = []
    empty_rows: list[int] = []
    input_count = 0
    cur = conn.cursor()
    next_id = (cur.execute(
        "SELECT COALESCE(MAX(row_id), 0) FROM core_rows"
    ).fetchone()[0]) + 1

    def flush() -> None:
        if pending_rows:
            cur.executemany(
                "INSERT INTO core_rows "
                "(row_id, table_id, member, record_number, core_id, "
                "raw_cells_json, is_valid) VALUES (?, ?, ?, ?, ?, ?, 1)",
                pending_rows,
            )
            pending_rows.clear()
        if pending_cells:
            cur.executemany(
                "INSERT INTO core_cells "
                "(core_row_id, term, value, origin) VALUES (?, ?, ?, ?)",
                pending_cells,
            )
            pending_cells.clear()

    for record in iter_records(zf, spec):
        input_count += 1
        key = record.row_key if record.row_key is not None else ""
        row_id = next_id
        next_id += 1
        pending_rows.append((
            row_id, spec.table_id, spec.member, record.record_number, key,
            json.dumps(list(record.raw_cells), ensure_ascii=False),
        ))
        if len(pending_rows) >= INSERT_BATCH:
            flush()
        pending_cells.extend(
            (row_id, term, cell.value, cell.origin)
            for term, cell in record.cells.items()
        )
        if len(pending_cells) >= INSERT_BATCH:
            flush()

        if record.is_short:
            # Even a short row with a parseable non-empty id participates in
            # duplicate detection; its raw cells are already staged on disk.
            short_rows.append((row_id, key))
        if key == "":
            empty_rows.append(row_id)
            continue
        if key in seen_keys:
            # Marking happens after the final flush: rows may still be in a
            # pending batch, so an in-loop UPDATE would miss them.
            ambiguous.add(key)
        else:
            seen_keys.add(key)

    flush()

    # One relational pass invalidates every row carrying an ambiguous key,
    # including the duplicates still held in the last pending batch.
    if ambiguous:
        placeholders = ",".join("?" for _ in ambiguous)
        cur.execute(
            f"UPDATE core_rows SET is_valid = 0 "
            f"WHERE core_id IN ({placeholders})",
            tuple(ambiguous),
        )

    # Quarantine empty-key rows.
    for row_id in empty_rows:
        cur.execute(
            "UPDATE core_rows SET is_valid = 0 WHERE row_id = ?", (row_id,)
        )
        cur.execute(
            "INSERT INTO quarantine "
            "(table_id, member, record_number, key_value, reasons_json, "
            "raw_cells_json, data_row_pk) "
            "SELECT table_id, member, record_number, NULL, ?, "
            "raw_cells_json, row_id FROM core_rows WHERE row_id = ?",
            (json.dumps(["empty_key"]), row_id),
        )

    # Quarantine short rows; a short row may simultaneously be a duplicate
    # key, in which case both reasons are retained.
    for row_id, key in short_rows:
        reasons = [REASON_SHORT_ROW]
        if key in ambiguous:
            reasons.insert(0, REASON_DUPLICATE_KEY)
        cur.execute(
            "UPDATE core_rows SET is_valid = 0 WHERE row_id = ?", (row_id,)
        )
        cur.execute(
            "INSERT INTO quarantine "
            "(table_id, member, record_number, key_value, reasons_json, "
            "raw_cells_json, data_row_pk) "
            "SELECT table_id, member, record_number, ?, ?, "
            "raw_cells_json, row_id FROM core_rows WHERE row_id = ?",
            (
                key if key != "" else None,
                json.dumps(reasons),
                row_id,
            ),
        )

    # Duplicate rows that were not short still need a quarantine entry.
    # Their original raw cells are staged on the core row itself.
    if ambiguous:
        placeholders = ",".join("?" for _ in ambiguous)
        cur.execute(
            f"INSERT INTO quarantine "
            f"(table_id, member, record_number, key_value, reasons_json, "
            f"raw_cells_json, data_row_pk) "
            f"SELECT table_id, member, record_number, core_id, ?, "
            f"raw_cells_json, row_id FROM core_rows "
            f"WHERE core_id IN ({placeholders}) AND row_id NOT IN "
            f"(SELECT data_row_pk FROM quarantine "
            f"WHERE data_row_pk IS NOT NULL)",
            (json.dumps([REASON_DUPLICATE_KEY]), *ambiguous),
        )

    cur.execute(
        "UPDATE tables SET input_count = ? WHERE table_id = ?",
        (input_count, spec.table_id),
    )


# ---------------------------------------------------------------------------
# Phase 2: extensions, attribution by relational query
# ---------------------------------------------------------------------------

def _import_extension(conn: sqlite3.Connection, zf: zipfile.ZipFile,
                      spec: TableSpec) -> None:
    pending_rows: list[tuple] = []
    pending_cells: list[tuple] = []
    input_count = 0
    cur = conn.cursor()
    next_id = (cur.execute(
        "SELECT COALESCE(MAX(row_id), 0) FROM ext_rows"
    ).fetchone()[0]) + 1

    def flush() -> None:
        if pending_rows:
            cur.executemany(
                "INSERT INTO ext_rows "
                "(row_id, table_id, member, record_number, coreid_value, "
                "raw_cells_json, is_valid) VALUES (?, ?, ?, ?, ?, ?, 1)",
                pending_rows,
            )
            pending_rows.clear()
        if pending_cells:
            cur.executemany(
                "INSERT INTO ext_cells "
                "(ext_row_id, term, value, origin) VALUES (?, ?, ?, ?)",
                pending_cells,
            )
            pending_cells.clear()

    # Only ids are retained in Python; raw cells are staged on disk.
    short_rows: list[int] = []

    for record in iter_records(zf, spec):
        input_count += 1
        coreid = record.row_key if record.row_key is not None else ""
        row_id = next_id
        next_id += 1
        pending_rows.append((
            row_id, spec.table_id, spec.member, record.record_number,
            coreid,
            json.dumps(list(record.raw_cells), ensure_ascii=False),
        ))
        if len(pending_rows) >= INSERT_BATCH:
            flush()
        pending_cells.extend(
            (row_id, term, cell.value, cell.origin)
            for term, cell in record.cells.items()
        )
        if len(pending_cells) >= INSERT_BATCH:
            flush()
        if record.is_short:
            short_rows.append(row_id)

    flush()
    cur.execute(
        "UPDATE tables SET input_count = ? WHERE table_id = ?",
        (input_count, spec.table_id),
    )

    # --- relational attribution -------------------------------------------
    # Every reference predicate copies the staged raw cells from ext_rows,
    # so quarantined rows never lose their original values.
    # Empty reference.
    cur.execute(
        "INSERT INTO quarantine "
        "(table_id, member, record_number, key_value, reasons_json, "
        "raw_cells_json, data_row_pk) "
        "SELECT table_id, member, record_number, NULL, ?, "
        "raw_cells_json, row_id "
        "FROM ext_rows WHERE table_id = ? AND coreid_value = ''",
        (json.dumps([REASON_EMPTY_REF]), spec.table_id),
    )
    # Missing reference: no core row with this exact decoded id.
    cur.execute(
        "INSERT INTO quarantine "
        "(table_id, member, record_number, key_value, reasons_json, "
        "raw_cells_json, data_row_pk) "
        "SELECT e.table_id, e.member, e.record_number, e.coreid_value, "
        "?, e.raw_cells_json, e.row_id FROM ext_rows e "
        "WHERE e.table_id = ? AND e.coreid_value <> '' "
        "AND NOT EXISTS (SELECT 1 FROM core_rows c "
        "WHERE c.core_id = e.coreid_value)",
        (json.dumps([REASON_MISSING_REF]), spec.table_id),
    )
    # Ambiguous reference: id exists only on duplicate-key (invalid) core
    # rows, i.e. more than one core row carries it.
    cur.execute(
        "INSERT INTO quarantine "
        "(table_id, member, record_number, key_value, reasons_json, "
        "raw_cells_json, data_row_pk) "
        "SELECT e.table_id, e.member, e.record_number, e.coreid_value, "
        "?, e.raw_cells_json, e.row_id FROM ext_rows e "
        "WHERE e.table_id = ? AND e.coreid_value <> '' AND EXISTS ("
        "SELECT 1 FROM core_rows c WHERE c.core_id = e.coreid_value) "
        "AND (SELECT COUNT(*) FROM core_rows c "
        "WHERE c.core_id = e.coreid_value) > 1",
        (json.dumps([REASON_AMBIGUOUS_REF]), spec.table_id),
    )
    # Bad-core reference: exactly one matching core row, itself invalid
    # (e.g. short row) but not a duplicate-key case.
    cur.execute(
        "INSERT INTO quarantine "
        "(table_id, member, record_number, key_value, reasons_json, "
        "raw_cells_json, data_row_pk) "
        "SELECT e.table_id, e.member, e.record_number, e.coreid_value, "
        "?, e.raw_cells_json, e.row_id FROM ext_rows e "
        "WHERE e.table_id = ? AND e.coreid_value <> '' AND 1 = ("
        "SELECT COUNT(*) FROM core_rows c WHERE c.core_id = e.coreid_value) "
        "AND EXISTS (SELECT 1 FROM core_rows c "
        "WHERE c.core_id = e.coreid_value AND c.is_valid = 0)",
        (json.dumps([REASON_BAD_CORE_REF]), spec.table_id),
    )

    # Valid attributions get their link; order of SQL writes does not affect
    # read order, which always comes from record_number.
    cur.execute(
        "UPDATE ext_rows SET core_row_id = ("
        "SELECT c.row_id FROM core_rows c "
        "WHERE c.core_id = ext_rows.coreid_value AND c.is_valid = 1) "
        "WHERE table_id = ? AND coreid_value <> '' AND row_id NOT IN ("
        "SELECT data_row_pk FROM quarantine "
        "WHERE table_id = ? AND data_row_pk IS NOT NULL)",
        (spec.table_id, spec.table_id),
    )
    cur.execute(
        "UPDATE ext_rows SET is_valid = 0 WHERE table_id = ? AND row_id IN ("
        "SELECT data_row_pk FROM quarantine "
        "WHERE table_id = ? AND data_row_pk IS NOT NULL)",
        (spec.table_id, spec.table_id),
    )

    # Short extension rows additionally carry the short_row reason.  Their
    # raw cells were staged on ext_rows and copied into quarantine above.
    for row_id in short_rows:
        reasons_row = cur.execute(
            "SELECT reasons_json FROM quarantine WHERE data_row_pk = ?",
            (row_id,),
        ).fetchone()
        if reasons_row is None:
            # A short row whose reference itself is fine: the short row is
            # still invalid.
            cur.execute(
                "UPDATE ext_rows SET is_valid = 0 WHERE row_id = ?",
                (row_id,),
            )
            cur.execute(
                "INSERT INTO quarantine "
                "(table_id, member, record_number, key_value, reasons_json, "
                "raw_cells_json, data_row_pk) "
                "SELECT table_id, member, record_number, "
                "NULLIF(coreid_value, ''), ?, raw_cells_json, row_id "
                "FROM ext_rows WHERE row_id = ?",
                (json.dumps([REASON_SHORT_ROW]), row_id),
            )
            continue
        reasons = json.loads(reasons_row[0])
        if REASON_SHORT_ROW not in reasons:
            reasons.append(REASON_SHORT_ROW)
            cur.execute(
                "UPDATE quarantine SET reasons_json = ? "
                "WHERE data_row_pk = ?",
                (json.dumps(reasons), row_id),
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _finalize_counts(conn: sqlite3.Connection) -> list[TableCount]:
    counts: list[TableCount] = []
    rows = conn.execute(
        "SELECT t.table_id, t.member, t.row_type, t.input_count, "
        "(SELECT COUNT(*) FROM core_rows cr WHERE cr.table_id = t.table_id "
        "AND cr.is_valid = 1) + "
        "(SELECT COUNT(*) FROM ext_rows er WHERE er.table_id = t.table_id "
        "AND er.is_valid = 1) AS valid_n, "
        "(SELECT COUNT(*) FROM quarantine q WHERE q.table_id = t.table_id) "
        "AS quar_n FROM tables t ORDER BY t.ordinal"
    ).fetchall()
    for table_id, member, row_type, input_n, valid_n, quar_n in rows:
        counts.append(
            TableCount(table_id, member, row_type, input_n, valid_n, quar_n)
        )
    return counts


def import_archive(zip_path: str, db_path: str) -> ImportResult:
    """Import ``zip_path`` into a brand-new SQLite file at ``db_path``.

    Raises :class:`DescriptorError` for unsupported/invalid descriptors and
    refuses to overwrite an existing target.  On failure the partial database
    file is removed, so no half-imported library remains.
    """
    if os.path.exists(db_path):
        raise FileExistsError(
            f"refusing to overwrite existing target database: {db_path}"
        )

    conn: Optional[sqlite3.Connection] = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Hard guard against any path-like member surprise; descriptor
            # validation later re-checks every declared location.
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.startswith("/") or ".." in info.filename.split("/"):
                    raise DescriptorError(
                        f"unsafe ZIP member name {info.filename!r}: absolute "
                        "paths and '..' segments are not read"
                    )
            try:
                xml_bytes = zf.read("meta.xml")
            except KeyError:
                raise DescriptorError(
                    "archive root is missing meta.xml"
                ) from None
            spec = parse_archive(zf, xml_bytes)

            conn = _connect(db_path)
            conn.executescript(SCHEMA_SQL)
            _store_metadata(conn, spec, zip_path)

            # Core first (members may appear in any order inside the ZIP).
            _import_core(conn, zf, spec.core)
            for ext_spec in spec.extensions:
                _import_extension(conn, zf, ext_spec)

            conn.execute(
                "UPDATE tables SET valid_count = ("
                "SELECT COUNT(*) FROM core_rows cr WHERE cr.table_id = "
                "tables.table_id AND cr.is_valid = 1) + ("
                "SELECT COUNT(*) FROM ext_rows er WHERE er.table_id = "
                "tables.table_id AND er.is_valid = 1)"
            )
            conn.execute(
                "UPDATE tables SET quarantined_count = ("
                "SELECT COUNT(*) FROM quarantine q "
                "WHERE q.table_id = tables.table_id)"
            )
            conn.commit()
            result = ImportResult(db_path, _finalize_counts(conn))
            conn.close()
            return result
    except Exception:
        if conn is not None:
            conn.close()
        if os.path.exists(db_path):
            os.remove(db_path)
        raise
