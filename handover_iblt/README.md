# handover_iblt

Reconciling handover of specimen checklists. Two parties (a reserve and a
partner institution, two collection databases, two field teams) each derive a
**fixed-size digest** from their local checklist and exchange *only that
digest*. The digest is an **invertible Bloom lookup table (IBLT)**: a capped
array of cells that can be subtracted. Peeling the difference recovers a small
number of differences and reports, for each one:

* `LEFT_ONLY` — the identifier is present in the left/sender checklist only;
* `RIGHT_ONLY` — present in the right/receiver checklist only;
* `CONTENT_CHANGED` — the same 16-byte identifier exists on both sides but with
  a different 32-byte content fingerprint. Both fingerprints are reported
  together, so one changed specimen never looks like two unrelated losses.

The module stores identities in SQLite for exact deduplication, but the
transferred artifact contains no specimen payload and no list of the original
identities. It does not touch the repository's occupancy, point-cloud or
DwC-A modules.

## Contract in one page

### Element and checklist format

A checklist is UTF-8 text, one entry per line:

```
<UUID: 32 hex chars>,<SHA-256 content fingerprint: 64 hex chars>
```

* Both fields are **fixed-width hex**. Anything else (wrong length, `zz`,
  missing field, an extra field, blank lines) is rejected with the line number.
  Input is read line by line.
* A UUID is exactly 16 bytes, a fingerprint exactly 32; an element is those 48
  bytes. **Identity comparison always uses the decoded bytes**, so uppercase
  and lowercase hex describe the same specimen.
* Exact repeats of `(uuid, fingerprint)` count once. They are discarded by a
  SQLite `UNIQUE` constraint, never by a Python set, so an arbitrarily large
  checklist does not accumulate identities in memory.
* One UUID carrying **two different fingerprints inside one checklist** is a
  checklist conflict: the build is refused, no digest is written, and both
  input line numbers and fingerprints are named.
* No specimen payload is read or transmitted.

### Digest protocol (version `iblt-handover/1`)

An IBLT has `capacity` cells, each holding:

| field | width | meaning |
| --- | --- | --- |
| `count` | 8 bytes, signed big-endian | number of elements hashed here (with sign) |
| `key_xor` | 48 bytes | XOR of the full 48-byte elements |
| `check_xor` | 8 bytes | XOR of each element's 8-byte checksum |

`k`, `capacity` and the 64-bit `seed` are fixed in the header. The digest file
is exactly **96 + 64 × capacity bytes**:

```
 0   8   magic b"HNDIBLT1"
 8  16   protocol, NUL padded, "iblt-handover/1"
24   4   capacity (uint32)
28   4   k (uint32)
32   8   seed (uint64)
40   8   unique elements (uint64)
48   8   input lines read (uint64)
56   8   duplicate lines ignored (uint64)
64  64*capacity   cells
end 32  SHA-256 over bytes [0, end-32)
```

Sizes are fixed-width and the body size depends only on `capacity`: folding in
10 or 100,000 common entries produces the same file size.

### Stable hash, cell selection and checksum

All hashes are SHA-256 with explicit domain separation. For element `E`
(48 bytes) and seed `s`:

```
h1 = BE64( SHA256( "iblt-handover/hash-cell/v1" || b"i" || BE64(s) || E ) )
h2 = BE64( SHA256( "iblt-handover/hash-cell/v1" || b"j" || BE64(s) || E ) ) | 1
cell_i = (h1 + i*h2 + i*i) mod capacity              for i = 0 .. k-1
```

If `cell_i` was already chosen for this element, it advances by 1 modulo
capacity until unused, so the `k` cells are **pairwise distinct**. The same
function runs at insertion and at peeling; a recovered element must map back
to the cell that produced it.

```
check(E) = first 8 bytes of SHA256( "iblt-handover/check/v1" || E )
```

Inserting an element adds `+1` to `count` and XORs `E` and `check(E)` into the
`k` chosen cells. Removing does the same with `-1`. Changing `seed` or `k`
requires rebuilding; **digests with different capacity/k/seed are refused as
incomparable**, and neither file is modified.

### Subtraction and peeling

The difference table is `left − right`: cell counts subtract, key/check XOR
fields XOR. A cell is **pure** when `abs(count) == 1` *and* the cell's 8-byte
check equals `check(key_xor)`. The element is recovered with the sign of
`count` (`+1` = left only, `-1` = right only), then removed from every one of
its `k` cells. This repeats until no pure cell is left.

A recovered element is only accepted if it maps to the cell it was found in;
`count == 1` alone is not proof. Peeling stops when no cell is pure.
**Success means every cell is all-zero.** Otherwise the result is
`INCOMPLETE`: no difference list is claimed, the number of remaining nonzero
cells, `sum|count|`, `max|count|` and sample indexes are reported, and any
partially peeled elements are shown only as diagnostic clues. There is no
hidden copy of the checklist inside the digest to paper over a failed peel.

