"""Differential tests against a brute-force set reference.

Each trial builds two random checklists with a known number of differences,
builds real digest files and compares the production result with the
independent reference computed directly from the two element sets.
"""

from __future__ import annotations

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reference as R  # noqa: E402

from handover_iblt.builder import build_from_checklist  # noqa: E402
from handover_iblt.config import DigestConfig  # noqa: E402
from handover_iblt.digest import load_digest  # noqa: E402
from handover_iblt.reconcile import compare_digests  # noqa: E402


def _uuid(rng) -> bytes:
    return rng.getrandbits(128).to_bytes(16, "big")


def _fp(rng) -> bytes:
    return rng.getrandbits(256).to_bytes(32, "big")


def _write(path, elements, order):
    with open(path, "w", encoding="utf-8") as fh:
        for uuid_b, fp_b in order(elements):
            fh.write(f"{uuid_b.hex()},{fp_b.hex()}\n")


def _random_case(rng, capacity, k, n_common, n_left_only, n_right_only,
                 n_changed):
    common = [(_uuid(rng), _fp(rng)) for _ in range(n_common)]
    left = list(common)
    right = list(common)
    for _ in range(n_left_only):
        left.append((_uuid(rng), _fp(rng)))
    for _ in range(n_right_only):
        right.append((_uuid(rng), _fp(rng)))
    for _ in range(n_changed):
        uuid_b, _ = common[rng.randrange(len(common))]
        right = [(u, (_fp(rng) if u == uuid_b else f)) for u, f in right]
    return left, right


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_cases_match_reference(tmp_path, seed):
    rng = random.Random(seed)
    cfg = DigestConfig(capacity=256, k=4, seed=seed)
    for trial in range(6):
        left, right = _random_case(
            rng,
            capacity=cfg.capacity,
            k=cfg.k,
            n_common=rng.randrange(0, 30),
            n_left_only=rng.randrange(0, 6),
            n_right_only=rng.randrange(0, 6),
            n_changed=rng.randrange(0, 3),
        )
        # Duplicates and order do not change identity.
        left_rows = left + left[:3]
        right_rows = right + right[:2]

        def shuffled(rows):
            copy = list(rows)
            rng.shuffle(copy)
            return copy

        lp = tmp_path / f"l{trial}.txt"
        rp = tmp_path / f"r{trial}.txt"
        _write(lp, left_rows, shuffled)
        _write(rp, right_rows, shuffled)
        build_from_checklist(
            str(lp), cfg, digest_path=str(tmp_path / f"l{trial}.dig")
        )
        build_from_checklist(
            str(rp), cfg, digest_path=str(tmp_path / f"r{trial}.dig")
        )
        result = compare_digests(
            load_digest(str(tmp_path / f"l{trial}.dig")),
            load_digest(str(tmp_path / f"r{trial}.dig")),
        )
        assert result.complete, f"trial {trial} should peel"
        expected = R.expected_differences(left_rows, right_rows)
        assert R.flatten(result) == expected, f"trial {trial}"


@pytest.mark.parametrize("capacity,k", [(16, 2), (32, 3), (64, 4)])
def test_empty_difference_always_peels(tmp_path, capacity, k):
    rng = random.Random(capacity * 100 + k)
    cfg = DigestConfig(capacity=capacity, k=k, seed=99)
    rows = [(_uuid(rng), _fp(rng)) for _ in range(5)]
    _write(tmp_path / "a.txt", rows, lambda x: x)
    _write(tmp_path / "b.txt", rows[::-1], lambda x: x)
    build_from_checklist(
        str(tmp_path / "a.txt"), cfg, digest_path=str(tmp_path / "a.dig")
    )
    build_from_checklist(
        str(tmp_path / "b.txt"), cfg, digest_path=str(tmp_path / "b.dig")
    )
    result = compare_digests(
        load_digest(str(tmp_path / "a.dig")),
        load_digest(str(tmp_path / "b.dig")),
    )
    assert result.complete
    assert result.differences == []
    assert result.peeled_count == 0
