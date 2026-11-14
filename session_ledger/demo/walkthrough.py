"""End-to-end hand-computed walkthrough: empty store -> many corrections.

Run offline (no network, no analytics service)::

    python3 -I demo/walkthrough.py

The script builds every JSONL patch itself, prints the stats of each round and
the sessions/memberships that result, and asserts the hand-computed timeline
documented in docs/WALKTHROUGH.md.  The crash hook is exercised in a real child
process; the parent reads the database back to prove the rollback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_ledger import (  # noqa: E402
    PatchError,
    RevisionConflictError,
    StaleRevisionError,
    all_sessions,
    counts,
    event_header,
    events_in_interval,
    idle_gap,
    ingest,
    initialise,
    parse_patch,
    session_events,
    sessions_in_interval,
)
from session_ledger.store import EXIT_POINT_CODE  # noqa: E402

GAP = 10


def write_patch(directory: Path, name: str, lines: list[dict]) -> Path:
    path = directory / name
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def snapshot(db_path: Path) -> dict[str, list]:
    sessions = [
        (s.user_id, s.session_id, s.start_ms, s.end_ms, s.event_count, s.span_ms)
        for s in all_sessions(db_path)
    ]
    members = {
        row[1]: [e.event_id for e in session_events(db_path, row[1])]
        for row in sessions
    }
    return {"sessions": sessions, "members": members}


def show(title: str, db_path: Path, stats=None) -> None:
    print(f"\n=== {title} ===")
    if stats is not None:
        print("stats:", json.dumps(stats.as_dict(), sort_keys=True))
    snap = snapshot(db_path)
    for row in snap["sessions"]:
        print("session", row, "members", snap["members"][row[1]])


def expect(actual: list, wanted: list, label: str) -> None:
    if actual != wanted:
        raise AssertionError(
            f"{label} mismatch\n wanted={wanted}\n actual={actual}"
        )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="session-ledger-demo-"))
    db_path = workdir / "ledger.sqlite"
    initialise(db_path, GAP)
    print(f"store: {db_path}  G={idle_gap(db_path)} ms")

    # Round 0: equal-to-G link, just-over-G split, same-instant ids, long chain.
    r0 = write_patch(workdir, "r0.jsonl", [
        {"op": "put", "event_id": "a", "revision": 1, "user_id": "alice", "event_ms": 0},
        {"op": "put", "event_id": "b", "revision": 1, "user_id": "alice", "event_ms": 10},
        {"op": "put", "event_id": "c", "revision": 1, "user_id": "alice", "event_ms": 21},
        {"op": "put", "event_id": "d", "revision": 1, "user_id": "alice", "event_ms": 35},
        {"op": "put", "event_id": "e", "revision": 1, "user_id": "alice", "event_ms": 35},
        {"op": "put", "event_id": "f", "revision": 1, "user_id": "alice", "event_ms": 45},
        {"op": "put", "event_id": "g", "revision": 1, "user_id": "alice", "event_ms": 60},
        {"op": "put", "event_id": "x", "revision": 1, "user_id": "bob", "event_ms": 0},
    ])
    st = ingest(db_path, parse_patch(r0))
    show("R0 initial timeline", db_path, st)
    snap = snapshot(db_path)
    # Hand computation (G=10): a@0--b@10 gap 10 = G, linked; b->c@21 gap 11
    # just over G, so c@21 is alone; d@35 and e@35 share an instant (both
    # kept, id order d,e), f@45 continues (10 = G); f@45 -> g@60 gap 15 > G
    # so g is a singleton.  Chain d-e-f spans 10 >? its span is 45-35=10, but
    # long-span chains appear from R1 onward.
    expect(snap["sessions"], [
        ("alice", "a", 0, 10, 2, 10),
        ("alice", "c", 21, 21, 1, 0),
        ("alice", "d", 35, 45, 3, 10),
        ("alice", "g", 60, 60, 1, 0),
        ("bob", "x", 0, 0, 1, 0),
    ], "R0 sessions")
    expect(snap["members"], {
        "a": ["a", "b"],
        "c": ["c"],
        "d": ["d", "e", "f"],
        "g": ["g"],
        "x": ["x"],
    }, "R0 members")
    expect(st.sessions_before, 0, "R0 before count")
    expect(st.sessions_after, 5, "R0 after count")

    # Closed interval overlap query: window [11, 22] catches the [0,10]
    # session (touches nothing?) -- no: it catches [21,21] and only sessions
    # intersecting. [0,10] does not intersect [11,22]; prove overlap semantics
    # with a window that starts inside the long chain.
    hits = sessions_in_interval(db_path, "alice", 40, 55)
    expect([s.session_id for s in hits], ["d"], "interval overlap")
    hits = sessions_in_interval(db_path, "alice", 5, 15)
    expect([s.session_id for s in hits], ["a"], "interval covering start")

    # Round 1: bridge point merges three sessions into one long chain.
    r1 = write_patch(workdir, "r1.jsonl", [
        {"op": "put", "event_id": "h", "revision": 1, "user_id": "alice", "event_ms": 15},
        {"op": "put", "event_id": "i", "revision": 1, "user_id": "alice", "event_ms": 28},
    ])
    st = ingest(db_path, parse_patch(r1))
    show("R1 bridge points merge all alice sessions", db_path, st)
    # h@15 bridges b@10..c@21; i@28 bridges c@21..d@35.  The merged run ends
    # at f@45; g@60 stays 15 ms away (> G), so the right-expansion probe at g
    # stops and g's singleton is never touched or even re-read.
    snap = snapshot(db_path)
    expect(snap["sessions"], [
        ("alice", "a", 0, 45, 8, 45),
        ("alice", "g", 60, 60, 1, 0),
        ("bob", "x", 0, 0, 1, 0),
    ], "R1 sessions")
    expect(snap["members"]["a"],
           ["a", "b", "h", "c", "i", "d", "e", "f"], "R1 members")
    expect(st.per_user["alice"].resegment_rows_read, 8,
           "R1 only reads the merged neighbourhood, not g")

    # Round 2: retract the bridges -> chain splits again (with new boundaries).
    r2 = write_patch(workdir, "r2.jsonl", [
        {"op": "retract", "event_id": "h", "revision": 2},
        {"op": "retract", "event_id": "i", "revision": 2},
    ])
    st = ingest(db_path, parse_patch(r2))
    show("R2 retract bridges -> split", db_path, st)
    snap = snapshot(db_path)
    expect(snap["sessions"], [
        ("alice", "a", 0, 10, 2, 10),
        ("alice", "c", 21, 21, 1, 0),
        ("alice", "d", 35, 45, 3, 10),
        ("alice", "g", 60, 60, 1, 0),
        ("bob", "x", 0, 0, 1, 0),
    ], "R2 sessions")
    expect(snap["members"], {"a": ["a", "b"], "c": ["c"],
                             "d": ["d", "e", "f"], "g": ["g"],
                             "x": ["x"]},
           "R2 members")
    expect(counts(db_path)["retracted_events"], 2, "R2 retracted headers kept")

    # Round 3: retract the smallest identity of the long chain; session id
    # migrates and surviving members repoint.
    r3 = write_patch(workdir, "r3.jsonl", [
        {"op": "retract", "event_id": "d", "revision": 2},
    ])
    st = ingest(db_path, parse_patch(r3))
    show("R3 retract segment-minimum id -> session id moves", db_path, st)
    snap = snapshot(db_path)
    expect(snap["sessions"], [
        ("alice", "a", 0, 10, 2, 10),
        ("alice", "c", 21, 21, 1, 0),
        ("alice", "e", 35, 45, 2, 10),
        ("alice", "g", 60, 60, 1, 0),
        ("bob", "x", 0, 0, 1, 0),
    ], "R3 sessions")
    expect(snap["members"]["e"], ["e", "f"], "R3 repointed members")

    # Round 4: cross-user move (e -> bob) and time change (f @9) which joins
    # alice's first session.  Final state must reflect the combined patch, not
    # per-line transient states.
    r4 = write_patch(workdir, "r4.jsonl", [
        {"op": "put", "event_id": "e", "revision": 3, "user_id": "bob", "event_ms": 7},
        {"op": "put", "event_id": "f", "revision": 2, "user_id": "alice", "event_ms": 9},
    ])
    st = ingest(db_path, parse_patch(r4))
    show("R4 cross-user move + time change (batch semantics)", db_path, st)
    snap = snapshot(db_path)
    expect(snap["sessions"], [
        ("alice", "a", 0, 10, 3, 10),
        ("alice", "c", 21, 21, 1, 0),
        ("alice", "g", 60, 60, 1, 0),
        ("bob", "e", 0, 7, 2, 7),
    ], "R4 sessions")
    expect(snap["members"]["a"], ["a", "f", "b"], "R4 alice time order")
    expect(snap["members"]["e"], ["x", "e"], "R4 bob merged, id min is e")

    # Round 5: same-batch multi-point joint bridging, plus a simultaneous
    # retract.  p@30 links c(21)->p(30)->g(60)?  60-30=30 >10; add q@50 so
    # p(30)-q(50) gap 20 still too big -> use q@40, r@50: c21 p30 q40 r50 g60
    # all gaps <=10.  Retract a to prove deletion and joint bridging coexist.
    r5 = write_patch(workdir, "r5.jsonl", [
        {"op": "put", "event_id": "p", "revision": 1, "user_id": "alice", "event_ms": 30},
        {"op": "put", "event_id": "q", "revision": 1, "user_id": "alice", "event_ms": 40},
        {"op": "put", "event_id": "r", "revision": 1, "user_id": "alice", "event_ms": 50},
        {"op": "retract", "event_id": "a", "revision": 2},
    ])
    st = ingest(db_path, parse_patch(r5))
    show("R5 joint bridging + simultaneous retract", db_path, st)
    snap = snapshot(db_path)
    # f@9,b@10 stay separate from c@21 (gap 11 > G); the new p/q/r chain
    # joins c to g.  a's retraction removed the old segment-minimum id, so the
    # surviving [9,10] segment is now named after b.
    expect(snap["sessions"], [
        ("alice", "b", 9, 10, 2, 1),
        ("alice", "c", 21, 60, 5, 39),
        ("bob", "e", 0, 7, 2, 7),
    ], "R5 sessions")
    expect(snap["members"]["c"],
           ["c", "p", "q", "r", "g"], "R5 joint bridge members")
    expect(snap["members"]["b"], ["f", "b"], "R5 retracted a leaves b segment")

    # Round 6: retract an entire session (bob's singleton x) -> no empty
    # session remains; then restore it with a newer put.
    r6 = write_patch(workdir, "r6.jsonl", [
        {"op": "retract", "event_id": "x", "revision": 2},
    ])
    st = ingest(db_path, parse_patch(r6))
    show("R6 retract whole session away", db_path, st)
    expect(event_header(db_path, "x").is_retracted, True, "x retracted")
    expect(
        [s.session_id for s in all_sessions(db_path) if s.user_id == "bob"],
        ["e"], "R6 no empty session",
    )
    r6b = write_patch(workdir, "r6b.jsonl", [
        {"op": "put", "event_id": "x", "revision": 3, "user_id": "bob", "event_ms": 5},
    ])
    st = ingest(db_path, parse_patch(r6b))
    show("R6b restore retracted identity", db_path, st)
    snap = snapshot(db_path)
    expect([s for s in snap["sessions"] if s[0] == "bob"], [
        ("bob", "e", 5, 7, 2, 2),
    ], "R6b restored session joins neighbour, id stays min(e)")

    # Round 7: same patch again -> entirely no-op.
    st = ingest(db_path, parse_patch(r6b))
    show("R7 repeat patch -> all no-op", db_path, st)
    expect(st.effective_changes, 0, "R7 no effective change")
    expect(st.noops, 1, "R7 one noop")
    expect(st.resegment_rows_read, 0, "R7 no historical reads")
    expect(st.sessions_before, st.sessions_after, "R7 counts unchanged")

    # Round 8: "reverse order" delivery -- a higher revision is already
    # stored, a later-arriving lower revision is stale and aborts the whole
    # patch.  A companion valid line in the same stale patch must not apply.
    r8_seed = write_patch(workdir, "r8_seed.jsonl", [
        {"op": "put", "event_id": "z", "revision": 3, "user_id": "bob",
         "event_ms": 100},
    ])
    ingest(db_path, parse_patch(r8_seed))
    r8 = write_patch(workdir, "r8.jsonl", [
        {"op": "put", "event_id": "w", "revision": 1, "user_id": "bob",
         "event_ms": 110},
        {"op": "put", "event_id": "z", "revision": 2, "user_id": "bob",
         "event_ms": 90},
    ])
    try:
        ingest(db_path, parse_patch(r8))
    except StaleRevisionError as exc:
        print(f"\nR8 reverse-order patch refused as stale: {exc}")
    else:
        raise AssertionError("R8 stale line must abort the whole patch")
    expect(event_header(db_path, "z").event_ms, 100, "R8 z keeps rev 3 value")
    expect(event_header(db_path, "w"), None, "R8 companion line not applied")
    # Skipping revisions is legal: rev 5 jumps ahead and overwrites rev 3.
    r8_jump = write_patch(workdir, "r8_jump.jsonl", [
        {"op": "put", "event_id": "z", "revision": 5, "user_id": "bob",
         "event_ms": 101},
    ])
    st = ingest(db_path, parse_patch(r8_jump))
    expect(st.effective_changes, 1, "R8 jump revision applied")
    expect(event_header(db_path, "z").revision, 5, "R8 stored revision is 5")

    # Round 9: same-revision conflict -> earlier valid lines in the patch must
    # not have taken effect either.
    before = snapshot(db_path)
    r9 = write_patch(workdir, "r9.jsonl", [
        {"op": "put", "event_id": "k", "revision": 1, "user_id": "alice", "event_ms": 70},
        {"op": "put", "event_id": "f", "revision": 2, "user_id": "alice", "event_ms": 99},
    ])
    try:
        ingest(db_path, parse_patch(r9))
    except RevisionConflictError as exc:
        print(f"R9 conflict refused: {exc}")
    else:
        raise AssertionError("R9 conflict must abort the whole patch")
    expect(snapshot(db_path), before, "R9 earlier valid line not applied")

    # Structural patch errors, each pointing at a physical line.
    bad_cases: list[tuple[list[str], str]] = [
        (['{"op": "put", "event_id": "m", "revision": 1, '
          '"user_id": "u", "event_ms": 1, "extra": 2}'], "extra field"),
        (['{"op": "retract", "event_id": "m", "revision": 2, '
          '"user_id": "u"}'], "retract with user_id"),
        (['{"op": "put", "event_id": "m", "revision": true, '
          '"user_id": "u", "event_ms": 1}'], "boolean revision"),
        (['{"op": "put", "event_id": "m", "revision": 1, '
          '"user_id": "u", "event_ms": 9223372036854775808}'], "int64 overflow"),
        (['{"op": "put", "event_id": "m", "revision": 1, '
          '"user_id": "", "event_ms": 1}'], "empty user id"),
        (['{"op": "put", "event_id": "m", "revision": 1, '
          '"user_id": "u", "event_ms": 1, "event_ms": 2}'], "duplicate key"),
        (['{"op": "put", "event_id": "m", "revision": 1, '
          '"user_id": "u", "event_ms": 1}',
          '{"op": "put", "event_id": "m", "revision": 2, '
          '"user_id": "u", "event_ms": 2}'], "duplicate identity in patch"),
    ]
    for i, (lines, label) in enumerate(bad_cases):
        path = workdir / f"bad{i}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            ingest(db_path, parse_patch(path))
        except PatchError as exc:
            print(f"bad patch ({label}) refused: {exc}")
        else:
            raise AssertionError(f"bad patch not refused: {label}")
    expect(snapshot(db_path), before, "no bad patch changed state")

    # Empty patch is legal and does nothing.
    empty = write_patch(workdir, "empty.jsonl", [])
    st = ingest(db_path, parse_patch(empty))
    expect(st.effective_changes, 0, "empty patch no change")
    print("\nempty patch accepted:", st.as_dict())

    # Unknown-identity retract is refused.
    unknown = write_patch(workdir, "unknown.jsonl", [
        {"op": "retract", "event_id": "ghost", "revision": 1},
    ])
    try:
        ingest(db_path, parse_patch(unknown))
    except PatchError as exc:
        print("unknown retract refused:", exc)
    else:
        raise AssertionError("unknown retract must be refused")

    # ---- real-process crash hook: rollback and re-ingest -----------------
    hook_patch = write_patch(workdir, "hook.jsonl", [
        {"op": "put", "event_id": "h", "revision": 3, "user_id": "alice",
         "event_ms": 15},
    ])
    pre_hook = snapshot(db_path)
    runner = Path(__file__).resolve().parent / "_cli_runner.py"
    proc = subprocess.run(
        [sys.executable, "-I", str(runner), "ingest",
         str(db_path), str(hook_patch), "--exit-after-event-writes"],
        capture_output=True,
        text=True,
    )
    print("\nhook subprocess exit code:", proc.returncode)
    expect(proc.returncode, EXIT_POINT_CODE, "os._exit code")
    # New process opens the database: old headers and members are intact.
    expect(snapshot(db_path), pre_hook, "rollback after os._exit")
    expect(event_header(db_path, "h").is_retracted, True, "h still retracted")
    # The same patch now applies cleanly.
    st = ingest(db_path, parse_patch(hook_patch))
    show("hook patch re-ingested after rollback", db_path, st)
    expect(event_header(db_path, "h").is_retracted, False, "h restored")

    # Event-level interval read still follows business time.
    rows = events_in_interval(db_path, "alice", 20, 45)
    print("\nalice events [20,45]:", [r.event_id for r in rows])
    print("\nfinal counts:", counts(db_path))
    print("WALKTHROUGH OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