Retry with a larger table by rebuilding both digests **from the original
checklists** (`build --capacity ...`). The tool never silently widens a table.

Peeled elements are grouped by UUID. One element on one side is `LEFT_ONLY` or
`RIGHT_ONLY`; two elements with the same UUID and opposite signs are one
`CONTENT_CHANGED` row carrying both fingerprints.

### Load-time validation

`load_digest` rejects, before any comparison: missing/short files, wrong
magic, unknown protocol, magic/header/body/trailer length mismatch, extra
trailing bytes, a changed body or header (SHA-256 mismatch), impossible
`capacity`/`k` combinations, cell counts of `INT64_MIN` or implausible
magnitude, nonzero count with zero key and check, and a header whose element
count disagrees with the cell arithmetic.

### Collision boundary (what SHA-256 and checksums do and do not give you)

This is a **probabilistic** check, not a mathematical zero-collision
guarantee. In practical terms:

* A 32-byte SHA-256 fingerprint identifies content: the chance two different
  contents share one is on the order of 2⁻¹²⁸ for random inputs. The protocol
  treats the fingerprint as the definition of “same content”.
* The 48-byte key stored in a cell is the full element (UUID + fingerprint),
  so the recovered identifier/fingerprint is not truncated.
* The 8-byte cell checksum guards the peeling decision. A false “pure” cell
  matching an unrelated element's checksum has probability about 2⁻⁶⁴ per
  candidate, and the recovered element must also hash back into that exact
  cell. If it does not, peeling stops as inconsistent instead of reporting a
  wrong difference.
* Diversity of `k` cells per element and a table noticeably larger than the
  expected difference keep the peelable threshold comfortable. A saturated
  table is reported as incomplete, never as an empty difference.

## Installation and commands

```bash
pip install -e .            # no runtime dependencies; tests use pytest
```

Build a digest from one checklist:

```bash
handover-iblt build sender.txt sender.digest --capacity 32 --k 4 --seed 20261113
handover-iblt build receiver.txt receiver.digest --capacity 32 --k 4 --seed 20261113
```

`--store identities.db` keeps the SQLite identity store (refused if it already
exists); without it uniqueness runs in a temporary in-memory database.
`--overwrite` replaces an existing digest atomically; without it an existing
file is refused. A failed build leaves no digest.

Inspect and compare digest files **without the checklists**:

```bash
handover-iblt info sender.digest
handover-iblt compare sender.digest receiver.digest \
    --left-name sender --right-name receiver
handover-iblt compare sender.digest receiver.digest --json
```

Exit codes: `0` complete, `1` invalid input, `2` refused overwrite, `3`
incomplete peel, `4` checklist conflict, `5` incomparable configurations.
`compare` only reads the two digest files and never rewrites them.

### Full demo

```bash
handover-iblt demo --workdir /tmp/handover-demo \
    --capacity 32 --k 4 --seed 20261113
```

The demo runs the real CLI in a subprocess and checks every expectation; see
[`docs/DEMO.md`](docs/DEMO.md) for the transcript and the story.

## Layout

| Module | Responsibility |
| --- | --- |
| `handover_iblt/config.py` | Protocol version, element/cell widths, `DigestConfig`, domain-separated hash, cell index generation |
| `handover_iblt/store.py` | Streaming checklist parse, SQLite uniqueness, duplicate/conflict adjudication, fixed-width hex validation |
| `handover_iblt/table.py` | Cell array, add/subtract arithmetic, wire body encoding |
| `handover_iblt/digest.py` | Header, SHA-256 trailer, save/load, strict format validation, fold store → IBLT |
| `handover_iblt/peel.py` | Pure-cell detection, peeling, residual reporting |
| `handover_iblt/reconcile.py` | Config check, difference table, direction pairing, text/JSON report |
| `handover_iblt/builder.py` | Checklist → store → digest orchestration, atomic write |
| `handover_iblt/cli.py` | `build` / `info` / `compare` / `demo` |
| `handover_iblt/materials.py` | Literal demo checklists (including the engineered residual cycle) |

See [`docs/DESIGN.md`](docs/DESIGN.md) for the design rationale and failure
handling, and [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the byte-level
protocol.

## Tests

```bash
python -I -m pytest tests/ -q
```

Expectations are independent of the production decoder: `tests/expected.py`
recomputes every cell of a small 8-cell table from the documented hash formula
and byte layout (count, 48-byte key XOR, 8-byte check XOR per cell);
`tests/reference.py` gives the brute-force two-sided difference over explicit
sets. The suite covers the A,B,C / B,C,D story, content change, direction
reversal, reordered and duplicated inputs, conflicts, malformed hex,
incomparable digests, tampered/truncated files, an engineered unpeelable
collision cycle plus a successful larger retry, differential random cases, and
fixed digest size / streaming memory over growing checklists.
