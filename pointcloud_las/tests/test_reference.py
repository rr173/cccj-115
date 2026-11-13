"""Coordinate reference admission rules."""

from __future__ import annotations

import pytest

from materials import TestPoint, build_las
from pointcloud_las import IngestionError


WKT_A = 'PROJCS["crs A",GEOGCS["gcs"]]'
WKT_B = 'PROJCS["crs B",GEOGCS["gcs"]]'


def test_unreferenced_file_only_in_unreferenced_library(strict_lib, noref_lib, tmp_path):
    path = str(tmp_path / "plain.las")
    build_las(path, [TestPoint(x=1, y=1, z=1)], point_format=0)

    with pytest.raises(IngestionError, match="no coordinate reference"):
        strict_lib.ingest_file(path)
    assert strict_lib.sources() == []

    result = noref_lib.ingest_file(path)
    assert result.ingested is True
    assert noref_lib.sources()[0].reference_desc is None


def test_referenced_file_rejected_by_unreferenced_library(noref_lib, tmp_path):
    path = str(tmp_path / "wkt.las")
    build_las(path, [TestPoint(x=1, y=1, z=1)], point_format=0, wkt=WKT_A)
    with pytest.raises(IngestionError, match="unreferenced"):
        noref_lib.ingest_file(path)
    assert noref_lib.sources() == []


def test_conflicting_references_rejected(strict_lib, tmp_path):
    first = str(tmp_path / "a.las")
    second = str(tmp_path / "b.las")
    build_las(first, [TestPoint(x=1, y=1, z=1)], point_format=0, wkt=WKT_A)
    build_las(second, [TestPoint(x=2, y=2, z=2)], point_format=0, wkt=WKT_B)
    strict_lib.ingest_file(first)
    with pytest.raises(IngestionError, match="conflict"):
        strict_lib.ingest_file(second)
    # Only the first source survived; the conflict added nothing.
    assert len(strict_lib.sources()) == 1
    assert strict_lib.count() == 1


def test_identical_references_accepted(strict_lib, tmp_path):
    for name, k in (("a.las", 1), ("b.las", 2)):
        path = str(tmp_path / name)
        build_las(path, [TestPoint(x=k, y=k, z=k)], point_format=0, wkt=WKT_A)
        strict_lib.ingest_file(path)
    assert len(strict_lib.sources()) == 2


def test_geotiff_vlrs_count_as_reference(strict_lib, tmp_path):
    path = str(tmp_path / "geo.las")
    build_las(
        path, [TestPoint(x=1, y=1, z=1)], point_format=0,
        geotiff_keys=[(1024, 0, 1, 1), (3072, 0, 1, 32633)],
        geo_ascii="WGS 84 / UTM zone 33N",
    )
    strict_lib.ingest_file(path)
    desc = strict_lib.sources()[0].reference_desc
    assert desc is not None and desc.startswith("REF:")

    # A different GeoKey set is a conflict.
    other = str(tmp_path / "geo2.las")
    build_las(
        other, [TestPoint(x=1, y=1, z=1)], point_format=0,
        geotiff_keys=[(1024, 0, 1, 1), (3072, 0, 1, 32632)],
        geo_ascii="WGS 84 / UTM zone 32N",
    )
    with pytest.raises(IngestionError, match="conflict"):
        strict_lib.ingest_file(other)


def test_same_reference_different_scale_coexists(strict_lib, tmp_path):
    first = str(tmp_path / "a.las")
    second = str(tmp_path / "b.las")
    build_las(first, [TestPoint(x=1, y=1, z=1)], point_format=0,
              scales=(0.001, 0.001, 0.001), wkt=WKT_A)
    build_las(second, [TestPoint(x=10, y=10, z=10)], point_format=0,
              scales=(1.0, 1.0, 1.0), wkt=WKT_A)
    strict_lib.ingest_file(first)
    strict_lib.ingest_file(second)
    assert len(strict_lib.sources()) == 2
