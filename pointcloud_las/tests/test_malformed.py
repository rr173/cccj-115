"""Rejection of compressed, unsupported, lying and truncated LAS files."""

from __future__ import annotations

import struct

import pytest

from materials import (
    TestPoint,
    add_laz_marker_vlr,
    append_garbage,
    build_las,
    lie_about_extents,
    lie_about_point_count,
    patch_bytes,
    set_compression_bit,
    set_point_format,
    set_version,
    truncate_tail,
)
from pointcloud_las import IngestionError


def _good_path(tmp_path, name="ok.las", **kw):
    path = str(tmp_path / name)
    points = kw.pop("points", [TestPoint(x=1, y=2, z=3), TestPoint(x=4, y=5, z=6)])
    build_las(path, points, point_format=kw.pop("point_format", 0), **kw)
    return path


def test_truncated_payload_rejected_and_leaves_nothing(noref_lib, tmp_path):
    path = _good_path(tmp_path, points=[TestPoint(x=i, y=i, z=i) for i in range(5)])
    truncate_tail(path, 12)  # remove part of one 20-byte record
    with pytest.raises(IngestionError, match="truncated"):
        noref_lib.ingest_file(path)
    assert noref_lib.sources() == []
    assert noref_lib.count() == 0


def test_trailing_bytes_rejected(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    append_garbage(path)
    with pytest.raises(IngestionError, match="trailing bytes"):
        noref_lib.ingest_file(path)
    assert noref_lib.sources() == []


def test_header_smaller_than_fixed_size(noref_lib, tmp_path):
    path = str(tmp_path / "tiny.las")
    with open(path, "wb") as fh:
        fh.write(b"LASF" + b"\x00" * 10)
    with pytest.raises(IngestionError, match="smaller than a LAS 1.2"):
        noref_lib.ingest_file(path)


def test_bad_signature(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    patch_bytes(path, 0, b"NOPE")
    with pytest.raises(IngestionError, match="LASF"):
        noref_lib.ingest_file(path)


def test_unsupported_version(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    set_version(path, 1, 4)
    with pytest.raises(IngestionError, match="unsupported LAS version"):
        noref_lib.ingest_file(path)
    set_version(path, 1, 2)
    set_version(path, 2, 0)
    with pytest.raises(IngestionError, match="unsupported LAS version"):
        noref_lib.ingest_file(path)


def test_unsupported_point_format(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    set_point_format(path, 4)
    with pytest.raises(IngestionError, match="unsupported point format"):
        noref_lib.ingest_file(path)


def test_compression_bit_rejected(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    set_compression_bit(path)
    with pytest.raises(IngestionError, match="compressed"):
        noref_lib.ingest_file(path)


def test_laszip_vlr_rejected(noref_lib, tmp_path):
    path = _good_path(tmp_path)
    add_laz_marker_vlr(path)
    with pytest.raises(IngestionError, match="LASzip"):
        noref_lib.ingest_file(path)


def test_record_length_too_small(noref_lib, tmp_path):
    path = _good_path(tmp_path, point_format=1)
    # pf1 requires 28 bytes; claim 20.
    with open(path, "r+b") as fh:
        fh.seek(105)
        fh.write(struct.pack("<H", 20))
    with pytest.raises(IngestionError, match="record length"):
        noref_lib.ingest_file(path)


def test_lying_point_count_rejected(noref_lib, tmp_path):
    path = _good_path(
        tmp_path,
        points=[TestPoint(x=i, y=i, z=i) for i in range(3)],
    )
    lie_about_point_count(path, 999)
    with pytest.raises(IngestionError, match="truncated point payload"):
        noref_lib.ingest_file(path)
    assert noref_lib.sources() == []


def test_lying_extents_rejected(noref_lib, tmp_path):
    path = _good_path(tmp_path, points=[TestPoint(x=10, y=10, z=10)])
    lie_about_extents(path, x_min=999.0, x_max=1000.0)
    with pytest.raises(IngestionError, match="header lies"):
        noref_lib.ingest_file(path)
    assert noref_lib.sources() == []


def test_reasonably_rounded_extents_accepted(noref_lib, tmp_path):
    # A scale that makes the physical extent a long decimal; laspy itself
    # rounds header doubles, and a 1-ULP tolerance must not reject it.
    path = str(tmp_path / "rounded.las")
    build_las(
        path, [TestPoint(x=1, y=1, z=1), TestPoint(x=3, y=3, z=3)],
        point_format=0, scales=(0.3, 0.3, 0.3),
    )
    result = noref_lib.ingest_file(path)
    assert result.ingested is True


def test_bad_file_remains_absent_after_good_file(noref_lib, tmp_path):
    good = _good_path(tmp_path, name="good.las")
    noref_lib.ingest_file(good)
    bad = _good_path(tmp_path, name="bad.las")
    truncate_tail(bad, 4)
    with pytest.raises(IngestionError):
        noref_lib.ingest_file(bad)
    assert len(noref_lib.sources()) == 1
    assert noref_lib.count() == 2


def test_missing_file(noref_lib, tmp_path):
    with pytest.raises(IngestionError, match="not found"):
        noref_lib.ingest_file(str(tmp_path / "nope.las"))
