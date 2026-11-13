"""The fixed-size cell array shared by digest building and comparison.

A table has ``capacity`` cells.  Each cell keeps a signed ``count``, a
48-byte XOR of element keys and an 8-byte XOR of element checksums.  Adding
an element increments count by its sign and XORs key/check into all cells the
element maps to; removing does the same with the opposite sign.

The table is *fixed size*: memory and on-disk body size depend only on
``capacity``, never on how many elements were added.
"""

from __future__ import annotations

import struct
from typing import Iterable

from .config import (
    CELL_BYTES,
    CHECK_XOR_BYTES,
    COUNT_BYTES,
    KEY_XOR_BYTES,
    DigestConfig,
    cell_indexes,
    element_check,
    key_prefix,
)

COUNT_STRUCT = struct.Struct(">q")


class Cell:
    """One IBLT cell; mutable byte arrays make in-place XOR cheap."""

    __slots__ = ("count", "key_xor", "check_xor")

    def __init__(self, count: int = 0, key_xor: bytes | None = None,
                 check_xor: bytes | None = None) -> None:
        self.count = count
        self.key_xor = bytearray(key_xor if key_xor is not None else b"\x00" * KEY_XOR_BYTES)
        self.check_xor = bytearray(check_xor if check_xor is not None else b"\x00" * CHECK_XOR_BYTES)

    def is_zero(self) -> bool:
        return (
            self.count == 0
            and not any(self.key_xor)
            and not any(self.check_xor)
        )

    def key_bytes(self) -> bytes:
        return bytes(self.key_xor)

    def check_bytes(self) -> bytes:
        return bytes(self.check_xor)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Cell(count={self.count}, key={self.key_bytes().hex()[:12]}…, "
            f"check={self.check_bytes().hex()})"
        )


def xor_bytes(left: bytes, right: bytes) -> bytes:
    """XOR two equal-length byte strings via big integers (fast and exact)."""
    if len(left) != len(right):
        raise ValueError("xor operands differ in length")
    value = int.from_bytes(left, "big") ^ int.from_bytes(right, "big")
    return value.to_bytes(len(left), "big")


