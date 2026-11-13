"""Small LAS 1.2 material generator for the test-suite.

This module is test infrastructure, never imported by the production package.
It writes valid LAS 1.2 files for point formats 0..3 with laspy and provides
byte-level helpers to produce deliberately broken variants (truncation,
lying headers, compression markers, unsupported versions).
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Optional, Sequence

import laspy
import numpy as np

from laspy.vlrs.known import (
    GeoAsciiParamsVlr,
    GeoKeyDirectoryVlr,
    WktCoordinateSystemVlr,
)
from laspy.vlrs.geotiff import GeoKeyEntryStruct


@dataclass
class TestPoint:
    """One point expressed as raw integer coords plus stored attributes."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    x: int
    y: int
    z: int
    intensity: int = 0
    classification: int = 0
    return_number: int = 1
    number_of_returns: int = 1
    synthetic: int = 0
    key_point: int = 0
    withheld: int = 0
    gps_time: float = 0.0
    red: int = 0
    green: int = 0
    blue: int = 0
    extra: bytes = b""


_STD_SIZES = {0: 20, 1: 28, 2: 26, 3: 34}


def build_las(
    path: str,
    points: Sequence[TestPoint],
    *,
    point_format: int = 0,
    scales=(0.001, 0.001, 0.001),
    offsets=(0.0, 0.0, 0.0),
    wkt: Optional[str] = None,
    geotiff_keys: Optional[Sequence[tuple]] = None,
    geo_ascii: Optional[str] = None,
    extra_byte_count: int = 0,
    extra_dims: Optional[dict] = None,
) -> str:
    """Write a valid uncompressed LAS 1.2 file and return its path.

    ``extra_byte_count`` appends raw unnamed bytes per record (filled from
    ``TestPoint.extra``); ``extra_dims`` instead registers named laspy extra
    dimensions (mapping name -> numpy dtype string).
    """
    if point_format not in (0, 1, 2, 3):
        raise ValueError("test builder supports formats 0..3")
    header = laspy.LasHeader(version="1.2", point_format=point_format)
    header.scales = list(scales)
    header.offsets = list(offsets)

    if wkt is not None:
        header.vlrs.append(WktCoordinateSystemVlr(wkt))
    if geotiff_keys is not None:
        gvlr = GeoKeyDirectoryVlr()
        gvlr.geo_keys = [GeoKeyEntryStruct(*entry) for entry in geotiff_keys]
        header.vlrs.append(gvlr)
        if geo_ascii is not None:
            avlr = GeoAsciiParamsVlr()
            avlr.strings = [geo_ascii]
            header.vlrs.append(avlr)

    data = laspy.LasData(header)
    n = len(points)
    data.X = np.array([p.x for p in points], dtype=np.int32)
    data.Y = np.array([p.y for p in points], dtype=np.int32)
    data.Z = np.array([p.z for p in points], dtype=np.int32)
    data.intensity = np.array([p.intensity for p in points], dtype=np.uint16)
    data.classification = np.array(
        [p.classification for p in points], dtype=np.uint8
    )
    data.return_number = np.array(
        [p.return_number for p in points], dtype=np.uint8
    )
    data.number_of_returns = np.array(
        [p.number_of_returns for p in points], dtype=np.uint8
    )
    data.synthetic = np.array([p.synthetic for p in points], dtype=bool)
    data.key_point = np.array([p.key_point for p in points], dtype=bool)
    data.withheld = np.array([p.withheld for p in points], dtype=bool)

    if point_format in (1, 3):
        data.gps_time = np.array(
            [p.gps_time for p in points], dtype=np.float64
        )
    if point_format in (2, 3):
        data.red = np.array([p.red for p in points], dtype=np.uint16)
        data.green = np.array([p.green for p in points], dtype=np.uint16)
        data.blue = np.array([p.blue for p in points], dtype=np.uint16)

    if extra_dims:
        for name, dtype in extra_dims.items():
            data.add_extra_dim(laspy.ExtraBytesParams(name=name, type=dtype))
            setattr(data, name, np.array(
                [getattr(p, name) for p in points],
                dtype=np.dtype(dtype),
            ))
    data.write(path)

    if extra_byte_count:
        _append_raw_extra_bytes(path, point_format, points, extra_byte_count)
    return path


