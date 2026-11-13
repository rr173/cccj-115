"""Streaming ingestion of LAS 1.2 files into the SQLite library."""

from __future__ import annotations

import hashlib
import math
import os
from decimal import Decimal
from typing import Optional, Tuple

import laspy

from .decimal_utils import float_to_decimal, physical_value
from .errors import IngestionError, LibraryError
from .las_header import describe_reference, parse_raw_header
from .models import IngestionResult, SourceInfo
from .schema import META_REF_DESC, META_REF_POLICY, get_meta, set_meta

_STANDARD_LENGTH = {0: 20, 1: 28, 2: 26, 3: 34}


class AbortForTest(IngestionError):
    """Raised by the graceful test hook; the transaction is rolled back."""


class HardCrashSimulated(IngestionError):
    """Raised after the connection was closed without rollback/commit.

    SQLite WAL recovery on the next open takes care of the half-written
    transaction, mirroring a real process kill mid-import.
    """


def hash_file(path: str, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of the raw file bytes, computed without loading the file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _check_admission(conn, reference_desc: Optional[str]) -> None:
    policy = get_meta(conn, META_REF_POLICY)
    stored = get_meta(conn, META_REF_DESC)
    if policy == "none":
        if reference_desc is not None:
            raise IngestionError(
                "this library was explicitly created as unreferenced, but the "
                "file carries a coordinate reference description"
            )
        return
    if policy != "strict":
        raise LibraryError(f"unknown reference policy stored: {policy!r}")
    if reference_desc is None:
        raise IngestionError(
            "file has no coordinate reference description; it can only enter a "
            "library explicitly created as unreferenced"
        )
    if stored is not None and stored != reference_desc:
        raise IngestionError(
            "coordinate reference description conflict: the file's reference "
            "does not match the description already stored in this library"
        )


def _close_ulp(value: float, expected: Decimal) -> bool:
    """Whether double ``value`` equals decimal ``expected`` within one ULP.

    Header extents are stored as doubles and routinely rounded by the
    producing software, so exact equality would reject valid files.  A one
    ULP tolerance still catches a genuinely lying header.
    """
    if not math.isfinite(value):
        return False
    got = float_to_decimal(value)
    if got == expected:
        return True
    low = float_to_decimal(math.nextafter(value, -math.inf))
    high = float_to_decimal(math.nextafter(value, math.inf))
    return low <= expected <= high


def ingest(
    conn,
    path: str,
    *,
    read_batch: int = 10_000,
    _fail_after_batches: Optional[int] = None,
    _hard_crash_after_batches: Optional[int] = None,
) -> IngestionResult:
    """Ingest one LAS file as a single all-or-nothing transaction.

    Either every point of the file becomes visible together (and the source
    appears in the manifest) or nothing is left behind.  Re-ingesting bytes
    already stored is a no-op regardless of file name or path.

    ``read_batch`` bounds how many points are pulled from laspy (and buffered
    for one INSERT) at a time -- memory use is O(read_batch), never O(file).
    The two underscore-prefixed hooks exist for failure/recovery tests.
    """
    if read_batch < 1:
        raise ValueError("read_batch must be >= 1")
    if not os.path.isfile(path):
        raise IngestionError(f"source file not found: {path}")

    # Byte-level validation first: compressed, unsupported or truncated files
    # never reach laspy and never open a write transaction.
    raw = parse_raw_header(path)
    sha = hash_file(path)

    existing = conn.execute(
        "SELECT n_points FROM sources WHERE source_sha256 = ?", (sha,)
    ).fetchone()
    if existing is not None:
        # Identity is content-based: renaming a file never makes a new source.
        return IngestionResult(
            source_sha256=sha,
            ingested=False,
            n_points=existing[0],
            original_path=path,
            reference_desc=get_meta(conn, META_REF_DESC),
        )

    reference_desc = describe_reference(raw)
    _check_admission(conn, reference_desc)

    scale_dec = tuple(float_to_decimal(v) for v in raw.scales)
    offset_dec = tuple(float_to_decimal(v) for v in raw.offsets)
    extra_len = raw.record_length - _STANDARD_LENGTH[raw.point_format_id]

    # One immediate transaction covers the whole source.  WAL readers that
    # started before this point keep their snapshot until commit, so
    # half-written points are never observable from another connection.
    conn.execute("BEGIN IMMEDIATE")
    crashed = False
    batches_seen = 0
    total = 0
    int_min: list = [None, None, None]
    int_max: list = [None, None, None]
    use_rtree = get_meta(conn, "rtree_available") == "1"
    try:
        try:
            reader = laspy.open(path)
        except Exception as exc:
            raise IngestionError(
                f"{path}: laspy could not open the file: {exc}"
            ) from exc

        with reader, open(path, "rb") as raw_fh:
            _cross_check_header(path, raw, reader.header)

            # The source row is created up-front inside the same transaction;
            # nothing is visible externally until the final commit.
            cur = conn.execute(
                "INSERT INTO sources (source_sha256, original_path, point_format,"
                " record_length, extra_bytes_len, n_points, scale_x, scale_y,"
                " scale_z, offset_x, offset_y, offset_z, has_gps, has_rgb,"
                " reference_desc, x_int_min, x_int_max, y_int_min, y_int_max,"
                " z_int_min, z_int_max, declared_x_min, declared_x_max,"
                " declared_y_min, declared_y_max, declared_z_min, declared_z_max,"
                " actual_x_min, actual_x_max, actual_y_min, actual_y_max,"
                " actual_z_min, actual_z_max, file_size_bytes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?)",
                (
                    sha, path, raw.point_format_id, raw.record_length, extra_len,
                    raw.n_points,
                    str(scale_dec[0]), str(scale_dec[1]), str(scale_dec[2]),
                    str(offset_dec[0]), str(offset_dec[1]), str(offset_dec[2]),
                    int(raw.has_gps), int(raw.has_rgb), reference_desc,
                    None, None, None, None, None, None,
                    raw.declared_min[0], raw.declared_max[0],
                    raw.declared_min[1], raw.declared_max[1],
                    raw.declared_min[2], raw.declared_max[2],
                    None, None, None, None, None, None,
                    raw.file_size,
                ),
            )
            src_id = cur.lastrowid

            for chunk in reader.chunk_iterator(read_batch):
                batches_seen += 1
                n = len(chunk.X)
                xs = [int(v) for v in chunk.X]
                ys = [int(v) for v in chunk.Y]
                zs = [int(v) for v in chunk.Z]
                classifications = [int(v) for v in chunk.classification]
                # The classification byte packs class (low 5 bits) and the
                # synthetic / key_point / withheld flags (bits 5..7).  laspy
                # already separates them; persist both, never shifted together.
                flags = [
                    (int(s) << 5) | (int(k) << 6) | (int(w) << 7)
                    for s, k, w in zip(
                        chunk.synthetic, chunk.key_point, chunk.withheld
                    )
                ]
                return_numbers = [int(v) for v in chunk.return_number]
                n_returns = [int(v) for v in chunk.number_of_returns]
                intensities = [int(v) for v in chunk.intensity]
                gps_values = (
                    [float(v) for v in chunk.gps_time] if raw.has_gps else None
                )
                rgb_values = (
                    [(int(r), int(g), int(b))
                     for r, g, b in zip(chunk.red, chunk.green, chunk.blue)]
                    if raw.has_rgb else None
                )

                # Slice raw record bytes directly so extra bytes survive
                # byte-for-byte, including dimensions laspy does not model.
                base = raw.offset_to_point_data + total * raw.record_length
                raw_fh.seek(base)
                payload = raw_fh.read(n * raw.record_length)
                if len(payload) != n * raw.record_length:
                    raise IngestionError(
                        f"{path}: truncated payload while reading point {total}"
                    )

                rows = []
                std_len = _STANDARD_LENGTH[raw.point_format_id]
                for j in range(n):
                    idx = total + j
                    rec = payload[j * raw.record_length:(j + 1) * raw.record_length]
                    extra = rec[std_len:]
                    if len(extra) != extra_len:
                        raise IngestionError(
                            f"{path}: record length mismatch at point {idx}"
                        )
                    gps_val = gps_values[j] if gps_values is not None else None
                    if rgb_values is not None:
                        red_v, green_v, blue_v = rgb_values[j]
                    else:
                        red_v = green_v = blue_v = None
                    rows.append((
                        src_id, idx, xs[j], ys[j], zs[j], intensities[j],
                        classifications[j], flags[j], return_numbers[j],
                        n_returns[j], gps_val, red_v, green_v, blue_v, extra,
                    ))
                    for axis, val in enumerate((xs[j], ys[j], zs[j])):
                        if int_min[axis] is None or val < int_min[axis]:
                            int_min[axis] = val
                        if int_max[axis] is None or val > int_max[axis]:
                            int_max[axis] = val

                conn.executemany(
                    "INSERT INTO points (src_id, point_index, x, y, z, intensity,"
                    " classification, class_flags, return_number,"
                    " number_of_returns, gps_time, red, green, blue, extra_bytes)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                if use_rtree:
                    # Populate the spatial index per batch so no segment list
                    # accumulates for the whole file.
                    conn.execute(
                        "INSERT INTO points_rtree(id, min_x, max_x, min_y, max_y,"
                        " min_z, max_z) "
                        "SELECT rowid, x, x, y, y, z, z FROM points "
                        "WHERE src_id = ? AND point_index >= ? AND point_index < ?",
                        (src_id, total, total + n),
                    )
                total += n

                if _fail_after_batches is not None and batches_seen >= _fail_after_batches:
                    raise AbortForTest(
                        f"scheduled failure after {batches_seen} batch(es)"
                    )
                if (_hard_crash_after_batches is not None
                        and batches_seen >= _hard_crash_after_batches):
                    crashed = True
                    # Kill the connection without commit or rollback; the next
                    # connection performs WAL recovery, like a process crash.
                    conn.close()
                    raise HardCrashSimulated(
                        f"simulated hard crash after {batches_seen} batch(es)"
                    )

            if total != raw.n_points:
                raise IngestionError(
                    f"{path}: header declared {raw.n_points} points but only "
                    f"{total} were readable"
                )
            _verify_declared_extents(
                path, int_min, int_max, scale_dec, offset_dec, raw
            )

            actual = _actual_bounds_text(int_min, int_max, scale_dec, offset_dec)
            conn.execute(
                "UPDATE sources SET n_points = ?, x_int_min = ?, x_int_max = ?,"
                " y_int_min = ?, y_int_max = ?, z_int_min = ?, z_int_max = ?,"
                " actual_x_min = ?, actual_x_max = ?, actual_y_min = ?,"
                " actual_y_max = ?, actual_z_min = ?, actual_z_max = ? "
                "WHERE src_id = ?",
                (total, int_min[0], int_max[0], int_min[1], int_max[1],
                 int_min[2], int_max[2], *actual, src_id),
            )
            if reference_desc is not None:
                set_meta(conn, META_REF_DESC, reference_desc)
        conn.commit()
    except HardCrashSimulated:
        # Connection already closed; do not touch it.  Re-raise for the test.
        raise
    except IngestionError:
        if not crashed:
            conn.rollback()
        raise
    except Exception as exc:
        if not crashed:
            conn.rollback()
        raise IngestionError(f"{path}: ingestion failed: {exc}") from exc

    return IngestionResult(
        source_sha256=sha,
        ingested=True,
        n_points=total,
        original_path=path,
        reference_desc=reference_desc,
    )


def _cross_check_header(path, raw, header) -> None:
    if header.point_format.id != raw.point_format_id:
        raise IngestionError(
            f"{path}: point format disagreement between raw header "
            f"({raw.point_format_id}) and laspy ({header.point_format.id})"
        )
    if header.point_format.size != raw.record_length:
        raise IngestionError(
            f"{path}: record length disagreement between raw header "
            f"({raw.record_length}) and laspy ({header.point_format.size})"
        )
    if header.point_count != raw.n_points:
        raise IngestionError(
            f"{path}: point count disagreement between raw header "
            f"({raw.n_points}) and laspy ({header.point_count})"
        )
    for axis, (raw_v, laspy_v) in enumerate(
        zip(raw.scales, (header.x_scale, header.y_scale, header.z_scale))
    ):
        if raw_v != laspy_v:
            raise IngestionError(f"{path}: scale disagreement on axis {axis}")
    for axis, (raw_v, laspy_v) in enumerate(
        zip(raw.offsets, (header.x_offset, header.y_offset, header.z_offset))
    ):
        if raw_v != laspy_v:
            raise IngestionError(f"{path}: offset disagreement on axis {axis}")


def _verify_declared_extents(path, int_min, int_max, scale_dec, offset_dec, raw
                             ) -> None:
    if raw.n_points == 0:
        return
    actual = []
    for axis in range(3):
        actual.append(
            physical_value(int_min[axis], scale_dec[axis], offset_dec[axis])
        )
        actual.append(
            physical_value(int_max[axis], scale_dec[axis], offset_dec[axis])
        )
    declared = [
        raw.declared_min[0], raw.declared_max[0],
        raw.declared_min[1], raw.declared_max[1],
        raw.declared_min[2], raw.declared_max[2],
    ]
    for name, declared_v, actual_v in zip(
        ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"),
        declared, actual,
    ):
        if not _close_ulp(declared_v, actual_v):
            raise IngestionError(
                f"{path}: header lies about {name}: declared {declared_v!r} "
                f"but the points span {actual_v}"
            )


def _actual_bounds_text(int_min, int_max, scale_dec, offset_dec
                        ) -> Tuple[Optional[str], ...]:
    if int_min[0] is None:
        return (None,) * 6
    vals = []
    for axis in range(3):
        vals.append(
            str(physical_value(int_min[axis], scale_dec[axis], offset_dec[axis]))
        )
        vals.append(
            str(physical_value(int_max[axis], scale_dec[axis], offset_dec[axis]))
        )
    return tuple(vals)


def list_sources(conn) -> list:
    """Return the manifest of completely ingested sources, sorted by id."""
    out = []
    for row in conn.execute(
        "SELECT src_id, source_sha256, original_path, point_format, record_length,"
        " extra_bytes_len, n_points, scale_x, scale_y, scale_z, offset_x,"
        " offset_y, offset_z, has_gps, has_rgb, reference_desc, x_int_min,"
        " x_int_max, y_int_min, y_int_max, z_int_min, z_int_max, declared_x_min,"
        " declared_x_max, declared_y_min, declared_y_max, declared_z_min,"
        " declared_z_max, actual_x_min, actual_x_max, actual_y_min, actual_y_max,"
        " actual_z_min, actual_z_max, file_size_bytes, ingested_at "
        "FROM sources ORDER BY src_id"
    ):
        out.append(SourceInfo(
            src_id=row[0],
            source_sha256=row[1],
            original_path=row[2],
            point_format=row[3],
            record_length=row[4],
            extra_bytes_len=row[5],
            n_points=row[6],
            scales=(float(row[7]), float(row[8]), float(row[9])),
            offsets=(float(row[10]), float(row[11]), float(row[12])),
            has_gps=bool(row[13]),
            has_rgb=bool(row[14]),
            reference_desc=row[15],
            int_bounds=(row[16], row[17], row[18], row[19], row[20], row[21]),
            declared_bounds=(
                row[22], row[23], row[24], row[25], row[26], row[27]),
            actual_bounds=(
                row[28], row[29], row[30], row[31], row[32], row[33]),
            file_size_bytes=row[34],
            ingested_at=row[35],
        ))
    return out
