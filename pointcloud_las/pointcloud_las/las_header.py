"""Byte-level validation of LAS 1.2 headers before laspy touches the file.

Validation deliberately works on the raw bytes first so that compressed
files, unsupported point formats/versions and truncated payloads are rejected
with precise errors instead of being handed to a third-party parser.

Only uncompressed LAS 1.2 (version 1.2) point formats 0..3 are accepted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import IngestionError

LAS_SIGNATURE = b"LASF"
ACCEPTED_MAJOR = 1
ACCEPTED_MINOR = 2
ACCEPTED_POINT_FORMATS = frozenset({0, 1, 2, 3})
EXPECTED_RECORD_LENGTH = {0: 20, 1: 28, 2: 26, 3: 34}
FIXED_HEADER_SIZE = 227
VLR_HEADER_SIZE = 54
COMPRESSION_BIT = 0x80  # high bit of point data format id means LAZ
# User id / record id used by LASzip to mark a compressed file.
LASZIP_USER_IDS = frozenset({"laszip encoded", "lazperf", "LASzip encoded"})
LASZIP_RECORD_ID = 2220
# VLRs that carry a coordinate reference description in LAS 1.2.
WKT_RECORD_ID = 2112
GEOTIFF_RECORD_IDS = frozenset({34735, 34736, 34737})
PROJECTION_USER_ID = "LASF_Projection"


@dataclass(frozen=True)
class RawVLR:
    user_id: str
    record_id: int
    record_length: int
    description: str
    record_data: bytes


@dataclass(frozen=True)
class RawHeader:
    file_size: int
    header_size: int
    offset_to_point_data: int
    n_vlr: int
    point_format_id_raw: int
    point_format_id: int
    record_length: int
    n_points: int
    n_points_by_return: Tuple[int, ...]
    scales: Tuple[float, float, float]
    offsets: Tuple[float, float, float]
    declared_max: Tuple[float, float, float]
    declared_min: Tuple[float, float, float]
    vlrs: Tuple[RawVLR, ...]

    @property
    def has_gps(self) -> bool:
        return self.point_format_id in (1, 3)

    @property
    def has_rgb(self) -> bool:
        return self.point_format_id in (2, 3)


def _read_exact(data: bytes, offset: int, size: int, what: str) -> bytes:
    if offset < 0 or offset + size > len(data):
        raise IngestionError(f"truncated LAS header while reading {what}")
    return data[offset:offset + size]


def parse_raw_header(path: str) -> RawHeader:
    """Parse and strictly validate the fixed header and VLR list of ``path``."""
    with open(path, "rb") as fh:
        data = fh.read()
    file_size = len(data)

    if file_size < FIXED_HEADER_SIZE:
        raise IngestionError(
            f"{path}: file is only {file_size} bytes, smaller than a LAS 1.2 "
            f"fixed header ({FIXED_HEADER_SIZE} bytes)"
        )
    if data[:4] != LAS_SIGNATURE:
        raise IngestionError(f"{path}: missing 'LASF' signature, not a LAS file")

    major, minor = data[24], data[25]
    if (major, minor) != (ACCEPTED_MAJOR, ACCEPTED_MINOR):
        raise IngestionError(
            f"{path}: unsupported LAS version {major}.{minor}; only 1.2 is accepted"
        )

    header_size = struct.unpack_from("<H", data, 94)[0]
    if header_size != FIXED_HEADER_SIZE:
        raise IngestionError(
            f"{path}: header size {header_size} is not the LAS 1.2 fixed size "
            f"{FIXED_HEADER_SIZE}"
        )

    offset_to_point_data = struct.unpack_from("<I", data, 96)[0]
    n_vlr = struct.unpack_from("<I", data, 100)[0]
    if n_vlr > 1_000_000:
        raise IngestionError(f"{path}: implausible VLR count {n_vlr}")

    pf_raw = data[104]
    record_length = struct.unpack_from("<H", data, 105)[0]
    n_points = struct.unpack_from("<I", data, 107)[0]
    n_by_return = struct.unpack_from("<5I", data, 111)

    if pf_raw & COMPRESSION_BIT:
        raise IngestionError(
            f"{path}: compressed (LAZ) point data format {pf_raw}; "
            "only uncompressed LAS is accepted"
        )
    point_format = pf_raw & 0x7F
    if point_format not in ACCEPTED_POINT_FORMATS:
        raise IngestionError(
            f"{path}: unsupported point format {point_format}; "
            "only formats 0..3 are accepted"
        )
    expected_length = EXPECTED_RECORD_LENGTH[point_format]
    if record_length < expected_length:
        raise IngestionError(
            f"{path}: point data record length {record_length} is smaller than "
            f"the {expected_length} bytes required by point format {point_format}"
        )

    scales = struct.unpack_from("<3d", data, 131)
    offsets = struct.unpack_from("<3d", data, 155)
    # The header interleaves max/min per axis (Max X @179, Min X @187,
    # Max Y @195, Min Y @203, Max Z @211, Min Z @219).
    declared_max = (
        struct.unpack_from("<d", data, 179)[0],
        struct.unpack_from("<d", data, 195)[0],
        struct.unpack_from("<d", data, 211)[0],
    )
    declared_min = (
        struct.unpack_from("<d", data, 187)[0],
        struct.unpack_from("<d", data, 203)[0],
        struct.unpack_from("<d", data, 219)[0],
    )
    for name, value in zip(("x", "y", "z"), scales):
        if not _finite_positive(value):
            raise IngestionError(
                f"{path}: {name} scale {value!r} is not a finite positive number"
            )
    for name, value in zip(("x", "y", "z"), offsets):
        if not _finite(value):
            raise IngestionError(f"{path}: {name} offset {value!r} is not finite")

    vlrs = _parse_vlrs(path, data, header_size, n_vlr, offset_to_point_data)

    # The point data region must start after every declared VLR.
    vlr_end = header_size + n_vlr * VLR_HEADER_SIZE + sum(v.record_length for v in vlrs)
    if offset_to_point_data < vlr_end:
        raise IngestionError(
            f"{path}: point data offset {offset_to_point_data} overlaps the "
            f"VLR area which ends at {vlr_end}"
        )

    expected_payload = n_points * record_length
    expected_size = offset_to_point_data + expected_payload
    if file_size < expected_size:
        raise IngestionError(
            f"{path}: truncated point payload: header declares {n_points} points "
            f"x {record_length} bytes = {expected_payload} payload bytes starting "
            f"at {offset_to_point_data} ({expected_size} needed, {file_size} present)"
        )
    if file_size > expected_size:
        raise IngestionError(
            f"{path}: {file_size - expected_size} trailing bytes after the last "
            "declared point; LAS 1.2 has no EVLRs, refusing an ambiguous file"
        )

    return RawHeader(
        file_size=file_size,
        header_size=header_size,
        offset_to_point_data=offset_to_point_data,
        n_vlr=n_vlr,
        point_format_id_raw=pf_raw,
        point_format_id=point_format,
        record_length=record_length,
        n_points=n_points,
        n_points_by_return=tuple(n_by_return),
        scales=scales,
        offsets=offsets,
        declared_max=declared_max,
        declared_min=declared_min,
        vlrs=tuple(vlrs),
    )


def _parse_vlrs(
    path: str, data: bytes, header_size: int, n_vlr: int, point_offset: int
) -> List[RawVLR]:
    vlrs: List[RawVLR] = []
    cursor = header_size
    for i in range(n_vlr):
        block = _read_exact(data, cursor, VLR_HEADER_SIZE, f"VLR {i} header")
        # Layout: reserved (u16), user_id (16), record_id (u16),
        # record_length (u16), description (32) = 54 bytes total.
        user_id = block[2:18].split(b"\x00", 1)[0].decode("ascii", "replace")
        record_id = struct.unpack_from("<H", block, 18)[0]
        record_length = struct.unpack_from("<H", block, 20)[0]
        description = block[22:54].split(b"\x00", 1)[0].decode("ascii", "replace")
        record_data = _read_exact(data, cursor + VLR_HEADER_SIZE, record_length,
                                  f"VLR {i} payload")
        if cursor + VLR_HEADER_SIZE + record_length > point_offset:
            raise IngestionError(f"{path}: VLR {i} runs past the point data offset")
        if record_id == LASZIP_RECORD_ID and user_id in LASZIP_USER_IDS:
            raise IngestionError(
                f"{path}: LASzip VLR present, the file is compressed (LAZ); "
                "only uncompressed LAS is accepted"
            )
        vlrs.append(
            RawVLR(
                user_id=user_id,
                record_id=record_id,
                record_length=record_length,
                description=description,
                record_data=record_data,
            )
        )
        cursor += VLR_HEADER_SIZE + record_length
    return vlrs


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _finite_positive(value: float) -> bool:
    return _finite(value) and value > 0.0


def reference_vlrs(header: RawHeader) -> List[RawVLR]:
    """Return the VLRs that describe the coordinate reference, if any."""
    out: List[RawVLR] = []
    for vlr in header.vlrs:
        if vlr.user_id != PROJECTION_USER_ID:
            continue
        if vlr.record_id == WKT_RECORD_ID or vlr.record_id in GEOTIFF_RECORD_IDS:
            out.append(vlr)
    return out


def describe_reference(header: RawHeader) -> Optional[str]:
    """Build a deterministic textual reference description from raw VLRs.

    The description only needs to be stable and equality-comparable: two files
    get the same description exactly when their reference VLRs are identical.
    ``None`` means the file carries no reference description at all.
    """
    refs = reference_vlrs(header)
    if not refs:
        return None
    parts = []
    for vlr in refs:
        parts.append(
            f"{vlr.user_id}|{vlr.record_id}|"
            f"{vlr.record_data.hex()}"
        )
    return "REF:" + ";".join(parts)
