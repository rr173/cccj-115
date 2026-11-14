"""Structural patch validation, revision rules and read-API tests.

Runnable with the standard library::

    python3 -m unittest tests.test_patch_and_query -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from session_ledger import (
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
from session_ledger.cli import main as cli_main
from session_ledger.store import EXIT_POINT_CODE

GAP = 10
REPO = Path(__file__).resolve().parents[1]


class PatchValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        initialise(self.db, GAP)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch(self, text: str) -> Path:
        p = Path(self.tmp.name) / "p.jsonl"
        p.write_text(text, encoding="utf-8")
        return p

    def _put(self, **over) -> dict:
        base = {"op": "put", "event_id": "e", "revision": 1,
                "user_id": "u", "event_ms": 0}
        base.update(over)
        return base

    def test_empty_patch_is_legal(self) -> None:
        p = self._patch("")
        stats = ingest(self.db, parse_patch(p))
        self.assertEqual(stats.effective_changes, 0)
        self.assertEqual(counts(self.db)["events"], 0)

    def test_gap_must_be_positive_int_not_bool(self) -> None:
        from session_ledger.errors import StoreError

        for bad in (0, -1, True, False, 1.5, "10"):
            with self.assertRaises(StoreError):
                initialise(Path(self.tmp.name) / f"x{bad!r}.sqlite", bad)

    def test_gap_is_fixed(self) -> None:
        from session_ledger.errors import StoreError

        with self.assertRaises(StoreError):
            initialise(self.db, GAP + 1)
        self.assertEqual(idle_gap(self.db), GAP)

    def test_structural_errors_point_at_line_and_abort_all(self) -> None:
        cases = [
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": "u", "event_ms": 1, "x": 2}', "unexpected field"),
            ('{"op": "retract", "event_id": "e", "revision": 1, '
             '"event_ms": 2}', "retract payload"),
            ('{"op": "put", "event_id": "", "revision": 1, '
             '"user_id": "u", "event_ms": 1}', "empty event id"),
            ('{"op": "put", "event_id": "e", "revision": true, '
             '"user_id": "u", "event_ms": 1}', "bool revision"),
            ('{"op": "put", "event_id": "e", "revision": 0, '
             '"user_id": "u", "event_ms": 1}', "zero revision"),
            ('{"op": "put", "event_id": "e", "revision": 1.0, '
             '"user_id": "u", "event_ms": 1}', "float revision"),
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": "u", "event_ms": true}', "bool event_ms"),
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": 3, "event_ms": 1}', "non-string user"),
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": "u", "event_ms": -9223372036854775809}',
             "int64 underflow"),
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": "u", "event_ms": 9223372036854775808}',
             "int64 overflow"),
            ('{"op": "put", "event_id": "e", "revision": 1, '
             '"user_id": "u"}', "missing field"),
            ('{"op": "delete", "event_id": "e", "revision": 1}', "bad op"),
            ("not json", "invalid json"),
            ("[1,2]", "non-object"),
            ("", "blank"),
        ]
        for text, label in cases:
            with self.subTest(label):
                with self.assertRaises(PatchError) as ctx:
                    parse_patch(self._patch(text + "\n"))
                self.assertEqual(ctx.exception.line, 1)

        # Duplicate object key and duplicate identity point at the right line.
        dup_key = self._patch(
            '{"op":"put","event_id":"e","revision":1,"user_id":"u",'
            '"event_ms":1,"event_ms":2}\n'
        )
        with self.assertRaises(PatchError) as ctx:
            parse_patch(dup_key)
        self.assertEqual(ctx.exception.line, 1)

        dup_id = self._patch(
            json.dumps(self._put(event_id="e")) + "\n"
            + json.dumps(self._put(event_id="e", revision=2)) + "\n"
        )
        with self.assertRaises(PatchError) as ctx:
            parse_patch(dup_id)
        self.assertEqual(ctx.exception.line, 2)

    def test_identifiers_are_case_and_byte_sensitive(self) -> None:
        p = self._patch(
            json.dumps(self._put(event_id="E", user_id="U", event_ms=0)) + "\n"
            + json.dumps(self._put(event_id="e", user_id="u", event_ms=0)) + "\n"
        )
        ingest(self.db, parse_patch(p))
        self.assertIsNotNone(event_header(self.db, "E"))
        self.assertIsNotNone(event_header(self.db, "e"))
        sessions = {s.user_id for s in all_sessions(self.db)}
        self.assertEqual(sessions, {"U", "u"})

    def test_int64_boundary_times_and_window_clamping(self) -> None:
        p = self._patch(
            "\n".join([
                json.dumps(self._put(event_id="lo",
                                     event_ms=-(2**63))),
                json.dumps(self._put(event_id="hi", event_ms=2**63 - 1)),
                json.dumps(self._put(event_id="mid", event_ms=-5)),
            ]) + "\n"
        )
        ingest(self.db, parse_patch(p))
        self.assertEqual(event_header(self.db, "lo").event_ms, -(2**63))
        self.assertEqual(event_header(self.db, "hi").event_ms, 2**63 - 1)
        self.assertEqual(len(all_sessions(self.db)), 3)
        # An update adjacent to INT64_MIN must not overflow the G-padded window.
        near = self._patch(json.dumps(self._put(
            event_id="mid", revision=2, event_ms=-(2**63) + GAP)) + "\n")
        ingest(self.db, parse_patch(near))
        merged = [s for s in all_sessions(self.db) if s.session_id == "lo"][0]
        self.assertEqual(merged.event_count, 2)
        self.assertEqual(merged.start_ms, -(2**63))

    def test_revision_order_rules(self) -> None:
        p = self._patch(json.dumps(self._put(event_id="e", revision=2)) + "\n")
        ingest(self.db, parse_patch(p))

        # Stale smaller revision.
        stale = self._patch(json.dumps(self._put(event_id="e", revision=1)) + "\n")
        with self.assertRaises(StaleRevisionError) as ctx:
            ingest(self.db, parse_patch(stale))
        self.assertEqual(ctx.exception.line, 1)
        self.assertEqual(event_header(self.db, "e").revision, 2)

        # Equal revision, identical payload -> no-op.
        same = self._patch(json.dumps(self._put(event_id="e", revision=2)) + "\n")
        stats = ingest(self.db, parse_patch(same))
        self.assertEqual(stats.noops, 1)
        self.assertEqual(stats.effective_changes, 0)

        # Equal revision, different payload -> conflict, whole patch refused.
        conflict = self._patch(
            json.dumps(self._put(event_id="other", event_ms=100)) + "\n"
            + json.dumps(self._put(event_id="e", revision=2, event_ms=99)) + "\n"
        )
        with self.assertRaises(RevisionConflictError):
            ingest(self.db, parse_patch(conflict))
        self.assertIsNone(event_header(self.db, "other"))

        # Jumping revisions is allowed.
        jump = self._patch(
            json.dumps(self._put(event_id="e", revision=9, event_ms=5)) + "\n"
        )
        ingest(self.db, parse_patch(jump))
        self.assertEqual(event_header(self.db, "e").revision, 9)
        self.assertEqual(event_header(self.db, "e").event_ms, 5)

    def test_unknown_retract_refused(self) -> None:
        p = self._patch(
            json.dumps({"op": "retract", "event_id": "ghost", "revision": 1})
            + "\n"
        )
        with self.assertRaises(PatchError):
            ingest(self.db, parse_patch(p))

    def test_retract_restore_and_repeated_retract(self) -> None:
        p = self._patch(json.dumps(self._put(event_id="e")) + "\n")
        ingest(self.db, parse_patch(p))
        r = self._patch(
            json.dumps({"op": "retract", "event_id": "e", "revision": 2}) + "\n"
        )
        ingest(self.db, parse_patch(r))
        self.assertTrue(event_header(self.db, "e").is_retracted)
        self.assertEqual(counts(self.db)["members"], 0)

        # Repeated retraction at the same revision is a no-op, no ghost member.
        stats = ingest(self.db, parse_patch(r))
        self.assertEqual(stats.effective_changes, 0)
        self.assertEqual(counts(self.db)["members"], 0)

        # A newer put restores the identity.
        restore = self._patch(
            json.dumps(self._put(event_id="e", revision=3, event_ms=7)) + "\n"
        )
        ingest(self.db, parse_patch(restore))
        self.assertFalse(event_header(self.db, "e").is_retracted)
        self.assertEqual(counts(self.db)["members"], 1)


class QueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        initialise(self.db, GAP)
        lines = [
            {"op": "put", "event_id": "a", "revision": 1, "user_id": "u",
             "event_ms": 0},
            {"op": "put", "event_id": "b", "revision": 1, "user_id": "u",
             "event_ms": 10},
            {"op": "put", "event_id": "c", "revision": 1, "user_id": "u",
             "event_ms": 100},
        ]
        p = Path(self.tmp.name) / "seed.jsonl"
        p.write_text("\n".join(map(json.dumps, lines)) + "\n")
        ingest(self.db, parse_patch(p))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_closed_interval_uses_intersection(self) -> None:
        # Window entirely before the second session but touching its start.
        hits = sessions_in_interval(self.db, "u", 90, 100)
        self.assertEqual([s.session_id for s in hits], ["c"])
        # Window that starts inside the long session still returns it.
        hits = sessions_in_interval(self.db, "u", 5, 50)
        self.assertEqual([s.session_id for s in hits], ["a"])
        # No intersection.
        self.assertEqual(sessions_in_interval(self.db, "u", 11, 99), [])

    def test_session_events_business_time_order(self) -> None:
        rows = session_events(self.db, "a")
        self.assertEqual([(r.event_id, r.event_ms) for r in rows],
                         [("a", 0), ("b", 10)])

    def test_events_interval(self) -> None:
        rows = events_in_interval(self.db, "u", 0, 10)
        self.assertEqual([r.event_id for r in rows], ["a", "b"])


class CliAndCrashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_roundtrip(self) -> None:
        rc = cli_main(["init", str(self.db), "--gap-ms", "10"])
        self.assertEqual(rc, 0)
        patch = Path(self.tmp.name) / "p.jsonl"
        patch.write_text(
            json.dumps({"op": "put", "event_id": "a", "revision": 1,
                        "user_id": "u", "event_ms": 0}) + "\n"
        )
        rc = cli_main(["ingest", str(self.db), str(patch)])
        self.assertEqual(rc, 0)
        rc = cli_main(["stats", str(self.db)])
        self.assertEqual(rc, 0)

    def test_real_process_exit_point_rolls_back(self) -> None:
        initialise(self.db, GAP)
        patch = Path(self.tmp.name) / "p.jsonl"
        patch.write_text(
            json.dumps({"op": "put", "event_id": "a", "revision": 1,
                        "user_id": "u", "event_ms": 0}) + "\n"
        )
        ingest(self.db, parse_patch(patch))

        hook = Path(self.tmp.name) / "hook.jsonl"
        hook.write_text(
            json.dumps({"op": "put", "event_id": "b", "revision": 1,
                        "user_id": "u", "event_ms": 5}) + "\n"
        )
        runner = REPO / "demo" / "_cli_runner.py"
        proc = subprocess.run(
            [sys.executable, "-I", str(runner), "ingest", str(self.db),
             str(hook), "--exit-after-event-writes"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, EXIT_POINT_CODE)

        # A brand new process sees the complete pre-patch state.
        self.assertIsNone(event_header(self.db, "b"))
        self.assertEqual(counts(self.db)["members"], 1)
        # The identical patch now applies and its stats are auditable.
        stats = ingest(self.db, parse_patch(hook))
        self.assertEqual(stats.effective_changes, 1)
        self.assertEqual(counts(self.db)["members"], 2)
        # Repeating it is entirely a no-op.
        stats = ingest(self.db, parse_patch(hook))
        self.assertEqual(stats.effective_changes, 0)
        self.assertEqual(stats.resegment_rows_read, 0)


if __name__ == "__main__":
    unittest.main()
