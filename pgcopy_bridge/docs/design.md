# pgcopy_bridge design notes

## 1. Scope

A pure-Python, dependency-free converter between PostgreSQL's **binary
COPY** format and a **typed, line-oriented JSONL** representation.  The
column catalogue is the only source of truth for names, order and types;
no type is ever inferred from the payload.  The module deliberately does
not touch log templates, spatial queries or species analysis, and it does
not talk to a database.

## 2. Wire format implemented

```
11 bytes   "PGCOPY\n\xff\r\n\x00"
int32be    flags                          (this subset accepts 0 only)
int32be    extension header length N
N bytes    extension payload              (skipped by declared length)
repeat:
  int16be  field count                    (-1 terminates the file)
  per field:
    int32be field length                  (-1 = SQL NULL)
    body
int16be    -1                             (end marker)
```

This layout was checked against the output of PostgreSQL 16 (`COPY ...
WITH (FORMAT binary)`), and PostgreSQL was used to read files produced by
this encoder (see `materials/generated/interop.sql` and the opt-in
`tests/test_interop_postgres.py`).

Everything is read as explicit big-endian integers via
`int.from_bytes`; no protocol number is ever passed through `float`.

The parser (`copy_stream.CopyDecoder` + `reader.ByteReader`) is incremental
by construction: every `read_exact` loops until it has the requested bytes
or hits EOF, so a source that returns one byte per `read()` (a pipe,
`--read-chunk 1`) behaves identically.  The extension header is skipped
with `skip(N)`, so its bytes are never misread as a tuple count.

Rejection rules:
* signature mismatch, flags != 0, negative extension length, truncated
extension header;
* tuple field count != catalogue size, or any negative count other than -1;
* field length < -1, field length beyond the caller's byte limit;
* truncation of any length field or body;
* missing end marker, or any bytes after it.

Each error carries the absolute byte offset, the 1-based tuple (line)
number and the 1-based field index plus name.

## 3. Logical representation

JSONL: one compact object per tuple, keys in catalogue order.

| Type | JSON |
| --- | --- |
| int4, int8 | integer (exact Python int, arbitrary precision) |
| bool | `true` / `false` |
| text | string; UTF-8, NUL rejected |
| bytea | lowercase hex string, `""` = empty bytes |
| uuid | canonical lowercase `8-4-4-4-12` |
| numeric | `{"ndigits","weight","sign","dscale","digits"}` or exact decimal string on input |
| date | integer days since 2000-01-01; ±(2^31∓1) sentinels |
| timestamp | integer µs since 2000-01-01 00:00:00, no zone |
| timestamptz | integer µs since 2000-01-01 00:00:00 UTC |
| int4[]/text[]/numeric[] | `{"shape":[...], "lower_bounds":[...], "elements":[...]}` |

Distinctions preserved: SQL NULL (`null`) vs empty text (`""`) vs empty
bytea (`""`, distinct because the declared type differs) vs empty array
(`{"shape": [], "lower_bounds": [], "elements": []}`).  A `null` array
element is SQL NULL and differs from `""`.

`timestamp` and `timestamptz` share wire bytes but not declarations; the
catalogue's declared type is the marker, and the integer is always
interpreted as UTC-based for `timestamptz` and zone-free for `timestamp`.
No local timezone conversion is performed anywhere.

### No floats, anywhere

`json.loads` is called with a `parse_float` hook that returns a marker
object, and any such marker anywhere in the record (including inside
arrays and numeric objects) makes the record fail.  Bare `NaN`,
`Infinity`, `-Infinity` JSON literals are rejected the same way; the only
valid spelling for numeric specials is the quoted string.  `json.dumps`
runs with `allow_nan=False`.  Integers stay `int`; nothing is rounded.

## 4. numeric

Header: `int16 ndigits, int16 weight, uint16 sign, uint16 dscale`, then
`ndigits` base-10000 groups.  Value = Σ digits[i] · 10000^(weight−i).  Sign
codes: `0x0000` positive, `0x4000` negative, `0xC000` NaN, `0xD000`
+Infinity, `0xF000` −Infinity.  `dscale` is the *display* scale, so
`0.0000` is `ndigits=0, weight=0, sign=0, dscale=4`.

