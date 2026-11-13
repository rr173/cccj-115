# Design notes

## 1. Where decisions are made

| Layer | Module | Responsibility |
| --- | --- | --- |
| Descriptor | `dwca/descriptor.py` | Parse/validate `meta.xml`: namespaced terms, dialect whitelist, escapes, member safety, duplicate-term/index checks. No data bytes are read here. |
| Parsing | `dwca/parser.py` | Streaming CSV reader per table: physical-line header skip, logical record numbering, quoting, defaults and cell origins, short-row detection. |
| Import | `dwca/importer.py` | Two-phase disk-backed load, duplicate-key adjudication, SQL attribution, quarantine, counts. |
| Storage | `dwca/schema.py` | SQLite tables, indexes and the reason/origin vocabularies. |
| Reads | `dwca/library.py` | Iterable facade; extension rows grouped per core, never wide-joined. |
| CLI | `dwca/cli.py` | `import` / `inspect` entry points. |

## 2. Descriptor validation order

The archive is rejected before any table data is parsed when:

1. `meta.xml` is absent or not well-formed XML.
2. The root is not `<archive>`, or the core is not exactly one
   `rowType=".../terms/Occurrence"`.
3. A table has not exactly one `files/location`; the normalized member path
   is absolute, uses backslashes/drive letters, contains `..`, or does not
   physically exist in the ZIP.
4. A dialect attribute is outside the whitelist (encodings UTF-8 / UTF-8
   BOM / Latin-1; comma/tab; LF/CRLF; double-quote or no quoting).
5. Backslash escapes contain a dang backslash or an unknown escape.
6. A field has neither `index` nor `default`; an index is non-integer or
   negative; a term URI repeats inside one table; two extensions share a
   row type; two tables map the same member.

Extension row types being distinct is a subset restriction that keeps
`table_id` a stable, single-valued identity (`core`, `ext:<rowType URI>`).

## 3. Parsing semantics

- The ZIP member stream is wrapped in `TextIOWrapper(..., newline="")` so
  CR/LF bytes are preserved for the csv module; embedded newlines inside
  quoted fields therefore stay inside one logical record. UTF-8 members use
  the `utf-8-sig` codec, which differs from plain UTF-8 only by consuming
  one leading BOM, so a BOM cannot leak into the first id cell even with
  `ignoreHeaderLines=0`; Latin-1 members use `latin-1` directly.
- Header skipping is implemented as a stream wrapper
  (`_HeaderSkipper`). CPython's C csv reader may call either `readline` or
  the iterator protocol, so both routes drain the declared number of
  physical lines exactly once, on the first read, before the first data
  line is handed over.
- Record numbers count logical records from 1 after the header drain; a
  quoted field spanning physical lines increments the count once.
- Cell provenance is explicit: `source` (a non-empty decoded cell),
  `default` (empty cell replaced by a declared default, or a no-index
  default-only field), `empty` (empty cell, no default). Values stay text.
- A short row is detected when *any mapped* index (id/coreid included) is
  beyond the cells actually present. Such rows still keep every cell that
  did parse plus the raw cell list; missing terms are never invented.

## 4. Keys and the two-phase import

Phase 1 streams the core. Python holds only two sets of key strings
(`seen_keys`, `ambiguous`) and short/empty row bookkeeping; all row text is
inserted into SQLite in batches. After the stream ends, one SQL
`UPDATE ... WHERE core_id IN (...)` invalidates every row of each
duplicate key in a single statement — this is what makes a duplicate that
appears on the last physical row still invalidate the first occurrence,
including rows sitting in the final pending batch.

Empty-key rows are invalid and unreferenceable. A short core row with a
non-empty id still takes part in duplicate detection; it can carry both
`short_row` and `duplicate_key` reasons.

Phase 2 streams each extension the same way, then resolves attribution with
set-based SQL, in a fixed precedence:

1. `empty_reference` — `coreid` decoded to `""`;
2. `missing_reference` — no core row has that exact id;
3. `ambiguous_reference` — the id occurs on more than one core row;
4. `bad_core_reference` — exactly one core row matches and it is invalid
   without being part of a duplicate-key group.

Only rows outside all four predicates receive a `core_row_id` link. The
four predicates are mutually exclusive per row, which keeps each
quarantined extension row attributable to one precise cause (a short
extension row may additionally carry `short_row`).

Key comparison is always SQLite text equality on the decoded string: no
trimming, no case folding. `occurrenceID` is treated as an ordinary field
and is never used as a join key.

## 5. Why no wide join

A core with *m* measurement rows and *n* media rows is stored as one core
row plus two independent child lists. A Cartesian join would fabricate
`m*n` business records and make "which measurements does this occurrence
have?" ambiguous. `iter_core()` therefore returns
`extensions: {table_id: [ExtensionRecord, ...]}` with an explicit empty
list for extension tables that contribute nothing. Identical rows in one
extension are distinct because `(table_id, record_number)` is unique.

## 6. Memory and disk boundary

- Parsing materialises one logical record at a time plus one insert batch
  (`INSERT_BATCH`, 2 000 rows) of pending tuples.
- Python-side key bookkeeping scales with the number of *distinct core ids*,
  not with row width or total text volume.
- Attribution and adjudication run inside SQLite; the importer never loads
  an extension table or the core table back into Python lists.
- Reads use server-side cursors (`fetchmany(FETCH_SIZE)`) and bulk cell /
  extension preloading per batch, so neither a full table scan nor an N+1
  query pattern accumulates result text in memory.
- Everything needed for later reads — rows, cells, origins, raw quarantine
  cells, field maps and the original `meta.xml` — is inside the SQLite
  file. The ZIP can be deleted or moved after import.

## 7. Failure atomicity

The target database must not pre-exist. If descriptor validation, decoding
or any later step raises, the partial SQLite file is removed and the
caller sees the original exception; no half-imported database is left
behind.
