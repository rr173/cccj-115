"""End-to-end tests of the documented handover stories."""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reference as R  # noqa: E402

from handover_iblt import materials as M  # noqa: E402
from handover_iblt.config import DigestConfig  # noqa: E402
from handover_iblt.digest import (  # noqa: E402
    build_digest_from_store,
    load_digest,
)
from handover_iblt.builder import build_from_checklist  # noqa: E402
from handover_iblt.errors import (  # noqa: E402
    ChecklistConflictError,
    DigestMismatchError,
)
from handover_iblt.reconcile import compare_digests  # noqa: E402
from handover_iblt.store import ChecklistStore  # noqa: E402


def _digest_for(path: str, cfg: DigestConfig, tmp_path, name: str):
    out = str(tmp_path / name)
    build_from_checklist(path, cfg, digest_path=out)
    return load_digest(out)


def _write(tmp_path, name: str, rows: list[tuple[str, str]]) -> str:
    path = str(tmp_path / name)
    with open(path, "w", encoding="utf-8") as fh:
        for uuid_hex, fp_hex in rows:
            fh.write(f"{uuid_hex},{fp_hex}\n")
    return path


SMALL = DigestConfig(capacity=32, k=4, seed=20261113)


def test_sender_abc_receiver_bcd(tmp_path):
    s = M.story_main(str(tmp_path))
    left = _digest_for(s["left"], SMALL, tmp_path, "l.dig")
    right = _digest_for(s["right"], SMALL, tmp_path, "r.dig")
    result = compare_digests(left, right)
    assert result.complete
    kinds = {(d.kind.value, d.uuid_hex) for d in result.differences}
    assert kinds == {("LEFT_ONLY", M.A), ("RIGHT_ONLY", M.D)}
    assert result.left_only[0].left_fingerprint.hex() == M.FP_A
    assert result.right_only[0].right_fingerprint.hex() == M.FP_D


def test_changed_content_is_one_paired_row(tmp_path):
    c = M.story_changed(str(tmp_path))
    left = _digest_for(c["left"], SMALL, tmp_path, "l.dig")
    right = _digest_for(c["right"], SMALL, tmp_path, "r.dig")
    result = compare_digests(left, right)
    assert result.complete
    assert len(result.changed) == 1
    row = result.changed[0]
    assert row.uuid_hex == M.C
    assert row.left_fingerprint.hex() == M.FP_C
    assert row.right_fingerprint.hex() == M.FP_C2
    assert len(result.left_only) == 1 and len(result.right_only) == 1


def test_direction_reverses_when_arguments_swap(tmp_path):
    s = M.story_main(str(tmp_path))
    left = _digest_for(s["left"], SMALL, tmp_path, "l.dig")
    right = _digest_for(s["right"], SMALL, tmp_path, "r.dig")
    forward = R.flatten(compare_digests(left, right))
    reverse = R.flatten(compare_digests(right, left))
    assert forward["left_only"] == reverse["right_only"]
    assert forward["right_only"] == reverse["left_only"]
    assert forward["left_only"] == [
        (bytes.fromhex(M.A), bytes.fromhex(M.FP_A))
    ]
    assert forward["right_only"] == [
        (bytes.fromhex(M.D), bytes.fromhex(M.FP_D))
    ]


def test_identical_different_order_and_duplicates_is_empty(tmp_path):
    s = M.story_same(str(tmp_path))
    left = _digest_for(s["left"], SMALL, tmp_path, "l.dig")
    right = _digest_for(s["right"], SMALL, tmp_path, "r.dig")
    result = compare_digests(left, right)
    assert result.complete
    assert result.differences == []
    assert result.left_unique == 3
    assert result.right_unique == 3


def test_duplicate_lines_are_counted_once(tmp_path):
    cfg = DigestConfig(capacity=16, k=3, seed=1)
    path = M.story_duplicates(str(tmp_path))
    outcome = build_from_checklist(
        path, cfg, digest_path=str(tmp_path / "d.dig")
    )
    assert outcome.source_lines == 5
    assert outcome.unique_elements == 2
    assert outcome.duplicate_lines == 3
    assert outcome.digest.table.distinct_elements() == 2


def test_conflict_refused_and_reports_two_lines(tmp_path):
    cfg = DigestConfig(capacity=16, k=3, seed=1)
    path = M.story_conflict(str(tmp_path))
    out = str(tmp_path / "c.dig")
    with pytest.raises(ChecklistConflictError) as info:
        build_from_checklist(path, cfg, digest_path=out)
    text = str(info.value)
    assert M.A in text
    assert "line 1" in text and "line 4" in text
    assert M.FP_A in text and M.FP_C2 in text
    assert not os.path.exists(out)


def test_conflict_across_batches_is_detected(tmp_path):
    cfg = DigestConfig(capacity=8, k=2, seed=1)
    rows = [(M.A, M.FP_A)]
    rows += [(f"{i:032x}", hashlib.sha256(bytes([i])).hexdigest())
             for i in range(10)]
    rows.append((M.A, M.FP_C2))
    path = _write(tmp_path, "batch.txt", rows)
    with pytest.raises(ChecklistConflictError) as info:
        build_from_checklist(
            path, cfg, digest_path=str(tmp_path / "b.dig"), batch_size=4
        )
    assert "line 1" in str(info.value) and "line 12" in str(info.value)


def test_store_uses_sqlite_uniqueness(tmp_path):
    store = ChecklistStore(str(tmp_path / "store.db"))
    try:
        stats = store.ingest_lines(
            iter([
                f"{M.A},{M.FP_A}\n",
                f"{M.A},{M.FP_A}\n",
                f"{M.B},{M.FP_B}\n",
            ])
        )
        rows = store.connection.execute(
            "SELECT uuid, fingerprint, first_line FROM elements ORDER BY uuid"
        ).fetchall()
        assert len(rows) == 2
        assert stats["source_lines"] == 3
        assert stats["unique_elements"] == 2
        assert stats["duplicate_lines"] == 1
        # The stored identity is bytes, not hex text.
        assert all(isinstance(r[0], bytes) and len(r[0]) == 16 for r in rows)
        assert all(isinstance(r[1], bytes) and len(r[1]) == 32 for r in rows)
    finally:
        store.close()


def test_case_of_hex_does_not_matter(tmp_path):
    cfg = DigestConfig(capacity=16, k=2, seed=2)
    upper = _write(tmp_path, "upper.txt", [(M.A.upper(), M.FP_A.upper())])
    lower = _write(tmp_path, "lower.txt", [(M.A, M.FP_A)])
    du = build_from_checklist(
        upper, cfg, digest_path=str(tmp_path / "u.dig")
    ).digest
    dl = build_from_checklist(
        lower, cfg, digest_path=str(tmp_path / "l.dig")
    ).digest
    assert du.table.to_body() == dl.table.to_body()


def test_mismatched_configuration_is_refused_and_digests_untouched(tmp_path):
    s = M.story_main(str(tmp_path))
    a = str(tmp_path / "a.dig")
    b = str(tmp_path / "b.dig")
    build_from_checklist(s["left"], SMALL, digest_path=a)
    build_from_checklist(
        s["right"], DigestConfig(capacity=64, k=4, seed=20261113),
        digest_path=b,
    )
    before = {p: open(p, "rb").read() for p in (a, b)}
    with pytest.raises(DigestMismatchError):
        compare_digests(load_digest(a), load_digest(b))
    assert {p: open(p, "rb").read() for p in (a, b)} == before


def test_digest_is_self_contained(tmp_path):
    """The digest can be compared in another process without the checklist."""
    s = M.story_main(str(tmp_path))
    a = str(tmp_path / "a.dig")
    b = str(tmp_path / "b.dig")
    build_from_checklist(s["left"], SMALL, digest_path=a)
    build_from_checklist(s["right"], SMALL, digest_path=b)
    os.remove(s["left"])
    os.remove(s["right"])
    result = compare_digests(load_digest(a), load_digest(b))
    assert result.complete
    assert len(result.differences) == 2


def test_build_digest_from_store_reads_disk(tmp_path):
    """A stored checklist on disk can be folded into a digest later."""
    cfg = DigestConfig(capacity=16, k=3, seed=3)
    store_path = str(tmp_path / "later.db")
    s = M.story_main(str(tmp_path))
    build_from_checklist(
        s["left"], cfg, store_path=store_path, digest_path=str(tmp_path / "l.dig")
    )
    store = ChecklistStore.open_existing(store_path)
    try:
        digest = build_digest_from_store(store, cfg)
    finally:
        store.close()
    disk = load_digest(str(tmp_path / "l.dig"))
    assert digest.table.to_body() == disk.table.to_body()
    assert digest.unique_elements == 3


def test_empty_checklist_digest_compares_clean(tmp_path):
    cfg = DigestConfig(capacity=8, k=2, seed=1)
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    build_from_checklist(
        str(empty), cfg, digest_path=str(tmp_path / "e.dig")
    )
    digest = load_digest(str(tmp_path / "e.dig"))
    assert digest.unique_elements == 0
    result = compare_digests(digest, digest)
    assert result.complete and result.differences == []


def test_cli_json_direction_fields(tmp_path):
    """JSON rows carry both fingerprint fields with explicit nulls."""
    s = M.story_changed(str(tmp_path))
    a = str(tmp_path / "l.dig")
    b = str(tmp_path / "r.dig")
    build_from_checklist(s["left"], SMALL, digest_path=a)
    build_from_checklist(s["right"], SMALL, digest_path=b)
    payload = compare_digests(
        load_digest(a), load_digest(b)
    ).as_dict()
    for row in payload["differences"]:
        assert set(row) == {
            "kind", "uuid", "left_fingerprint", "right_fingerprint"
        }
        if row["kind"] == "CONTENT_CHANGED":
            assert row["left_fingerprint"] and row["right_fingerprint"]
        elif row["kind"] == "LEFT_ONLY":
            assert row["right_fingerprint"] is None
        else:
            assert row["left_fingerprint"] is None
