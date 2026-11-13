# Design notes

## 1. Why an IBLT for this handover

The reserve and the partner each keep a local checklist. Publishing the whole
list back and forth costs bandwidth proportional to the collection and still
only tells you *how many* rows differ. A capped-size IBLT stores enough
algebraic information to recover a **small** number of differences:

* both sides build a digest with the same configuration;
* they exchange only the digests (fixed number of bytes);
* subtraction cancels the common identities exactly, leaving a table whose
  entries are the raw set difference;
* peeling expands that difference into concrete UUID/fingerprint records.

When the difference exceeds what the table can peel, the protocol fails
loudly. It never degrades into “close enough”.

## 2. Layers and boundaries

| Layer | Module | Responsibility |
| --- | --- | --- |
| Identity | `store.py` | Line-by-line parse, fixed-width hex checks, SQLite uniqueness, duplicate counting, conflict detection with line numbers |
| Hash/index | `config.py` | Protocol constant, domain-separated SHA-256, `k` distinct cell indices, element checksum |
| Algebra | `table.py` | Fixed cell array, insertion with sign, subtraction, wire body |
| Container | `digest.py` | 64-byte header, cell body, SHA-256 trailer, strict loader, store folding |
| Decoder | `peel.py` | Pure-cell test, peeling loop, consistency refusal, residual report |
| Reporting | `reconcile.py` | Config equality, direction pairing, human/JSON output |
| Orchestration | `builder.py`, `cli.py`, `demo.py` | Atomic writes, exit codes, executable story |

The module imports nothing from `occupancy`, `pointcloud_las` or
`dwca_archive`; it only uses the standard library (`sqlite3`, `hashlib`,
`struct`, `json`).

## 3. Identity and disk deduplication

Checklist lines are streamed. For each accepted line the decoded
`(uuid BLOB, fingerprint BLOB)` pair is inserted into

```sql
CREATE TABLE elements (
    uuid        BLOB PRIMARY KEY,
    fingerprint BLOB NOT NULL,
    first_line  INTEGER NOT NULL
) WITHOUT ROWID;
```

* The primary key enforces “one fingerprint per identifier”.
* Exact repeats hit the same primary key; they are counted as duplicates and
  the first occurrence's line is kept.
* Before insertion a batch is checked with indexed `IN` lookups in chunks of
  400, so a conflicting fingerprint is detected against earlier committed
  rows without a Python set. Within a batch an overlay dict is needed only to
  pick the line number.
* A conflict raises `ChecklistConflictError` with both line numbers and both
  fingerprints; the transaction rolls back, so no digest and no committed
  store remain from that run.

Python memory during ingestion is `batch_size` parsed identities plus one
lookup chunk. The digest fold reads the table through `fetchmany(1000)`.

## 4. Fixed-capacity guarantee

The digest has `capacity` cells of 64 bytes each. Nothing scales with the
checklist:

* the body is serialized from the cell array;
* the header has fixed-width counters only;
* `unique_elements`, `source_lines`, `duplicate_lines` are metadata, not data.

The only “hidden” copy of checklist information would be a JSON blob of
identities. The format has no place for one, and the loader verifies the
header count against `sum(count) / k` so a hand-edited header cannot claim a
different element count than the cells encode.

## 5. Peeling correctness conditions

The decoder refuses to conclude anything from a single ambiguous cell:

1. `abs(count) == 1`; a count of 0 with residual key material is the XOR of
   at least two elements and never treated as empty.
2. The 8-byte check XOR must equal `check(key_xor)`.
3. The element's hash must include the cell index where it was found; a pure
   count with a mismatched mapping stops peeling with a consistency error.
4. Every peeled element is removed from all `k` cells; the queue is only
   re-examined when a cell becomes pure.
5. Completion requires every cell to be all-zero *and* the net signed count
   to equal `left.unique_elements − right.unique_elements`.
6. Partially peeled elements are exposed separately (`peeled_clues`) and
   never merged into the reported differences.

Conditions 2–3 make an accidental “count 1” from unrelated XOR residue
extremely unlikely to be accepted, and condition 5 catches arithmetic drift
between the header and the cells.

## 6. Same-identifier pairing

Raw peeling produces signed elements. Two elements for the same UUID with
opposite signs are the same specimen under old and new content. They are not
reported as a loss plus an addition; the reporter emits one
`CONTENT_CHANGED` row with both fingerprints. Identical fingerprints cannot
survive subtraction (they cancel bit for bit), so seeing the pair at all
means they really differ.

## 7. Direction and argument order

The difference table is `left − right`; the sign of a recovered element is
therefore relative to the argument order. `LEFT_ONLY` / `RIGHT_ONLY` always
name the first and second digest arguments. Swapping the arguments negates
every sign and swaps the labels — the demo checks both directions and the
tests diff them against the independent reference.

## 8. Failure atomicity

* Checklist errors and conflicts happen before any digest byte is written.
* The digest is written to a temporary file in the same directory, fsynced
  and moved/linked into place. An existing file is refused unless
  `--overwrite` is given; even with `--overwrite` a crash leaves the
  destination either old or new, not half-written.
* An identity store path is never silently replaced; a failed run removes the
  store it just created.
* `compare` treats digests as read-only and checks the configuration before
  any arithmetic.

## 9. Exit statuses

| code | meaning |
| --- | --- |
| 0 | success, complete reconciliation |
| 1 | invalid input / malformed digest / I/O error |
| 2 | refused to overwrite an existing digest or store |
| 3 | peeling incomplete — no difference list claimed |
| 4 | checklist conflict (same UUID, two fingerprints) |
| 5 | digests built with different configurations |

Exit status 3 exists so a pipeline cannot mistake an incomplete peel for a
successful empty difference.

## 10. Limits

* Capacity is capped at 2²⁰ cells (64 MiB body); `k` at 16. Larger tables
  are a rebuild decision, not a silent upgrade.
* `peel` mutates its own difference table only; the loaded digests are never
  modified, so the same objects can be compared again or saved unchanged.
* The tool reports a **probabilistic** guarantee: SHA-256 fingerprints and an
  8-byte cell checksum. It deliberately does not describe this as
  zero-collision proof, and a saturated table is reported as incomplete.
