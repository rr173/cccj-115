# Design notes

## 1. Validation pipeline, byte-level first

`las_header.parse_raw_header` reads the whole header region and enforces the
LAS 1.2 fixed layout before laspy opens the file:

1. `LASF` signature and at least 227 header bytes.
2. Version exactly `1.2`; point format byte with the compression bit clear and
   format id in `0..3`. A LASzip VLR (`laszip encoded`, record id 2220) is
   also rejected even if the bit were clear.
3. Point data record length at least the standard size of the format
   (20/28/26/34 bytes); anything longer is treated as opaque extra bytes.
4. `offset_to_point_data + n_points * record_length == file_size`. A smaller
   file is truncated; a larger file has ambiguous trailing bytes (LAS 1.2 has
   no EVLRs) and is also rejected.
5. Scale values must be finite and strictly positive; offsets finite.
6. VLRs are parsed with their real layout (reserved u16, 16-byte user id,
   record id u16, record length u16, 32-byte description) and must fit before
   the point payload.

laspy is then opened as a second opinion: point format, record length, point
count, scales and offsets must agree with the raw header, otherwise the
import fails. If the number of points actually streamed differs from the
header count, the import fails too.

Header extents are checked against the points actually read. Header doubles
are commonly rounded by producers, so the check allows a one-ULP difference;
a genuinely lying header (e.g. `x_min = 999` for points near `0`) fails.

## 2. Identity and deduplication

The SHA-256 is computed over the file bytes in 1 MiB streaming blocks.
`sources.source_sha256` is unique, so:

- identical bytes ingested twice (even under a new path/name) return
  `ingested=False` and add no rows;
- changed content at the same path gets a new SHA and a new source row;
- per-point identity is `(source_sha256, point_index)`, which is the primary
  key of `points`. Duplicate coordinates are therefore different points.

## 3. Coordinate reference admission

Reference descriptions are derived deterministically from raw VLR bytes of
the `LASF_Projection` user id (WKT record 2112, GeoTIFF records 34735/34736/
34737). The description is `REF:<user|id|hexpayload>;...`; two files are
admitted together exactly when their reference VLR bytes are identical.
No projection library is required.

- `unreferenced` libraries reject any file that carries reference VLRs.
- `strict` libraries reject files without reference VLRs and any file whose
  description differs from the one already stored (the first referenced file
  pins the library's description).

Different files may still use different scale/offset values; the query layer
translates boxes per source.

## 4. Exact spatial arithmetic

A stored point has integer coordinates `k` and per-source decimal
scale/offset; its physical coordinate is `s*k + o`. Header doubles are
converted with `Decimal(repr(x))`, i.e. their shortest round-tripping
decimal, rather than expanding full binary error.

For a requested closed physical interval `[lo, hi]`,
`closed_int_bounds` computes the smallest integer index range that can
contain matches (`ceil((lo-o)/s)` … `floor((hi-o)/s)`), clamped to int32.
The SQL predicate (`BETWEEN` plus the R-tree when present) is therefore a
**coarse** integer filter. Every surviving candidate is re-evaluated with
`Decimal` in Python; only points whose three physical values lie inside the
closed box are returned. The final hit set is exact even when float
arithmetic would move an edge point across the boundary.

The R-tree stores zero-volume boxes keyed by the `points` rowid. If the
SQLite build lacks R-tree, the library records that in `meta` and falls back
to the integer `BETWEEN` predicates — the decimal re-check is identical in
both paths.

## 5. Stored attributes and the optional-attribute rule

`points` keeps raw integer X/Y/Z, intensity, the 5-bit classification code,
the three classification flag bits in their original positions (bits 5–7),
return number, number of returns, optional GPS time, optional RGB and a BLOB
of extra bytes. GPS/RGB columns are nullable: a format without those fields
stores `NULL`, while a present value of `0.0`/`0` is stored as such. Extra
bytes are sliced directly from the raw record payload at the standard
format length, so unknown extra dimensions survive byte-for-byte.

## 6. Atomicity and concurrency

The database runs in WAL mode with `synchronous=FULL`. Each ingestion opens
one `BEGIN IMMEDIATE` transaction: the source row is inserted first (with
final bounds filled in at the end), then point batches and the R-tree are
written, and only a successful commit makes the source visible.

- A normal failure rolls the transaction back; no source row, point or
  R-tree entry survives.
- The hard-crash test hook closes the connection without commit/rollback;
  the next opener performs WAL recovery and sees a clean library, after
  which the unchanged source file imports normally.
- A reader that started a read transaction keeps its snapshot; concurrent
  queries on fresh statements observe either the pre-import or the
  post-import count, never an intermediate one.

Queries only touch committed database rows, so source LAS files do not need
to remain available after ingestion.

## 7. Streaming

Ingestion pulls laspy chunks of `read_batch` points, slices the matching raw
record bytes, and inserts one batch per transaction (the single import
transaction). Peak Python memory is proportional to one batch, not the file.

`iter_points` uses server-side cursors with `fetchmany(fetch_size)` and
`count_points` streams candidates through the same decimal re-check, so
neither path materialises a full result set. `export_csv` writes rows as
they arrive.
