"""Peeling behaviour: complete, residual, and refusal to claim success."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handover_iblt import materials as M  # noqa: E402
from handover_iblt.builder import build_from_checklist  # noqa: E402
from handover_iblt.config import (  # noqa: E402
    DigestConfig,
    cell_indexes,
    element_check,
)
from handover_iblt.digest import load_digest  # noqa: E402
from handover_iblt.errors import UndecodedError  # noqa: E402
from handover_iblt.peel import PeeledElement, cell_is_pure, peel  # noqa: E402
from handover_iblt.reconcile import compare_digests, pair_differences  # noqa: E402
from handover_iblt.table import Cell, IbltTable  # noqa: E402


def _el(n: int) -> bytes:
    return n.to_bytes(16, "big") + bytes([n % 251]) * 32


def _table(n_each: int, cfg: DigestConfig) -> IbltTable:
    table = IbltTable(cfg)
    for i in range(n_each):
        table.add(_el(i + 1), 1)
    return table


def test_capacity_shortage_is_not_success():
    cfg = DigestConfig(capacity=8, k=4, seed=7)
    table = _table(40, cfg)
    outcome = peel(table)
    assert outcome.complete is False
    assert outcome.residual.nonzero_cells > 0
    assert outcome.consistency_error is None


def test_crafted_collision_cycle_blocks_peeling():
    """Two elements with identical cell sets produce an unpeelable residue."""
    cfg = DigestConfig(capacity=M.CYCLE_CAPACITY, k=M.CYCLE_K,
                       seed=M.CYCLE_SEED)
    p = bytes.fromhex(M.P_UUID) + bytes.fromhex(M.P_FP)
    q = bytes.fromhex(M.Q_UUID) + bytes.fromhex(M.Q_FP)
    indexes = cell_indexes(cfg.capacity, cfg.k, cfg.seed, p)
    assert indexes == cell_indexes(cfg.capacity, cfg.k, cfg.seed, q)
    table = _table(8, cfg)
    table.add(p, 1)
    table.add(q, -1)
    outcome = peel(table)
    assert outcome.complete is False
    assert outcome.residual.nonzero_cells >= len(indexes)
    for item in outcome.peeled:
        assert item.element not in (p, q)


def test_collision_cycle_through_files_and_larger_retry(tmp_path):
    story = M.story_cycle(str(tmp_path))
    small = DigestConfig(capacity=M.CYCLE_CAPACITY, k=M.CYCLE_K,
                         seed=M.CYCLE_SEED)
    retry = DigestConfig(capacity=M.CYCLE_CAPACITY_RETRY, k=M.CYCLE_K,
                         seed=M.CYCLE_SEED)
    for tag, cfg in (("small", small), ("retry", retry)):
        build_from_checklist(
            story["left"], cfg, digest_path=str(tmp_path / f"{tag}_l.dig")
        )
        build_from_checklist(
            story["right"], cfg, digest_path=str(tmp_path / f"{tag}_r.dig")
        )
    small_result = compare_digests(
        load_digest(str(tmp_path / "small_l.dig")),
        load_digest(str(tmp_path / "small_r.dig")),
    )
    assert small_result.complete is False
    assert small_result.differences == []
    assert small_result.residual.nonzero_cells > 0
    # The collision residue is unpeelable from the start: nothing at all is
    # reported as a difference, not even a partial clue list.
    assert small_result.peeled_count == 0
    clue_uuids = {c.uuid.hex() for c in small_result.peeled_clues}
    assert M.P_UUID not in clue_uuids and M.Q_UUID not in clue_uuids

    retry_result = compare_digests(
        load_digest(str(tmp_path / "retry_l.dig")),
        load_digest(str(tmp_path / "retry_r.dig")),
    )
    assert retry_result.complete is True
    kinds = {(d.kind.value, d.uuid_hex) for d in retry_result.differences}
    assert kinds == {("LEFT_ONLY", M.P_UUID), ("RIGHT_ONLY", M.Q_UUID)}


def test_pure_cell_requires_checksum_and_mapping():
    cfg = DigestConfig(capacity=8, k=2, seed=1)
    key = _el(1)
    good = Cell(1, key, element_check(key))
    assert cell_is_pure(good)

    wrong_check = Cell(1, key, b"\x00" * 8)
    assert not cell_is_pure(wrong_check)

    two = Cell(2, key, element_check(key))
    assert not cell_is_pure(two)

    zero = Cell(0, key, element_check(key))
    assert not cell_is_pure(zero)


def test_count_one_without_mapping_is_refused(monkeypatch):
    """A count of 1 is not proof; the hash must include the cell itself."""
    cfg = DigestConfig(capacity=8, k=3, seed=1)
    table = IbltTable(cfg)
    key = _el(1)
    table.cells[0] = Cell(1, key, element_check(key))
    # Sabotage the mapping so cell 0 is not among the element's cells.
    real = cell_indexes

    def wrong_mapping(capacity, k, seed, element):
        return [(i + 1) % capacity for i in real(capacity, k, seed, element)]

    monkeypatch.setattr("handover_iblt.peel.cell_indexes", wrong_mapping)
    outcome = peel(table)
    assert outcome.complete is False
    assert outcome.consistency_error is not None
    assert "does not map" in outcome.consistency_error


def test_zero_count_with_odd_key_material_is_not_pure():
    cfg = DigestConfig(capacity=8, k=2, seed=1)
    cell = Cell(0, b"\x01" * 48, b"\x01" * 8)
    assert not cell_is_pure(cell)


def test_identifier_with_two_same_sign_elements_is_refused():
    cfg = DigestConfig(capacity=16, k=3, seed=2)
    uuid_b = bytes.fromhex(M.A)
    left_only = [
        PeeledElement(1, uuid_b + bytes.fromhex(M.FP_A)),
        PeeledElement(1, uuid_b + bytes.fromhex(M.FP_C2)),
    ]
    with pytest.raises(UndecodedError, match="expected one element per side"):
        pair_differences(left_only)


def test_identical_pairs_cannot_survive():
    cfg = DigestConfig(capacity=16, k=3, seed=2)
    uuid_b = bytes.fromhex(M.A)
    items = [
        PeeledElement(1, uuid_b + bytes.fromhex(M.FP_A)),
        PeeledElement(-1, uuid_b + bytes.fromhex(M.FP_A)),
    ]
    with pytest.raises(UndecodedError, match="identical"):
        pair_differences(items)


def test_coincident_uuid_with_changed_fingerprint_pairs(tmp_path):
    """Two unrelated-looking peeled elements become one paired row."""
    cfg = DigestConfig(capacity=16, k=4, seed=3)
    left = tmp_path / "l.txt"
    right = tmp_path / "r.txt"
    left.write_text(f"{M.A},{M.FP_A}\n", encoding="utf-8")
    right.write_text(f"{M.A},{M.FP_C2}\n", encoding="utf-8")
    build_from_checklist(
        str(left), cfg, digest_path=str(tmp_path / "l.dig")
    )
    build_from_checklist(
        str(right), cfg, digest_path=str(tmp_path / "r.dig")
    )
    result = compare_digests(
        load_digest(str(tmp_path / "l.dig")),
        load_digest(str(tmp_path / "r.dig")),
    )
    assert result.complete
    assert len(result.differences) == 1
    assert result.differences[0].kind.value == "CONTENT_CHANGED"
    assert result.left_only == [] and result.right_only == []


def test_reconcile_result_keeps_inputs_byte_identical(tmp_path):
    story = M.story_main(str(tmp_path))
    cfg = DigestConfig(capacity=16, k=3, seed=4)
    build_from_checklist(str(story["left"]), cfg, digest_path=str(tmp_path / "l.dig"))
    build_from_checklist(str(story["right"]), cfg, digest_path=str(tmp_path / "r.dig"))
    a = str(tmp_path / "l.dig")
    b = str(tmp_path / "r.dig")
    original = (open(a, "rb").read(), open(b, "rb").read())
    left = load_digest(a)
    right = load_digest(b)
    compare_digests(left, right)
    assert left.to_bytes() == original[0]
    assert right.to_bytes() == original[1]