def _append_raw_extra_bytes(path, point_format, points, count) -> None:
    """Grow each point record with unnamed raw extra bytes.

    laspy's named extra dimensions go through an ExtraBytes VLR; this helper
    instead rewrites the payload directly so we also exercise "extra bytes the
    library treats as opaque".
    """
    with open(path, "rb") as fh:
        blob = bytearray(fh.read())
    offset = struct.unpack_from("<I", blob, 96)[0]
    record_len = struct.unpack_from("<H", blob, 105)[0]
    n = struct.unpack_from("<I", blob, 107)[0]
    std = _STD_SIZES[point_format]
    if record_len != std:
        raise RuntimeError("cannot combine raw extra bytes with laspy dims")
    payload = blob[offset:offset + n * std]
    new_payload = bytearray()
    for i in range(n):
        rec = payload[i * std:(i + 1) * std]
        extra = points[i].extra[:count].ljust(count, b"\x00")
        new_payload.extend(rec + extra)
    new_blob = blob[:offset] + new_payload
    struct.pack_into("<H", new_blob, 105, std + count)
    with open(path, "wb") as fh:
        fh.write(new_blob)


# --------------------------------------------------------------------------
# Deliberately broken variants
# --------------------------------------------------------------------------

def truncate_tail(path: str, n_bytes: int) -> str:
    blob = open(path, "rb").read()
    with open(path, "wb") as fh:
        fh.write(blob[:-n_bytes])
    return path


def append_garbage(path: str, garbage: bytes = b"\x00\x01\x02") -> str:
    with open(path, "ab") as fh:
        fh.write(garbage)
    return path


def patch_bytes(path: str, offset: int, values: bytes) -> str:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        fh.write(values)
    return path


def set_compression_bit(path: str) -> str:
    with open(path, "r+b") as fh:
        fh.seek(104)
        value = fh.read(1)[0]
        fh.seek(104)
        fh.write(bytes([value | 0x80]))
    return path


def set_version(path: str, major: int, minor: int) -> str:
    with open(path, "r+b") as fh:
        fh.seek(24)
        fh.write(bytes([major, minor]))
    return path


def set_point_format(path: str, fmt: int) -> str:
    with open(path, "r+b") as fh:
        fh.seek(104)
        fh.write(bytes([fmt]))
    return path


def lie_about_point_count(path: str, fake_count: int) -> str:
    with open(path, "r+b") as fh:
        fh.seek(107)
        fh.write(struct.pack("<I", fake_count))
    return path


def lie_about_extents(path: str, *, x_min: float, x_max: float) -> str:
    # Max X at 179, Min X at 187 (header interleaves max/min per axis).
    with open(path, "r+b") as fh:
        fh.seek(187)
        fh.write(struct.pack("<d", x_min))
        fh.seek(179)
        fh.write(struct.pack("<d", x_max))
    return path


def add_laz_marker_vlr(path: str) -> str:
    """Insert a fake LASzip VLR and shift the point payload accordingly."""
    with open(path, "rb") as fh:
        blob = bytearray(fh.read())
    offset = struct.unpack_from("<I", blob, 96)[0]
    n_vlr = struct.unpack_from("<I", blob, 100)[0]
    record_data = b"\x00" * 34
    header = bytearray(54)
    user_id = b"laszip encoded"
    header[2:2 + len(user_id)] = user_id
    struct.pack_into("<H", header, 18, 2220)
    struct.pack_into("<H", header, 20, len(record_data))
    vlr = bytes(header) + record_data
    new_blob = blob[:offset] + vlr + blob[offset:]
    struct.pack_into("<I", new_blob, 96, offset + len(vlr))
    struct.pack_into("<I", new_blob, 100, n_vlr + 1)
    with open(path, "wb") as fh:
        fh.write(new_blob)
    return path


def copy_file(src: str, dst: str) -> str:
    with open(src, "rb") as fh:
        data = fh.read()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst
