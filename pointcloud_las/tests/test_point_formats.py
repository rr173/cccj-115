"""Round-trip ingestion for the four accepted point formats 0..3."""

from __future__ import annotations

import pytest

from materials import TestPoint, build_las
from pointcloud_las.ingestion import hash_file


def _sample_points(n=6):
    return [
        TestPoint(
            x=100 + i,
            y=-200 + i,
            z=300 - i,
            intensity=10 + i,
            classification=(i * 3) % 32,
            return_number=(i % 3) + 1,
            number_of_returns=3,
            synthetic=i % 2,
            key_point=(i + 1) % 2,
            withheld=0,
            gps_time=1000.0 + i * 0.25,
            red=1000 + i,
            green=2000 + i,
            blue=3000 + i,
            extra=bytes([0xA0 + i, 0x0F]),
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("point_format", [0, 1, 2, 3])
def test_format_roundtrip(strict_lib, tmp_path, point_format):
    # strict_lib requires a reference; attach WKT to every file.
    path = str(tmp_path / f"pf{point_format}.las")
    points = _sample_points()
    build_las(
        path, points,
        point_format=point_format,
        scales=(0.001, 0.001, 0.001),
        offsets=(500.0, -250.0, 10.0),
        wkt='PROJCS["test crs",GEOGCS["gcs"]]',
        extra_byte_count=2,
    )
    result = strict_lib.ingest_file(path)
    assert result.ingested is True
    assert result.n_points == len(points)
    assert result.source_sha256 == hash_file(path)

    sources = strict_lib.sources()
    assert len(sources) == 1
    src = sources[0]
    assert src.point_format == point_format
    assert src.n_points == len(points)
    assert src.extra_bytes_len == 2
    assert src.has_gps is (point_format in (1, 3))
    assert src.has_rgb is (point_format in (2, 3))
    assert src.reference_desc is not None and src.reference_desc.startswith("REF:")

    got = strict_lib.query(_full_box(points, (0.001,) * 3, (500.0, -250.0, 10.0)))
    assert len(got) == len(points)
    assert [p.point_index for p in got] == list(range(len(points)))

    for i, (stored, expected) in enumerate(zip(got, points)):
        assert (stored.x_int, stored.y_int, stored.z_int) == (
            expected.x, expected.y, expected.z
        )
        assert stored.intensity == expected.intensity
        assert stored.classification == expected.classification
        # flags kept in their bit positions, not merged into the class code
        assert stored.class_flags == (
            (expected.synthetic << 5)
            | (expected.key_point << 6)
            | (expected.withheld << 7)
        )
        assert stored.return_number == expected.return_number
        assert stored.number_of_returns == expected.number_of_returns
        assert stored.extra_bytes == bytes([0xA0 + i, 0x0F])
        if point_format in (1, 3):
            assert stored.gps_time == pytest.approx(expected.gps_time)
        else:
            assert stored.gps_time is None  # missing, never confused with 0.0
        if point_format in (2, 3):
            assert (stored.red, stored.green, stored.blue) == (
                expected.red, expected.green, expected.blue
            )
        else:
            assert (stored.red, stored.green, stored.blue) == (None, None, None)


def test_zero_point_file(strict_lib, tmp_path):
    path = str(tmp_path / "empty.las")
    build_las(path, [], point_format=0, wkt='PROJCS["c"]')
    result = strict_lib.ingest_file(path)
    assert result.ingested is True
    assert result.n_points == 0
    src = strict_lib.sources()[0]
    assert src.n_points == 0
    assert src.int_bounds == (None,) * 6
    assert src.actual_bounds == (None,) * 6
    assert strict_lib.count(_any_box()) == 0


def test_named_extra_dimensions_are_kept_opaque(strict_lib, tmp_path):
    path = str(tmp_path / "named.las")
    points = [
        TestPoint(x=1, y=2, z=3),
        TestPoint(x=4, y=5, z=6),
    ]
    for i, p in enumerate(points):
        p.wave = 700 + i
    build_las(
        path, points, point_format=0,
        wkt='PROJCS["c"]',
        extra_dims={"wave": "u2"},
    )
    strict_lib.ingest_file(path)
    got = strict_lib.query(_any_box())
    # Two unsigned-16 little-endian bytes per point, preserved verbatim.
    assert got[0].extra_bytes == (700).to_bytes(2, "little")
    assert got[1].extra_bytes == (701).to_bytes(2, "little")


def _full_box(points, scales, offsets):
    from decimal import Decimal
    box = {}
    for axis, prefix in enumerate(("x", "y", "z")):
        vals = [getattr(p, prefix) for p in points]
        lo = min(vals)
        hi = max(vals)
        box[f"{prefix}min"] = str(
            Decimal(repr(scales[axis])) * Decimal(lo)
            + Decimal(repr(offsets[axis]))
        )
        box[f"{prefix}max"] = str(
            Decimal(repr(scales[axis])) * Decimal(hi)
            + Decimal(repr(offsets[axis]))
        )
    return box


def _any_box():
    return {
        "xmin": "-1000000000", "xmax": "1000000000",
        "ymin": "-1000000000", "ymax": "1000000000",
        "zmin": "-1000000000", "zmax": "1000000000",
    }
