"""Crash/fault-point behaviour and post-recovery equivalence.

The injected fault runs after templates/revisions/index rows are staged but
before the message row is written, then calls ``os._exit``.  Because the
whole batch is one SQLite transaction, the staged work is rolled back.  The
re-run must therefore produce exactly the same template ids, revision numbers
and membership as a fault-free run over the same records.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, dump_library, ingest, run_child, write_stream, run_cli
from logtempl.library import Library, Record

# Child program: monkeypatch the fault hook so it inspects the *open*, staged
# transaction before exiting.  This proves the fault point really sits after
# template + index updates and before message registration.
INSPECT_CHILD = r"""
import json, os, sys
import logtempl.library as L
from logtempl.library import Library, Record

db, source, seq, message, report_path = sys.argv[1:6]
seq = int(seq)
lib = Library.open(db)
seen = {}

def hook(src, s):
    if (src, s) != (source, seq):
        return
    c = lib.conn
    seen["templates"] = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
    seen["revisions"] = c.execute("SELECT COUNT(*) FROM template_revs").fetchone()[0]
    seen["nodes"] = c.execute("SELECT COUNT(*) FROM index_nodes").fetchone()[0]
    seen["leaves"] = c.execute("SELECT COUNT(*) FROM index_leaves").fetchone()[0]
    seen["messages"] = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    seen["pending_message"] = c.execute(
        "SELECT COUNT(*) FROM messages WHERE source=? AND seq=?", (src, s)
    ).fetchone()[0]
    seen["index_has_template"] = c.execute(
        "SELECT COUNT(*) FROM index_leaves WHERE template_id = "
        "(SELECT MAX(template_id) FROM templates)"
    ).fetchone()[0]
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)
    os._exit(23)

