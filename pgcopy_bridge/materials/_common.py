"""Shared helpers for hand-written binary materials.

Materials are produced *without* the production codecs: every field body is
spelled byte-by-byte here so the samples and the expected JSON are
independent evidence, not a self-round-trip of the converter.
"""

from __future__ import annotations

import struct

SIGNATURE = b"PGCOPY\n\xff\r\n\x00"


def header(flags: int = 0, extension: bytes = b"") -> bytes:
    return SIGNATURE + struct.pack(">ii", flags, len(extension)) + extension


def end_marker() -> bytes:
    return struct.pack(">h", -1)


def field(body: bytes | None) -> bytes:
    if body is None:
        return struct.pack(">i", -1)
    return struct.pack(">i", len(body)) + body


def int4(value: int) -> bytes:
    return struct.pack(">i", value)


def int8(value: int) -> bytes:
    return struct.pack(">q", value)


def bool_(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def text(value: str) -> bytes:
    return value.encode("utf-8")


def bytea(raw: bytes) -> bytes:
    return raw


def uuid_hex(canonical: str) -> bytes:
    return bytes.fromhex(canonical.replace("-", ""))


def numeric(ndigits: int, weight: int, sign: int, dscale: int,
            digits: list[int]) -> bytes:
    body = struct.pack(">hhHH", ndigits, weight, sign, dscale)
    for d in digits:
        body += struct.pack(">H", d)
    return body


def date(days: int) -> bytes:
    return struct.pack(">i", days)


def timestamp(us: int) -> bytes:
    return struct.pack(">q", us)


def array(dims: list[tuple[int, int]], oid: int, elements: list,
          has_nulls: int | None = None) -> bytes:
    if has_nulls is None:
        # PostgreSQL sets the flag exactly when at least one element is NULL.
        has_nulls = 1 if any(e is None for e in elements) else 0
    body = struct.pack(">iii", len(dims), has_nulls, oid)
    for length, lower in dims:
        body += struct.pack(">ii", length, lower)
    for element in elements:
        body += field(element)
    return body


def tuple_(fields: list[bytes | None]) -> bytes:
    out = struct.pack(">h", len(fields))
    for f in fields:
        out += field(f)
    return out


def build(tuples: list[bytes], *, flags: int = 0,
          extension: bytes = b"", trailing: bytes = b"",
          end: bool = True) -> bytes:
    data = header(flags, extension)
    for t in tuples:
        data += t
    if end:
        data += end_marker()
    return data + trailing
