"""Independent expected values for the documentation's small demo table.

These numbers are written down from the byte layout by hand.  They are *not*
produced by the production table code; the tests recompute them from the
byte layout and compare.  The checklist used in ``docs/DEMO.md`` is::

    sender   : A 0xa1..  B 0xb1..  C 0xc1..   (plus a duplicate of B)
    receiver : B 0xb1..  C 0xc1..  D 0xd1..

so the difference is ``A (+1)``, ``D (-1)`` and, in the changed-content
variant, ``C`` appears with two fingerprints ``0xc1..`` / ``0xc2..``.

For the small table below only three elements are used so every expected
cell is small enough to verify by hand::

    capacity = 8, k = 3, seed = 7
    X = uuid 0000..0001, fingerprint 00..00
    Y = uuid 0000..0002, fingerprint 00..00
    Z = uuid 0000..0003, fingerprint 00..00

The cell index lists are derived by the documented hash formula:

    h1 = first 8 bytes (big-endian) of SHA256("iblt-handover/hash-cell/v1" ||
         "i" || seed_u64_be || element_48)
    h2 = first 8 bytes (big-endian) of SHA256("iblt-handover/hash-cell/v1" ||
         "j" || seed_u64_be || element_48) | 1
    cell_i = (h1 + i*h2 + i*i) mod capacity, advancing by 1 on collision

Each element has count +1, key_xor = key (the 48-byte element itself) and
check_xor = first 8 bytes of SHA256("iblt-handover/check/v1" || element).
Cells are the XOR of the elements hashing into them, and the XOR of their
checks.
"""

from __future__ import annotations

import hashlib

CAPACITY = 8
K = 3
SEED = 7

HASH_DOMAIN_CELL = b"iblt-handover/hash-cell/v1"
HASH_DOMAIN_CHECK = b"iblt-handover/check/v1"


def _h64(tag: bytes, seed_u64_be: bytes, element: bytes) -> int:
    return int.from_bytes(
        hashlib.sha256(tag + seed_u64_be + element).digest()[:8], "big"
    )


def expected_cell_indexes(element: bytes) -> list[int]:
    """Recompute the documented mapping from first principles."""
    seed = SEED.to_bytes(8, "big")
    h1 = _h64(HASH_DOMAIN_CELL + b"i", seed, element)
    h2 = _h64(HASH_DOMAIN_CELL + b"j", seed, element) | 1
    used: list[int] = []
    for i in range(K):
        candidate = (h1 + i * h2 + i * i) % CAPACITY
        while candidate in used:
            candidate = (candidate + 1) % CAPACITY
        used.append(candidate)
    return used


def expected_check(element: bytes) -> bytes:
    return hashlib.sha256(HASH_DOMAIN_CHECK + element).digest()[:8]


def element(n: int, fp_byte: int = 0) -> bytes:
    return n.to_bytes(16, "big") + bytes([fp_byte]) * 32


def expected_cells(elements_with_signs: list[tuple[bytes, int]]) -> list[tuple[int, bytes, bytes]]:
    """Return ``(count, key_xor, check_xor)`` per cell, by hand arithmetic."""
    cells = [0] * CAPACITY
    keys = [b"\x00" * 48 for _ in range(CAPACITY)]
    checks = [b"\x00" * 8 for _ in range(CAPACITY)]
    for elem, sign in elements_with_signs:
        for idx in expected_cell_indexes(elem):
            cells[idx] += sign
            keys[idx] = bytes(a ^ b for a, b in zip(keys[idx], elem))
            checks[idx] = bytes(a ^ b for a, b in zip(checks[idx], expected_check(elem)))
    return list(zip(cells, keys, checks))