class IbltTable:
    """A fixed-capacity IBLT cell array."""

    __slots__ = ("config", "cells")

    def __init__(self, config: DigestConfig,
                 cells: list[Cell] | None = None) -> None:
        self.config = config
        if cells is None:
            cells = [Cell() for _ in range(config.capacity)]
        elif len(cells) != config.capacity:
            raise ValueError("cell list does not match capacity")
        self.cells = cells

    # -- element arithmetic ---------------------------------------------
    def add(self, element: bytes, sign: int = 1) -> None:
        if sign not in (1, -1):
            raise ValueError("sign must be +1 or -1")
        key = key_prefix(element)
        check = element_check(element)
        for index in cell_indexes(
            self.config.capacity, self.config.k, self.config.seed, element
        ):
            cell = self.cells[index]
            new_count = cell.count + sign
            if abs(new_count) > MAX_ABS_COUNT:
                raise ValueError("cell count exceeds the supported range")
            cell.count = new_count
            cell.key_xor[:] = xor_bytes(cell.key_xor, key)
            cell.check_xor[:] = xor_bytes(cell.check_xor, check)

    def remove(self, element: bytes, sign: int = 1) -> None:
        """Remove one occurrence of ``element`` inserted with ``sign``."""
        self.add(element, -sign)

    # -- table arithmetic ------------------------------------------------
    def subtract(self, other: "IbltTable") -> "IbltTable":
        """Return ``self - other`` without modifying either input.

        Counts subtract; key and check XOR are XORed.  Only tables with
        identical configuration may be combined.
        """
        if self.config != other.config:
            raise ValueError("tables have different configurations")
        cells = []
        for left, right in zip(self.cells, other.cells):
            cells.append(
                Cell(
                    left.count - right.count,
                    xor_bytes(left.key_xor, right.key_xor),
                    xor_bytes(left.check_xor, right.check_xor),
                )
            )
        return IbltTable(self.config, cells)

    # -- summaries -------------------------------------------------------
    def nonzero_indexes(self) -> list[int]:
        return [i for i, c in enumerate(self.cells) if not c.is_zero()]

    def nonzero_count(self) -> int:
        return sum(1 for c in self.cells if not c.is_zero())

    def sum_abs_counts(self) -> int:
        return sum(abs(c.count) for c in self.cells)

    def max_abs_count(self) -> int:
        return max((abs(c.count) for c in self.cells), default=0)

    def total_count(self) -> int:
        return sum(c.count for c in self.cells)

    def is_zero(self) -> bool:
        return all(c.is_zero() for c in self.cells)

    def distinct_elements(self) -> int:
        """Number of inserted elements, derived from the count invariant.

        Every element touches exactly ``k`` distinct cells with one unit of
        count, so the sum of all counts is ``k * N``.
        """
        total = self.total_count()
        if total % self.config.k != 0:
            raise ValueError("count sum is not a multiple of k")
        return total // self.config.k

    # -- wire format ------------------------------------------------------
    def to_body(self) -> bytes:
        parts = []
        for cell in self.cells:
            parts.append(COUNT_STRUCT.pack(cell.count))
            parts.append(bytes(cell.key_xor))
            parts.append(bytes(cell.check_xor))
        body = b"".join(parts)
        if len(body) != CELL_BYTES * self.config.capacity:
            raise ValueError("serialized body has the wrong length")
        return body

    @classmethod
    def from_body(cls, config: DigestConfig, body: bytes,
                  *, validate_counts: bool = True) -> "IbltTable":
        expected = CELL_BYTES * config.capacity
        if len(body) != expected:
            raise ValueError(
                f"cell body is {len(body)} bytes, expected {expected}"
            )
        cells: list[Cell] = []
        offset = 0
        for _ in range(config.capacity):
            count = COUNT_STRUCT.unpack_from(body, offset)[0]
            offset += COUNT_BYTES
            key = body[offset:offset + KEY_XOR_BYTES]
            offset += KEY_XOR_BYTES
            check = body[offset:offset + CHECK_XOR_BYTES]
            offset += CHECK_XOR_BYTES
            if validate_counts:
                _validate_cell_fields(count, key, check)
            cells.append(Cell(count, key, check))
        return cls(config, cells)

    # -- introspection -----------------------------------------------------
    def iter_nonzero(self) -> Iterable[tuple[int, Cell]]:
        for i, cell in enumerate(self.cells):
            if not cell.is_zero():
                yield i, cell


#: A count beyond this is treated as a corrupt cell rather than a real
#: aggregation.  Even a checklist with more entries than cells cannot reach
#: it through legitimate ``+1`` increments in a digest of this module.
MAX_ABS_COUNT = 1_000_000_000

#: ``-int64`` is not representable, so such a count breaks sign arithmetic.
COUNT_MIN = -(1 << 63)


def _validate_cell_fields(count: int, key: bytes, check: bytes) -> None:
    """Reject cell values that cannot come from this protocol's arithmetic.

    * a count of ``INT64_MIN`` cannot be negated safely;
    * an implausible magnitude indicates corrupt or hand-edited bytes;
    * a nonzero count with zero key *and* zero check carries no element
      material at all.  Even the all-zero 48-byte element has a nonzero
      checksum (the chance of a genuine zero is 2^-64).
    """
    if count == COUNT_MIN:
        raise ValueError("cell count is INT64_MIN and cannot be negated")
    if abs(count) > MAX_ABS_COUNT:
        raise ValueError(f"cell count magnitude {abs(count)} is implausible")
    if count != 0 and not any(key) and not any(check):
        raise ValueError(
            "cell claims elements (count != 0) but carries zero key and check"
        )
