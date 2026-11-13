"""Shared pytest fixtures and independent expected-value helpers.

The expected points here are computed independently from the raw integer
coords and the decimal scale/offset -- the production query filters are never
used to derive expectations.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pointcloud_las import PointCloudLibrary  # noqa: E402


@pytest.fixture()
def strict_lib(tmp_path):
    path = str(tmp_path / "strict.db")
    lib = PointCloudLibrary.create(path)
    yield lib
    lib.close()


@pytest.fixture()
def noref_lib(tmp_path):
    path = str(tmp_path / "noref.db")
    lib = PointCloudLibrary.create(path, unreferenced=True)
    yield lib
    lib.close()


def expected_physical(int_coord: int, scale: float, offset: float) -> Decimal:
    """Independent physical-coordinate calculation for test expectations."""
    return Decimal(repr(scale)) * Decimal(int_coord) + Decimal(repr(offset))


def expected_box_hits(points, scales, offsets, box_decimal):
    """Brute-force, independent containment check over test points.

    ``box_decimal`` maps xmin/xmax/... to Decimal values.  Every edge is
    closed.  Returns the list of (x_int, y_int, z_int) expected to match.
    """
    hits = []
    for p in points:
        coords = (p.x, p.y, p.z)
        ok = True
        for axis, prefix in enumerate(("x", "y", "z")):
            phys = expected_physical(
                coords[axis], scales[axis], offsets[axis]
            )
            if not (box_decimal[f"{prefix}min"] <= phys
                    <= box_decimal[f"{prefix}max"]):
                ok = False
                break
        if ok:
            hits.append((p.x, p.y, p.z))
    return hits


def expected_class_hits(points, classes):
    if classes is None:
        return list(points)
    wanted = set(classes)
    return [p for p in points if p.classification in wanted]
