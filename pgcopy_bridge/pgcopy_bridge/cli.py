"""``pgcopy`` command line: decode, encode, inspect, types and catalogue.

    pgcopy decode  CATALOGUE.json IN.copy OUT.jsonl
    pgcopy encode  CATALOGUE.json IN.jsonl OUT.copy
    pgcopy inspect CATALOGUE.json IN.copy
    pgcopy types
    pgcopy catalogue-template

``-`` means standard input for the input path.  See README.md for the
standard-output-free rule: both conversion commands write only to the named
output file and publish it atomically, so a failed run never leaves a
truncated destination, including when the input is a non-rewindable stdin
stream.
"""

from __future__ import annotations

import argparse
import sys

from .errors import PgCopyError
from .limits import Limits
from .library import decode_file, encode_file, inspect_file

TYPE_DOC = """\
Supported types and their logical JSON representation
=====================================================

Scalar columns
--------------
int4        JSON integer, -2147483648 .. 2147483647
            {"pid": 2147483647}
int8        JSON integer, exact; -9223372036854775808 .. 9223372036854775807
            {"big": 9007199254740993}
bool        JSON true or false
            {"ok": true}
text        JSON string, UTF-8; a NUL byte is rejected. "" is empty text,
            which is different from null (SQL NULL).
            {"note": ""}
bytea       JSON string of lowercase hex, two characters per byte; "" is the
            empty byte string. The representation is reversible: any byte
            value 00..ff round-trips.
            {"blob": "00ff10"}
uuid        JSON string, canonical lowercase 8-4-4-4-12 spelling; any
            accepted input spelling is normalised on output.
            {"id": "123e4567-e89b-12d3-a456-426614174000"}
numeric     JSON object with the exact PostgreSQL fields, or an exact
            decimal string when encoding:
            {"n": {"ndigits": 2, "weight": 0, "sign": 0, "dscale": 4,
                   "digits": [1, 2300]}}          -> 1.2300
            {"n": "1.2300"}                      -> same value on encoding
            sign is 0 (positive) or 1 (negative) for finite values, and
            "NaN", "Infinity" or "-Infinity" for the three specials:
            {"n": {"ndigits": 0, "weight": 0, "sign": "Infinity",
                   "dscale": 0, "digits": []}}
            digits are base-10000 groups, 0..9999; value =
            sum(digits[i] * 10000 ** (weight - i)). dscale is the displayed
            number of decimal places, so 0.0000 has ndigits=0, digits=[] and
            dscale=4 -- it is not the same as 0. Floats are never used.
date        JSON integer: days since 2000-01-01.
            -2147483648 and 2147483647 are -infinity and infinity.
            {"d": 0}          -> 2000-01-01
            {"d": -2147483648} -> -infinity
timestamp   JSON integer: microseconds since 2000-01-01 00:00:00 with no
            time zone. -2**63 and 2**63-1 are -infinity and infinity.
            {"ts": 0}         -> 2000-01-01 00:00:00
timestamptz JSON integer: microseconds since 2000-01-01 00:00:00+00, i.e.
            always UTC-based. The value is never reinterpreted in the local
            time zone. -2**63 and 2**63-1 are -infinity and infinity.
            {"ts": 3600000000} -> 2000-01-01 01:00:00+00

Array columns (int4[], text[], numeric[])
-----------------------------------------
    {"shape": [2, 2], "lower_bounds": [0, -1],
     "elements": [1, null, 3, 4]}

shape gives each dimension's length, lower_bounds gives its first subscript.
A lower bound other than 1 is preserved exactly; the value is never turned
into a plain nested Python list. elements are in row-major order (the last
dimension varies fastest) and use the scalar encoding of the element type.
A null element is SQL NULL and is different from "" (empty text). The empty
array is {"shape": [], "lower_bounds": [], "elements": []} and is different
from null. PostgreSQL stores the number of dimensions, a NULL flag and the
element type OID in the array header; all are written on encoding and
validated on decoding.

Full example record (catalogue order is the JSON field order)
-------------------------------------------------------------
    {"id": 7, "ok": true, "note": "", "blob": "00ff", \
"uid": "123e4567-e89b-12d3-a456-426614174000",
     "amount": "12345678901234567890.1234567890000", "d": 0,
     "ts": null, "grid": {"shape": [2, 2], "lower_bounds": [0, 0],
     "elements": [[1, 2], [3, 4]]}}
"""

