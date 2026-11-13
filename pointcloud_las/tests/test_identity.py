"""Point identity: SHA-256 of file bytes plus original point index."""

from __future__ import annotations

from materials import TestPoint, build_las, copy_file
from pointcloud_las.ingestion import hash_file


def test_duplicate_coordinates_are_distinct_points(noref_lib, tmp_path):
    path = str(tmp_path / "dups.las")
    points = [
        TestPoint(x=5, y=5, z=5, classification=2),
        TestPoint(x=5, y=5, z=5, classification=3),
        TestPoint(x=5, y=5, z=5, classification=4),
    ]
    build_las(path, points, point_format=0, scales=(1.0, 1.0, 1.0))
    noref_lib.ingest_file(path)
    got = noref_lib.query({
        "xmin": "5", "xmax": "5",
        "ymin": "5", "ymax": "5",
        "zmin": "5", "zmax": "5",
    })
    assert len(got) == 3
    identities = {(p.source_sha256, p.point_index) for p in got}
    assert len(identities) == 3
    assert sorted(p.point_index for p in got) == [0, 1, 2]
    assert {p.classification for p in got} == {2, 3, 4}


def test_reingest_same_bytes_adds_no_points(noref_lib, tmp_path):
    path = str(tmp_path / "a.las")
    points = [TestPoint(x=i, y=i, z=i) for i in range(4)]
    build_las(path, points, point_format=1)
    first = noref_lib.ingest_file(path)
    assert first.ingested is True

    # Same content under a different name and directory.
    renamed = str(tmp_path / "sub" / "renamed.las")
    copy_file(path, renamed)
    second = noref_lib.ingest_file(renamed)
    assert second.ingested is False
    assert second.source_sha256 == first.source_sha256
    assert len(noref_lib.sources()) == 1
    assert noref_lib.count() == 4


def test_same_path_new_content_is_new_identity(noref_lib, tmp_path):
    path = str(tmp_path / "changing.las")
    build_las(path, [TestPoint(x=1, y=1, z=1)], point_format=0)
    first = noref_lib.ingest_file(path)

    build_las(path, [
        TestPoint(x=1, y=1, z=1),
        TestPoint(x=2, y=2, z=2),
    ], point_format=0)
    second = noref_lib.ingest_file(path)
    assert second.ingested is True
    assert second.source_sha256 != first.source_sha256
    sources = noref_lib.sources()
    assert len(sources) == 2
    assert {s.source_sha256 for s in sources} == {
        first.source_sha256, second.source_sha256
    }
    assert noref_lib.count() == 3


def test_provenance_points_back_to_source_and_index(noref_lib, tmp_path):
    files = {}
    for name, base in (("first.las", 0), ("second.las", 100)):
        path = str(tmp_path / name)
        build_las(
            path,
            [TestPoint(x=base + k, y=base + k, z=base + k,
                       classification=k % 5) for k in range(3)],
            point_format=2, scales=(1.0, 1.0, 1.0),
        )
        files[name] = (path, hash_file(path))
        noref_lib.ingest_file(path)

    all_points = list(noref_lib.iter_query())
    # Fixed order: source identity first, then original index.
    shas = [p.source_sha256 for p in all_points]
    assert shas == sorted(shas)
    for sha in set(shas):
        indexes = [p.point_index for p in all_points
                   if p.source_sha256 == sha]
        assert indexes == [0, 1, 2]

    # source_sha + index range locates points without any spatial bound.
    target_sha = files["first.las"][1]
    located = list(noref_lib.iter_query(
        None, source_sha=target_sha, index_start=1, index_end=1
    ))
    assert len(located) == 1
    assert located[0].source_sha256 == target_sha
    assert located[0].point_index == 1


def test_ingest_order_does_not_change_result_order(noref_lib, tmp_path):
    shas = []
    for i in range(3):
        path = str(tmp_path / f"f{i}.las")
        build_las(path, [TestPoint(x=i, y=i, z=i)], point_format=0)
        result = noref_lib.ingest_file(path)
        shas.append(result.source_sha256)
    got = list(noref_lib.iter_query())
    assert [p.source_sha256 for p in got] == sorted(shas)
