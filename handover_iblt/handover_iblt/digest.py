"""Digest container: fixed-width header, fixed-size body, SHA-256 trailer.

Wire format (all integers big-endian)::

    offset 0   8 bytes   magic b"HNDIBLT1"
    offset 8  16 bytes   protocol name, NUL padded ("iblt-handover/1")
    offset 24  4 bytes   capacity (number of cells, uint32)
    offset 28  4 bytes   k (cells touched per element, uint32)
    offset 32  8 bytes   seed (uint64)
    offset 40  8 bytes   unique elements in the checklist (uint64)
    offset 48  8 bytes   input lines read (uint64)
    offset 56  8 bytes   exact duplicate lines ignored (uint64)
    offset 64  capacity * 64 bytes   cells
    end - 32  32 bytes   SHA-256 of the 64-byte header followed by the body

The header is fixed at 64 bytes and every count is fixed-width, so for a
fixed capacity the file size is exactly ``96 + 64 * capacity`` bytes
regardless of how many checklist rows were folded in.  Nothing about the
original checklist is smuggled into the digest: the body only contains the
IBLT cells.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field

from .config import (
    CELL_BYTES,
    PROTOCOL_VERSION,
    DigestConfig,
)
from .errors import DigestFormatError
from .table import IbltTable

MAGIC = b"HNDIBLT1"
PROTOCOL_FIELD_BYTES = 16
HEADER_STRUCT = struct.Struct(">8s16sIIQQQQ")
HEADER_BYTES = HEADER_STRUCT.size  # 64
TRAILER_BYTES = 32

#: Identities fetched per round trip while folding a store into a digest.
DIGEST_BATCH = 1000


def digest_size(config: DigestConfig) -> int:
    """Exact on-disk size for a digest with this configuration."""
    return HEADER_BYTES + CELL_BYTES * config.capacity + TRAILER_BYTES


@dataclass
class Digest:
    """A parsed digest plus its header counts."""

    config: DigestConfig
    table: IbltTable
    unique_elements: int
    source_lines: int
    duplicate_lines: int
    integrity_sha256: bytes | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.table.config != self.config:
            raise ValueError("table configuration does not match digest")
        if self.table.cells and len(self.table.cells) != self.config.capacity:
            raise ValueError("cell count does not match capacity")
        for name in ("unique_elements", "source_lines", "duplicate_lines"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            if value >= 1 << 64:
                raise ValueError(f"{name} does not fit in 64 bits")

    @property
    def body_bytes(self) -> int:
        return CELL_BYTES * self.config.capacity

    @property
    def total_bytes(self) -> int:
        return digest_size(self.config)

    def header_bytes(self) -> bytes:
        protocol = PROTOCOL_VERSION.encode("ascii")
        if len(protocol) > PROTOCOL_FIELD_BYTES:
            raise ValueError("protocol name does not fit the header")
        protocol = protocol.ljust(PROTOCOL_FIELD_BYTES, b"\x00")
        return HEADER_STRUCT.pack(
            MAGIC,
            protocol,
            self.config.capacity,
            self.config.k,
            self.config.seed,
            self.unique_elements,
            self.source_lines,
            self.duplicate_lines,
        )

    def to_bytes(self) -> bytes:
        header = self.header_bytes()
        body = self.table.to_body()
        trailer = hashlib.sha256(header + body).digest()
        blob = header + body + trailer
        if len(blob) != self.total_bytes:
            raise ValueError("serialized digest has the wrong size")
        return blob

    def save(self, path: str, *, overwrite: bool = False) -> int:
        """Write the digest; refuse to replace an existing file by default.

        Returns the number of bytes written.
        """
        blob = self.to_bytes()
        mode = "wb" if overwrite else "xb"
        try:
            with open(path, mode) as fh:
                fh.write(blob)
        except FileExistsError:
            raise FileExistsError(
                f"refusing to overwrite existing digest: {path}"
            ) from None
        return len(blob)

    def metadata(self) -> dict:
        return {
            "protocol": PROTOCOL_VERSION,
            "capacity": self.config.capacity,
            "k": self.config.k,
            "seed": self.config.seed,
            "unique_elements": self.unique_elements,
            "source_lines": self.source_lines,
            "duplicate_lines": self.duplicate_lines,
            "cell_body_bytes": self.body_bytes,
            "digest_bytes": self.total_bytes,
            "integrity_sha256": (
                self.integrity_sha256.hex()
                if self.integrity_sha256 is not None
                else hashlib.sha256(self.header_bytes() + self.table.to_body()).hexdigest()
            ),
            "nonzero_cells": self.table.nonzero_count(),
            "sum_abs_counts": self.table.sum_abs_counts(),
        }


def build_digest_from_store(store, config: DigestConfig) -> Digest:
    """Fold every distinct checklist identity from a store into one digest.

    Rows are read with a server-side cursor in small batches; Python holds
    only one batch plus the fixed-size cell array.  Neither the raw
    identifiers nor a copy of the checklist is retained anywhere.
    """
    table = IbltTable(config)
    count = 0
    cursor = store.connection.execute("SELECT uuid, fingerprint FROM elements")
    while True:
        rows = cursor.fetchmany(DIGEST_BATCH)
        if not rows:
            break
        for uuid_b, fingerprint_b in rows:
            if len(uuid_b) != 16 or len(fingerprint_b) != 32:
                raise DigestFormatError("store contains a malformed element")
            table.add(bytes(uuid_b) + bytes(fingerprint_b), 1)
            count += 1
    meta = store.meta()
    if table.distinct_elements() != count:
        raise DigestFormatError(
            "internal count invariant failed while building the digest"
        )
    return Digest(
        config=config,
        table=table,
        unique_elements=count,
        source_lines=meta.get("source_lines", count),
        duplicate_lines=meta.get("duplicate_lines", 0),
    )


def parse_digest_bytes(data: bytes) -> Digest:
    """Parse and fully validate a digest blob.

    Rejects: wrong magic, unsupported protocol, impossible configuration,
    truncated/missing cells, extra trailing bytes, impossible cell counts and
    any content change not matching the trailing SHA-256.
    """
    data = bytes(data)
    if len(data) < HEADER_BYTES + TRAILER_BYTES:
        raise DigestFormatError(
            f"digest is truncated: {len(data)} bytes is too short for the header"
        )
    header = data[:HEADER_BYTES]
    try:
        (magic, protocol_raw, capacity, k, seed,
         unique_elements, source_lines, duplicate_lines) = HEADER_STRUCT.unpack(
            header
        )
    except struct.error as exc:  # pragma: no cover - length already checked
        raise DigestFormatError("digest header is malformed") from exc
    if magic != MAGIC:
        raise DigestFormatError("not a handover IBLT digest (bad magic)")
    protocol = protocol_raw.rstrip(b"\x00")
    try:
        protocol_name = protocol.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DigestFormatError("digest protocol field is not ASCII") from exc
    if protocol_name != PROTOCOL_VERSION:
        raise DigestFormatError(
            f"unsupported protocol {protocol_name!r}; "
            f"this tool speaks {PROTOCOL_VERSION!r}"
        )
    try:
        config = DigestConfig(capacity=capacity, k=k, seed=seed)
    except ValueError as exc:
        raise DigestFormatError(f"digest configuration is invalid: {exc}") from exc

    expected = HEADER_BYTES + CELL_BYTES * capacity + TRAILER_BYTES
    if len(data) != expected:
        if len(data) < expected:
            missing = expected - len(data)
            raise DigestFormatError(
                f"digest is truncated: {missing} bytes/cells are missing "
                f"(expected {expected} bytes for {capacity} cells, got {len(data)})"
            )
        raise DigestFormatError(
            f"digest has {len(data) - expected} unexpected trailing bytes"
        )

    body = data[HEADER_BYTES:HEADER_BYTES + CELL_BYTES * capacity]
    trailer = data[-TRAILER_BYTES:]
    actual = hashlib.sha256(header + body).digest()
    if not hmac.compare_digest(actual, trailer):
        raise DigestFormatError(
            "digest integrity check failed: the file was altered or damaged"
        )

    try:
        table = IbltTable.from_body(config, body, validate_counts=True)
    except ValueError as exc:
        raise DigestFormatError(f"digest cells are invalid: {exc}") from exc

    # The header is covered by the trailer, but cross-check its counts against
    # the cell arithmetic so a fully re-sealed forgery cannot claim a
    # different checklist size than its own cells encode.
    try:
        derived = table.distinct_elements()
    except ValueError as exc:
        raise DigestFormatError(f"digest counts are inconsistent: {exc}") from exc
    if derived != unique_elements:
        raise DigestFormatError(
            "digest header says "
            f"{unique_elements} elements but cells encode {derived}"
        )
    if duplicate_lines > source_lines:
        raise DigestFormatError(
            "digest header has more duplicate lines than input lines"
        )
    if unique_elements > source_lines:
        raise DigestFormatError(
            "digest header has more unique elements than input lines"
        )

    return Digest(
        config=config,
        table=table,
        unique_elements=unique_elements,
        source_lines=source_lines,
        duplicate_lines=duplicate_lines,
        integrity_sha256=trailer,
    )


def load_digest(path: str) -> Digest:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise DigestFormatError(f"cannot read digest {path}: {exc}") from exc
    return parse_digest_bytes(data)


def save_digest(digest: Digest, path: str, *, overwrite: bool = False) -> int:
    return digest.save(path, overwrite=overwrite)


def compare_digest_files(sender_path: str, receiver_path: str):
    """Convenience wrapper: load two digest files and compare them."""
    from .reconcile import compare_digests

    return compare_digests(load_digest(sender_path), load_digest(receiver_path))
