"""Index plans, R-tree usage, no-R-tree fallback and exact re-check tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from materials import TestPoint, build_las
from pointcloud_las import PointCloudLibrary
from pointcloud_las.query import _build_execution, _row_matches_box
from pointcloud_las.schema import connect, create_library, get_meta


def _ingest_two_sources(lib, tmp_path):
    p1 = str(tmp_path / "a.las")
    p2 = str(tmp_path / "b.las")
    build_las(p1, [
        TestPoint(x=0, y=0, z=0),
        TestPoint(x=10, y=10, z=10),
        TestPoint(x=20, y=20, z=20),
    ], point_format=0, scales=(1.0, 1.0, 1.0))
    build_las(p2, [
        TestPoint(x=5, y=5, z=5),
        TestPoint(x=15, y=15, z=15),
    ], point_format=0, scales=(2.0, 2.0, 2.0))
    lib.ingest_file(p1, read_batch=2)
    lib.ingest_file(p2, read_batch=2)


def test_query_plan_uses_rtree_when_available(noref_lib, tmp_path):
    _ingest_two_sources(noref_lib, tmp_path)
    assert get_meta(noref_lib.connection, "rtree_available") == "1"
    bounds = {
        "xmin": "0", "xmax": "12",
        "ymin": "0", "ymax": "12",
        "zmin": "0", "zmax": "12",
    }
    plans = _build_execution(
        noref_lib.connection, bounds=bounds, classifications=None,
        source_sha=None, index_start=None, index_end=None, box_given=True,
    )
    assert plans, "at least one source plan expected"
    for sql, _params, *_ in plans:
        assert "JOIN points_rtree rt" in sql
        assert "BETWEEN ? AND ?" in sql
    plan = noref_lib.connection.execute(
        "EXPLAIN QUERY PLAN " + plans[0][0], plans[0][1]
    ).fetchall()
    joined = " ".join(row[3] for row in plan)
    # R-tree virtual tables appear as "SCAN <name> VIRTUAL TABLE INDEX n".
    assert "rt VIRTUAL TABLE" in joined


def test_library_without_rtree_falls_back_to_integer_scan(tmp_path):
    db_path = str(tmp_path / "nort.db")
    # Build normally, then drop the virtual table and flip the metadata flag.
    create_library(db_path, "none")
    conn = connect(db_path)
    conn.execute("DROP TABLE points_rtree")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('rtree_available', '0') "
        "ON CONFLICT(key) DO UPDATE SET value = '0'"
    )
    conn.commit()
    conn.close()

    with PointCloudLibrary(db_path) as lib:
        assert get_meta(lib.connection, "rtree_available") == "0"
        tile_dir = tmp_path / "tiles"
        tile_dir.mkdir()
        _ingest_two_sources(lib, tile_dir)
        box = {
            "xmin": "0", "xmax": "12",
            "ymin": "0", "ymax": "12",
            "zmin": "0", "zmax": "12",
        }
        plans = _build_execution(
            lib.connection, bounds=box, classifications=None,
            source_sha=None, index_start=None, index_end=None, box_given=True,
        )
        for sql, _params, *_ in plans:
            assert "points_rtree" not in sql
            assert "BETWEEN ? AND ?" in sql
        # Source 1 (scale 1): ints 0 and 10 inside, 20 outside.
        # Source 2 (scale 2): int 5 -> physical 10 inside, 15 -> 30 outside.
        got = lib.query(box)
        assert {(p.source_sha256, p.x_int) for p in got} == {
            (lib.sources()[0].source_sha256, 0),
            (lib.sources()[0].source_sha256, 10),
            (lib.sources()[1].source_sha256, 5),
        }
        assert lib.count(box) == 3


def test_row_matches_box_is_decimal_exact():
    scales = (Decimal("0.1"), Decimal("1"), Decimal("1"))
    offsets = (Decimal("0"), Decimal("0"), Decimal("0"))
    box = (
        Decimal("0"), Decimal("0.3"),
        Decimal("0"), Decimal("0"),
        Decimal("0"), Decimal("0"),
    )
    # Columns: sha, idx, x, y, z, intensity, class, flags, ret, nret,
    # gps, r, g, b, extra
    def row(x):
        return ("sha", 0, x, 0, 0, 0, 0, 0, 1, 1, None, None, None, None, b"")

    assert _row_matches_box(row(3), scales, offsets, box) is True
    assert _row_matches_box(row(4), scales, offsets, box) is False
    assert _row_matches_box(row(2), scales, offsets, box) is True


def test_integer_filter_is_coarse_but_decimal_recheck_decides(noref_lib, tmp_path):
    """Scale/offset combos where int candidates need decimal rejection.

    scale 0.1, offset 0.05: int 3 -> physical 0.35, int 2 -> 0.25.
    A box ending at 0.30 excludes 0.35; the integer ceiling/floor math keeps
    the query range tight and the decimal re-check makes the final decision.
    """
    path = str(tmp_path / "recheck.las")
    build_las(path, [
        TestPoint(x=2, y=0, z=0),
        TestPoint(x=3, y=0, z=0),
    ], point_format=0, scales=(0.1, 1.0, 1.0), offsets=(0.05, 0.0, 0.0))
    noref_lib.ingest_file(path)
    box = {
        "xmin": "0.2", "xmax": "0.3",
        "ymin": "0", "ymax": "0",
        "zmin": "0", "zmax": "0",
    }
    got = noref_lib.query(box)
    assert [p.x_int for p in got] == [2]
    assert noref_lib.count(box) == 1


def test_malformed_box_and_bad_sha_are_query_errors(noref_lib, tmp_path):
    path = str(tmp_path / "q.las")
    build_las(path, [TestPoint(x=1, y=1, z=1)], point_format=0,
              scales=(1.0, 1.0, 1.0))
    noref_lib.ingest_file(path)
    from pointcloud_las.errors import QueryError
    with pytest.raises(QueryError):
        noref_lib.count({"xmin": "oops", "xmax": "1",
                         "ymin": "0", "ymax": "1",
                         "zmin": "0", "zmax": "1"})
    with pytest.raises(QueryError):
        noref_lib.count({
            "xmin": "9", "xmax": "1",
            "ymin": "0", "ymax": "1",
            "zmin": "0", "zmax": "1",
        })
    with pytest.raises(QueryError):
        list(noref_lib.iter_query(None, source_sha="not-a-sha"))
    # Unknown but well-formed sha is simply an empty result, not an error.
    sha64 = "0" * 64
    assert list(noref_lib.iter_query(None, source_sha=sha64)) == []
    assert noref_lib.count(None, source_sha=sha64) == 0


def test_empty_classification_set_matches_nothing(noref_lib, tmp_path):
    path = str(tmp_path / "c.las")
    build_las(path, [
        TestPoint(x=1, y=1, z=1, classification=2),
        TestPoint(x=2, y=2, z=2, classification=3),
    ], point_format=0, scales=(1.0, 1.0, 1.0))
    noref_lib.ingest_file(path)
    assert noref_lib.count(classifications=[]) == 0
    assert list(noref_lib.iter_query(None, classifications=[])) == []
    # Streaming export of an empty match writes only the header.
    out = str(tmp_path / "empty.csv")
    assert noref_lib.export_csv(out, classifications=[]) == 0
    import csv
    with open(out, newline="", encoding="utf-8") as fh:
        assert len(list(csv.reader(fh))) == 1
