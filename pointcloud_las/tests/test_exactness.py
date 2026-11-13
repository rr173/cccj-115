"""Exact boundary arithmetic: scales, offsets, edge hits, one-unit misses."""

from __future__ import annotations

from decimal import Decimal

import pytest

from materials import TestPoint, build_las
from pointcloud_las.decimal_utils import (
    closed_int_bounds,
    float_to_decimal,
    parse_decimal,
    physical_value,
)
from pointcloud_las.errors import QueryError


def test_float_to_decimal_uses_shortest_roundtrip():
    assert float_to_decimal(0.1) == Decimal("0.1")
    assert float_to_decimal(1e-9) == Decimal("0.000000001")
    assert float_to_decimal(-273.15) == Decimal("-273.15")


def test_parse_decimal_accepts_strings_rejects_floats():
    assert parse_decimal("1.234", "x") == Decimal("1.234")
    assert parse_decimal("-0.0000001", "x") == Decimal("-0.0000001")
    assert parse_decimal("1E3", "x") == Decimal("1E3")
    for bad in ("", "  ", "nan", "inf", "-Infinity", "1.2.3", "0x10", "1 000"):
        with pytest.raises(QueryError):
            parse_decimal(bad, "x")
    with pytest.raises(QueryError):
        parse_decimal(1.5, "x")


def test_closed_bounds_rounding_examples():
    scale = Decimal("0.1")
    offset = Decimal("0")
    # physical 1.0 is integer 10 exactly; a box ending at 0.99 excludes it.
    assert closed_int_bounds(Decimal("0"), Decimal("0.99"), scale, offset) == (0, 9)
    # a box ending exactly at 1.0 keeps integer 10.
    assert closed_int_bounds(Decimal("0"), Decimal("1.0"), scale, offset) == (0, 10)
    # negative offset
    assert closed_int_bounds(
        Decimal("-1.0"), Decimal("-1.0"), scale, Decimal("-0.5")
    ) == (-5, -5)
    # interval between two adjacent representable values matches nothing
    assert closed_int_bounds(
        Decimal("0.95"), Decimal("0.99"), scale, offset
    ) is None
    # tiny scale with non-trivial decimal threshold
    tiny = Decimal("0.0000001")
    lo, hi = closed_int_bounds(Decimal("0"), Decimal("0.0000005"), tiny, Decimal("0"))
    assert physical_value(lo, tiny, Decimal("0")) <= Decimal("0.0000005")
    assert physical_value(hi, tiny, Decimal("0")) <= Decimal("0.0000005")
    assert physical_value(hi + 1, tiny, Decimal("0")) > Decimal("0.0000005")


def test_edge_and_one_unit_miss(noref_lib, tmp_path):
    """Points exactly on every box edge hit; points one int unit away miss."""
    scale, offset = 0.001, 100.0
    path = str(tmp_path / "edges.las")
    # integer coords: edges at 1000/2000 etc.; physical = 0.001*k + 100
    points = [
        TestPoint(x=1000, y=1000, z=1000),  # corner, inside
        TestPoint(x=2000, y=2000, z=2000),  # opposite corner, inside
        TestPoint(x=999, y=1500, z=1500),   # one unit outside xmin
        TestPoint(x=2001, y=1500, z=1500),  # one unit outside xmax
        TestPoint(x=1500, y=999, z=1500),
        TestPoint(x=1500, y=2001, z=1500),
        TestPoint(x=1500, y=1500, z=999),
        TestPoint(x=1500, y=1500, z=2001),
    ]
    build_las(path, points, point_format=0,
              scales=(scale,) * 3, offsets=(offset,) * 3)
    noref_lib.ingest_file(path)

    def phys(k):
        return str(Decimal(repr(scale)) * Decimal(k) + Decimal(repr(offset)))

    box = {
        "xmin": phys(1000), "xmax": phys(2000),
        "ymin": phys(1000), "ymax": phys(2000),
        "zmin": phys(1000), "zmax": phys(2000),
    }
    got = noref_lib.query(box)
    assert {(p.x_int, p.y_int, p.z_int) for p in got} == {
        (1000, 1000, 1000), (2000, 2000, 2000),
    }
    assert noref_lib.count(box) == 2


def test_naive_float_would_drop_the_edge_point(noref_lib, tmp_path):
    """A point whose float reconstruction overshoots the edge is still kept.

    0.1 * 3 = 0.30000000000000004 in IEEE double, strictly above the requested
    closed edge 0.3.  The exact decimal value Decimal('0.1') * 3 equals 0.3,
    so decimal comparison must keep the point while a float filter would drop
    it.
    """
    path = str(tmp_path / "float_trap.las")
    build_las(path, [
        TestPoint(x=2, y=0, z=0),
        TestPoint(x=3, y=0, z=0),
        TestPoint(x=4, y=0, z=0),
    ], point_format=0, scales=(0.1, 1.0, 1.0))
    noref_lib.ingest_file(path)
    # Prove the float trap exists:
    assert 0.1 * 3 > 0.3
    box = {
        "xmin": "0", "xmax": "0.3",
        "ymin": "0", "ymax": "0",
        "zmin": "0", "zmax": "0",
    }
    got = noref_lib.query(box)
    assert [p.x_int for p in got] == [2, 3]
    assert noref_lib.count(box) == 2


