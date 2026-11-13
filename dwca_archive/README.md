# dwca_archive

Streaming, attribution-tracked importer for a documented subset of
**Darwin Core Archives (DwC-A)**. A ZIP containing a namespaced
`meta.xml`, one **Occurrence core** and zero or more extensions is turned
into a single SQLite database in which every row keeps its provenance, so a
data steward can trace exactly which measurements and media belong to one
occurrence record.

The module deliberately performs **no** scientific inference: no numeric,
date or name parsing, no taxonomic decisions, no deduplication of business
content. Values stay text. It does not touch the occupancy model or the
point-cloud modules in this repository.

## Supported DwC-A subset

The contract below is enforced; anything outside it is rejected with an
explicit error rather than guessed.

### Archive layout

- One ZIP whose root contains a namespaced `meta.xml`
  (`http://rs.tdwg.org/dwc/text/`).
- Exactly one `<core>` with `rowType=".../terms/Occurrence"` and zero or more
  `<extension>` elements; extension row types must be distinct.
- Each table declares exactly one `files/location`. Members are read only
  from the ZIP itself: absolute paths, drive letters, backslash paths and
  `..` traversal are rejected, and every declared location must physically
  exist. Nothing is fetched over the network.
- Members may appear in any order inside the ZIP (extensions before the core
  is fine).

### Descriptor attributes parsed

Per table: `encoding`, `fieldsTerminatedBy`, `linesTerminatedBy`,
`fieldsEnclosedBy`, `ignoreHeaderLines`; per field: `index`, `term`,
`default`; plus core `<id>` and extension `<coreid>` indices.

| Attribute | Accepted values |
| --- | --- |
| `encoding` | UTF-8, UTF-8 with BOM, Latin-1 (ISO-8859-1) |
| `fieldsTerminatedBy` | comma or tab (literal or `\t` escape) |
| `linesTerminatedBy` | LF (`\n`) or CRLF (`\r\n`), backslash escapes allowed |
| `fieldsEnclosedBy` | double quote, or empty string for no quoting |
| `ignoreHeaderLines` | non-negative integer count of **physical** lines (absent means 0) |

Backslash escapes in descriptor attributes decode `\t`, `\n`, `\r` and
`\\`; dangling or unknown escapes are rejected. A UTF-8 BOM, when present,
is stripped from the first member byte regardless of how many header lines
are skipped, so it never pollutes the first key cell.

### Text format semantics

- Quoted fields may contain the delimiter, embedded newlines and doubled
  quote escapes (`""`). A quoted line break does **not** split a record.
- Header skipping counts physical lines before CSV parsing; afterwards
  record numbers count **logical** data records starting at 1.
- Terms are stored as full URIs. Two URPs sharing a local name
  (e.g. `.../dwc/terms/note` and `http://example.org/ns-alpha/note`) remain
  distinct columns.
- A `<field>` without `index` must carry a `default`. For indexed fields an
  empty source cell uses the declared default when present, otherwise the
  empty string is kept. The origin of every cell is recorded as
  `source`, `default` or `empty`.
- Unmapped columns are ignored (no invented terms). A mapped column that is
  physically out of range on a short row quarantines the row instead of
  silently filling empty values.
- Duplicate field terms within a table, negative/non-integer indices and
  unsupported dialects are descriptor errors that reject the whole archive
  before data is read.

## Keys and attribution rules

The core's `<id>` is the archive's join key — never `occurrenceID`. Two core
rows may share an `occurrenceID` and remain two records.

- Keys match on the exact decoded string: no trimming, no case folding; an
  empty key is invalid.
- If a non-empty key occurs on more than one core row — even if the repeat
  is the last row of the file — **all** core rows carrying that key become
  ambiguous. A short core row with a parseable non-empty id still
  participates in duplicate detection.
- Extension `coreid` attribution yields one of four locatable failure
  reasons, kept in quarantine with source values intact:
  - `empty_reference` — decoded coreid is the empty string;
  - `missing_reference` — no core row carries that id;
  - `ambiguous_reference` — the id matches duplicate-key core rows;
  - `bad_core_reference` — exactly one core row matches but it is itself
    invalid (e.g. short row).
- Rows of different extensions are stored independently; two identical rows
  in one extension are never deduplicated.

## Storage model (SQLite)

- `tables`, `field_maps` — parsed descriptor and dialect, plus per-table
  `input_count / valid_count / quarantined_count` for reconciliation.
- `core_rows` / `core_cells`, `ext_rows` / `ext_cells` — valid and invalid
  rows alike; row tables stage the raw decoded cell list (as JSON) and cell
  rows store value plus origin.
- `quarantine` — isolated rows with member name, logical record number, key
  value (when decodable), JSON reason list, raw cells and links back to the
  stored term cells.
- `descriptor_xml` — the original `meta.xml` bytes; `meta` — import metadata.

Reads group extension rows per core record instead of producing a wide
Cartesian join: one core with two measurements and three media stays one
core with two lists (length 2 and 3), never six business rows. Empty
extension groups are represented explicitly.

## Memory boundary

Import is two-phase and disk-backed:

1. Core rows stream from the ZIP through the CSV parser into SQLite in small
   batches. Python retains only sets of key strings (seen keys, ambiguous
   keys) — never record texts.
2. Extensions stream the same way; attribution is resolved with SQL against
   the stored core rows, so late duplicate keys invalidate prior references
   without buffering extension data.

Peak Python memory is bounded by one insert batch plus the number of
distinct key strings, independent of total record text. Reads use
server-side cursors (`fetchmany`) as well. The test suite includes a
scalable synthetic archive (500 vs 20,000 cores, 80,000 extension rows)
asserting resident memory does not grow with the record-text volume.

## Usage

Command line:

```bash
python -m dwca.cli import archive.zip output.db   # refuses to overwrite
python -m dwca.cli inspect output.db              # works after ZIP is gone
```

Python API:

```python
from dwca import import_archive, DwcaLibrary

result = import_archive("archive.zip", "output.db")
print(result.format_report())          # per-table input/valid/quarantined

with DwcaLibrary.open("output.db") as lib:   # no ZIP access needed
    for core in lib.iter_core():              # streaming, source order
        core.core_id                          # exact decoded key
        core.values["http://rs.tdwg.org/dwc/terms/occurrenceID"]
        core.origins[term]                    # "source" | "default" | "empty"
        for table_id, rows in core.extensions.items():
            ...                               # grouped lists, never a join
    for q in lib.iter_quarantine():           # isolated rows + reasons
        q.table_id, q.record_number, q.reasons, q.raw_cells
    lib.field_maps("core")                    # parsed mapping, full URIs
```

## Tests

```bash
python -I -m pytest tests/ -q
```

Tests synthesize archives from hand-written bytes
(`tests/materials.py`) with expectations written independently of the
production code, covering: a core with two measurements and three media,
two cores sharing one `occurrenceID`, extensions preceding the core in the
ZIP, a duplicate key appearing only on the last core row, a bad core being
referenced, orphan extensions, reordered columns, unmapped columns,
defaults, same-local-name URIs, quoted embedded newlines, all three
encodings, short rows, descriptor rejection and path traversal, per-table
count reconciliation, offline reads after the ZIP is removed, and the
scalable memory-bound check.
