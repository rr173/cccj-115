"""Dialect and parsing guarantees: encodings, delimiters, quoting, short
rows, BOM, reordered columns, defaults and embedded newlines."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwca import DwcaLibrary, import_archive  # noqa: E402
from dwca.models import ORIGIN_DEFAULT, ORIGIN_SOURCE, REASON_SHORT_ROW  # noqa: E402

from . import materials as M

MEAS_ID = "ext:" + M.MEAS


@pytest.fixture()
def dialect_db(tmp_path):
    archive = tmp_path / "dialect.zip"
    db = tmp_path / "dialect.db"
    M.build_dialect_archive(str(archive))
    import_archive(str(archive), str(db))
    return db


def test_bom_tab_crlf_and_reordered_columns(dialect_db):
    with DwcaLibrary.open(str(dialect_db)) as lib:
        cores = list(lib.iter_core())
        assert [c.core_id for c in cores] == ["D1", "D2"]
        d1 = cores[0]
        # columns reordered vs feature archive; mapping resolves by index
        assert d1.values[M.CAT_TERM] == "cat-d1"
        assert d1.values[M.OCC_TERM] == "OCC-D1"
        # unmapped column 3 never appears
        assert all("skip-me" not in v for v in d1.values.values())


def test_quoted_embedded_newline_is_one_logical_record(dialect_db):
    with DwcaLibrary.open(str(dialect_db)) as lib:
        d1 = lib.get_core("D1")
        assert d1.values[M.NOTE_A] == "line1\nline2"
        # logical record numbering ignores the embedded physical line break
        assert d1.record_number == 1
        d2 = lib.get_core("D2")
        assert d2.record_number == 2


def test_latin1_extension_and_empty_cell_default(dialect_db):
    with DwcaLibrary.open(str(dialect_db)) as lib:
        d1 = lib.get_core("D1")
        ext = d1.extensions[MEAS_ID]
        assert len(ext) == 1
        # 0xE9 decoded as Latin-1, not as a UTF-8 error
        assert ext[0].values[M.MEAS_TERM] == "café"
        d2 = lib.get_core("D2")
        ext2 = d2.extensions[MEAS_ID]
        assert ext2[0].values[M.MEAS_TERM] == "unknown-measurement"
        assert ext2[0].origins[M.MEAS_TERM] == ORIGIN_DEFAULT
        assert ext2[0].origins[M.MEAS_VAL] == ORIGIN_SOURCE


def test_short_row_quarantined_without_silent_empty_fill(dialect_db):
    with DwcaLibrary.open(str(dialect_db)) as lib:
        q = list(lib.iter_quarantine("core"))
        assert len(q) == 1
        assert q[0].record_number == 3
        assert q[0].reasons == [REASON_SHORT_ROW]
        assert q[0].raw_cells == ["D3"]
        assert q[0].key_value == "D3"


def test_header_lines_are_physical(tmp_path):
    # Two physical header lines, including unquoted junk that must not be
    # parsed as data.
    archive = tmp_path / "h.zip"
    db = tmp_path / "h.db"
    data = b'junk header line one\nsecond physical header\nK1,a\n'
    meta = M.META.format(
        core=M.core_xml(
            "data/core.csv", [M.field_xml(M.OCC_TERM, 1)], 0, header=2
        ),
        extensions="",
    )
    M.write_zip(str(archive), [("data/core.csv", data)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        cores = list(lib.iter_core())
        assert [c.core_id for c in cores] == ["K1"]
        assert cores[0].record_number == 1


def test_escaped_tab_and_newline_in_descriptor(tmp_path):
    # Descriptor uses backslash escapes; data uses real tab/LF.
    archive = tmp_path / "e.zip"
    db = tmp_path / "e.db"
    data = M.csv_bytes(
        [["id", M.OCC_TERM], ["K1", "a b"]], delim="\t", line="\n"
    )
    meta = M.META.format(
        core=M.core_xml(
            "core.csv", [M.field_xml(M.OCC_TERM, 1)], 0,
            delim="\\t", line="\\n"),
        extensions="",
    )
    M.write_zip(str(archive), [("core.csv", data)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        assert lib.get_core("K1").values[M.OCC_TERM] == "a b"


def test_quoted_delimiter_and_doubled_quote(tmp_path):
    archive = tmp_path / "q.zip"
    db = tmp_path / "q.db"
    rows = [
        ["id", M.OCC_TERM],
        ["K1", 'she said "hi", loudly'],  # quote escape + embedded comma
    ]
    data = M.csv_bytes(rows)
    meta = M.META.format(
        core=M.core_xml("core.csv", [M.field_xml(M.OCC_TERM, 1)], 0),
        extensions="",
    )
    M.write_zip(str(archive), [("core.csv", data)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        assert lib.get_core("K1").values[M.OCC_TERM] == 'she said "hi", loudly'


def test_utf8_bom_round_trip(tmp_path):
    archive = tmp_path / "bom.zip"
    db = tmp_path / "bom.db"
    data = M.csv_bytes([["id", M.OCC_TERM], ["K1", "Ωμέγα"]])
    data = data.decode("utf-8").encode("utf-8-sig")
    meta = M.META.format(
        core=M.core_xml("core.csv", [M.field_xml(M.OCC_TERM, 1)], 0,
                        encoding="UTF-8"),
        extensions="",
    )
    M.write_zip(str(archive), [("core.csv", data)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        assert lib.get_core("K1").values[M.OCC_TERM] == "Ωμέγα"


def test_bom_without_header_skip_does_not_pollute_first_key(tmp_path):
    # ignoreHeaderLines=0: the BOM would otherwise land inside the first id
    # cell; it must be stripped so the key is exactly "K1".
    archive = tmp_path / "bom0.zip"
    db = tmp_path / "bom0.db"
    data = M.csv_bytes([["K1", "a"]]).decode("utf-8").encode("utf-8-sig")
    meta = M.META.format(
        core=M.core_xml(
            "core.csv", [M.field_xml(M.OCC_TERM, 1)], 0, header=0
        ),
        extensions="",
    )
    M.write_zip(str(archive), [("core.csv", data)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        rec = lib.get_core("K1")
        assert rec is not None and rec.record_number == 1
        assert rec.values[M.OCC_TERM] == "a"