def test_negative_offsets_and_tiny_scale(noref_lib, tmp_path):
    path = str(tmp_path / "neg.las")
    points = [TestPoint(x=-214748, y=5, z=9), TestPoint(x=100, y=5, z=9)]
    build_las(
        path, points, point_format=1,
        scales=(1e-7, 0.5, 2.5),
        offsets=(-987654321.123456, -0.25, -1000.0),
    )
    noref_lib.ingest_file(path)
    src = noref_lib.sources()[0]
    # manifest stores exact decimal text
    assert src.actual_bounds[0] == str(
        Decimal("-987654321.123456")
        + Decimal(repr(1e-7)) * Decimal(-214748)
    )
    scale = Decimal(repr(1e-7))
    offset = Decimal("-987654321.123456")
    target = scale * Decimal(-214748) + offset
    box = {
        "xmin": str(target), "xmax": str(target),
        "ymin": "-10", "ymax": "10",
        "zmin": "-2000", "zmax": "2000",
    }
    got = noref_lib.query(box)
    assert [p.x_int for p in got] == [-214748]


def test_large_integer_coordinates_full_int32_range(noref_lib, tmp_path):
    path = str(tmp_path / "big.las")
    lo, hi = -(2**31), 2**31 - 1
    build_las(path, [
        TestPoint(x=lo, y=lo, z=lo),
        TestPoint(x=hi, y=hi, z=hi),
        TestPoint(x=0, y=0, z=0),
    ], point_format=3, scales=(1.0, 1.0, 1.0), offsets=(0.0, 0.0, 0.0))
    noref_lib.ingest_file(path)
    box = {
        "xmin": str(lo), "xmax": str(hi),
        "ymin": str(lo), "ymax": str(hi),
        "zmin": str(lo), "zmax": str(hi),
    }
    got = noref_lib.query(box)
    assert len(got) == 3
    assert sorted(p.x_int for p in got) == [lo, 0, hi]


def test_different_scale_offset_same_physical_location(strict_lib, tmp_path):
    """Two files represent the same physical point with different integers.

    Fine file:   scale 0.001, int 123456 -> physical 123.456
    Coarse file: scale 0.01,  int 12345  -> physical 123.45
    and a third coarse int 12346 -> physical 123.46.  Each source keeps its
    own integer coords and scale/offset; queries translate per source.
    """
    wkt = 'PROJCS["same crs"]'
    p1 = str(tmp_path / "fine.las")
    build_las(p1, [TestPoint(x=123456, y=0, z=0)], point_format=0,
              scales=(0.001, 1.0, 1.0), offsets=(0.0, 0.0, 0.0), wkt=wkt)
    p2 = str(tmp_path / "coarse.las")
    build_las(p2, [
        TestPoint(x=12345, y=0, z=0),
        TestPoint(x=12346, y=0, z=0),
    ], point_format=0, scales=(0.01, 1.0, 1.0),
        offsets=(0.0, 0.0, 0.0), wkt=wkt)
    strict_lib.ingest_file(p1)
    strict_lib.ingest_file(p2)
    assert len(strict_lib.sources()) == 2

    zero_box = lambda x0, x1: {
        "xmin": x0, "xmax": x1,
        "ymin": "0", "ymax": "0",
        "zmin": "0", "zmax": "0",
    }
    # A box containing exactly 123.456 hits only the fine representation.
    got = strict_lib.query(zero_box("123.456", "123.456"))
    assert len(got) == 1
    assert got[0].x_int == 123456
    # A box containing exactly 123.45 hits only the coarse int 12345.
    got = strict_lib.query(zero_box("123.45", "123.45"))
    assert len(got) == 1
    assert got[0].x_int == 12345
    # A box covering both physical values returns points from both sources,
    # ordered by source identity rather than ingestion order.
    got2 = strict_lib.query(zero_box("123.45", "123.46"))
    assert {p.x_int for p in got2} == {12345, 12346, 123456}
    shas = [p.source_sha256 for p in got2]
    assert shas == sorted(shas)


def test_classification_filter_and_empty_result(noref_lib, tmp_path):
    path = str(tmp_path / "cls.las")
    points = [
        TestPoint(x=i, y=0, z=0, classification=(2 if i % 2 == 0 else 4))
        for i in range(6)
    ]
    build_las(path, points, point_format=0)
    noref_lib.ingest_file(path)
    box = {
        "xmin": "-100", "xmax": "100",
        "ymin": "0", "ymax": "0",
        "zmin": "0", "zmax": "0",
    }
    assert noref_lib.count(box, classifications=[2]) == 3
    got = noref_lib.query(box, classifications=[2, 4])
    assert len(got) == 6
    nobody = dict(box, xmin="999999", xmax="1000000")
    assert noref_lib.count(nobody) == 0
    assert noref_lib.query(nobody) == []
    with pytest.raises(QueryError):
        noref_lib.count(box, classifications=[99])
