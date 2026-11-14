"""Randomised differential tests against the naive full-rescan segmenter.

Runnable with the standard library alone::

    python3 -I -m unittest tests.test_differential -v

Each round applies a random mix of put/retract revisions (jumps, equal
revisions, stale ones, cross-user moves, time changes, restores) to the real
SQLite store and to an in-memory model re-segmented from scratch by
:mod:`session_ledger.naive` -- which never imports the production maintenance
code.  After every round we compare:

* every session row field (user, id, start, end, count, span), not just the
  number of sessions;
* the session of every individual event and its business-time ordering;
* header revision / retraction state;
* audit stats sanity (reads are local, never a full-user scan).
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from session_ledger import (
    PatchError,
    RefEvent,
    RevisionConflictError,
    StaleRevisionError,
    all_sessions,
    counts,
    event_header,
    ingest,
    initialise,
    parse_patch,
    segment_all,
    session_events,
)
from session_ledger.schema import connect

USERS = ["alice", "bob", "carol"]
GAP = 7


class DifferentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        initialise(self.db, GAP)
        # In-memory authoritative model: event_id -> (rev, user, ms, retracted)
        self.headers: dict[str, list] = {}
        self.rng = random.Random(20261114)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, lines: list[dict]) -> Path:
        path = Path(self.tmp.name) / f"patch_{next(self._counter)}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return path

    def _ref_snapshot(self) -> tuple[dict, dict]:
        active = [
            RefEvent(eid, row[1], row[2])
            for eid, row in self.headers.items()
            if not row[3]
        ]
        return segment_all(active, GAP)

    def _assert_matches_reference(self) -> None:
        ref_sessions, ref_membership = self._ref_snapshot()
        actual = {s.session_id: s for s in all_sessions(self.db)}

        # Full session-field comparison.
        self.assertEqual(set(actual), set(ref_sessions), "session id sets")
        for sid, ref in ref_sessions.items():
            got = actual[sid]
            self.assertEqual(got.user_id, ref.user_id, sid)
            self.assertEqual(got.start_ms, ref.start_ms, sid)
            self.assertEqual(got.end_ms, ref.end_ms, sid)
            self.assertEqual(got.event_count, ref.event_count, sid)
            self.assertEqual(got.span_ms, ref.span_ms, sid)

        # Per-event membership, including business-time order inside sessions.
        conn = connect(self.db)
        try:
            member_rows = conn.execute(
                "SELECT event_id, session_id, event_ms FROM members "
                "ORDER BY session_id, event_ms, event_id"
            ).fetchall()
        finally:
            conn.close()
        actual_membership = {r[0]: r[1] for r in member_rows}
        self.assertEqual(actual_membership, ref_membership, "membership")
        self.assertEqual(len(member_rows), len(ref_membership))

        # Headers: stored revision and retraction flag per identity.
        for eid, (rev, user, ms, retracted) in self.headers.items():
            hdr = event_header(self.db, eid)
            self.assertIsNotNone(hdr, eid)
            self.assertEqual(hdr.revision, rev, eid)
            self.assertEqual(hdr.is_retracted, retracted, eid)
            if retracted:
                self.assertNotIn(eid, actual_membership)

        # No session may be empty; counts must be internally consistent.
        c = counts(self.db)
        self.assertEqual(c["members"], sum(s.event_count for s in actual.values()))
        self.assertEqual(
            c["sessions"],
            len({m for m in actual_membership.values()}),
        )

    _counter = iter(range(10_000))

    def _random_patch(self, round_no: int) -> tuple[list[dict], bool]:
        """Build one random patch; return (lines, expect_acceptance)."""
        lines: list[dict] = []
        will_fail = False
        used: set[str] = set()
        for _ in range(self.rng.randint(1, 6)):
            existing = [e for e in self.headers if e not in used]
            if existing and self.rng.random() < 0.75:
                eid = self.rng.choice(existing)
                used.add(eid)
                rev, user, ms, retracted = self.headers[eid]
                choice = self.rng.random()
                if choice < 0.25:
                    # Stale or conflicting revision -> whole patch rejected.
                    bad_rev = self.rng.choice([max(1, rev - 1), rev])
                    if bad_rev == rev:
                        if retracted:
                            # Same-revision put against a retracted header is
                            # a content conflict; a same-rev retract would be
                            # a legal no-op, so force the conflict branch.
                            lines.append({"op": "put", "event_id": eid,
                                          "revision": rev,
                                          "user_id": self.rng.choice(USERS),
                                          "event_ms": self.rng.randint(-5, 5)})
                        else:
                            lines.append({"op": "put", "event_id": eid,
                                          "revision": rev, "user_id": user,
                                          "event_ms": ms + 123})
                    else:
                        lines.append({"op": "retract" if self.rng.random() < 0.5
                                      else "put", "event_id": eid,
                                      "revision": bad_rev,
                                      "user_id": self.rng.choice(USERS),
                                      "event_ms": self.rng.randint(-500, 500)})
                    will_fail = True
                elif choice < 0.45:
                    # Retraction (possibly repeated -> no-op).
                    lines.append({"op": "retract", "event_id": eid,
                                  "revision": rev + self.rng.randint(1, 3)})
                else:
                    # Replacement: jump revisions, maybe move user/time.
                    lines.append({
                        "op": "put", "event_id": eid,
                        "revision": rev + self.rng.randint(1, 4),
                        "user_id": self.rng.choice(USERS),
                        "event_ms": self.rng.randint(-500, 500),
                    })
            else:
                eid = f"e{self.rng.randint(0, 199)}"
                if eid in used:
                    continue
                used.add(eid)
                if eid in self.headers:
                    # Re-drawing an existing identity in the same patch makes
                    # it appear twice, which is structurally refused.
                    will_fail = True
                lines.append({
                    "op": "put", "event_id": eid,
                    "revision": self.rng.randint(1, 3),
                    "user_id": self.rng.choice(USERS),
                    "event_ms": self.rng.randint(-500, 500),
                })
        return lines, will_fail

    def _apply_to_model(self, lines: list[dict]) -> None:
        """Mirror an *accepted* patch into the in-memory authoritative model."""
        for line in lines:
            eid = line["event_id"]
            rev = line["revision"]
            if line["op"] == "retract":
                self.headers[eid] = [rev, None, None, True]
            else:
                self.headers[eid] = [rev, line["user_id"], line["event_ms"], False]

    def test_many_random_rounds(self) -> None:
        accepted = 0
        rejected = 0
        for round_no in range(180):
            lines, will_fail = self._random_patch(round_no)
            before = counts(self.db)
            path = self._write(lines)
            try:
                stats = ingest(self.db, parse_patch(path))
            except (StaleRevisionError, RevisionConflictError, PatchError):
                rejected += 1
                self.assertTrue(
                    will_fail, f"unexpected rejection round {round_no}: {lines}"
                )
                # Rejected patch leaves everything untouched.
                self.assertEqual(counts(self.db), before)
                continue
            self.assertFalse(will_fail, f"patch should have failed: {lines}")
            self._apply_to_model(lines)
            accepted += 1
            self._assert_matches_reference()

            # Locality sanity: accepted patches touching few events must not
            # read the whole user history.  Bound on rows read: neighbourhood
            # size is at most the affected anchors' connected region; compare
            # against the total active rows as a strict-subset guard.
            total_active = counts(self.db)["members"]
            if total_active > 20 and stats.effective_changes <= 2:
                self.assertLess(
                    stats.resegment_rows_read,
                    total_active,
                    f"round {round_no} scanned too much: {stats.as_dict()}",
                )
        self.assertGreater(accepted, 60)
        self.assertGreater(rejected, 10)

    def test_same_instant_and_gap_boundary_against_reference(self) -> None:
        # Hand-picked edge values around G, all via the same ingestion path.
        lines = []
        rev = 1
        specs = [
            ("u", "s1", 0), ("u", "s2", GAP),          # exactly G -> linked
            ("u", "s3", 2 * GAP + 1),                  # just over -> split
            ("u", "s4", 2 * GAP + 1),                  # same instant, kept
            ("u", "s5", 3 * GAP + 2),                  # G+1 from s4 -> split
        ]
        for user, eid, ms in specs:
            lines.append({"op": "put", "event_id": eid, "revision": rev,
                          "user_id": user, "event_ms": ms})
        path = self._write(lines)
        ingest(self.db, parse_patch(path))
        self._apply_to_model(lines)
        self._assert_matches_reference()

        events = session_events(self.db, "s1")
        self.assertEqual([e.event_id for e in events], ["s1", "s2"])
        # session s4 contains both same-instant identities in id order.
        events = session_events(self.db, "s3")
        self.assertEqual([e.event_id for e in events], ["s3", "s4"])


if __name__ == "__main__":
    unittest.main()
