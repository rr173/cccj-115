"""Protocol constants, the digest configuration and the record wire format.

The digest is a *reversible Bloom lookup table* (IBLT) whose cells hold a
signed count, a 48-byte XOR of keys and an 8-byte XOR of element checksums.
Configuration (capacity, k, seed) is part of the digest header; two digests
built with different configuration are never comparable.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

PROTOCOL_VERSION = "iblt-handover/1"

#: Element layout: 16-byte UUID followed by 32-byte SHA-256 fingerprint.
UUID_BYTES = 16
FINGERPRINT_BYTES = 32
ELEMENT_BYTES = UUID_BYTES + FINGERPRINT_BYTES

#: Per-cell wire layout.
COUNT_BYTES = 8
KEY_XOR_BYTES = 48
CHECK_XOR_BYTES = 8
CELL_BYTES = COUNT_BYTES + KEY_XOR_BYTES + CHECK_XOR_BYTES

#: Narrowest possible element key: the digest keeps only this prefix.
KEY_XOR_PREFIX_BYTES = KEY_XOR_BYTES

MAX_CAPACITY = 1 << 20
MAX_K = 16

#: Domain-separation tags for the stable hash.  Changing any of these is a
#: protocol change, not a tuning knob.
HASH_DOMAIN_CELL = b"iblt-handover/hash-cell/v1"
HASH_DOMAIN_CHECK = b"iblt-handover/check/v1"

#: Independent expected values for the small demo table used by the docs.
DEFAULT_CAPACITY = 64
DEFAULT_K = 4
DEFAULT_SEED = 20261113


@dataclass(frozen=True)
class DigestConfig:
    """Parameters fixed when a digest is built.

    ``capacity`` is the number of cells; ``k`` is the number of distinct
    cells each element touches; ``seed`` makes the hash reproducible.
    """

    capacity: int = DEFAULT_CAPACITY
    k: int = DEFAULT_K
    seed: int = DEFAULT_SEED

    def __post_init__(self) -> None:
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool):
            raise ValueError("capacity must be an integer")
        if not isinstance(self.k, int) or isinstance(self.k, bool):
            raise ValueError("k must be an integer")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer")
        if not 1 <= self.capacity <= MAX_CAPACITY:
            raise ValueError(f"capacity must be between 1 and {MAX_CAPACITY}")
        if not 1 <= self.k <= MAX_K:
            raise ValueError(f"k must be between 1 and {MAX_K}")
        if self.k > self.capacity:
            raise ValueError("k cannot exceed capacity (cells must be distinct)")
        if not 0 <= self.seed < (1 << 64):
            raise ValueError("seed must fit in 64 unsigned bits")

    def as_dict(self) -> dict:
        return {
            "protocol": PROTOCOL_VERSION,
            "capacity": self.capacity,
            "k": self.k,
            "seed": self.seed,
        }

    def digest_bytes(self) -> int:
        """Size of one full digest, independent of checklist length."""
        return CELL_BYTES * self.capacity


def default_config() -> DigestConfig:
    return DigestConfig()


def hex_to_bytes(value: str, length: int, *, what: str) -> bytes:
    """Decode a fixed-width lowercase/uppercase hex field.

    Rejects wrong length, non-hex characters and Python's permissive
    "whitespace between byte pairs" forms.
    """
    if not isinstance(value, str) or len(value) != length * 2:
        raise ValueError(
            f"{what} must be exactly {length * 2} hex characters"
        )
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{what} is not valid hexadecimal") from exc
    if len(raw) != length:
        raise ValueError(f"{what} must decode to {length} bytes")
    return raw


def is_hex(value: str, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length * 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def uuid_bytes(uuid_hex: str) -> bytes:
    return hex_to_bytes(uuid_hex, UUID_BYTES, what="uuid")


def fingerprint_bytes(fingerprint_hex: str) -> bytes:
    return hex_to_bytes(fingerprint_hex, FINGERPRINT_BYTES, what="fingerprint")


def element_from_hex(uuid_hex: str, fingerprint_hex: str) -> bytes:
    return uuid_bytes(uuid_hex) + fingerprint_bytes(fingerprint_hex)


def uuid_hex(uuid_b: bytes) -> str:
    return uuid_b.hex()


def fingerprint_hex(fp_b: bytes) -> str:
    return fp_b.hex()


def key_prefix(element: bytes) -> bytes:
    """The part of an element the cells can retain: first 48 bytes."""
    if len(element) != ELEMENT_BYTES:
        raise ValueError("element must be exactly 48 bytes")
    return element[:KEY_XOR_PREFIX_BYTES]


def element_check(element: bytes) -> bytes:
    """8-byte checksum used to confirm a recovered cell is a real element."""
    if len(element) != ELEMENT_BYTES:
        raise ValueError("element must be exactly 48 bytes")
    digest = hashlib.sha256(HASH_DOMAIN_CHECK + element).digest()
    return digest[:CHECK_XOR_BYTES]


def element_identifier(element: bytes) -> bytes:
    return element[:UUID_BYTES]


def element_fingerprint(element: bytes) -> bytes:
    return element[UUID_BYTES:]


def split_element(element: bytes) -> tuple[bytes, bytes, bytes]:
    """Return ``(uuid, fingerprint, check)`` for one 48-byte element."""
    return element[:UUID_BYTES], element[UUID_BYTES:], element_check(element)


def cell_indexes(capacity: int, k: int, seed: int, element: bytes) -> list[int]:
    """Map one element to ``k`` pairwise-distinct cell indices.

    The hash is domain-separated: ``seed`` is encoded big-endian as 8 bytes,
    followed by the full 48-byte element.  Two independent 64-bit hashes
    (tags ``i`` and ``j``) are derived, and cell ``i`` is

        h1 + i*h2 + i*i  (mod capacity),  for i = 0 .. k-1

    Distinctness is enforced with a small deterministic probe: if a candidate
    is already used, it advances by 1 modulo capacity until it is unused.  The
    same function runs during insertion and during peeling, so a recovered
    element always maps back to exactly the cells it was inserted into.
    """
    if len(element) != ELEMENT_BYTES:
        raise ValueError("element must be exactly 48 bytes")
    if not 1 <= k <= capacity:
        raise ValueError("need 1 <= k <= capacity")

    h1 = _hash64(HASH_DOMAIN_CELL + b"i" + struct.pack(">Q", seed), element)
    h2 = _hash64(HASH_DOMAIN_CELL + b"j" + struct.pack(">Q", seed), element)
    h2 |= 1  # odd stride cycles through every cell for a power-of-two table

    chosen: list[int] = []
    used: set[int] = set()
    for i in range(k):
        candidate = (h1 + i * h2 + i * i) % capacity
        while candidate in used:
            candidate = (candidate + 1) % capacity
        used.add(candidate)
        chosen.append(candidate)
    return chosen


def _hash64(tag: bytes, payload: bytes) -> int:
    digest = hashlib.sha256(tag + payload).digest()
    return int.from_bytes(digest[:8], "big")
