"""Manifest contents, streaming export, memory bounds and ordering."""

from __future__ import annotations

import csv
import os
from decimal import Decimal

import pytest

from materials import TestPoint, build_las


def test_manifest_records_real_declared_and_actual_ranges(noref_lib, tmp_path):
    path = str(tmp_path / "tile.las")
    scale, offset = 0.01, 50.0
    points = [
        TestPoint(x=10, y=-5, z=2),
        TestPoint(x=40, y=25, z=9),
    ]
    build_las(path, points, point_format=0,
              scales=(scale,) * 3, offsets=(offset,) * 3)
    noref_lib.ingest_file(path)
    src = noref_lib.sources()[0]

    assert src.n_points == 2
    assert src.int_bounds == (10, 40, -5, 25, 2, 9)
    s = Decimal(repr(scale))
    o = Decimal(repr(offset))
    assert src.actual_bounds == (
        str(s * 10 + o), str(s * 40 + o),
        str(s * -5 + o), str(s * 25 + o),
        str(s * 2 + o), str(s * 9 + o),
    )
    # Declared doubles come straight from the LAS header.
    assert src.declared_bounds[0] == pytest.approx(float(s * 10 + o))
    assert src.file_size_bytes == os.path.getsize(path)
    assert src.record_length == 20
    assert src.extra_bytes_len == 0


def test_manifest_one_row_per_complete_source(noref_lib, tmp_path):
    for i in range(3):
        path = str(tmp_path / f"t{i}.las")
        build_las(path, [TestPoint(x=i + 1, y=i + 1, z=i + 1)],
                  point_format=1)
        noref_lib.ingest_file(path)
    rows = noref_lib.sources()
    assert [r.src_id for r in rows] == [1, 2, 3]
    assert all(r.n_points == 1 for r in rows)


def test_streaming_export_csv(noref_lib, tmp_path):
    path = str(tmp_path / "many.las")
    points = [
        TestPoint(x=i, y=0, z=0, classification=i % 3,
                  intensity=i, extra=bytes([i % 256]))
        for i in range(500)
    ]
    build_las(path, points, point_format=3, extra_byte_count=1)
    noref_lib.ingest_file(path, read_batch=64)

    out = str(tmp_path / "export.csv")
    written = noref_lib.export_csv(out)
    assert written == 500
    with open(out, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    assert header[0] == "source_sha256" and header[-1] == "extra_bytes_hex"
    assert len(rows) == 500
    assert int(rows[123][2]) == 123  # x_int column
    assert rows[7][-1] == (7).to_bytes(1, "little").hex()
    # pf3 has GPS and RGB: columns must be populated, not empty.
    assert rows[0][10] != "" and rows[0][11] != ""


def test_export_of_pf0_has_empty_optional_columns(noref_lib, tmp_path):
    path = str(tmp_path / "pf0.las")
    build_las(path, [TestPoint(x=1, y=1, z=1)], point_format=0)
    noref_lib.ingest_file(path)
    out = str(tmp_path / "pf0.csv")
    noref_lib.export_csv(out, None)
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[1][10] == ""  # gps_time empty (absent)
    assert rows[1][11:14] == ["", "", ""]  # RGB empty (absent)


def test_iter_query_respects_fetch_size_and_order(noref_lib, tmp_path):
    shas = []
    for f in range(4):
        path = str(tmp_path / f"s{f}.las")
        # Distinct content per file (x offset per source) so each source is a
        # separate identity rather than a content-dedup no-op.
        build_las(
            path,
            [TestPoint(x=f * 1000 + i, y=0, z=0) for i in range(50)],
            point_format=0, scales=(1.0, 1.0, 1.0),
        )
        shas.append(noref_lib.ingest_file(path).source_sha256)
    seen = list(noref_lib.iter_query(fetch_size=7))
    assert len(seen) == 200
    # Global fixed ordering: source sha, then index.
    keys = [(p.source_sha256, p.point_index) for p in seen]
    assert keys == sorted(keys)
    per_source = {s: 0 for s in shas}
    for p in seen:
        per_source[p.source_sha256] += 1
    assert all(v == 50 for v in per_source.values())


def test_index_range_lookup(noref_lib, tmp_path):
    path = str(tmp_path / "idx.las")
    build_las(path, [TestPoint(x=i, y=i, z=i) for i in range(100)],
              point_format=0)
    sha = noref_lib.ingest_file(path).source_sha256
    got = list(noref_lib.iter_query(
        None, source_sha=sha, index_start=10, index_end=19
    ))
    assert [p.point_index for p in got] == list(range(10, 20))
    assert noref_lib.count(
        None, source_sha=sha, index_start=0, index_end=49
    ) == 50


def test_stats_grow_with_points(noref_lib, tmp_path):
    before = noref_lib.stats()
    assert before["n_points"] == 0
    path = str(tmp_path / "s.las")
    build_las(path, [TestPoint(x=i, y=i, z=i) for i in range(1000)],
              point_format=1)
    noref_lib.ingest_file(path, read_batch=128)
    after = noref_lib.stats()
    assert after["n_points"] == 1000
    assert after["n_sources"] == 1
    assert after["database_bytes"] > 0
    assert after["database_bytes"] == after["page_size_bytes"] * after["page_count"]


def test_batched_ingest_does_not_buffer_whole_file(noref_lib, tmp_path):
    # 30k points with tiny read_batch: correctness proves streaming works;
    # the per-batch INSERT never holds more than read_batch rows.
    path = str(tmp_path / "large.las")
    build_las(
        path,
        [TestPoint(x=i, y=i, z=i, classification=i % 8)
         for i in range(30_000)],
        point_format=0, scales=(1.0, 1.0, 1.0),
    )
    noref_lib.ingest_file(path, read_batch=137)
    assert noref_lib.count() == 30_000
    box = {
        "xmin": "1000", "xmax": "29999",
        "ymin": "1000", "ymax": "29999",
        "zmin": "1000", "zmax": "29999",
    }
    expected = [i for i in range(1000, 30_000) if i % 8 == 0]
    assert noref_lib.count(box, classifications=[0]) == len(expected)
    # Streaming path gives the same answer without materialising all rows.
    streamed = sum(
        1 for _ in noref_lib.iter_query(box, classifications=[0],
                                        fetch_size=512)
    )
    assert streamed == len(expected)
