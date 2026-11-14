"""Command line interface: ``python -m sql_lineage``."""

import argparse
import sys

from .errors import LineageError
from .report import LineageReportReader
from .runner import analyze_files


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m sql_lineage",
        description="Static field-level SQL lineage (no database access).")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="analyze SQL files and write JSONL")
    run.add_argument("--catalog", required=True,
                     help="path to the JSON data catalog")
    run.add_argument("--sql", required=True, nargs="+",
                     help="ordered SQL file paths")
    run.add_argument("--out", required=True,
                     help="output JSONL path (atomically replaced)")

    read = sub.add_parser("read", help="stream a JSONL report line by line")
    read.add_argument("path", help="path to a JSONL report")
    read.add_argument("--file", dest="file_filter")
    read.add_argument("--statement", type=int)
    read.add_argument("--kind", dest="kind_filter",
                      choices=("statement", "cte", "subquery"))
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.command == "run":
        try:
            records = analyze_files(args.sql, args.catalog, args.out)
        except LineageError as exc:
            detail = exc.to_dict()
            loc = ""
            if detail["line"] is not None:
                loc = f" at line {detail['line']}, column {detail['column']}"
            stmt = (f" statement {detail['statement']}"
                    if detail["statement"] else "")
            print(
                f"{detail['error']}: {detail['message']} "
                f"[file={detail['file']}{stmt}{loc}]",
                file=sys.stderr)
            return 1
        print(f"wrote {len(records)} lineage records to {args.out}")
        return 0

    if args.command == "read":
        count = 0
        with LineageReportReader(args.path) as reader:
            for record in reader:
                if args.file_filter and record["file"] != args.file_filter:
                    continue
                if args.statement is not None and \
                        record["statement"] != args.statement:
                    continue
                if args.kind_filter and \
                        record["record_kind"] != args.kind_filter:
                    continue
                print(reader.line_number, record["file"],
                      f"stmt={record['statement']}",
                      f"#{record['output_ordinal']}",
                      record["output_name"],
                      "sources=", record["base_sources"])
                count += 1
        print(f"consumed {count} records", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
