"""Zero values must stay distinguishable from absent optional attributes."""

from __future__ import annotations

from decimal import Decimal

from materials import TestPoint, build_las
from conftest import expected_box_hits, expected_class_hits


def test_zero_gps_and_rgb_are_stored_not_confused_with_absence(noref_lib, tmp_path):
    # pf3 carries both GPS time and RGB; genuine zero values.
    path = str(tmp_path / "zeros.las")
    points = [
        TestPoint(x=1, y=2, z=3, gps_time=0.0, red=0, green=0, blue=0),
        TestPoint(x=4, y=5, z=6, gps_time=0.0, red=0, green=0, blue=0,
                  classification=7),
    ]
    build_las(path, points, point_format=3, scales=(1.0, 1.0, 1.0))
    noref_lib.ingest_file(path)
    got = noref_lib.query({
        "xmin": "0", "xmax": "10",
        "ymin": "0", "ymax": "10",
        "zmin": "0", "zmax": "10",
    })
    assert len(got) == 2
    for p in got:
        assert p.gps_time == 0.0      # present zero, not None
        assert (p.red, p.green, p.blue) == (0, 0, 0)

    # pf0 has neither field: columns must be None.
    pf0 = str(tmp_path / "none.las")
    build_las(pf0, points, point_format=0, scales=(1.0, 1.0, 1.0))
    noref_lib.ingest_file(pf0)
    rows = noref_lib.query({
        "xmin": "0", "xmax": "10",
        "ymin": "0", "ymax": "10",
        "zmin": "0", "zmax": "10",
    })
    pf0_rows = [r for r in rows if r.gps_time is None]
    assert len(pf0_rows) == 2
    for p in pf0_rows:
        assert (p.red, p.green, p.blue) == (None, None, None)


def test_independent_pointwise_expectations_match_query(noref_lib, tmp_path):
    """Expectations come from a brute-force helper, never the production filter."""
    scales = (0.001, 0.001, 0.001)
    offsets = (50.0, -10.0, 2.0)
    points = [
        TestPoint(x=100, y=200, z=300, classification=2),
        TestPoint(x=200, y=400, z=600, classification=4),
        TestPoint(x=300, y=600, z=900, classification=2),
        TestPoint(x=400, y=800, z=1200, classification=5),
    ]
    path = str(tmp_path / "expect.las")
    build_las(path, points, point_format=1,
              scales=scales, offsets=offsets)
    noref_lib.ingest_file(path)

    def phys(k, axis):
        return Decimal(repr(scales[axis])) * Decimal(k) + Decimal(repr(offsets[axis]))

    box = {
        "xmin": str(phys(100, 0)), "xmax": str(phys(300, 0)),
        "ymin": str(phys(200, 1)), "ymax": str(phys(600, 1)),
        "zmin": str(phys(300, 2)), "zmax": str(phys(900, 2)),
    }
    expected = expected_box_hits(
        points, scales, offsets,
        {k: Decimal(v) for k, v in box.items()},
    )
    got = {(p.x_int, p.y_int, p.z_int) for p in noref_lib.query(box)}
    assert got == set(expected)

    # The independent helper also drives the classification expectation.
    cls_expected = {
        (p.x, p.y, p.z) for p in expected_class_hits(points, [2])
    }
    cls_got = {
        (p.x_int, p.y_int, p.z_int)
        for p in noref_lib.query(box, classifications=[2])
    }
    assert cls_got == cls_expected
