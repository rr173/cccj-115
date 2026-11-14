# pgcopy_bridge

Bidirectional converter between **PostgreSQL binary COPY** files and
**typed UTF-8 JSONL**, driven by an explicit column catalogue. Built for an
offline processing machine that receives binary `COPY` dumps from an
upstream warehouse and has no database connection.

* No dependencies, Python ≥ 3.10.
* Types are declared, never guessed. Column order is the catalogue order.
* Exactness: integers and base-10000 numerics never pass through a float.
* Streaming: memory is bounded by read buffer + one tuple, not row count.
* Atomic file publication: a failed conversion never replaces the target.
* Errors carry byte offset, tuple/line number and field name.

It does not handle log templates, spatial queries or species analysis, and
does not connect to a database.

## Install / run

```bash
cd pgcopy_bridge
python3 -m pip install -e .          # optional; the CLI also runs in place
python3 -m pgcopy_bridge.cli --help
# or, after installation:
pgcopy --help
```

Tests need `pytest`:

```bash
python3 -m pip install pytest
python3 -m pytest
```

## Catalogue

The catalogue is a JSON file that names columns **in store order** and
gives each one a PostgreSQL type. Duplicate names are rejected. Supported
spellings: `int4`, `int8`, `bool`, `text`, `bytea`, `uuid`, `numeric`,
`date`, `timestamp`, `timestamptz`, `int4[]`, `text[]`, `numeric[]`
(common aliases such as `integer`/`bigint`/`timestamp with time zone` are
accepted for the same types; nothing else is).

```json
{
  "columns": [
    {"name": "id",     "type": "int8"},
    {"name": "label",  "type": "text"},
    {"name": "amount", "type": "numeric"},
    {"name": "grid",   "type": "int4[]"}
  ]
}
```

`pgcopy catalogue-template` prints a full skeleton;
`pgcopy types` prints the complete logical representation with examples.

## Logical representation (JSONL)

One compact JSON object per line, keys in catalogue order. Full examples:

| Type | Example JSON value | Notes |
| --- | --- | --- |
| `int4` | `2147483647` | JSON integer |
| `int8` | `9007199254740993` | exact; beyond float's 2^53 |
| `bool` | `true` | |
| `text` | `"aé中"` | UTF-8, NUL rejected, `""` = empty text |
| `bytea` | `"00ff10"` | lowercase hex, reversible for all 256 byte values |
| `uuid` | `"123e4567-e89b-12d3-a456-426614174000"` | canonical lowercase; other input spellings normalised |
| `numeric` | `{"ndigits":2,"weight":0,"sign":0,"dscale":4,"digits":[1,2300]}` | = 1.2300, exactly |
| `numeric` (special) | `{"ndigits":0,"weight":0,"sign":"NaN","dscale":0,"digits":[]}` | also `"Infinity"`, `"-Infinity"` |
| `numeric` (input) | `"123456789012345678901234567890.1234567890000"` | exact decimal string; groups derived from its digits |
| `date` | `0` | days since 2000-01-01; `-2147483648` / `2147483647` = ±infinity |
| `timestamp` | `3600000000` | µs since 2000-01-01 00:00:00, no zone; ±2^63 sentinels |
| `timestamptz` | `3600000000` | µs since 2000-01-01 00:00:00+00; ±2^63 sentinels |
| `int4[]` | `{"shape":[2,2],"lower_bounds":[0,-3],"elements":[11,null,13,14]}` | row-major flat list, NULL element = `null` |

Key rules:

* **NULL is not empty.** `null`, `""` (empty text or empty bytea, per the
  declared type), and `{"shape": [], "lower_bounds": [], "elements": []}`
  (empty array) are three distinct values. Empty text and empty bytea
  share the spelling `""` but never share a column.
* **No floats.** JSON float literals (including nested inside arrays and
  numerics) and bare `NaN`/`Infinity` are rejected. Use integers or exact
  decimal strings. This prevents silent precision loss through `float`.
* **numeric** keeps `ndigits`, `weight`, `sign`, `dscale` and the digit
  groups. `0.0000` has `ndigits=0, dscale=4` and is not `0`.
* **Arrays** keep `shape` and `lower_bounds`; lower bounds of 0 or negative
  values survive and are never silently normalised to 1. A plain JSON list
  is rejected as input.
* **timestamptz** is always UTC-based; nothing is reinterpreted in the
  local timezone.

## Commands

```bash
pgcopy decode  CATALOGUE.json IN.copy  OUT.jsonl [--read-chunk 1] [limits]
pgcopy encode  CATALOGUE.json IN.jsonl OUT.copy  [--read-chunk 1] [limits]
pgcopy inspect CATALOGUE.json IN.copy            [limits]
pgcopy types
pgcopy catalogue-template
```

`-` in place of the input path means standard input.

Limits (checked against declared wire sizes *before* allocation):

```
--max-field-bytes     default 67108864  (one field body)
--max-array-dims      default 6
--max-array-elements  default 4000000
--max-record-bytes    default 134217728 (one JSONL input line, encode only)
--read-chunk          default 65536     (1 = byte-at-a-time input)
```

`inspect` prints column count, actual tuple count, per-column NULL and
non-NULL counts, header offset and the limits in force. It prints no
business values and fails on a corrupt file.

## File publication contract

Both conversions write to a temporary file beside the destination and
`os.replace` it **only after the entire input has been validated**:

* decode: after the end marker and the check that no bytes follow it;
* encode: after the last JSONL record has been parsed and encoded.

If anything fails — including bad data in the last tuple — the existing
destination keeps its exact bytes and the temporary file is removed.

**Standard input** cannot be rewound. The rule still holds because the
whole stdin stream is consumed into the temporary file first; publication
happens only after EOF validation. A failure mid-stream never touches the
destination. There is no output on stdout for `decode`/`encode`; the
result is the named file.

## Error format

```
copy: truncated input: field 2 (neg) body needs 10000 byte(s), only 397 available (byte 29, line 1, field 2 neg)
copy: 2 extra byte(s) after the end marker (byte 426)
value: numeric digit group 0 is 12345, allowed range 0..9999 (byte 81, line 1, field 6 amount)
value: array element OID 25 does not match catalogue element type OID 23 (byte 286, line 2, field 11 grid)
record: missing field(s) ['tz']; catalogue has [...column names...] (line 3)
```

`byte` is the absolute offset in the COPY file, `line` the 1-based tuple
(JSONL line for `record:`), `field` the 1-based column.

## Layout

```
pgcopy_bridge/       implementation
  catalogue.py       catalogue parsing, type registry, aliases
  limits.py          Limits
  reader.py          incremental byte reader (works 1 byte per read)
  copy_stream.py     COPY framing reader/writer
  values.py          scalar codecs + array wrapper
  numeric.py         exact base-10000 numeric codec
  jsonl.py           JSONL line reader/parser
  publish.py         atomic output
  library.py         decode_file / encode_file / inspect_file
  cli.py             command line
materials/           hand-written binary generator + generated artefacts
tests/               unit, fault, streaming, memory, interop suites
bench/               scale report (rows, peak memory)
docs/                design, verification, scale notes
```

## Reproduce from an empty directory

```bash
cd /path/to/pgcopy_bridge
python3 materials/generate.py            # rebuilds materials/generated/
python3 -m pytest                        # 250+ tests, no network needed
python3 bench/scale_report.py --rows 200000
```

The generated materials are checked in, so `pytest` works without running
the generator first. `tests/test_materials.py` verifies their SHA-256
manifest and every documented byte offset.
