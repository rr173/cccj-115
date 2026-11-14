"""Hand-written binary material generator -- run from an empty directory.

    python3 materials/generate.py [--out DIR]

Writes, into ``DIR`` (default ``materials/generated``):

* ``sample.copy``       -- the byte-level sample, built without the codec
* ``sample.jsonl``      -- the expected decoded records, hand-written here
* ``sample.offsets.txt``-- key byte offsets and their meaning
* ``arrays.copy``       -- 2-D arrays with NULL, empty text, lower bounds 0
* ``arrays.jsonl``      -- expected records for it
* ``empty.copy``        -- legal COPY header + end marker, zero tuples
* ``catalogue.json``    -- catalogue for the sample
* ``arrays_catalogue.json``
* ``interop.sql``       -- script to run on a real PostgreSQL server
* ``README.txt`` / ``manifest.json`` -- human summary and SHA-256 per file

Nothing in this file imports :mod:`pgcopy_bridge`; the bytes and the
expected JSON are independent of the implementation under test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _common as B  # noqa: E402


SAMPLE_CATALOGUE = {
    "columns": [
        {"name": "id", "type": "int4"},
        {"name": "neg", "type": "int8"},
        {"name": "label", "type": "text"},
        {"name": "raw", "type": "bytea"},
        {"name": "uid", "type": "uuid"},
        {"name": "amount", "type": "numeric"},
        {"name": "zero4", "type": "numeric"},
        {"name": "d", "type": "date"},
        {"name": "ts", "type": "timestamp"},
        {"name": "tz", "type": "timestamptz"},
        {"name": "grid", "type": "int4[]"},
        {"name": "notes", "type": "text[]"},
        {"name": "ranks", "type": "numeric[]"},
    ]
}

ARRAY_CATALOGUE = {
    "columns": [
        {"name": "tag", "type": "int4"},
        {"name": "grid", "type": "int4[]"},
        {"name": "notes", "type": "text[]"},
        {"name": "ranks", "type": "numeric[]"},
    ]
}


def build_arrays() -> tuple[bytes, list[dict], list[str]]:
    grid = B.array([(2, 0), (2, -3)], 23, [
        B.int4(7), None, B.int4(9), B.int4(10)])
    notes = B.array([(2, 0), (2, -3)], 25, [
        B.text(""), None, B.text("ok"), B.text("")])
    ranks = B.array([(2, 0), (1, 4)], 1700, [
        B.numeric(2, 0, 0, 4, [1, 2300]), None])
    t0 = B.tuple_([B.int4(1), grid, notes, ranks])
    # Zero-dimension array (the empty array) and a NULL array, side by side.
    t1 = B.tuple_([
        B.int4(2),
        B.array([], 23, []),
        B.array([], 25, []),
        None,
    ])
    # One-dimensional array with lower bound 0, containing a NULL and "".
    t2 = B.tuple_([
        B.int4(3),
        B.array([(3, 0)], 23, [B.int4(5), None, B.int4(-6)]),
        B.array([(3, 0)], 25, [B.text(""), None, B.text("q")]),
        B.array([(0, -2)], 1700, []),
    ])
    data = B.build([t0, t1, t2])
    expected = [
        {"tag": 1,
         "grid": {"shape": [2, 2], "lower_bounds": [0, -3],
                  "elements": [7, None, 9, 10]},
         "notes": {"shape": [2, 2], "lower_bounds": [0, -3],
                   "elements": ["", None, "ok", ""]},
         "ranks": {"shape": [2, 1], "lower_bounds": [0, 4],
                   "elements": [{"ndigits": 2, "weight": 0, "sign": 0,
                                 "dscale": 4, "digits": [1, 2300]},
                                None]}},
        {"tag": 2,
         "grid": {"shape": [], "lower_bounds": [], "elements": []},
         "notes": {"shape": [], "lower_bounds": [], "elements": []},
         "ranks": None},
        {"tag": 3,
         "grid": {"shape": [3], "lower_bounds": [0],
                  "elements": [5, None, -6]},
         "notes": {"shape": [3], "lower_bounds": [0],
                   "elements": ["", None, "q"]},
         "ranks": {"shape": [0], "lower_bounds": [-2], "elements": []}},
    ]
    offsets = [
        "0      signature",
        "11     flags 0",
        "15     extension header length 0",
        "19     tuple 1 field count int16 = 4",
        "21     tag   length int32 = 4, body 00000001",
        "29     grid  length int32 = 56 at 29: ndim 2, hasnull 1, oid 23, "
        "dims (2, 0), (2, -3), 4 elements",
        "89     notes length int32 = 46 at 89: ndim 2, hasnull 1, oid 25, "
        "dims (2, 0), (2, -3), elements empty-text, NULL, ok, empty-text",
        "139    ranks length int32 = 48 at 139: ndim 2, hasnull 1, oid 1700, "
        "dims (2, 0), (1, 4): element 1.2300 then NULL",
        "191    tuple 2 field count int16 = 4 (zero-dimension arrays and a "
        "NULL array)",
        "237    tuple 3 field count int16 = 4 (1-D arrays, lower bound 0 and "
        "-2, empty array with shape [0])",
        "352    end marker int16 = -1",
        "354    file size (no trailing bytes)",
    ]
    return data, expected, offsets


def build_empty() -> bytes:
    """Signature + flags + extension length + end marker: zero tuples."""
    return B.build([])


INTEROP_SQL = r"""-- Real PostgreSQL interoperability script.
--
-- Run against a database you own, with this directory reachable by the
-- server process (psql is a client; COPY ... FROM/TO file is executed by
-- the *server*, so the path must be on the server's filesystem).  With a
-- remote server, replace COPY ... FROM STDIN by \copy, which the psql
-- client performs locally.
--
--    psql -v ON_ERROR_STOP=1 -f interop.sql
--
-- It creates a scratch schema, loads interop.copy with the server's own
-- binary COPY reader, checks row counts and values, dumps it again with
-- COPY ... TO, and prints an MD5 so a second run proves determinism.
-- Nothing here was executed by the delivery environment unless the report
-- says so explicitly.

\set catalogue_present true
DROP SCHEMA IF EXISTS pgcopy_bridge_check CASCADE;
CREATE SCHEMA pgcopy_bridge_check;
SET search_path = pgcopy_bridge_check, public;

CREATE TABLE sample (
    id      int4,
    neg     int8,
    label   text,
    raw     bytea,
    uid     uuid,
    amount  numeric,
    zero4   numeric,
    d       date,
    ts      timestamp,
    tz      timestamptz,
    grid    int4[],
    notes   text[],
    ranks   numeric[]
);

\copy sample FROM 'sample.copy' WITH (FORMAT binary)

SELECT count(*) AS rows_loaded FROM sample;
-- expected: rows_loaded = 2

SELECT id, neg, label, raw, uid, amount::text, zero4::text, d::text,
       ts::text, tz::text, grid::text, notes::text, ranks::text
FROM sample ORDER BY id NULLS FIRST;

-- Values are compared in SQL, not in the client encoding:
SELECT bool_and(id IS NULL AND neg = -9007199254740993 AND label = ''
                AND raw = '\x00ff1080'::bytea
                AND uid = '123e4567-e89b-12d3-a456-426614174000'::uuid
                AND amount = 123456789012345678901234567890.1234567890000
                AND zero4 = 0.0000
                AND d = '-infinity'::date
                AND ts = 'infinity'::timestamp
                AND tz = '-infinity'::timestamptz
                AND grid IS NULL AND notes IS NULL AND ranks IS NULL)
         AS tuple1_ok
FROM sample WHERE id IS NULL;
-- expected: tuple1_ok = t

SELECT bool_and(neg = 0 AND label = 'plain' AND raw = '\x'::bytea
                AND amount = -1.2300 AND zero4 = 'NaN'::numeric
                AND d = '2000-01-01' AND ts = '2000-01-01 00:00:00'
                AND tz = '2000-01-01 01:00:00+00'
                AND grid::text = '[0:1][-3:-2]={{11,NULL},{13,14}}'
                AND notes::text = '[0:1][-3:-2]={{"",NULL},{ok,""}}'
                AND ranks::text = '[0:0]={1.2300}') AS tuple2_ok
FROM sample WHERE id = 0;
-- expected: tuple2_ok = t

\copy sample TO 'roundtrip.copy' WITH (FORMAT binary)

-- Determinism / round-trip proof: decode the PostgreSQL-produced file
-- again and compare counts.
DROP TABLE IF EXISTS sample2;
CREATE TABLE sample2 (LIKE sample);
\copy sample2 FROM 'roundtrip.copy' WITH (FORMAT binary)
SELECT (SELECT count(*) FROM sample2) = (SELECT count(*) FROM sample)
       AS roundtrip_rows_match;
-- expected: t

-- Optional independent byte check with md5sum on the server's filesystem.
-- \! md5sum sample.copy roundtrip.copy
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(HERE, "generated"))
    args = parser.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    arrays, arrays_expected, arrays_offsets = build_arrays()
    empty = build_empty()
    sample, expected, offsets = _build_full_sample()

    files = {
        "sample.copy": sample,
        "sample.jsonl": _jsonl(expected).encode("utf-8"),
        "sample.offsets.txt": ("\n".join(offsets) + "\n").encode("utf-8"),
        "arrays.copy": arrays,
        "arrays.jsonl": _jsonl(arrays_expected).encode("utf-8"),
        "arrays.offsets.txt": ("\n".join(arrays_offsets) + "\n").encode(
            "utf-8"),
        "empty.copy": empty,
        "catalogue.json": (json.dumps(SAMPLE_CATALOGUE, indent=2)
                           + "\n").encode("utf-8"),
        "arrays_catalogue.json": (json.dumps(ARRAY_CATALOGUE, indent=2)
                                  + "\n").encode("utf-8"),
        "interop.sql": INTEROP_SQL.encode("utf-8"),
        "README.txt": _material_readme(expected, arrays_expected).encode(
            "utf-8"),
    }
    for name, payload in files.items():
        with open(os.path.join(out, name), "wb") as fh:
            fh.write(payload)
    manifest = {
        name: {"bytes": len(payload),
               "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in files.items()
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {len(files)} files to {out}")
    return 0


def _build_full_sample():
    """The 13-column sample used by sample.copy / sample.jsonl.

    Columns: id, neg, label, raw, uid, amount, zero4, d, ts, tz, grid,
    notes, ranks.  Tuple 0 leaves the three array columns NULL so the
    scalar coverage stays easy to read; tuple 1 fills them.
    """
    grid = B.array([(2, 0), (2, -3)], 23, [
        B.int4(11), None, B.int4(13), B.int4(14)])
    notes = B.array([(2, 0), (2, -3)], 25, [
        B.text(""), None, B.text("ok"), B.text("")])
    ranks = B.array([(1, 0)], 1700, [B.numeric(2, 0, 0, 4, [1, 2300])])

    t0 = B.tuple_([
        None,                                     # id int4
        B.int8(-9007199254740993),                # neg int8
        B.text(""),                               # label text
        B.bytea(bytes([0x00, 0xFF, 0x10, 0x80])), # raw bytea
        B.uuid_hex("123e4567-e89b-12d3-a456-426614174000"),
        B.numeric(11, 7, 0x0000, 13,
                  [12, 3456, 7890, 1234, 5678, 9012, 3456, 7890, 1234,
                   5678, 9000]),
        B.numeric(0, 0, 0x0000, 4, []),           # 0.0000
        B.date(-2147483648),                      # -infinity
        B.timestamp(2 ** 63 - 1),                 # +infinity
        B.timestamp(-(2 ** 63)),                  # -infinity
        None, None, None,
    ])
    t1 = B.tuple_([
        B.int4(0),
        B.int8(0),
        B.text("plain"),
        B.bytea(b""),
        B.uuid_hex("00000000-0000-0000-0000-000000000000"),
        B.numeric(2, 0, 0x4000, 4, [1, 2300]),    # -1.2300
        B.numeric(0, 0, 0xC000, 0, []),           # NaN
        B.date(0),
        B.timestamp(0),
        B.timestamp(3600000000),
        grid, notes, ranks,
    ])
    data = B.build([t0, t1])
    expected = [
        {"id": None, "neg": -9007199254740993, "label": "", "raw": "00ff1080",
         "uid": "123e4567-e89b-12d3-a456-426614174000",
         "amount": {"ndigits": 11, "weight": 7, "sign": 0, "dscale": 13,
                    "digits": [12, 3456, 7890, 1234, 5678, 9012, 3456,
                               7890, 1234, 5678, 9000]},
         "zero4": {"ndigits": 0, "weight": 0, "sign": 0, "dscale": 4,
                   "digits": []},
         "d": -2147483648, "ts": 9223372036854775807,
         "tz": -9223372036854775808,
         "grid": None, "notes": None, "ranks": None},
        {"id": 0, "neg": 0, "label": "plain", "raw": "",
         "uid": "00000000-0000-0000-0000-000000000000",
         "amount": {"ndigits": 2, "weight": 0, "sign": 1, "dscale": 4,
                   "digits": [1, 2300]},
         "zero4": {"ndigits": 0, "weight": 0, "sign": "NaN", "dscale": 0,
                   "digits": []},
         "d": 0, "ts": 0, "tz": 3600000000,
         "grid": {"shape": [2, 2], "lower_bounds": [0, -3],
                  "elements": [11, None, 13, 14]},
         "notes": {"shape": [2, 2], "lower_bounds": [0, -3],
                   "elements": ["", None, "ok", ""]},
         "ranks": {"shape": [1], "lower_bounds": [0],
                   "elements": [{"ndigits": 2, "weight": 0, "sign": 0,
                                 "dscale": 4, "digits": [1, 2300]}]}},
    ]
    offsets = _sample_offsets()
    return data, expected, offsets


def _sample_offsets() -> list[str]:
    return [
        "  0  signature, 11 bytes: 50 47 43 4f 50 59 0a ff 0d 0a 00",
        " 11  flags int32 = 0",
        " 15  extension header length int32 = 0",
        " 19  tuple 1 field count int16 = 13",
        " 21  id      length int32 = -1 (SQL NULL)",
        " 25  neg     length 8, body at 29: int64 -9007199254740993 "
        "(= 0xffdfffffffffffff, beyond JSON float exactness)",
        " 37  label   length 0 at 37, body empty at 41 "
        "(empty text, not NULL)",
        " 41  raw     length 4, body at 45: 00 ff 10 80",
        " 49  uid     length 16, body at 53: 123e4567 e89b 12d3 a456 "
        "426614174000",
        " 69  amount  length 30 at 69: ndigits 11, weight 7, sign 0x0000, "
        "dscale 13, groups 0012 0d80 1ed2 04d2 162e 2334 0d80 1ed2 04d2 "
        "162e 2328",
        "103  zero4   length 8 at 103: ndigits 0, weight 0, sign 0, "
        "dscale 4 -> 0.0000 (not 0)",
        "115  d       length 4, body at 119: 80000000 = -infinity",
        "123  ts      length 8, body at 127: 7fffffffffffffff = +infinity",
        "135  tz      length 8, body at 139: 8000000000000000 = -infinity "
        "(timestamptz, not timestamp)",
        "147  grid    length -1 (NULL array)",
        "151  notes   length -1 (NULL array)",
        "155  ranks   length -1 (NULL array)",
        "159  tuple 2 field count int16 = 13",
        "161  id      length 4, body at 165: 00000000 = 0",
        "169  neg     length 8, body at 173: 0",
        "181  label   length 5, body at 185: 'plain'",
        "190  raw     length 0 (empty byte string, body at 194)",
        "194  uid     length 16, body at 198: all zero",
        "214  amount  length 12, body at 218: ndigits 2, weight 0, "
        "sign 0x4000, dscale 4, groups 0001 08fc -> -1.2300",
        "230  zero4   length 8, body at 234: ndigits 0, sign 0xc000 = NaN",
        "242  d       length 4, body at 246: 0 = 2000-01-01",
        "250  ts      length 8, body at 254: 0 = 2000-01-01 00:00:00",
        "262  tz      length 8, body at 266: 00000000d693a400 = 3600000000 "
        "us = 2000-01-01 01:00:00+00",
        "274  grid    length 56 at 274: dims 2, hasnull 1, oid 23, "
        "(2, 0), (2, -3); elements 11, NULL, 13, 14",
        "334  notes   length 46 at 334: dims 2, hasnull 1, oid 25, "
        "(2, 0), (2, -3); elements \"\", NULL, \"ok\", \"\"",
        "384  ranks   length 36 at 384: dims 1, hasnull 0, oid 1700, "
        "(1, 0); element 1.2300",
        "424  end marker int16 = -1",
        "426  file size (no trailing bytes after the end marker)",
    ]


def _jsonl(records) -> str:
    return "".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
        for r in records)


def _material_readme(sample_expected, arrays_expected) -> str:
    return f"""Hand-written binary materials
=============================

Generated by materials/generate.py from byte templates in _common.py.  The
generator never imports the production codecs; the byte strings and the
expected JSON were written independently of them.

Files
-----
sample.copy         13-column, 2-tuple sample built by hand
sample.jsonl        expected decode of sample.copy, one line per tuple
sample.offsets.txt  key byte offsets in sample.copy
arrays.copy         3 tuples of 2-D arrays (NULLs, empty text, lower
                    bounds 0 and -3/-2, zero-dimension empty arrays)
arrays.jsonl        expected decode of arrays.copy
empty.copy          signature + flags=0 + extension length 0 + end marker
catalogue.json      catalogue for sample.copy
arrays_catalogue.json
interop.sql         real-server interoperability script (COPY FROM/TO)
manifest.json       byte count and SHA-256 of every file here

Expected values (business layer, independent of the decoder)
------------------------------------------------------------
tuple 1:
{json.dumps(sample_expected[0], ensure_ascii=False, indent=2)}
tuple 2:
{json.dumps(sample_expected[1], ensure_ascii=False, indent=2)}

Arrays:
{json.dumps(arrays_expected, ensure_ascii=False, indent=2)}

Destructive tests
-----------------
tests/test_fault_plan.py corrupts each of these files (signature, flags,
field length, numeric groups, array shape/OID, trailing bytes) and asserts
both the located error and that an existing destination file is untouched.
"""


if __name__ == "__main__":
    raise SystemExit(main())
