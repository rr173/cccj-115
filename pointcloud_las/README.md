# pointcloud_las

Disk-backed ingestion and exact spatial retrieval of uncompressed **LAS 1.2**
point-cloud tiles (point formats 0–3).  The package stores original points and
their provenance in a single SQLite database and answers 3-D box /
classification / provenance queries without keeping whole tiles in memory.
It deliberately performs **no** scientific computation (no tree heights or
other derived metrics): only storage, indexing and retrieval.

## What it guarantees

- **Strict input acceptance.** Before any parsing library touches a file, the
  raw header is validated byte by byte: LASF signature, exactly version 1.2,
  point formats 0–3 only, no compression bit and no LASzip VLR, declared point
  count / record length consistent with the actual payload size, and no
  truncated or trailing bytes. Bad files are rejected and leave no queryable
  point behind.
- **Content identity.** A point's identity is `(sha256(file bytes), original
  point index)`. Re-ingesting identical bytes under a new name is a no-op;
  changing one byte makes a new source. Points sharing identical coordinates
  remain distinct.
- **Reference admission.** A library is created either *unreferenced*
  (accepts only files with no coordinate reference description) or *strict*
  (every file must carry the same reference VLR description). Referenced and
  unreferenced data can never be mixed.
- **Exact boundary arithmetic.** Query bounds are decimal strings. The box
  is translated per source using the source's own scale/offset with
  `Decimal`, integer indices do the coarse filter (R-tree when available),
  and every candidate is re-checked exactly. Closed intervals never lose
  edge points to binary floating-point rounding.
- **Optional attributes stay optional.** GPS time / RGB are stored only for
  formats that have them; genuine `0.0` / `0` values are stored, never
  confused with "field absent" (SQL `NULL`). Extra bytes are preserved
  byte-for-byte.
- **Atomic, streaming imports.** One source = one transaction. A failure
  after any number of batches (including a simulated hard crash) rolls back
  completely; the same file retries cleanly after restart. Concurrent readers
  only ever see fully committed sources.
- **Bounded memory.** Both ingestion and queries stream in configurable
  batches; nothing accumulates whole files or whole result sets.

## Installation

```bash
pip install -e .        # dependencies: laspy, numpy; tests use pytest
```

## Quick start

```python
from pointcloud_las import PointCloudLibrary

# A strictly referenced library; pass unreferenced=True for the other mode.
with PointCloudLibrary.create("tiles.db") as lib:
    lib.ingest_file("tile_a.las", read_batch=10_000)
    lib.ingest_file("tile_b.las", read_batch=10_000)

    box = {
        "xmin": "123.450", "xmax": "123.460",
        "ymin": "-10.000", "ymax":  "-9.000",
        "zmin": "300.0",   "zmax": "310.0",
    }
    n = lib.count(box, classifications=[2, 3, 4, 5])

    # Streaming, memory-bounded retrieval with full provenance:
    for point in lib.iter_query(box, classifications=[2, 3, 4, 5]):
        point.source_sha256      # content identity of the source file
        point.point_index        # original index inside that file
        point.x_int, point.y_int, point.z_int   # raw integer coordinates
        point.extra_bytes        # opaque extra bytes, verbatim

    # Provenance lookup without any spatial bound:
    list(lib.iter_query(None, source_sha=sha, index_start=10, index_end=19))

    # Stream matches to a CSV instead of materialising them:
    lib.export_csv("hits.csv", box, classifications=[2])
```

## Query semantics

- Bounds are required for spatial queries; all six keys use **decimal
  strings** (`"123.456"`, never a Python float). Every interval is closed.
- `classifications` is an optional collection of 5-bit LAS codes (0–31).
- `source_sha` plus `index_start` / `index_end` optionally locate points by
  provenance; with `bounds=None` the query is purely provenance-based.
- Results are always ordered by `source_sha256` then original point index,
  independent of ingestion order.
- Empty results are normal and return `0` / an empty iterator.

## Layout

| Module | Responsibility |
| --- | --- |
| `pointcloud_las/las_header.py` | Byte-level LAS 1.2 header/VLR validation and reference description extraction |
| `pointcloud_las/ingestion.py` | Streaming hashing, batched laspy reading, atomic transaction, manifest |
| `pointcloud_las/query.py` | Decimal box translation, integer/R-tree coarse filter, exact re-check, streaming export |
| `pointcloud_las/schema.py` | SQLite schema, R-tree, connection/WAL management, stats |
| `pointcloud_las/decimal_utils.py` | Exact decimal parsing and physical↔integer interval math |
| `pointcloud_las/library.py` | High-level `PointCloudLibrary` facade |
| `pointcloud_las/models.py` | `PointRecord`, `SourceInfo`, `IngestionResult` data classes |

See `docs/design.md` for the design rationale and `docs/space_overhead.md`
for memory/disk accounting and how to size `read_batch`.

## Tests

```bash
python -I -m pytest tests/ -q
```

The tests generate their own LAS material (`tests/materials.py`, including
deliberately broken files) and derive point-wise expectations independently
of the production query code.
