"""``session-ledger`` command line entry point.

    session-ledger init   DB.sqlite --gap-ms 10
    session-ledger ingest DB.sqlite patch.jsonl [--exit-after-event-writes]
    session-ledger show   DB.sqlite [--user U] [--from MS --to MS]
    session-ledger events DB.sqlite --session SID
    session-ledger header DB.sqlite --event EID
    session-ledger stats  DB.sqlite

Ingest prints one auditable JSON line of processing statistics.  Errors exit
with code 1; the deliberate crash hook exits with code 42 (without committing).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # direct `python .../cli.py` execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from session_ledger.errors import SessionLedgerError
    from session_ledger.patch import parse_patch
    from session_ledger.query import (
        all_sessions,
        counts,
        event_header,
        events_in_interval,
        idle_gap,
        session_events,
        sessions_in_interval,
    )
    from session_ledger.schema import initialise
    from session_ledger.store import ingest
else:
    from .errors import SessionLedgerError
    from .patch import parse_patch
    from .query import (
        all_sessions,
        counts,
        event_header,
        events_in_interval,
        idle_gap,
        session_events,
        sessions_in_interval,
    )
    from .schema import initialise
    from .store import ingest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create a store with a fixed gap G")
    p_init.add_argument("db")
    p_init.add_argument(
        "--gap-ms",
        type=int,
        required=True,
        help="idle interval G in milliseconds, positive integer",
    )

    p_ing = sub.add_parser("ingest", help="apply one JSONL patch atomically")
    p_ing.add_argument("db")
    p_ing.add_argument("patch")
    p_ing.add_argument(
        "--exit-after-event-writes",
        action="store_true",
        help="test hook: os._exit after headers change, before session work",
    )

    p_show = sub.add_parser("show", help="list sessions, optionally in a window")
    p_show.add_argument("db")
    p_show.add_argument("--user")
    p_show.add_argument("--from", dest="start_ms", type=int)
    p_show.add_argument("--to", dest="end_ms", type=int)

    p_ev = sub.add_parser("events", help="expand a session into its events")
    p_ev.add_argument("db")
    p_ev.add_argument("--session", required=True)

    p_hd = sub.add_parser("header", help="show the current header of an event")
    p_hd.add_argument("db")
    p_hd.add_argument("--event", required=True)

    p_st = sub.add_parser("stats", help="show row counts and the gap")
    p_st.add_argument("db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            initialise(args.db, args.gap_ms)
            print(json.dumps({"db": args.db, "idle_gap_ms": args.gap_ms}))
            return 0

        if args.cmd == "ingest":
            ops = parse_patch(Path(args.patch))
            stats = ingest(
                args.db,
                ops,
                exit_after_event_writes=args.exit_after_event_writes,
            )
            print(json.dumps(stats.as_dict(), ensure_ascii=False, sort_keys=True))
            return 0

        if args.cmd == "show":
            if args.user is not None and None not in (
                args.start_ms,
                args.end_ms,
            ):
                sessions = sessions_in_interval(
                    args.db, args.user, args.start_ms, args.end_ms
                )
            else:
                sessions = [
                    s
                    for s in all_sessions(args.db)
                    if args.user is None or s.user_id == args.user
                ]
            print(json.dumps([vars(s) for s in sessions], ensure_ascii=False))
            return 0

        if args.cmd == "events":
            rows = session_events(args.db, args.session)
            print(
                json.dumps(
                    [vars(r) for r in rows], ensure_ascii=False
                )
            )
            return 0

        if args.cmd == "header":
            row = event_header(args.db, args.event)
            print(json.dumps(vars(row) if row else None, ensure_ascii=False))
            return 0

        if args.cmd == "stats":
            data = counts(args.db)
            data["idle_gap_ms"] = idle_gap(args.db)
            print(json.dumps(data, sort_keys=True))
            return 0
    except SessionLedgerError as exc:
        print(f"session-ledger: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