Decode keeps every field.  Encode accepts either the object (validated:
digit range 0..9999, `ndigits == len(digits)`, dscale ≤ 16383, weight
int16, specials must have no groups) or an **exact decimal string** whose
groups are computed from its digits with integer arithmetic
(`int`, `divmod`, powers of ten) — never from `float` or `Decimal`
contexts, so >28 significant digits are unaffected.

`numeric.to_text` is a display helper mirroring PostgreSQL's printing
rule (digits beyond `dscale` are discarded, tested against PG:
`groups [1,2345], dscale 1` prints `1.2`).  It is not part of the
conversion path.

## 5. Arrays

Body: `int32 ndim, int32 hasnull, int32 element_oid`, then per dimension
`int32 length, int32 lower_bound`, then `total` elements in row-major
order, each `int32 length` (-1 = NULL) + scalar body.  Element OIDs used:
int4=23, text=25, numeric=1700.

Decoded as shape/lower_bounds/elements — never flattened into nested
lists, so a lower bound of 0 or −5 survives.  Zero dimensions means the
empty array with zero elements.

Validation on decode: ndim ≥ 0 and ≤ `max_array_dims`; NULL flag ∈ {0,1};
element OID equals the catalogue's; dimension lengths ≥ 0; element count
equals the product; no missing element length, no element length < −1,
no trailing element bytes; NULL flag exactly 1 iff a NULL element is
present.

On encode: shape/lower_bounds same arity, each int32, lengths ≥ 0,
`len(elements) == product`, elements encoded with the scalar codec, NULL
flag computed from the elements.

Every declared size is checked against `Limits` **before** the body is
read or built: `max_field_bytes` at each field length, `max_array_dims` at
`ndim`, `max_array_elements` on the running product (checked per
dimension, so a 1000×1000 claim fails on the header, not while allocating).

## 6. Streaming and atomic publication

Decode reads one tuple at a time and writes one JSON object line at a
time.  Encode reads one JSONL line at a time and writes one tuple's bytes.
Python holds: the read buffer (default 64 KiB), one field body (bounded by
`max_field_bytes`), one tuple's encoded bytes, the line being parsed
(bounded by `max_record_bytes`).  The scale bench (`bench/scale_report.py`)
reports measured peaks at 200 000 rows; see `docs/scale.md`.

Both commands write to a temporary file in the destination directory and
call `os.replace` only after the block completes — which happens only
after the end marker (decode) or the last JSONL line (encode) is
validated.  On any failure the temp file is removed and the previous
destination is untouched.

Standard input cannot be rewound, but this does not weaken the rule: the
conversion consumes the whole stream into the temp file first; the rename
happens after EOF validation.  If the stream ends mid-tuple, the
destination is never replaced.  This is documented in the CLI module
docstring and README.

## 7. inspect

`inspect` fully decodes and validates every value (so numeric groups and
array headers are checked) but retains no business values and prints
only: column count, tuple count, per-column NULL/non-NULL counts, the
header offset, and the limits in force.  A corrupt file exits non-zero
before any output about counts is produced.

## 8. Error model

`PgCopyError` subclasses: `DescriptorError`, `CopyFormatError`,
`ValueFormatError`, `LimitsError`, `RecordError`.  Every instance carries
`offset` (byte), `line` (tuple or JSONL line), `field` and `field_name`.
The rendered form is stable for tests:

```
copy: truncated input: field 2 (neg) body needs 10000 byte(s), only 397 available (byte 29, line 1, field 2 neg)
```

## 9. Verification strategy

Three layers of evidence:

1. **Business layer** — `materials/generated/*.jsonl` are written by
   hand in `materials/generate.py`, which builds bytes from `struct`
   templates and never imports the package.  Decoding must reproduce them
   exactly; re-encoding must reproduce the `*.copy` bytes exactly.
2. **Byte layer** — `tests/independent_checker.py` re-implements the
   wire walk (again without importing the package) and checks signatures,
   tuple counts, field lengths, numeric headers/groups and array
   headers/OIDs against encoder output.
3. **Interoperability** — `materials/generated/interop.sql` loads the
   same bytes with PostgreSQL's own reader, checks every value in SQL,
   dumps a fresh COPY, and a second load.  `tests/test_interop_postgres.py`
   runs the same steps through `psql`, or skips loudly when no server is
   reachable.

The environment used for delivery did have a PostgreSQL 16 server; see
`docs/verification.md` for exactly what was run and which claims were
verified.
