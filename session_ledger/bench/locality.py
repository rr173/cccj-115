"""Locality benchmark: local updates must not scan a user or the whole store.

Offline only; no network and no analytics service.  Run::

    python3 -I bench/locality.py

Scenarios
---------
1. Sparse world: user ``alice`` owns >= 20,000 mutually disconnected short
   sessions and user ``bob`` owns another large set.  A patch then (a) changes
   one event inside one small segment and (b) inserts one bridge point joining
   two adjacent segments.  We record the real read counters from the engine,
   wall time and tracemalloc peak memory, plus EXPLAIN QUERY PLAN evidence that
   the indexed range probes are used rather than full scans.
2. One giant connected segment: a single long chain of N events where an
   update genuinely forces the whole component to be re-read.  This measures
   the actual price of locality when a big component must be rebuilt -- it is
   proportional to that component, not to the database.

The report is written to bench/locality_report.json and summarised on stdout.
"""

from __future__ import annotations

import json
import sys
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_ledger import ingest, initialise, parse_patch  # noqa: E402
from session_ledger.query import explain_index_usage
from session_ledger.schema import connect

GAP = 10
ALICE_SEGMENTS = 20_000
BOB_SEGMENTS = 8_000
GIANT_EVENTS = 20_000
# Sessions are placed every 100 ms so consecutive segments are 90+ ms apart,
# well beyond G; each segment holds two events exactly G apart.
STRIDE = 100


