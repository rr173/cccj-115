"""Peeling of the cell-wise difference table.

The difference table is peeled by repeatedly finding a *pure* cell:

* ``abs(count) == 1``,
* the ``count == 0 / nonzero key`` contradiction does not hold, and
* the 8-byte checksum of the 48-byte key matches the cell's check XOR.

The recovered element is then removed from **every** cell it maps to, with
the sign the pure cell carried.  A recovered element is only accepted when

1. its checksum matches the cell's check XOR, and
2. the hash of the element includes the very cell index it was found in.

If the cell index does not appear in the element's mapping, peeling stops
with a consistency error instead of trusting the count.

Stopping is not success: only an all-zero table is a complete reconciliation.
Otherwise the stopped state — including residual scale and any peeled clues —
is returned and must not be presented as the difference list.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .config import (
    CHECK_XOR_BYTES,
    ELEMENT_BYTES,
    cell_indexes,
    element_check,
)
from .table import Cell, IbltTable, xor_bytes


@dataclass(frozen=True)
class ResidualReport:
    """Description of what was *not* peeled."""

    nonzero_cells: int
    total_cells: int
    sum_abs_counts: int
    max_abs_count: int
    sample_cell_indexes: tuple[int, ...]

    def as_dict(self) -> dict:
        return {
            "nonzero_cells": self.nonzero_cells,
            "total_cells": self.total_cells,
            "sum_abs_counts": self.sum_abs_counts,
            "max_abs_count": self.max_abs_count,
            "sample_cell_indexes": list(self.sample_cell_indexes),
        }


@dataclass(frozen=True)
class PeeledElement:
    """One element recovered by peeling, with the sign it carried.

    ``sign = +1`` means it exists on the sender side only; ``-1`` means the
    receiver side only.
    """

    sign: int
    element: bytes

    @property
    def uuid(self) -> bytes:
        return self.element[:16]

    @property
    def fingerprint(self) -> bytes:
        return self.element[16:]


@dataclass
class PeelOutcome:
    complete: bool
    peeled: list[PeeledElement] = field(default_factory=list)
    residual: ResidualReport | None = None
    consistency_error: str | None = None
    residual_table: IbltTable | None = None


def cell_is_pure(cell: Cell) -> bool:
    """A cell is pure when exactly one element (up to sign) remains in it
    and its 8-byte check confirms the key it carries.

    A count of 0 with nonzero key/check material is the XOR of several
    elements (an unrecoverable cycle), not a pure cell.
    """
    if cell.count == 0:
        return False
    if abs(cell.count) != 1:
        return False
    key = cell.key_bytes()
    if len(key) != ELEMENT_BYTES:
        return False
    return cell.check_bytes() == element_check(key)


def peel(table: IbltTable) -> PeelOutcome:
    """Peel ``table`` in place and report what remains.

    ``table`` is modified (peeling removes recovered elements).  Callers that
    need the original must pass a copy; :func:`reconcile.compare_digests`
    always builds a fresh difference table.
    """
    config = table.config
    cells = table.cells
    queue: deque[int] = deque(
        i for i, cell in enumerate(cells) if cell_is_pure(cell)
    )
    peeled: list[PeeledElement] = []
    queued = set(queue)
    consistency_error: str | None = None

    while queue:
        index = queue.popleft()
        queued.discard(index)
        cell = cells[index]
        if not cell_is_pure(cell):
            continue
        sign = 1 if cell.count == 1 else -1
        element = cell.key_bytes()
        expected_check = element_check(element)
        if cell.check_bytes() != expected_check:  # pragma: no cover - guarded
            consistency_error = "pure cell check changed during peeling"
            break
        mapped = cell_indexes(config.capacity, config.k, config.seed, element)
        if index not in mapped:
            # A count of 1 alone is not proof: the element must hash back to
            # the cell it was found in.
            consistency_error = (
                f"recovered element does not map to cell {index}; "
                "refusing to treat it as a real difference"
            )
            break

        peeled.append(PeeledElement(sign=sign, element=element))

        for target_index in mapped:
            target = cells[target_index]
            new_count = target.count - sign
            target.count = new_count
            target.key_xor[:] = xor_bytes(target.key_xor, element)
            target.check_xor[:] = xor_bytes(
                target.check_xor, expected_check
            )
            if cell_is_pure(target) and target_index not in queued:
                queue.append(target_index)
                queued.add(target_index)
        if consistency_error is not None:  # pragma: no cover - defensive
            break

    residual_table = _residual_snapshot(config, cells)
    if consistency_error is not None:
        return PeelOutcome(
            complete=False,
            peeled=peeled,
            residual=report_residual(residual_table),
            consistency_error=consistency_error,
            residual_table=residual_table,
        )

    if not table.is_zero():
        return PeelOutcome(
            complete=False,
            peeled=peeled,
            residual=report_residual(table),
            residual_table=residual_table,
        )

    return PeelOutcome(complete=True, peeled=peeled)


def report_residual(table: IbltTable) -> ResidualReport:
    nonzero = table.nonzero_indexes()
    return ResidualReport(
        nonzero_cells=len(nonzero),
        total_cells=len(table.cells),
        sum_abs_counts=table.sum_abs_counts(),
        max_abs_count=table.max_abs_count(),
        sample_cell_indexes=tuple(nonzero[:10]),
    )


def _residual_snapshot(config, cells: list[Cell]) -> IbltTable:
    """Copy cells so later work cannot change a reported residual."""
    return IbltTable(
        config, [Cell(c.count, c.key_bytes(), c.check_bytes()) for c in cells]
    )


__all__ = [
    "CHECK_XOR_BYTES",
    "PeelOutcome",
    "PeeledElement",
    "ResidualReport",
    "cell_is_pure",
    "peel",
    "report_residual",
]