CATALOGUE_TEMPLATE = """\
{
  "columns": [
    {"name": "id",      "type": "int8"},
    {"name": "ok",      "type": "bool"},
    {"name": "note",    "type": "text"},
    {"name": "blob",    "type": "bytea"},
    {"name": "uid",     "type": "uuid"},
    {"name": "amount",  "type": "numeric"},
    {"name": "d",       "type": "date"},
    {"name": "ts",      "type": "timestamp"},
    {"name": "tz",      "type": "timestamptz"},
    {"name": "grid",    "type": "int4[]"},
    {"name": "tags",    "type": "text[]"},
    {"name": "rates",   "type": "numeric[]"}
  ]
}
"""


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-field-bytes", type=int, default=None,
        help="reject any declared field body larger than this before reading "
             "it (default 67108864)")
    parser.add_argument(
        "--max-array-dims", type=int, default=None,
        help="maximum number of array dimensions (default 6)")
    parser.add_argument(
        "--max-array-elements", type=int, default=None,
        help="maximum element count of one array (default 4000000)")
    parser.add_argument(
        "--read-chunk", type=int, default=65536,
        help="read buffer size in bytes; 1 exercises byte-at-a-time input "
             "(default 65536)")
    parser.add_argument(
        "--max-record-bytes", type=int, default=128 * 1024 * 1024,
        help="maximum bytes of one JSONL input line (encode only)")


def _limits(args: argparse.Namespace) -> Limits:
    defaults = Limits()
    return Limits(
        max_field_bytes=(defaults.max_field_bytes
                         if args.max_field_bytes is None
                         else args.max_field_bytes),
        max_array_dims=(defaults.max_array_dims
                        if args.max_array_dims is None
                        else args.max_array_dims),
        max_array_elements=(defaults.max_array_elements
                            if args.max_array_elements is None
                            else args.max_array_elements),
    )


def _cmd_decode(args: argparse.Namespace) -> int:
    result = decode_file(
        args.input, args.catalogue, args.output,
        limits=_limits(args), chunk_size=args.read_chunk)
    print(f"decoded {result.tuples} tuple(s), {result.columns} column(s) "
          f"-> {args.output}")
    return 0


def _cmd_encode(args: argparse.Namespace) -> int:
    result = encode_file(
        args.input, args.catalogue, args.output,
        limits=_limits(args), chunk_size=args.read_chunk,
        max_record_bytes=args.max_record_bytes)
    print(f"encoded {result.tuples} tuple(s), {result.columns} column(s) "
          f"-> {args.output}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    result = inspect_file(
        args.input, args.catalogue,
        limits=_limits(args), chunk_size=args.read_chunk)
    print(result.format_report())
    return 0


def _cmd_types(_args: argparse.Namespace) -> int:
    print(TYPE_DOC, end="")
    return 0


def _cmd_catalogue_template(_args: argparse.Namespace) -> int:
    print(CATALOGUE_TEMPLATE, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgcopy",
        description="Convert PostgreSQL binary COPY files to typed JSONL "
                    "and back, driven by an explicit column catalogue.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dec = sub.add_parser(
        "decode", help="COPY -> UTF-8 JSONL (one tuple per line)")
    p_dec.add_argument("catalogue", help="column catalogue JSON")
    p_dec.add_argument("input", help="COPY file, or - for standard input")
    p_dec.add_argument("output", help="JSONL file to create atomically")
    _add_common(p_dec)
    p_dec.set_defaults(func=_cmd_decode)

    p_enc = sub.add_parser(
        "encode", help="JSONL -> PostgreSQL binary COPY")
    p_enc.add_argument("catalogue", help="column catalogue JSON")
    p_enc.add_argument("input", help="JSONL file, or - for standard input")
    p_enc.add_argument("output", help="COPY file to create atomically")
    _add_common(p_enc)
    p_enc.set_defaults(func=_cmd_encode)

    p_ins = sub.add_parser(
        "inspect", help="validate a COPY file and report counts only")
    p_ins.add_argument("catalogue", help="column catalogue JSON")
    p_ins.add_argument("input", help="COPY file, or - for standard input")
    _add_common(p_ins)
    p_ins.set_defaults(func=_cmd_inspect)

    p_typ = sub.add_parser("types", help="document the logical representation")
    p_typ.set_defaults(func=_cmd_types)

    p_cat = sub.add_parser("catalogue-template",
                           help="print a catalogue skeleton")
    p_cat.set_defaults(func=_cmd_catalogue_template)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PgCopyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
