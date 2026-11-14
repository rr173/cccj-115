"""Live fault-point and replay/conflict walkthrough.

Run ``python3 materials/fault_and_replay_demo.py``.

Steps
  1. Create a library and ingest two messages in a child process.
  2. Crash a child process at the injected fault point: template rows,
     revision rows and index leaves are staged, the message row is not, and
     ``os._exit`` abandons the transaction.
  3. Re-open: the staged work is gone; positions, template counts and
     revisions equal the pre-crash state.
  4. Re-feed the original batch: ids, revisions and membership match a
     fault-free reference library built from the same records.
  5. Replay a committed position with the same text (ignored) and with
     different text (conflict, nothing written); attempt a seq jump (batch
     refused, nothing written).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logtempl.library import (  # noqa: E402
    ConflictError,
    GapError,
    Library,
    Record,
)

FAULT_ENV = "LOGTEMPL_FAULT_BEFORE_MESSAGE"
CHILD = r"""
import sys
from logtempl.library import Library, Record
db, source, first, messages = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4:]
lib = Library.open(db)
out = lib.ingest_batch([Record(source, first + i, m) for i, m in enumerate(messages)])
print(f"accepted={len(out.accepted)} created={out.templates_created} "
      f"revised={out.templates_revised}")
"""


def run_child(db, source, first, messages, env=None):
    full = dict(os.environ)
    if env:
        full.update(env)
    return subprocess.run(
        [sys.executable, "-c", CHILD, db, source, str(first), *messages],
        capture_output=True, text=True, cwd=ROOT, env=full,
    )


def snapshot(lib):
    return {
        "next_seq": {s: lib.next_seq(s) for s, _n, _c in lib.source_report()},
        "templates": {
            tid: {
                "head": lib.template_head(tid),
                "revisions": {
                    r.revision: r.render
                    for r in lib.template_revisions(tid)
                },
                "members": [(m.source, m.seq) for m in lib.template_members(tid)],
            }
            for tid in lib.template_ids()
        },
        "messages": lib.message_count(),
        "revision_counter": lib.config_report()["revision_counter"],
    }


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "crashed.db")
        clean = os.path.join(tmp, "clean.db")
        messages = [
            "job 17 started on node a1",
            "job 18 started on node a1",
            "job 19 finished on node b2",
            "job 20 started on node c3",
        ]

        print("step 1: seed two messages")
        with Library.create(db, threshold="0.5", depth=2) as lib:
            lib.ingest_batch(
                [Record("s", i, m) for i, m in enumerate(messages[:2])]
            )
            before = snapshot(lib)
        print(json.dumps(before, indent=2, sort_keys=True))

        print("\nstep 2: crash at the fault point for s:2")
        proc = run_child(db, "s", 2, messages[2:3],
                         env={FAULT_ENV: "s:2"})
        print(f"  child exit code = {proc.returncode} (23 = injected os._exit)")
        assert proc.returncode == 23, proc.stderr

        print("\nstep 3: state after the crash (staged work rolled back)")
        with Library.open(db) as lib:
            after_crash = snapshot(lib)
            lib.verify_integrity()
        print(json.dumps(after_crash, indent=2, sort_keys=True))
        assert after_crash == before, (after_crash, before)

        print("\nstep 4: re-feed the original batch")
        proc = run_child(db, "s", 2, messages[2:])
        print(f"  child: {proc.stdout.strip()}")
        assert proc.returncode == 0, proc.stderr

        print("\n  fault-free reference run on the same records")
        with Library.create(clean, threshold="0.5", depth=2) as lib:
            lib.ingest_batch(
                [Record("s", i, m) for i, m in enumerate(messages)]
            )
            reference = snapshot(lib)
        with Library.open(db) as lib:
            recovered = snapshot(lib)
            lib.verify_integrity()
        for key in ("templates", "messages", "revision_counter", "next_seq"):
            assert recovered[key] == reference[key], key
        print("  recovered == fault-free reference (ids, revisions, members)")

        print("\nstep 5: replay and conflict behaviour")
        with Library.open(db) as lib:
            replay = lib.ingest_batch([Record("s", 0, messages[0])])
            print(f"  identical replay of s:0 -> accepted={replay.accepted} "
                  f"ignored={replay.ignored_replays}")
            assert replay.accepted == []
            try:
                lib.ingest_batch([Record("s", 0, "changed text")])
            except ConflictError as exc:
                print(f"  changed replay of s:0 -> conflict: {exc}")
            else:
                raise SystemExit("expected ConflictError")
            try:
                lib.ingest_batch([Record("s", 9, "jump")])
            except GapError as exc:
                print(f"  seq jump to 9 -> refused: {exc}")
            else:
                raise SystemExit("expected GapError")
            final = snapshot(lib)
            lib.verify_integrity()
        print(f"  committed positions: {final['next_seq']}, "
              f"messages={final['messages']}")
        assert final == reference
        print("\nall fault/replay assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
