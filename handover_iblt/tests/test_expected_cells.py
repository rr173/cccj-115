"""Tests for the recomputed small table (independent of production code)."""

from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import expected as E  # noqa: E402

from handover_iblt.config import DigestConfig  # noqa: E402
from handover_iblt.table import IbltTable  # noqa: E402


def test_expected_indexes_and_checks_are_literal():
    # Values printed by hand from the documented hash formula; if the
    # protocol changes these literals must be updated deliberately.
    assert E.expected_cell_indexes(E.element(1)) == [3, 4, 5]
    assert E.expected_cell_indexes(E.element(2)) == [7, 3, 1]
    assert E.expected_cell_indexes(E.element(3)) == [7, 0, 1]
    assert E.expected_check(E.element(1)).hex() == "f3b0cf9d24a15c5f"
    assert E.expected_check(E.element(2)).hex() == "75f6d851ab0a3844"
    assert E.expected_check(E.element(3)).hex() == "37dceb479da82f51"


def test_cell_contents_match_independent_arithmetic():
    cfg = DigestConfig(capacity=E.CAPACITY, k=E.K, seed=E.SEED)
    table = IbltTable(cfg)
    table.add(E.element(1), 1)
    table.add(E.element(2), 1)
    table.add(E.element(3), -1)
    expected = E.expected_cells([(E.element(1), 1), (E.element(2), 1),
                                 (E.element(3), -1)])
    for index, (count, key, check) in enumerate(expected):
        cell = table.cells[index]
        assert cell.count == count, f"cell {index} count"
        assert cell.key_bytes() == key, f"cell {index} key_xor"
        assert cell.check_bytes() == check, f"cell {index} check_xor"


def test_wire_body_layout_is_64_bytes_per_cell():
    cfg = DigestConfig(capacity=E.CAPACITY, k=E.K, seed=E.SEED)
    table = IbltTable(cfg)
    table.add(E.element(1), 1)
    table.add(E.element(2), 1)
    table.add(E.element(3), -1)
    body = table.to_body()
    assert len(body) == 64 * 8
    expected = E.expected_cells([(E.element(1), 1), (E.element(2), 1),
                                 (E.element(3), -1)])
    for index, (count, key, check) in enumerate(expected):
        offset = 64 * index
        assert struct.unpack_from(">q", body, offset)[0] == count
        assert body[offset + 8:offset + 56] == key
        assert body[offset + 56:offset + 64] == check


def test_subtract_is_signed_and_does_not_mutate_inputs():
    cfg = DigestConfig(capacity=E.CAPACITY, k=E.K, seed=E.SEED)
    left = IbltTable(cfg)
    right = IbltTable(cfg)
    left.add(E.element(1), 1)
    right.add(E.element(3), 1)
    left_before = left.to_body()
    right_before = right.to_body()
    diff = left.subtract(right)
    assert left.to_body() == left_before
    assert right.to_body() == right_before
    expected = E.expected_cells([(E.element(1), 1), (E.element(3), -1)])
    for index, (count, key, check) in enumerate(expected):
        cell = diff.cells[index]
        assert (cell.count, cell.key_bytes(), cell.check_bytes()) == (
            count, key, check
        )


def test_k_cells_are_distinct_and_reproducible():
    cfg = DigestConfig(capacity=8, k=3, seed=7)
    for n in range(1, 50):
        indexes = E.expected_cell_indexes(E.element(n))
        assert len(set(indexes)) == 3
        assert all(0 <= i < 8 for i in indexes)
    # Recompute through the production function as well.
    from handover_iblt.config import cell_indexes

    for n in range(1, 50):
        assert cell_indexes(8, 3, 7, E.element(n)) == E.expected_cell_indexes(
            E.element(n)
        )


@pytest.mark.parametrize("capacity,k", [(1, 2), (0, 1), (8, 0), (8, 9)])
def test_impossible_configurations_are_rejected(capacity, k):
    with pytest.raises(ValueError):
        DigestConfig(capacity=capacity, k=k, seed=1)


def test_small_table_peel_matches_two_sided_set_difference():
    """Peel the tiny table and compare, item by item, with the set difference.

    ``left = {X, Y}`` and ``right = {Y, Z}`` so the expected output is
    ``X`` left-only, ``Z`` right-only, and nothing else.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import reference as R

    from handover_iblt.peel import peel
    from handover_iblt.reconcile import DifferenceKind, pair_differences

    cfg = DigestConfig(capacity=E.CAPACITY, k=E.K, seed=E.SEED)
    left = IbltTable(cfg)
    right = IbltTable(cfg)
    left.add(E.element(1), 1)
    left.add(E.element(2), 1)
    right.add(E.element(2), 1)
    right.add(E.element(3), 1)

    diff = left.subtract(right)
    outcome = peel(diff)
    assert outcome.complete
    assert outcome.residual is None
    signed = sorted((item.sign, item.element) for item in outcome.peeled)
    assert signed == sorted(
        [(1, E.element(1)), (-1, E.element(3))]
    )
    rows = pair_differences(outcome.peeled)
    assert [(d.kind, d.uuid) for d in rows] == [
        (DifferenceKind.LEFT_ONLY, E.element(1)[:16]),
        (DifferenceKind.RIGHT_ONLY, E.element(3)[:16]),
    ]

    # Independent two-sided set difference over the original element sets.
    x, y, z = (E.element(n) for n in (1, 2, 3))
    expected = R.expected_differences(
        [(x[:16], x[16:]), (y[:16], y[16:])],
        [(y[:16], y[16:]), (z[:16], z[16:])],
    )
    produced = {
        "left_only": [(d.uuid, d.left_fingerprint) for d in rows
                      if d.kind is DifferenceKind.LEFT_ONLY],
        "right_only": [(d.uuid, d.right_fingerprint) for d in rows
                       if d.kind is DifferenceKind.RIGHT_ONLY],
        "changed": [(d.uuid, d.left_fingerprint, d.right_fingerprint)
                    for d in rows if d.kind is DifferenceKind.CONTENT_CHANGED],
    }
    assert produced == expected
    assert produced["left_only"] == [(x[:16], x[16:])]
    assert produced["right_only"] == [(z[:16], z[16:])]
