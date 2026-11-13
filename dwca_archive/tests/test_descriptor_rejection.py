"""Explicit rejection of unsupported descriptors, encodings and paths."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwca import DescriptorError, import_archive  # noqa: E402

from . import materials as M


def _expect_reject(tmp_path, meta_xml, members=None):
    archive = tmp_path / "bad.zip"
    db = tmp_path / "bad.db"
    M.build_bad_descriptor_zip(str(archive), meta_xml, members)
    with pytest.raises(DescriptorError):
        import_archive(str(archive), str(db))
    # No partial database survives a rejected import.
    assert not os.path.exists(str(db))


def test_unsupported_encoding_rejected(tmp_path):
    _expect_reject(tmp_path, M.minimal_core_meta(encoding="UTF-16"))


def test_unsupported_delimiter_rejected(tmp_path):
    _expect_reject(tmp_path, M.minimal_core_meta(delim=";"))


def test_unsupported_line_terminator_rejected(tmp_path):
    _expect_reject(tmp_path, M.minimal_core_meta(line="\\r"))


def test_unsupported_quote_character_rejected(tmp_path):
    _expect_reject(tmp_path, M.minimal_core_meta(quote="'"))


def test_duplicate_field_term_rejected(tmp_path):
    fields = [
        M.field_xml(M.OCC_TERM, 1),
        M.field_xml(M.OCC_TERM, 2),
    ]
    _expect_reject(tmp_path, M.minimal_core_meta(fields=fields))


def test_field_without_index_or_default_rejected(tmp_path):
    _expect_reject(
        tmp_path,
        M.minimal_core_meta(fields=[M.field_xml(M.OCC_TERM)]),
    )


def test_negative_index_rejected(tmp_path):
    _expect_reject(
        tmp_path,
        M.minimal_core_meta(fields=[M.field_xml(M.OCC_TERM, -1)]),
    )


def test_non_integer_index_rejected(tmp_path):
    meta = M.META.format(
        core=(
            f'<core rowType="{M.OCC}" ignoreHeaderLines="1">'
            "<files><location>data/core.csv</location></files>"
            '<id index="zero"/>'
            f'{M.field_xml(M.OCC_TERM, 1)}</core>'
        ),
        extensions="",
    )
    _expect_reject(tmp_path, meta)


def test_missing_member_rejected(tmp_path):
    meta = M.minimal_core_meta(member="data/ghost.csv")
    _expect_reject(
        tmp_path, meta, members=[("data/other.csv", b"id\nK1\n")]
    )


def test_path_traversal_rejected(tmp_path):
    archive = tmp_path / "trav.zip"
    db = tmp_path / "trav.db"
    M.build_traversal_zip(str(archive))
    with pytest.raises(DescriptorError):
        import_archive(str(archive), str(db))
    assert not os.path.exists(str(db))


def test_absolute_member_rejected(tmp_path):
    meta = M.minimal_core_meta(member="/etc/passwd")
    _expect_reject(tmp_path, meta)


def test_directory_member_rejected(tmp_path):
    meta = M.minimal_core_meta(member="data/")
    _expect_reject(
        tmp_path, meta,
        members=[("data/", b""), ("data/core.csv", b"id,x\nK1,a\n")],
    )


def test_missing_meta_xml_rejected(tmp_path):
    archive = tmp_path / "nometa.zip"
    db = tmp_path / "nometa.db"
    M.write_zip(
        str(archive),
        [("data/core.csv", b"id,x\nK1,a\n")],
        meta_xml=None,
    )
    with pytest.raises(DescriptorError):
        import_archive(str(archive), str(db))


def test_non_xml_meta_rejected(tmp_path):
    _expect_reject(tmp_path, "<?xml not closed")


def test_non_occurrence_core_rejected(tmp_path):
    meta = M.META.format(
        core=(
            '<core rowType="http://rs.tdwg.org/dwc/terms/Taxon" '
            'ignoreHeaderLines="1">'
            "<files><location>data/core.csv</location></files>"
            '<id index="0"/>'
            f'{M.field_xml(M.OCC_TERM, 1)}</core>'
        ),
        extensions="",
    )
    _expect_reject(tmp_path, meta)


def test_two_locations_rejected(tmp_path):
    meta = M.META.format(
        core=(
            f'<core rowType="{M.OCC}" ignoreHeaderLines="1">'
            "<files><location>data/core.csv</location>"
            "<location>data/core2.csv</location></files>"
            '<id index="0"/>'
            f'{M.field_xml(M.OCC_TERM, 1)}</core>'
        ),
        extensions="",
    )
    _expect_reject(
        tmp_path, meta,
        members=[
            ("data/core.csv", b"id,x\nK1,a\n"),
            ("data/core2.csv", b"id,x\nK2,b\n"),
        ],
    )


def test_duplicate_extension_rowtype_rejected(tmp_path):
    ext = M.ext_xml(
        M.MEAS, "m.csv", [M.field_xml(M.MEAS_TERM, 1)], 0
    )
    meta = M.META.format(
        core=M.core_xml(
            "core.csv", [M.field_xml(M.OCC_TERM, 1)], 0),
        extensions=ext + ext,
    )
    _expect_reject(
        tmp_path, meta,
        members=[
            ("core.csv", b"id,x\nK1,a\n"),
            ("m.csv", b"coreid,t\nK1,w\n"),
        ],
    )


def test_bad_escape_rejected(tmp_path):
    _expect_reject(tmp_path, M.minimal_core_meta(delim="\\x"))
