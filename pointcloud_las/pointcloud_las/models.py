"""Public data classes returned by ingestion and queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class PointRecord:
    """One stored point with full provenance.

    ``x_int/y_int/z_int`` are the raw signed integer coordinates; the source
    scale/offset (available from :class:`SourceInfo`) reconstruct the physical
    position.  ``gps_time`` is ``None`` when the point format has no GPS field
    (formats 0/2) -- a genuine value of ``0.0`` is distinguishable.  The RGB
    triple is likewise ``None`` for formats 0/1, never silently zero-filled.
    """

    source_sha256: str
    point_index: int
    x_int: int
    y_int: int
    z_int: int
    intensity: int
    classification: int
    class_flags: int
    return_number: int
    number_of_returns: int
    gps_time: Optional[float]
    red: Optional[int]
    green: Optional[int]
    blue: Optional[int]
    extra_bytes: bytes

    def as_tuple(self) -> Tuple:
        return (
            self.source_sha256,
            self.point_index,
            self.x_int,
            self.y_int,
            self.z_int,
            self.intensity,
            self.classification,
            self.class_flags,
            self.return_number,
            self.number_of_returns,
            self.gps_time,
            self.red,
            self.green,
            self.blue,
            self.extra_bytes,
        )


@dataclass(frozen=True)
class SourceInfo:
    """Manifest row for one completely ingested source file."""

    src_id: int
    source_sha256: str
    original_path: str
    point_format: int
    record_length: int
    extra_bytes_len: int
    n_points: int
    scales: Tuple[float, float, float]
    offsets: Tuple[float, float, float]
    has_gps: bool
    has_rgb: bool
    reference_desc: Optional[str]
    # Raw integer extents of the points actually read (None for empty files):
    int_bounds: Tuple[Optional[int], Optional[int], Optional[int],
                      Optional[int], Optional[int], Optional[int]]
    # Header-declared physical extents:
    declared_bounds: Tuple[float, float, float, float, float, float]
    # Physical extents recomputed from the read points, as exact decimal text:
    actual_bounds: Tuple[Optional[str], Optional[str], Optional[str],
                         Optional[str], Optional[str], Optional[str]]
    file_size_bytes: int
    ingested_at: str


@dataclass(frozen=True)
class IngestionResult:
    """Outcome of an ingestion call.

    ``ingested`` is False when the same content was already present (the
    identity is the SHA-256 of the file bytes, so renaming a file never
    creates a new source).
    """

    source_sha256: str
    ingested: bool
    n_points: int
    original_path: str
    reference_desc: Optional[str] = None
