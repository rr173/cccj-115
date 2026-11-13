"""End-to-end workflow and bounded-memory checks."""

from __future__ import annotations

import os
import tracemalloc
from decimal import Decimal

from materials import TestPoint, build_las
from pointcloud_las import PointCloudLibrary


def test_multi_tile_workflow_with_manifest_and_export(tmp_path):
    db_path = str(tmp_path / "workflow.db")
    wkt = 'PROJCS["workflow crs"]'
    # Three tiles, two formats, different scales, some shared coordinates.
    tiles = [
        ("t0.las", 0, (0.001, 0.001, 0.001), (0.0, 0.0, 0.0),
         [TestPoint(x=1000, y=2000, z=3000, classification=2),
          TestPoint(x=1001, y=2001, z=3001, classification=3)]),
        ("t1.las", 1, (0.01, 0.01, 0.01), (100.0, 100.0, 100.0),
         [TestPoint(x=100, y=200, z=300, classification=2),
          TestPoint(x=101, y=201, z=301, classification=4),
          TestPoint(x=100, y=200, z=300, classification=5)]),  # dup coords
        ("t2.las", 3, (1.0, 1.0, 1.0), (-50.0, -50.0, -50.0),
         [TestPoint(x=50, y=50, z=50, classification=3,
                    gps_time=1.5, red=7, green=8, blue=9)]),
    ]
    with PointCloudLibrary.create(db_path) as lib:
        for name, fmt, scales, offsets, pts in tiles:
            path = str(tmp_path / name)
            build_las(path, pts, point_format=fmt, scales=scales,
                      offsets=offsets, wkt=wkt)
            result = lib.ingest_file(path, read_batch=2)
            assert result.ingested is True
            assert result.n_points == len(pts)

        # Re-ingest every file: all no-ops, counts unchanged.
        total = sum(len(pts) for _, _, _, _, pts in tiles)
        for name, *_ in tiles:
            again = lib.ingest_file(str(tmp_path / name))
            assert again.ingested is False
        assert lib.count() == total
        assert len(lib.sources()) == 3

        manifest = lib.sources()
        for info, (_, fmt, _, _, pts) in zip(manifest, tiles):
            assert info.point_format == fmt
            assert info.n_points == len(pts)
            # Every manifest row carries declared and actual ranges.
            assert None not in info.declared_bounds
            assert all(v is not None for v in info.actual_bounds)
            assert info.file_size_bytes > 0

        # A tight box around tile 2's only point.
        box = {
            "xmin": "0", "xmax": "0",
            "ymin": "0", "ymax": "0",
            "zmin": "0", "zmax": "0",
        }
        got = lib.query(box)
        assert len(got) == 1
        assert got[0].gps_time == 1.5 and got[0].red == 7

        # Classification filter across tiles, streamed.
        cls2 = list(lib.iter_query(None, classifications=[2]))
        assert len(cls2) == 2
        # Fixed ordering by source identity then index.
        keys = [(p.source_sha256, p.point_index) for p in cls2]
        assert keys == sorted(keys)

        out = str(tmp_path / "export.csv")
        n = lib.export_csv(out, classifications=[2, 3, 4, 5])
        assert n == total
        assert os.path.getsize(out) > 0

    # Reopen from disk: sources and points answer without the LAS files.
    with PointCloudLibrary(db_path) as reopened:
        assert reopened.count() == total
        assert len(reopened.sources()) == 3


def test_streaming_query_memory_does_not_grow_with_result_size(tmp_path):
    db_path = str(tmp_path / "mem.db")
    path = str(tmp_path / "big.las")
    n = 40_000
    build_las(
        path,
        [TestPoint(x=i, y=0, z=0) for i in range(n)],
        point_format=0, scales=(1.0, 1.0, 1.0),
    )
    with PointCloudLibrary.create(db_path, unreferenced=True) as lib:
        lib.ingest_file(path, read_batch=512)
        box = {
            "xmin": "0", "xmax": str(n),
            "ymin": "0", "ymax": "0",
            "zmin": "0", "zmax": "0",
        }

        def streaming_peak(fetch_size):
            tracemalloc.start()
            count = 0
            for _ in lib.iter_query(box, fetch_size=fetch_size):
                count += 1
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert count == n
            return peak_bytes

        # Materialising every hit is genuinely O(n):
        tracemalloc.start()
        materialised = lib.query(box)
        _, materialised_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(materialised) == n

        # Streaming with a small fetch batch must stay far below the
        # materialised footprint even though it returns the same points.
        streamed_peak = streaming_peak(256)
        assert streamed_peak < materialised_peak / 4

        # Streaming stays far below the materialised footprint regardless of
        # fetch batch: its peak scales with the batch, never with the 40k
        # result size.
        peak_a = streaming_peak(256)
        peak_b = streaming_peak(4096)
        assert peak_a < materialised_peak / 10
        assert peak_b < materialised_peak / 3
