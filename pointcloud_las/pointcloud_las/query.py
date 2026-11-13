"""Spatial and provenance queries over the on-disk point index.

All box boundaries are closed intervals given as exact decimal strings.  The
physical coordinate of a stored point is::

    physical = scale * integer + offset

For every source we translate the requested physical interval into the
inclusive integer index range that can satisfy it.  SQLite performs that
integer coarse filter (with the R-tree when available), and every surviving
point is then re-checked with :mod:`decimal` so no boundary point is lost to
binary floating-point rounding.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterator, Optional, Sequence

from .decimal_utils import closed_int_bounds, parse_decimal, physical_value
from .errors import QueryError
from .models import PointRecord
from .schema import get_meta

_COLUMNS = (
    "s.source_sha256, p.point_index, p.x, p.y, p.z, p.intensity, "
    "p.classification, p.class_flags, p.return_number, p.number_of_returns, "
    "p.gps_time, p.red, p.green, p.blue, p.extra_bytes"
)


def _validate_box(bounds: dict) -> tuple:
    names = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
    for name in names:
        if name not in bounds:
            raise QueryError(f"missing required bound: {name}")
    vals = {name: parse_decimal(bounds[name], name) for name in names}
    for axis in ("x", "y", "z"):
        if vals[f"{axis}min"] > vals[f"{axis}max"]:
            raise QueryError(
                f"{axis}min must not be greater than {axis}max"
            )
    return (vals["xmin"], vals["xmax"], vals["ymin"], vals["ymax"],
            vals["zmin"], vals["zmax"])


def _validate_classes(classes) -> Optional[set]:
    if classes is None:
        return None
    if isinstance(classes, (str, int)):
        raise QueryError(
            "classifications must be a collection of integers, "
            "not a bare string or int"
        )
    out = set()
    for c in classes:
        if isinstance(c, bool) or not isinstance(c, int):
            raise QueryError(f"classification code must be an integer, got {c!r}")
        if not 0 <= c <= 31:
            raise QueryError(
                f"classification code {c} outside the 5-bit LAS range 0..31"
            )
        out.add(c)
    return out


def _validate_index_range(start, end) -> Optional[tuple]:
    if start is None and end is None:
        return None
    if start is None:
        start = 0
    if isinstance(start, bool) or isinstance(end, bool):
        raise QueryError("point index bounds must be integers")
    if not isinstance(start, int) or (end is not None and not isinstance(end, int)):
        raise QueryError("point index bounds must be integers")
    if start < 0:
        raise QueryError("point index start must be >= 0")
    if end is not None and end < start:
        raise QueryError("point index end must be >= start")
    return start, end


def _load_sources(conn, source_sha: Optional[str]):
    if source_sha is not None:
        if not isinstance(source_sha, str) or len(source_sha) != 64:
            raise QueryError("source_sha must be a 64-character hex SHA-256")
        rows = conn.execute(
            "SELECT src_id, source_sha256, scale_x, scale_y, scale_z, "
            "offset_x, offset_y, offset_z FROM sources WHERE source_sha256 = ?",
            (source_sha,),
        ).fetchall()
        if not rows:
            return []
        return rows
    return conn.execute(
        "SELECT src_id, source_sha256, scale_x, scale_y, scale_z, "
        "offset_x, offset_y, offset_z FROM sources ORDER BY source_sha256"
    ).fetchall()


def _source_scales_offsets(row):
    scales = tuple(Decimal(row[2 + i]) for i in range(3))
    offsets = tuple(Decimal(row[5 + i]) for i in range(3))
    return scales, offsets


def _build_execution(conn, *, bounds, classifications, source_sha,
                     index_start, index_end, box_given):
    class_set = _validate_classes(classifications)
    index_range = _validate_index_range(index_start, index_end)
    sources = _load_sources(conn, source_sha)
    use_rtree = get_meta(conn, "rtree_available") == "1"

    if box_given:
        box = _validate_box(bounds)
    else:
        box = None

    # An explicit empty classification set can match nothing, independent of
    # how a given SQLite version renders "IN ()".
    if class_set is not None and not class_set:
        return []

    # One statement per source because each source has its own scale/offset
    # and therefore its own integer translation of the physical box.
    plans = []
    for row in sources:
        src_id, sha = row[0], row[1]
        scales, offsets = _source_scales_offsets(row)
        params: list = []
        rtree_params: list = []
        int_ranges = []
        if box_given:
            for axis in range(3):
                lo, hi = box[axis * 2], box[axis * 2 + 1]
                translated = closed_int_bounds(lo, hi, scales[axis], offsets[axis])
                if translated is None:
                    int_ranges = None
                    break
                int_ranges.append(translated)
            if int_ranges is None:
                continue  # no integer coordinate of this source can match
            for k_min, k_max in int_ranges:
                if use_rtree:
                    rtree_params.extend([k_min, k_max])
                params.extend([k_min, k_max])

        where_extra = []
        if box_given:
            # Integer coarse filter per axis; the exact decimal re-check in
            # Python decides the final hits.
            where_extra.extend([
                "p.x BETWEEN ? AND ?",
                "p.y BETWEEN ? AND ?",
                "p.z BETWEEN ? AND ?",
            ])
        if class_set is not None:
            where_extra.append(
                "p.classification IN ("
                + ",".join("?" for _ in class_set) + ")"
            )
            params.extend(sorted(class_set))
        if index_range is not None:
            where_extra.append("p.point_index BETWEEN ? AND ?")
            params.extend(index_range)
        where_extra.append("p.src_id = ?")
        params.append(src_id)

        joins = ""
        if use_rtree and box_given:
            joins = (
                " JOIN points_rtree rt ON rt.id = p.rowid AND "
                "rt.max_x >= ? AND rt.min_x <= ? AND "
                "rt.max_y >= ? AND rt.min_y <= ? AND "
                "rt.max_z >= ? AND rt.min_z <= ?"
            )
        sql = (
            f"SELECT {_COLUMNS} FROM points p "
            "JOIN sources s ON s.src_id = p.src_id"
            f"{joins} WHERE " + " AND ".join(where_extra)
            + " ORDER BY s.source_sha256, p.point_index"
        )
        final_params = rtree_params + params
        plans.append((sql, final_params, scales, offsets, box, sha))
    return plans


def _row_matches_box(row, scales, offsets, box) -> bool:
    """Exact decimal containment test for one candidate row."""
    x, y, z = row[2], row[3], row[4]
    ints = (x, y, z)
    for axis in range(3):
        physical = physical_value(ints[axis], scales[axis], offsets[axis])
        if not (box[axis * 2] <= physical <= box[axis * 2 + 1]):
            return False
    return True


def _row_to_point(row, scales, offsets, box) -> Optional[PointRecord]:
    (sha, idx, x, y, z, intensity, classification, class_flags,
     return_number, n_returns, gps_time, red, green, blue, extra) = row
    if box is not None and not _row_matches_box(row, scales, offsets, box):
        return None
    return PointRecord(
        source_sha256=sha,
        point_index=idx,
        x_int=x,
        y_int=y,
        z_int=z,
        intensity=intensity,
        classification=classification,
        class_flags=class_flags,
        return_number=return_number,
        number_of_returns=n_returns,
        gps_time=gps_time,
        red=red,
        green=green,
        blue=blue,
        extra_bytes=bytes(extra),
    )


def iter_points(
    conn,
    bounds: Optional[dict] = None,
    *,
    classifications: Optional[Sequence[int]] = None,
    source_sha: Optional[str] = None,
    index_start: Optional[int] = None,
    index_end: Optional[int] = None,
    fetch_size: int = 1000,
) -> Iterator[PointRecord]:
    """Stream matching points without materialising the whole result set.

    ``bounds`` holds decimal-string keys xmin/xmax/ymin/ymax/zmin/zmax and may
    be omitted when filtering only by source/classification/index.  Every
    interval, including the box edges, is closed.  Results are ordered by
    source SHA-256 then original point index.
    """
    if fetch_size < 1:
        raise QueryError("fetch_size must be >= 1")
    box_given = bounds is not None
    plans = _build_execution(
        conn,
        bounds=bounds or {},
        classifications=classifications,
        source_sha=source_sha,
        index_start=index_start,
        index_end=index_end,
        box_given=box_given,
    )
    last_sha = None
    for sql, params, scales, offsets, box, sha in plans:
        # Source plans are already ordered by SHA; enforce the global order
        # defensively in case callers pass a single source_sha.
        if last_sha is not None and sha < last_sha:
            raise RuntimeError("internal error: source plans out of order")
        last_sha = sha
        cursor = conn.execute(sql, params)
        cursor.arraysize = fetch_size
        while True:
            rows = cursor.fetchmany(fetch_size)
            if not rows:
                break
            for row in rows:
                point = _row_to_point(row, scales, offsets, box)
                if point is not None:
                    yield point
        cursor.close()


def count_points(
    conn,
    bounds: Optional[dict] = None,
    *,
    classifications: Optional[Sequence[int]] = None,
    source_sha: Optional[str] = None,
    index_start: Optional[int] = None,
    index_end: Optional[int] = None,
) -> int:
    """Return the exact number of matching points (empty results return 0).

    The same integer coarse filter plus exact decimal re-check as
    :func:`iter_points` is applied; counting never loads the points.
    """
    box_given = bounds is not None
    plans = _build_execution(
        conn,
        bounds=bounds or {},
        classifications=classifications,
        source_sha=source_sha,
        index_start=index_start,
        index_end=index_end,
        box_given=box_given,
    )
    total = 0
    for sql, params, scales, offsets, box, _sha in plans:
        if box is None:
            # No decimal re-check needed without a box; the integer/filter
            # predicates are themselves exact.
            count_sql = sql.replace(
                f"SELECT {_COLUMNS} FROM", "SELECT COUNT(*) FROM", 1
            )
            count_sql = count_sql.split(" ORDER BY ")[0]
            total += conn.execute(count_sql, params).fetchone()[0]
            continue
        # With a box the integer SQL filter is coarse: stream the candidate
        # rows and apply the exact decimal re-check without materialising
        # PointRecord objects.
        cursor = conn.execute(sql, params)
        cursor.arraysize = 2000
        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                if _row_matches_box(row, scales, offsets, box):
                    total += 1
        cursor.close()
    return total


def query_points(
    conn,
    bounds: dict,
    *,
    classifications: Optional[Sequence[int]] = None,
    source_sha: Optional[str] = None,
    index_start: Optional[int] = None,
    index_end: Optional[int] = None,
) -> list:
    """Convenience wrapper returning the full match list (use for small sets).

    For large result sets prefer :func:`iter_points`, which streams.
    """
    return list(iter_points(
        conn,
        bounds,
        classifications=classifications,
        source_sha=source_sha,
        index_start=index_start,
        index_end=index_end,
    ))


def export_points(
    conn,
    out_path: str,
    bounds: Optional[dict] = None,
    *,
    classifications: Optional[Sequence[int]] = None,
    source_sha: Optional[str] = None,
    index_start: Optional[int] = None,
    index_end: Optional[int] = None,
) -> int:
    """Stream matches to a UTF-8 CSV file with full provenance; return count.

    The export is written incrementally so memory use stays bounded by
    ``fetch_size`` regardless of the result size.
    """
    import csv

    written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow((
            "source_sha256", "point_index", "x_int", "y_int", "z_int",
            "intensity", "classification", "class_flags", "return_number",
            "number_of_returns", "gps_time", "red", "green", "blue",
            "extra_bytes_hex",
        ))
        for point in iter_points(
            conn,
            bounds,
            classifications=classifications,
            source_sha=source_sha,
            index_start=index_start,
            index_end=index_end,
        ):
            writer.writerow((
                point.source_sha256, point.point_index,
                point.x_int, point.y_int, point.z_int,
                point.intensity, point.classification, point.class_flags,
                point.return_number, point.number_of_returns,
                "" if point.gps_time is None else repr(point.gps_time),
                "" if point.red is None else point.red,
                "" if point.green is None else point.green,
                "" if point.blue is None else point.blue,
                point.extra_bytes.hex(),
            ))
            written += 1
    return written
