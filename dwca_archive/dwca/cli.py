"""Command line entry point.

    python -m dwca.cli import ARCHIVE.ZIP OUTPUT.DB
    python -m dwca.cli inspect OUTPUT.DB [--table TABLE_ID]

``import`` refuses to overwrite an existing database and prints per-table
input / valid / quarantined counts for reconciliation.  ``inspect`` reads an
already imported database without needing the original ZIP at all.
"""

from __future__ import annotations

import argparse
import sys

from .errors import DwcaError
from .importer import import_archive
from .library import DwcaLibrary


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        result = import_archive(args.archive, args.database)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except DwcaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {args.archive} -> {args.database}")
    print(result.format_report())
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    with DwcaLibrary.open(args.database) as lib:
        print(f"database: {args.database}")
        for key, value in sorted(lib.meta().items()):
            print(f"meta {key}: {value}")
        print()
        print(lib_table_report(lib))
        if args.table is None:
            tables = [q.table_id for q in _unique_quarantine_tables(lib)]
        else:
            tables = [args.table]
        print()
        for table_id in tables:
            print(f"-- quarantine: {table_id}")
            n = 0
            for q in lib.iter_quarantine(table_id):
                n += 1
                print(
                    f"  record #{q.record_number} key={q.key_value!r} "
                    f"reasons={','.join(q.reasons)}"
                )
            if n == 0:
                print("  (none)")
    return 0


def lib_table_report(lib: DwcaLibrary) -> str:
    lines = ["table_id                            input  valid  quarantined"]
    for t in lib.table_counts():
        lines.append(
            f"{t.table_id:<34} {t.input_count:>5} {t.valid_count:>6} "
            f"{t.quarantined_count:>12}"
        )
    return "\n".join(lines)


def _unique_quarantine_tables(lib: DwcaLibrary):
    seen: set[str] = set()
    for q in lib.iter_quarantine():
        if q.table_id not in seen:
            seen.add(q.table_id)
            yield q


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dwca",
        description="Import a supported DwC-A ZIP subset into SQLite.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import", help="import an archive ZIP")
    p_imp.add_argument("archive", help="path to the DwC-A .zip")
    p_imp.add_argument("database", help="new SQLite file to create")
    p_imp.set_defaults(func=_cmd_import)

    p_ins = sub.add_parser("inspect", help="inspect an imported database")
    p_ins.add_argument("database", help="existing SQLite file")
    p_ins.add_argument("--table", help="limit quarantine listing to one table")
    p_ins.set_defaults(func=_cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DwcaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
