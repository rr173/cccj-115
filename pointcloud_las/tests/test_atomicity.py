"""All-or-nothing imports, restart/retry and reader isolation."""

from __future__ import annotations

import os
import threading

import pytest

from materials import TestPoint, build_las
from pointcloud_las import PointCloudLibrary
from pointcloud_las.ingestion import (
    HardCrashSimulated,
    hash_file,
)
from pointcloud_las.schema import connect


def _big_file(path, n=20_000):
    build_las(
        path,
        [TestPoint(x=i, y=i % 7, z=i % 13, classification=i % 5)
         for i in range(n)],
        point_format=1,
    )
    return path


def test_graceful_failure_rolls_back_everything(noref_lib, tmp_path):
    path = _big_file(str(tmp_path / "big.las"))
    with pytest.raises(Exception):
        noref_lib.ingest_file(path, read_batch=500, _fail_after_batches=3)
    # No half source, no half points.
    assert noref_lib.sources() == []
    assert noref_lib.count() == 0
    # No placeholder rows survived either.
    raw = noref_lib.connection.execute(
        "SELECT COUNT(*) FROM points"
    ).fetchone()[0]
    assert raw == 0
    # R-tree is empty as well.
    rt = noref_lib.connection.execute(
        "SELECT COUNT(*) FROM points_rtree"
    ).fetchone()[0]
    assert rt == 0


def test_retry_after_failure_succeeds(noref_lib, tmp_path):
    path = _big_file(str(tmp_path / "big.las"), n=5_000)
    with pytest.raises(Exception):
        noref_lib.ingest_file(path, read_batch=250, _fail_after_batches=2)
    # Reopen the library (simulate restart) and retry the same file.
    db_path = noref_lib.path
    noref_lib.close()
    with PointCloudLibrary(db_path) as reopened:
        result = reopened.ingest_file(path, read_batch=250)
        assert result.ingested is True
        assert result.n_points == 5_000
        assert reopened.count() == 5_000
        assert reopened.sources()[0].n_points == 5_000


def test_hard_crash_connection_recovery(noref_lib, tmp_path):
    path = _big_file(str(tmp_path / "crash.las"), n=3_000)
    with pytest.raises(HardCrashSimulated):
        noref_lib.ingest_file(
            path, read_batch=200, _hard_crash_after_batches=4
        )
    # The first connection is dead; a brand new process-like connection must
    # recover the WAL and see a clean, empty library.
    db_path = noref_lib.path
    noref_lib.close()
    with PointCloudLibrary(db_path) as recovered:
        assert recovered.sources() == []
        assert recovered.count() == 0
        # The original file retries normally after the crash.
        result = recovered.ingest_file(path, read_batch=200)
        assert result.ingested is True
        assert result.n_points == 3_000
        assert recovered.count() == 3_000


def test_reader_sees_only_complete_sources(tmp_path):
    db_path = str(tmp_path / "iso.db")
    las_path = _big_file(str(tmp_path / "iso.las"), n=10_000)

    # A long-lived reader opens before the import and starts a read
    # transaction, pinning the pre-import snapshot.
    lib = PointCloudLibrary.create(db_path, unreferenced=True)
    reader_conn = connect(db_path)
    reader_conn.execute("BEGIN")
    before = reader_conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert before == 0

    barrier = threading.Event()
    errors = []

    def import_worker():
        try:
            writer = PointCloudLibrary(db_path)
            writer.ingest_file(
                las_path, read_batch=300,
                _fail_after_batches=None,
            )
            writer.close()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            barrier.set()

    t = threading.Thread(target=import_worker)
    t.start()
    # While the writer is mid-transaction, the reader keeps the old snapshot.
    mid = reader_conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert mid == 0
    mid_points = reader_conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    assert mid_points == 0
    barrier.wait(timeout=60)
    t.join(timeout=60)
    assert not errors
    # After commit, a fresh read sees the complete source; the old snapshot
    # still sees nothing until it ends its read transaction.
    reader_conn.commit()
    after = reader_conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert after == 1
    after_points = reader_conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    assert after_points == 10_000
    reader_conn.close()
    lib.close()


def test_query_during_import_never_returns_half_points(tmp_path):
    db_path = str(tmp_path / "concurrent.db")
    lib = PointCloudLibrary.create(db_path, unreferenced=True)
    # A fully imported first source.
    first = str(tmp_path / "first.las")
    build_las(first, [TestPoint(x=1, y=1, z=1)], point_format=0)
    lib.ingest_file(first)

    second = _big_file(str(tmp_path / "second.las"), n=6_000)
    seen_counts = []
    stop = threading.Event()

    def reader_worker():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                # Each query is a fresh statement: it can see either the
                # pre-commit state or the post-commit state, never a partial
                # point count.
                n = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
                seen_counts.append(n)
        finally:
            conn.close()

    reader = threading.Thread(target=reader_worker)
    reader.start()
    writer = PointCloudLibrary(db_path)
    writer.ingest_file(second, read_batch=123)
    writer.close()
    stop.set()
    reader.join(timeout=30)

    assert seen_counts, "reader never ran"
    allowed = {1, 6001}
    assert set(seen_counts) <= allowed
    assert 6001 in seen_counts  # the committed state was observed
    lib.close()


def test_points_queryable_without_source_file(tmp_path):
    db_path = str(tmp_path / "offline.db")
    src_dir = tmp_path / "tiles"
    src_dir.mkdir()
    path = str(src_dir / "tile.las")
    build_las(path, [TestPoint(x=7, y=7, z=7, classification=2)],
              point_format=2, scales=(1.0, 1.0, 1.0))
    sha = hash_file(path)
    with PointCloudLibrary.create(db_path, unreferenced=True) as lib:
        lib.ingest_file(path)
    # Remove the source entirely; the library must answer on its own.
    os.unlink(path)
    with PointCloudLibrary(db_path) as lib:
        got = lib.query({
            "xmin": "7", "xmax": "7",
            "ymin": "7", "ymax": "7",
            "zmin": "7", "zmax": "7",
        })
        assert len(got) == 1
        assert got[0].source_sha256 == sha