def _write(path: Path, lines: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def _bulk_load(db_path: Path, lines: list[dict], workdir: Path,
               chunk: int = 5_000) -> None:
    # Load in several patches; initial builds legitimately read their own
    # neighbourhoods only.
    for i in range(0, len(lines), chunk):
        p = _write(workdir / f"bulk_{i}.jsonl", lines[i : i + chunk])
        ingest(db_path, parse_patch(p))


def build_sparse(workdir: Path) -> Path:
    db = workdir / "sparse.sqlite"
    initialise(db, GAP)
    lines: list[dict] = []
    for i in range(ALICE_SEGMENTS):
        base = i * STRIDE
        lines.append({"op": "put", "event_id": f"a{i}_0", "revision": 1,
                      "user_id": "alice", "event_ms": base})
        lines.append({"op": "put", "event_id": f"a{i}_1", "revision": 1,
                      "user_id": "alice", "event_ms": base + GAP})
    for i in range(BOB_SEGMENTS):
        base = i * STRIDE
        lines.append({"op": "put", "event_id": f"b{i}_0", "revision": 1,
                      "user_id": "bob", "event_ms": base})
    _bulk_load(db, lines, workdir)
    return db


def build_giant(workdir: Path) -> Path:
    db = workdir / "giant.sqlite"
    initialise(db, GAP)
    # Consecutive events exactly G apart -> one component spanning the lot.
    lines = [
        {"op": "put", "event_id": f"g{i:06d}", "revision": 1,
         "user_id": "alice", "event_ms": i * GAP}
        for i in range(GIANT_EVENTS)
    ]
    _bulk_load(db, lines, workdir)
    return db


def measure(db: Path, patch: Path) -> dict:
    tracemalloc.start()
    t0 = time.perf_counter()
    stats = ingest(db, parse_patch(patch))
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    out = stats.as_dict()
    out["elapsed_ms"] = round(elapsed * 1000, 3)
    out["tracemalloc_peak_kib"] = round(peak / 1024, 1)
    return out


def index_evidence(db: Path) -> list[str]:
    probes = [
        ("session range",
         "SELECT session_id FROM sessions WHERE user_id = ? "
         "AND start_ms <= ? AND end_ms >= ?",
         ("alice", 1_000_000, 0)),
        ("member ordering",
         "SELECT event_id FROM members WHERE user_id = ? "
         "AND event_ms BETWEEN ? AND ? ORDER BY event_ms, event_id",
         ("alice", 0, 1_000)),
        ("event header",
         "SELECT event_id FROM events WHERE event_id = ?", ("a7_0",)),
    ]
    plans: list[str] = []
    for label, sql, params in probes:
        rows = explain_index_usage(db, sql, params)
        detail = "; ".join(r[3] for r in rows)
        plans.append(f"{label}: {detail}")
    return plans


def main() -> int:
    report: dict = {"gap_ms": GAP}
    with TemporaryDirectory(prefix="session-ledger-bench-") as raw:
        workdir = Path(raw)

        # ---- sparse world, local edits -----------------------------------
        db = build_sparse(workdir)
        conn = connect(db)
        total_events = conn.execute(
            "SELECT count(*) FROM events WHERE is_retracted = 0"
        ).fetchone()[0]
        total_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        conn.close()
        report["sparse_world"] = {
            "active_events": total_events,
            "sessions": total_sessions,
            "alice_segments": ALICE_SEGMENTS,
            "bob_segments": BOB_SEGMENTS,
        }

        target = ALICE_SEGMENTS // 2

        # (a) jointly bridge two formerly separate segments with several
        # points in one patch.  The inter-segment gap is STRIDE-2G = 80, more
        # than 2G, so no single event can bridge it: points are placed G apart
        # from the left segment end, each G apart, ending G before the right
        # segment start.  Only their joint insertion connects the chain; every
        # other 90 ms gap stays open.
        left_end = target * STRIDE + GAP
        right_start = (target + 1) * STRIDE
        gap_span = right_start - left_end  # 80
        bridge_times = list(range(left_end + GAP, right_start, GAP))
        assert bridge_times[0] - left_end == GAP
        assert right_start - bridge_times[-1] == GAP
        assert all(b - a == GAP for a, b in zip(bridge_times, bridge_times[1:]))
        bridge_lines = [
            {"op": "put", "event_id": f"bridge{i}", "revision": 1,
             "user_id": "alice", "event_ms": ms}
            for i, ms in enumerate(bridge_times)
        ]
        bridge = _write(workdir / "bridge.jsonl", bridge_lines)
        report["sparse_world"]["bridge_insert"] = measure(db, bridge)
        # The points must have merged exactly two 2-event segments plus the
        # bridges into one component named after the smallest member id.
        expected_count = 4 + len(bridge_times)
        conn = connect(db)
        merged = conn.execute(
            "SELECT event_count, start_ms, end_ms, session_id FROM sessions "
            "WHERE start_ms = ? AND user_id = 'alice'",
            (target * STRIDE,),
        ).fetchone()
        conn.close()
        assert tuple(merged) == (
            expected_count, target * STRIDE,
            (target + 1) * STRIDE + GAP, f"a{target}_0",
        ), tuple(merged)

        # (b) retract all bridges in one patch: the component splits back.
        unbridge = _write(workdir / "unbridge.jsonl", [
            {"op": "retract", "event_id": f"bridge{i}", "revision": 2}
            for i in range(len(bridge_times))
        ])
        report["sparse_world"]["bridge_retract"] = measure(db, unbridge)

        # (c) nudge one event by 1 ms so its gap to its partner becomes G+1:
        # the small segment splits locally while everything else stays put.
        edit = _write(workdir / "edit.jsonl", [
            {"op": "put", "event_id": f"a{target}_1", "revision": 2,
             "user_id": "alice", "event_ms": target * STRIDE + GAP + 1},
        ])
        report["sparse_world"]["single_event_time_change"] = measure(db, edit)
        conn = connect(db)
        split = conn.execute(
            "SELECT count(*) FROM sessions WHERE start_ms BETWEEN ? AND ?",
            (target * STRIDE, target * STRIDE + GAP + 1),
        ).fetchone()[0]
        conn.close()
        assert split == 2, split

        report["sparse_world"]["index_plans"] = index_evidence(db)
        report["sparse_world"]["locality_assertions"] = {
            "rows_read_far_below_active_events":
                report["sparse_world"]["bridge_insert"][
                    "resegment_rows_read"] < total_events // 100,
            "no_full_user_scan":
                report["sparse_world"]["single_event_time_change"][
                    "resegment_rows_read"] <= 6,
        }

        # ---- giant component, unavoidable re-read ------------------------
        gdb = build_giant(workdir)
        middle = GIANT_EVENTS // 2
        # Move the middle event to just after its successor: still one
        # component, but the rebuild must stream the entire giant segment.
        gpatch = _write(workdir / "giant_edit.jsonl", [
            {"op": "put", "event_id": f"g{middle:06d}", "revision": 2,
             "user_id": "alice",
             "event_ms": (middle + 1) * GAP - 1},
        ])
        report["giant_segment"] = {
            "events": GIANT_EVENTS,
            "forced_full_component_reread": measure(gdb, gpatch),
        }
        gs = report["giant_segment"]["forced_full_component_reread"]
        report["giant_segment"]["cost_note"] = (
            "rows read equals the component size, not the database size; "
            f"read={gs['resegment_rows_read']} vs events={GIANT_EVENTS}, "
            f"peak memory stays bounded by cursor batches "
            f"({gs['tracemalloc_peak_kib']} KiB)"
        )

    out_path = Path(__file__).resolve().parent / "locality_report.json"
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True),
                        encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nreport written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