L._fault_point = hook
lib.ingest_batch([Record(source, seq, message)])
print("no fault fired")
"""


def _fault_child(db, source, seq, message, report_path, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", INSPECT_CHILD, db, source, str(seq), message,
         report_path],
        capture_output=True, text=True, env=full_env, cwd=ROOT,
    )


def _plain_child(db, source, seq, message):
    code = (
        "import sys\n"
        "from logtempl.library import Library, Record\n"
        "lib = Library.open(sys.argv[1])\n"
        "out = lib.ingest_batch([Record(sys.argv[2], int(sys.argv[3]), sys.argv[4])])\n"
        "print(out.accepted)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, db, source, str(seq), message],
        capture_output=True, text=True, cwd=ROOT,
    )


FAULT_ENV = "LOGTEMPL_FAULT_BEFORE_MESSAGE"


def test_fault_point_state_and_recovery(tmp_path):
    db = str(tmp_path / "lib.db")
    report = str(tmp_path / "staged.json")
    with Library.create(db, threshold="0.5") as lib:
        ingest(lib, ["job 10 started", "job 11 started"])

    # Batch that generalises template 1 at position 2.
    staged_message = "job 12 finished"
    proc = _fault_child(db, "s", 2, staged_message, report,
                        env={FAULT_ENV: "s:2"})
    assert proc.returncode == 23, proc.stderr
    info = json.loads(open(report, encoding="utf-8").read())
    # Inside the staged transaction: revision and index rows exist ...
    assert info["revisions"] >= 1
    assert info["leaves"] >= 1
    assert info["index_has_template"] == 1
    # ... but no message row for this position, and only the two old messages.
    assert info["pending_message"] == 0
    assert info["messages"] == 2

    # After the crash the uncommitted batch is fully rolled back.
    with Library.open(db) as lib:
        lib.verify_integrity()
        assert lib.next_seq("s") == 2
        assert lib.message_count() == 2
        assert lib.get_message("s", 2) is None
        head_before = lib.template_head(1)
        assert head_before["render"] == "job <INT> started"
        assert head_before["revision"] == 1
        assert head_before["member_count"] == 2
        meta_before = lib.config_report()

    # Re-run the original batch with no fault injected.
    proc = _plain_child(db, "s", 2, staged_message)
    assert proc.returncode == 0, proc.stderr
    with Library.open(db) as lib:
        lib.verify_integrity()
        head_after = lib.template_head(1)
        assert head_after["render"] == "job <INT> <*>"
        assert head_after["revision"] == 2
        assert head_after["member_count"] == 3
        assert lib.next_seq("s") == 3
        assert lib.get_message("s", 2).template_rev == 2
        assert lib.reconstruct_message("s", 2) == staged_message
        assert lib.config_report()["tx_counter"] == meta_before["tx_counter"] + 1


def test_recovered_run_equals_fault_free_run(tmp_path):
    messages = [
        "job 10 started on node a1",
        "job 11 started on node a1",
        "job 12 finished on node b2",
        "job 13 started on node c3",
        "job 14 failed on node c3",
    ]
    # Fault-free reference run.
    clean = str(tmp_path / "clean.db")
    with Library.create(clean, threshold="0.5", depth=2) as lib:
        ingest(lib, messages)
        reference = dump_library(lib, include_tx=False)
    # Run with a crash at position 2, then resume the same records.
    crashed = str(tmp_path / "crashed.db")
    with Library.create(crashed, threshold="0.5", depth=2) as lib:
        ingest(lib, messages[:2])
    proc = run_child(crashed, messages[2:3], start=2,
                     env={FAULT_ENV: "s:2"})
    assert proc.returncode == 23, proc.stderr
    # Restart and replay positions 2..4 in one batch.
    proc = run_child(crashed, messages[2:], start=2)
    assert proc.returncode == 0, proc.stderr
    with Library.open(crashed) as lib:
        lib.verify_integrity()
        recovered = dump_library(lib, include_tx=False)
    assert recovered == reference


def test_crash_then_replay_of_whole_source_is_idempotent(tmp_path):
    messages = [f"evt {n} code {1000 + n} status ok" for n in range(12)]
    db = str(tmp_path / "lib.db")
    with Library.create(db, threshold="0.5", depth=2,
                        template_cache=1, node_cache=2) as lib:
        ingest(lib, messages[:6])
    proc = run_child(db, messages[6:7], start=6, env={FAULT_ENV: "s:6"})
    assert proc.returncode == 23
    # Replay the complete source in chunks after restart: earlier positions
    # are ignored replicas, positions 6.. are accepted.
    proc = run_child(db, messages, start=0)
    assert proc.returncode == 0, proc.stderr
    with Library.open(db, template_cache=1, node_cache=1) as lib:
        assert lib.message_count() == len(messages)
        assert lib.next_seq("s") == len(messages)
        for seq, message in enumerate(messages):
            assert lib.reconstruct_message("s", seq) == message
        lib.verify_integrity()


def test_conflict_after_crash_is_refused(tmp_path):
    db = str(tmp_path / "lib.db")
    with Library.create(db, threshold="0.5") as lib:
        ingest(lib, ["alpha beta"])
    proc = run_child(db, ["alpha gamma"], start=1, env={FAULT_ENV: "s:1"})
    assert proc.returncode == 23
    # Committed position 0 has "alpha beta"; a changed replay conflicts.
    proc = run_cli(["ingest", db, "-"], env=None) if False else None
    with Library.open(db) as lib:
        from logtempl.library import ConflictError
        with pytest.raises(ConflictError):
            lib.ingest_batch([Record("s", 0, "alpha CHANGED")])
        assert lib.message_count() == 1
        assert lib.next_seq("s") == 1


def test_fault_point_before_any_template_mutation_is_not_reached(tmp_path):
    """A fault target that is never processed must not terminate the run."""
    db = str(tmp_path / "lib.db")
    with Library.create(db, threshold="0.5") as lib:
        ingest(lib, ["a b"])
    proc = run_child(db, ["c d"], start=1, env={FAULT_ENV: "s:99"})
    assert proc.returncode == 0, proc.stderr
    with Library.open(db) as lib:
        assert lib.message_count() == 2


def test_multi_record_batch_crash_leaves_no_partial_batch(tmp_path):
    db = str(tmp_path / "lib.db")
    messages = ["x y z", "x y w", "x q w", "p q r"]
    with Library.create(db, threshold="0.5") as lib:
        ingest(lib, messages[:1])
    # Crash on the second record of a 3-record batch.
    proc = run_child(db, messages[1:], start=1, env={FAULT_ENV: "s:2"})
    assert proc.returncode == 23
    with Library.open(db) as lib:
        assert lib.message_count() == 1
        assert lib.next_seq("s") == 1
        assert lib.template_head(1)["member_count"] == 1
        lib.verify_integrity()
    # Resume; all three should land and match a clean run.
    proc = run_child(db, messages[1:], start=1)
    assert proc.returncode == 0
    clean = str(tmp_path / "clean.db")
    with Library.create(clean, threshold="0.5") as lib:
        ingest(lib, messages)
        reference = dump_library(lib, include_tx=False)
    with Library.open(db) as lib:
        assert dump_library(lib, include_tx=False) == reference
