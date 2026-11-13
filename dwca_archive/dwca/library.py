"""Read facade over an imported DwC-A SQLite database.

Every method streams through server-side cursors: opening a large archive and
walking its core records never loads the record texts of the whole archive
into Python memory.  Extension rows are returned *grouped per core record*,
never as a wide Cartesian-product join.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator, Optional

from .importer import TableCount, import_archive
from .models import (
    CoreRecord,
    ExtensionRecord,
    QuarantineRecord,
)

FETCH_SIZE = 500


class DwcaLibrary:
    """Iterable, read-only access to one imported archive."""

    def __init__(self, conn: sqlite3.Connection, db_path: str):
        self._conn = conn
        self._db_path = db_path
        conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ open
    @classmethod
    def open(cls, db_path: str) -> "DwcaLibrary":
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA query_only = ON")
        try:
            conn.execute("SELECT 1 FROM tables LIMIT 1").fetchone()
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return cls(conn, db_path)

    @classmethod
    def import_archive(cls, zip_path: str, db_path: str) -> "DwcaLibrary":
        import_archive(zip_path, db_path)
        return cls.open(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DwcaLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------- metadata
    def table_counts(self) -> list[TableCount]:
        rows = self._conn.execute(
            "SELECT table_id, member, row_type, input_count, valid_count, "
            "quarantined_count FROM tables ORDER BY ordinal"
        ).fetchall()
        return [
            TableCount(
                r["table_id"], r["member"], r["row_type"],
                r["input_count"], r["valid_count"], r["quarantined_count"],
            )
            for r in rows
        ]

    def table_ids(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT table_id FROM tables ORDER BY ordinal"
            )
        ]

    def extension_table_ids(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT table_id FROM tables WHERE kind = 'extension' "
                "ORDER BY ordinal"
            )
        ]

    def field_maps(self, table_id: str) -> list[dict]:
        """Parsed field mapping as stored (role, full-term URI, index,
        default)."""
        rows = self._conn.execute(
            "SELECT column_order, role, term, col_index, default_value "
            "FROM field_maps WHERE table_id = ? ORDER BY column_order",
            (table_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def descriptor_xml(self) -> bytes:
        return self._conn.execute(
            "SELECT xml FROM descriptor_xml WHERE id = 1"
        ).fetchone()[0]

    def meta(self) -> dict[str, str]:
        return dict(
            self._conn.execute("SELECT key, value FROM meta").fetchall()
        )

    # ----------------------------------------------------------- valid data
    def _load_cells(self, row_table: str, row_id: int) -> tuple[dict, dict]:
        if row_table == "core":
            sql = (
                "SELECT term, value, origin FROM core_cells "
                "WHERE core_row_id = ? ORDER BY rowid"
            )
        else:
            sql = (
                "SELECT term, value, origin FROM ext_cells "
                "WHERE ext_row_id = ? ORDER BY rowid"
            )
        values: dict[str, str] = {}
        origins: dict[str, str] = {}
        for r in self._conn.execute(sql, (row_id,)):
            values[r["term"]] = r["value"]
            origins[r["term"]] = r["origin"]
        return values, origins

    def _load_cells_bulk(self, row_table: str,
                         row_ids: list[int]) -> dict[int, tuple[dict, dict]]:
        """Load term cells for many rows in one query (avoids N+1 scans)."""
        if not row_ids:
            return {}
        if row_table == "core":
            sql = (
                "SELECT core_row_id AS rid, term, value, origin "
                "FROM core_cells WHERE core_row_id IN ("
                + ",".join("?" for _ in row_ids) + ") ORDER BY rowid"
            )
        else:
            sql = (
                "SELECT ext_row_id AS rid, term, value, origin "
                "FROM ext_cells WHERE ext_row_id IN ("
                + ",".join("?" for _ in row_ids) + ") ORDER BY rowid"
            )
        out: dict[int, tuple[dict, dict]] = {
            rid: ({}, {}) for rid in row_ids
        }
        for r in self._conn.execute(sql, row_ids):
            values, origins = out[r["rid"]]
            values[r["term"]] = r["value"]
            origins[r["term"]] = r["origin"]
        return out

    def _load_extensions_bulk(
        self, core_row_ids: list[int]
    ) -> dict[int, dict[str, list[ExtensionRecord]]]:
        """All valid extension rows for a core batch, grouped per core and
        per extension table, using one query per extension table."""
        grouped: dict[int, dict[str, list[ExtensionRecord]]] = {
            cid: {tid: [] for tid in self.extension_table_ids()}
            for cid in core_row_ids
        }
        if not core_row_ids:
            return grouped
        placeholders = ",".join("?" for _ in core_row_ids)
        for tid in self.extension_table_ids():
            rows = self._conn.execute(
                "SELECT row_id, core_row_id, table_id, member, "
                "record_number, coreid_value FROM ext_rows "
                "WHERE is_valid = 1 AND table_id = ? AND core_row_id IN ("
                + placeholders + ") ORDER BY record_number",
                [tid, *core_row_ids],
            ).fetchall()
            if not rows:
                continue
            cells_by_row = self._load_cells_bulk(
                "ext", [r["row_id"] for r in rows]
            )
            for r in rows:
                values, origins = cells_by_row[r["row_id"]]
                grouped[r["core_row_id"]][tid].append(ExtensionRecord(
                    table_id=r["table_id"], member=r["member"],
                    record_number=r["record_number"],
                    core_id=r["coreid_value"],
                    values=values, origins=origins,
                ))
        return grouped

    def iter_core(self) -> Iterator[CoreRecord]:
        """Yield valid core rows in source order, each with extension rows
        grouped by extension table (empty groups are present explicitly)."""
        ext_tables = self.extension_table_ids()
        cur = self._conn.execute(
            "SELECT row_id, table_id, member, record_number, core_id "
            "FROM core_rows WHERE is_valid = 1 ORDER BY record_number"
        )
        while True:
            batch = cur.fetchmany(FETCH_SIZE)
            if not batch:
                break
            core_cells = self._load_cells_bulk(
                "core", [r["row_id"] for r in batch]
            )
            ext_groups = self._load_extensions_bulk(
                [r["row_id"] for r in batch]
            )
            for r in batch:
                values, origins = core_cells[r["row_id"]]
                yield CoreRecord(
                    table_id=r["table_id"],
                    member=r["member"],
                    record_number=r["record_number"],
                    core_id=r["core_id"],
                    values=values,
                    origins=origins,
                    extensions=ext_groups[r["row_id"]],
                )

    def get_core(self, core_id: str) -> Optional[CoreRecord]:
        """Exact, decoded-string lookup; two ids differing by whitespace or
        case never match."""
        r = self._conn.execute(
            "SELECT row_id, table_id, member, record_number, core_id "
            "FROM core_rows WHERE is_valid = 1 AND core_id = ?",
            (core_id,),
        ).fetchone()
        if r is None:
            return None
        values, origins = self._load_cells("core", r["row_id"])
        groups = {
            tid: list(self.iter_extensions(tid, r["row_id"]))
            for tid in self.extension_table_ids()
        }
        return CoreRecord(
            table_id=r["table_id"], member=r["member"],
            record_number=r["record_number"], core_id=r["core_id"],
            values=values, origins=origins, extensions=groups,
        )

    def iter_extensions(
        self, table_id: str, core_row_id: Optional[int] = None
    ) -> Iterator[ExtensionRecord]:
        """Valid, attributed extension rows of one table in source order."""
        sql = (
            "SELECT row_id, table_id, member, record_number, coreid_value, "
            "core_row_id FROM ext_rows WHERE is_valid = 1 AND table_id = ?"
        )
        params: list = [table_id]
        if core_row_id is not None:
            sql += " AND core_row_id = ?"
            params.append(core_row_id)
        sql += " ORDER BY record_number"
        cur = self._conn.execute(sql, params)
        while True:
            batch = cur.fetchmany(FETCH_SIZE)
            if not batch:
                break
            for r in batch:
                values, origins = self._load_cells("ext", r["row_id"])
                yield ExtensionRecord(
                    table_id=r["table_id"], member=r["member"],
                    record_number=r["record_number"],
                    core_id=r["coreid_value"],
                    values=values, origins=origins,
                )

    # ---------------------------------------------------------- quarantine
    def _quarantine_cells(self, table_id: str, data_pk) -> tuple[dict, dict]:
        """Term values/origins of a quarantined row, resolved by table kind."""
        if table_id == "core":
            sql = (
                "SELECT term, value, origin FROM core_cells "
                "WHERE core_row_id = ? ORDER BY rowid"
            )
        else:
            sql = (
                "SELECT term, value, origin FROM ext_cells "
                "WHERE ext_row_id = ? ORDER BY rowid"
            )
        if data_pk is None:
            return {}, {}
        values, origins = {}, {}
        for r in self._conn.execute(sql, (data_pk,)):
            values[r["term"]] = r["value"]
            origins[r["term"]] = r["origin"]
        return values, origins

    def iter_quarantine(
        self, table_id: Optional[str] = None
    ) -> Iterator[QuarantineRecord]:
        sql = (
            "SELECT table_id, member, record_number, key_value, reasons_json, "
            "raw_cells_json, data_row_pk FROM quarantine"
        )
        params: list = []
        if table_id is not None:
            sql += " WHERE table_id = ?"
            params.append(table_id)
        sql += " ORDER BY table_id, record_number"
        cur = self._conn.execute(sql, params)
        while True:
            batch = cur.fetchmany(FETCH_SIZE)
            if not batch:
                break
            for r in batch:
                values, origins = self._quarantine_cells(
                    r["table_id"], r["data_row_pk"]
                )
                yield QuarantineRecord(
                    table_id=r["table_id"],
                    member=r["member"],
                    record_number=r["record_number"],
                    key_value=r["key_value"],
                    reasons=json.loads(r["reasons_json"]),
                    values=values,
                    origins=origins,
                    raw_cells=json.loads(r["raw_cells_json"]),
                )
