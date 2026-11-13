"""Fixed-size digest and streaming (bounded-memory) tests."""

from __future__ import annotations

import os
import resource
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handover_iblt.builder import build_from_checklist  # noqa: E402
from handover_iblt.config import DigestConfig  # noqa: E402
from handover_iblt.digest import load_digest  # noqa: E402
from handover_iblt.store import ChecklistStore  # noqa: E402


def _write_large(path, n: int, *, dup_every: int = 0) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            uuid_b = i.to_bytes(16, "big")
            fp_b = bytes([i % 251]) * 32
            fh.write(f"{uuid_b.hex()},{fp_b.hex()}\n")
            if dup_every and i % dup_every == 0:
                fh.write(f"{uuid_b.hex()},{fp_b.hex()}\n")


def test_digest_size_independent_of_record_count(tmp_path):
    cfg = DigestConfig(capacity=64, k=4, seed=10)
    sizes = []
    for n in (10, 1_000, 10_000):
        path = tmp_path / f"c{n}.txt"
        _write_large(path, n)
        out = tmp_path / f"c{n}.dig"
        build_from_checklist(str(path), cfg, digest_path=str(out))
        sizes.append(out.stat().st_size)
    assert len(set(sizes)) == 1
    assert sizes[0] == 96 + 64 * cfg.capacity


def test_common_count_grows_but_digest_stays_fixed(tmp_path):
    cfg = DigestConfig(capacity=128, k=4, seed=10)
    fixed = tmp_path / "left.txt"
    _write_large(fixed, 3)
    left_out = tmp_path / "left.dig"
    build_from_checklist(str(fixed), cfg, digest_path=str(left_out))

    right_sizes = []
    for n in (100, 5_000, 20_000):
        right = tmp_path / f"right{n}.txt"
        _write_large(right, 3)
        with open(right, "a", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(
                    f"{(i + 10_000).to_bytes(16, 'big').hex()},"
                    f"{(bytes([i % 251]) * 32).hex()}\n"
                )
        right_out = tmp_path / f"right{n}.dig"
        build_from_checklist(str(right), cfg, digest_path=str(right_out))
        right_sizes.append(right_out.stat().st_size)
        assert load_digest(str(right_out)).unique_elements >= n
    assert set(right_sizes) == {left_out.stat().st_size}


def test_streaming_memory_does_not_grow_with_checklist(tmp_path):
    small = tmp_path / "small.txt"
    big = tmp_path / "big.txt"
    _write_large(small, 2_000, dup_every=10)
    _write_large(big, 120_000, dup_every=10)
    cfg = DigestConfig(capacity=4096, k=4, seed=11)

    build_from_checklist(str(small), cfg, digest_path=str(tmp_path / "s.dig"))
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    build_from_checklist(str(big), cfg, digest_path=str(tmp_path / "b.dig"))
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    # 120k rows with 12k duplicates: a set/list holding all identities would
    # add tens of MB.  A 40 MB band covers allocator noise.
    assert after - before < 40.0, f"RSS grew from {before} to {after} MB"
    digest = load_digest(str(tmp_path / "b.dig"))
    assert digest.source_lines == 120_000 + 12_000
    assert digest.unique_elements == 120_000
    assert digest.duplicate_lines == 12_000


def test_store_row_count_and_filesize_scale_with_identities(tmp_path):
    """The SQLite store is line-independent: it holds bytes on disk."""
    cfg = DigestConfig(capacity=64, k=4, seed=12)
    path = tmp_path / "store_in.txt"
    _write_large(path, 2_000, dup_every=5)
    store = tmp_path / "identities.db"
    build_from_checklist(
        str(path), cfg, store_path=str(store),
        digest_path=str(tmp_path / "d.dig"),
    )
    assert store.exists()
    with ChecklistStore.open_existing(str(store)) as s:
        assert s.count() == 2_000
        assert s.meta()["source_lines"] == 2_400
        assert s.meta()["duplicate_lines"] == 400


def test_cli_demo_run(tmp_path):
    """The documented demo must exit successfully in a clean workdir."""
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [
            sys.executable, "-m", "handover_iblt.cli", "demo",
            "--workdir", str(tmp_path), "--capacity", "32", "--k", "4",
            "--quiet",
        ],
        cwd=pkg_root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": pkg_root},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Demo complete" in proc.stdout


def test_cli_compare_reports_incomplete_with_exit_three(tmp_path):
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONPATH": pkg_root}
    story_left = tmp_path / "l.txt"
    story_right = tmp_path / "r.txt"
    _diff_rows(story_left, 30)
    _diff_rows(story_right, 30, offset=1)
    for name, src in (("l.dig", story_left), ("r.dig", story_right)):
        proc = subprocess.run(
            [sys.executable, "-m", "handover_iblt.cli", "build",
             str(src), str(tmp_path / name), "--capacity", "16", "--k", "3"],
            cwd=pkg_root, capture_output=True, text=True, check=False, env=env,
        )
        assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        [sys.executable, "-m", "handover_iblt.cli", "compare",
         str(tmp_path / "l.dig"), str(tmp_path / "r.dig")],
        cwd=pkg_root, capture_output=True, text=True, check=False, env=env,
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "STATUS: INCOMPLETE" in proc.stdout
    assert "no difference list is claimed" in proc.stdout


def _diff_rows(path, n: int, *, offset: int = 0) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            uuid_b = (i + offset).to_bytes(16, "big")
            fh.write(f"{uuid_b.hex()},{(bytes([i % 251]) * 32).hex()}\n")
