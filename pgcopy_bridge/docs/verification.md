# Verification report

Everything below was executed in this delivery environment.  Nothing is
asserted from memory.

## 1. Environment

* Python 3.11.2, Linux aarch64. `pytest` 9.2.0 installed in an isolated
  environment (the system Python had no pip; a throwaway micromamba env
  provided pytest only — the module under test has **no dependencies**
  and runs on stock CPython).
* PostgreSQL **16.16** (conda-forge build) was initialised in a scratch
  data directory, listening on a Unix socket
  (`/tmp`, port 55432, `listen_addresses=''`), trust auth.  This is a real
  server, not a mock: `SELECT version()` returned
  `PostgreSQL 16.16 ... 64-bit`.

## 2. Test suites

```
$ PGCOPY_BRIDGE_PSQL=/path/to/psql PGCOPY_BRIDGE_PGHOST=/tmp \
  PGCOPY_BRIDGE_PGPORT=55432 PGCOPY_BRIDGE_PGUSER=postgres \
  PGCOPY_BRIDGE_SERVER_DIR=/tmp/pginterop python3 -m pytest tests
# 269 passed  (real PostgreSQL 16.16 used)

$ python3 -m pytest tests
# 264 passed, 5 skipped  (no psql: interop tests skip with a reason)
```

(The five skips are the opt-in interop tests, skipped with the explicit
reason `no psql on PATH; set PGCOPY_BRIDGE_PSQL to run`.  They are not
silently counted as passes.)

| File | What it proves |
| --- | --- |
| `test_catalogue.py` | exact type set, aliases, duplicate names, key order, limits |
| `test_copy_framing.py` | signature, flags, extension header, end marker, byte-at-a-time, short reads, truncation, field counts |
| `test_scalars.py` | int exactness beyond 2^53, bool, text/bytea/uuid, NUL, UTF-8, date/timestamp sentinels, TZ independence |
| `test_numeric.py` | header fields, 0.0000, ±0 sign, long decimals (>28 digits), specials, every rejection rule, no float/Decimal in module |
| `test_arrays.py` | shape/lower_bounds, row-major, NULL vs empty text, OID mismatch, element count, negative dims, plain list rejected |
| `test_jsonl.py` | catalogue field order, missing/unknown/duplicate keys, float literal rejection anywhere, offsets, CRLF, chunked UTF-8 |
| `test_materials.py` | hand-written expectations, SHA-256 manifest, offsets, independent byte walk, byte-identical re-encode |
| `test_fault_plan.py` | one corruption per protocol element, located errors, target preserved, no temp file left |
| `test_limits.py` | exact boundary and one-over for all four limits, declared-size rejection before read |
| `test_streaming.py` | 1-byte and random short reads, extension headers, zero-row legal files, every truncation cut point |
| `test_memory.py` | 2k vs 40k rows peak flat; lazy decode |
| `test_cli.py` | all subcommands, exit codes, stderr locations, stdin publication, no business values in inspect |
| `test_interop_postgres.py` | server loads both our hand-written file and our encoder's output; our decoder reads a server-written file byte-for-byte equal to expected |

Key measurements from `bench/scale_report.json` (200 000 rows, 42.8 MB
JSONL → 32.4 MB COPY):

| phase | tuples | seconds | tracemalloc peak | process maxrss |
| --- | --- | --- | --- | --- |
| encode | 200 000 | 43.1 | 0.32 MB | 13.6 MB |
| decode | 200 000 | 72.6 | 0.07 MB | 13.6 MB |

At 2 000 rows the decode tracemalloc peak is 0.07 MB; at 40 000 rows it is
also 0.07 MB.  Peak does not scale with row count.  (`test_memory.py`
asserts `large_peak < small_peak + 2.0 MB` and prints the numbers.)

## 3. PostgreSQL interoperability actually performed

Script: `materials/generated/interop.sql` (also run by
`tests/test_interop_postgres.py`).

1. `COPY sample FROM 'sample.copy' WITH (FORMAT binary)` — PostgreSQL's
   own binary reader accepted the hand-written file.  2 rows.
2. In-SQL value checks returned `t` for both tuples:
   * `neg = -9007199254740993`, `label = ''`, `raw = 00ff1080`,
     `uid = 123e4567-...`, `amount = 123456789012345678901234567890.1234567890000`,
     `zero4 = 0.0000` (`scale()=4`), `d = -infinity`, `ts = infinity`,
     `tz = -infinity`, arrays `[0:1][-3:-2]` with NULL and `""` elements,
     `ranks = [0:0]={1.2300}`.
   * second tuple: `amount = -1.2300`, `zero4 = NaN`,
     `tz = 2000-01-01 01:00:00+00` (UTC, no local reinterpretation).
3. `COPY sample TO 'roundtrip.copy' WITH (FORMAT binary)`; our decoder
   read that server-produced file and produced JSONL **byte-identical**
   to the independently hand-written `sample.jsonl`.
4. Our `encode` output loaded with `COPY ... FROM ... (FORMAT binary)` and
   `EXCEPT`-diffed against the server-loaded hand-written table: 0 rows
   different in either direction.
5. PostgreSQL printed our `0.0000` value as `0.0000` with `scale()=4`.

## 4. Protocol details confirmed against the server

These were probed directly and are encoded in the implementation:

| Question | PostgreSQL 16 answer |
| --- | --- |
| numeric sign codes | `0x0000`, `0x4000`, `0xC000`, `0xD000`, `0xF000`; all others rejected |
| dscale maximum | 16383; 16384 rejected |
| digit group limit | 9999; 10000 rejected |
| numeric with ndigits=0, weight any | accepted, value 0 with dscale digits |
| hidden groups beyond dscale | accepted; `numeric_out` truncates (1.2345, dscale 1 → `1.2`) |
| NaN/Inf with digit groups | NaN accepted even with ndigits; we still reject it as malformed |
| date sentinels | INT32_MIN/INT32_MAX accepted as ∓infinity |
| timestamp sentinels | INT64_MIN/MAX accepted as ∓infinity |
| array dims max | 6 (`MAXDIM`) |
| array OID mismatch | rejected: `binary data has array element type 25 ...` |
| array extra element body | rejected (`improper binary format`) |
| array missing element | rejected (`incorrect binary data format`) |
| array NULL flag ≠ contents | rejected both ways |
| 1-D array with length 0 | accepted, canonicalised to 0-dimensional |
| dimension length 0 with supplied elements | rejected (we mirror this) |
| text NUL / invalid UTF-8 | rejected by server, so we reject on both paths |
| bool byte 2 | server accepts as true (non-zero); we reject anything but 0/1 (stricter subset) |

## 5. What was *not* verified

* We did not test a remote TCP server or a different PostgreSQL major
  version; the interop run used a local 16.16 cluster over a Unix socket.
* We did not test non-UTF-8 server encodings.
* The interop SQL uses server-side filesystem paths / `\copy`; with a
  remote server the reader must place the file on the server or use
  `\copy` (the script says so).
* No extreme array lower bounds (INT32_MAX) are produced by this encoder;
  decode preserves whatever the upstream writes.

## 6. Empty-directory reproduction

```
$ mkdir /tmp/cleanroom && cp -r pgcopy_bridge /tmp/cleanroom/
$ rm -rf /tmp/cleanroom/pgcopy_bridge/materials/generated
$ cd /tmp/cleanroom/pgcopy_bridge
$ python3 materials/generate.py          # wrote 11 files
$ python3 -m pytest tests                # 255 passed (PG env vars set)
$ diff -r materials/generated /path/to/original/materials/generated
                                         # identical
```

The generator is deterministic; `diff -r` was empty.
