# Memory and space overhead

All numbers are planning estimates; use `PointCloudLibrary.stats()` for the
actual page-based footprint of a concrete library.

## Per-point disk cost

The `points` table stores, per row:

| Column group | Bytes |
| --- | --- |
| rowid (implicit integer primary key) | 8 |
| src_id, point_index | 8 |
| x, y, z (int32 stored as SQLite integers) | up to 8 each |
| intensity, classification, class_flags, return_number, number_of_returns | up to 8 each |
| gps_time | 8 when present, NULL otherwise |
| red, green, blue | up to 8 each when present, NULL otherwise |
| extra_bytes BLOB | exactly the source extra-byte length |
| SQLite record header | ~6–10 |

Practical budget (including page fill and the `(src_id, classification)`
index):

- **Point formats 0/1/2 without extra bytes:** roughly **70–95 bytes per
  point**.
- **Point format 3 (GPS + RGB) without extra bytes:** roughly
  **100–130 bytes per point**.
- Add the per-point extra-byte length on top, plus BLOB overhead (~4–8
  bytes).
- **R-tree:** each point is a zero-volume 3-D box with an integer id; budget
  about **40–55 bytes per point** of additional index space.

So a 10-million-point format-3 library typically occupies on the order of
1.4–1.9 GB including the R-tree. The original LAS records are 34 bytes each
for format 3; the roughly 4–5x factor is the cost of fully typed, queryable
SQLite storage plus provenance and spatial indexing.

`stats()` returns `page_size_bytes * page_count` (the main database file;
add the `-wal`/`-shm` sidecars while connections are open).

## WAL sidecar

WAL mode keeps a write-ahead log next to the database. It grows during a
large transaction (an import) up to roughly the size of that transaction's
new pages and is checkpointed back automatically (default threshold 1000
pages) or on clean connection close. During a very large single-file import
expect the WAL to reach the same order of magnitude as the pages that import
adds; disk headroom of at least the expected library size is advisable.

## Memory

### Ingestion

- Streaming SHA-256: 1 MiB read buffer, constant memory.
- laspy chunk plus the Python row list and raw record slice:
  **O(read_batch)** rows. Each Python row is dominated by the integer/
  float objects (~15 objects per point, roughly 0.4–0.7 KB per point once
  lists, tuples and the extra BLOB are counted).
- Rule of thumb with default `read_batch=10_000`: a few to ~10 MB of
  working set regardless of file size. Lower it (e.g. 500–2000) on
  constrained machines; throughput drops slightly due to more frequent
  `executemany` calls, memory drops roughly linearly.
- The full point cloud is never held in memory.

### Queries

- `iter_points` / `export_csv`: one source plan at a time,
  `fetchmany(fetch_size)` rows per round trip. Memory is **O(fetch_size)**
  plus one cursor per source plan.
- `count_points`: streams candidate rowids/rows through the exact decimal
  re-check; it does not build the match list.
- `query_points` materialises the list by design and should only be used for
  bounded result sets; prefer the streaming API for large hits.
- The R-tree and B-tree page caches are SQLite's page cache (default
  ~2 MB per connection); they are independent of result size.

## Sizing checklist

1. Estimate rows: sum of true point counts from the manifest.
2. Apply the per-point budget above for the highest point format used.
3. Add the R-tree budget, then ~10% for page slack and indexes.
4. Reserve WAL headroom equal to the largest single import.
5. Choose `read_batch` so `read_batch * ~0.7 KB` fits comfortably in RAM.
