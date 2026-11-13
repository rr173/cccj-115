"""Comparison of two digests and the human-facing difference report.

Directions are always explicit and always relative to the two *arguments*:

* left digest  = the party whose checklist is the reference;
* right digest = the party it is compared with.

``compare_digests(left, right)`` reports

* ``LEFT_ONLY``       — present in left, absent from right;
* ``RIGHT_ONLY``      — present in right, absent from left;
* ``CONTENT_CHANGED`` — same 16-byte identifier on both sides but a different
  32-byte fingerprint; both fingerprints are shown together, never as two
  unrelated one-sided rows.

Swapping the arguments therefore swaps every ``LEFT_ONLY`` with
``RIGHT_ONLY``; ``CONTENT_CHANGED`` keeps the pair, with the fingerprints
swapped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from .digest import Digest
from .errors import DigestMismatchError, UndecodedError
from .peel import PeeledElement, ResidualReport, peel


class DifferenceKind(str, Enum):
    LEFT_ONLY = "LEFT_ONLY"
    RIGHT_ONLY = "RIGHT_ONLY"
    CONTENT_CHANGED = "CONTENT_CHANGED"


@dataclass(frozen=True)
class Difference:
    kind: DifferenceKind
    uuid: bytes
    left_fingerprint: bytes | None = None
    right_fingerprint: bytes | None = None

    @property
    def uuid_hex(self) -> str:
        return self.uuid.hex()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "uuid": self.uuid_hex,
            "left_fingerprint": (
                self.left_fingerprint.hex()
                if self.left_fingerprint is not None
                else None
            ),
            "right_fingerprint": (
                self.right_fingerprint.hex()
                if self.right_fingerprint is not None
                else None
            ),
        }


@dataclass
class ReconcileResult:
    """Outcome of comparing two digests.

    ``complete=True`` binds ``differences`` to a fully peeled table: the list
    is exactly the difference between the two checklists.  ``complete=False``
    means nothing about the difference may be claimed; ``peeled_clues`` and
    ``residual`` are diagnostics only, and ``differences`` stays empty.
    """

    complete: bool
    left_name: str
    right_name: str
    left_config: dict
    right_config: dict
    left_unique: int
    right_unique: int
    differences: list[Difference] = field(default_factory=list)
    peeled_clues: list[PeeledElement] = field(default_factory=list)
    residual: ResidualReport | None = None
    message: str = ""
    peeled_count: int = 0

    @property
    def left_only(self) -> list[Difference]:
        return [d for d in self.differences if d.kind is DifferenceKind.LEFT_ONLY]

    @property
    def right_only(self) -> list[Difference]:
        return [d for d in self.differences if d.kind is DifferenceKind.RIGHT_ONLY]

    @property
    def changed(self) -> list[Difference]:
        return [
            d for d in self.differences
            if d.kind is DifferenceKind.CONTENT_CHANGED
        ]

    def as_dict(self) -> dict:
        return {
            "complete": self.complete,
            "left": self.left_name,
            "right": self.right_name,
            "left_unique_elements": self.left_unique,
            "right_unique_elements": self.right_unique,
            "configuration": {
                "left": self.left_config,
                "right": self.right_config,
            },
            "counts": {
                "left_only": len(self.left_only),
                "right_only": len(self.right_only),
                "content_changed": len(self.changed),
                "differences": len(self.differences),
                "peeled": self.peeled_count,
            },
            "differences": [d.as_dict() for d in self.differences],
            "peeled_clues": [
                {
                    "sign": c.sign,
                    "uuid": c.uuid.hex(),
                    "fingerprint": c.fingerprint.hex(),
                }
                for c in self.peeled_clues
            ],
            "residual": self.residual.as_dict() if self.residual else None,
            "message": self.message,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


def compare_digests(left: Digest, right: Digest, *,
                    left_name: str = "sender",
                    right_name: str = "receiver") -> ReconcileResult:
    """Compare ``left`` against ``right`` without touching either digest.

    Digests built with different capacity/k/seed are not comparable and raise
    :class:`DigestMismatchError`; neither digest is modified in that case.
    """
    if left.config != right.config:
        raise DigestMismatchError(
            "digests use different configurations and cannot be compared: "
            f"left={left.config.as_dict()} right={right.config.as_dict()}; "
            "rebuild both from their original checklists with one configuration"
        )

    diff_table = left.table.subtract(right.table)
    expected_net = left.unique_elements - right.unique_elements
    if diff_table.total_count() != diff_table.config.k * expected_net:
        return ReconcileResult(
            complete=False,
            left_name=left_name,
            right_name=right_name,
            left_config=left.config.as_dict(),
            right_config=right.config.as_dict(),
            left_unique=left.unique_elements,
            right_unique=right.unique_elements,
            message=(
                "digest headers disagree with their own cell counts; "
                "no difference list is claimed"
            ),
        )
    outcome = peel(diff_table)

    if outcome.consistency_error is not None:
        return ReconcileResult(
            complete=False,
            left_name=left_name,
            right_name=right_name,
            left_config=left.config.as_dict(),
            right_config=right.config.as_dict(),
            left_unique=left.unique_elements,
            right_unique=right.unique_elements,
            peeled_clues=list(outcome.peeled),
            residual=outcome.residual,
            message=(
                "peeling aborted as inconsistent: "
                f"{outcome.consistency_error}; no difference list is claimed"
            ),
            peeled_count=len(outcome.peeled),
        )

    if not outcome.complete:
        residual = outcome.residual
        cells = (
            f"{residual.nonzero_cells}/{residual.total_cells}"
            if residual else "?"
        )
        return ReconcileResult(
            complete=False,
            left_name=left_name,
            right_name=right_name,
            left_config=left.config.as_dict(),
            right_config=right.config.as_dict(),
            left_unique=left.unique_elements,
            right_unique=right.unique_elements,
            peeled_clues=list(outcome.peeled),
            residual=residual,
            message=(
                f"peeling did not reach an empty table: {cells} cells remain "
                f"(sum|count|={residual.sum_abs_counts if residual else '?'}, "
                f"max|count|={residual.max_abs_count if residual else '?'}); "
                "no complete difference list is available. Increase capacity "
                "and rebuild both digests from the original checklists."
            ),
            peeled_count=len(outcome.peeled),
        )

    differences = pair_differences(outcome.peeled)
    net = sum(item.sign for item in outcome.peeled)
    if net != expected_net:
        return ReconcileResult(
            complete=False,
            left_name=left_name,
            right_name=right_name,
            left_config=left.config.as_dict(),
            right_config=right.config.as_dict(),
            left_unique=left.unique_elements,
            right_unique=right.unique_elements,
            peeled_clues=list(outcome.peeled),
            message=(
                f"peeled net count {net} does not match the header net "
                f"{expected_net}; no difference list is claimed"
            ),
            peeled_count=len(outcome.peeled),
        )

    return ReconcileResult(
        complete=True,
        left_name=left_name,
        right_name=right_name,
        left_config=left.config.as_dict(),
        right_config=right.config.as_dict(),
        left_unique=left.unique_elements,
        right_unique=right.unique_elements,
        differences=differences,
        peeled_count=len(outcome.peeled),
        message=(
            "reconciliation complete: table fully peeled to zero"
            if differences
            else "reconciliation complete: checklists are identical"
        ),
    )


def pair_differences(peeled: list[PeeledElement]) -> list[Difference]:
    """Group peeled elements by identifier and assign explicit directions.

    Sorting is by UUID only, so the output does not depend on the order in
    which cells happened to peel.
    """
    by_uuid: dict[bytes, list[PeeledElement]] = {}
    for item in peeled:
        by_uuid.setdefault(item.uuid, []).append(item)

    differences: list[Difference] = []
    for uuid_b, items in by_uuid.items():
        if len(items) == 1:
            item = items[0]
            if item.sign > 0:
                differences.append(
                    Difference(
                        DifferenceKind.LEFT_ONLY,
                        uuid_b,
                        left_fingerprint=item.fingerprint,
                    )
                )
            else:
                differences.append(
                    Difference(
                        DifferenceKind.RIGHT_ONLY,
                        uuid_b,
                        right_fingerprint=item.fingerprint,
                    )
                )
            continue

        if len(items) == 2:
            signs = sorted(item.sign for item in items)
            if signs != [-1, 1]:
                # Two elements with the same sign for one identifier: the
                # input was not a set, so the result cannot be trusted.
                raise UndecodedError(
                    f"identifier {uuid_b.hex()} peeled into two elements "
                    f"with signs {signs}; expected one element per side"
                )
            left_fp = next(i.fingerprint for i in items if i.sign > 0)
            right_fp = next(i.fingerprint for i in items if i.sign < 0)
            if left_fp == right_fp:
                # Identical elements cancel exactly in the difference table
                # and cannot survive peeling; this would be contradictory.
                raise UndecodedError(
                    f"element {uuid_b.hex()} was recovered twice with "
                    "identical fingerprints and opposite signs"
                )
            differences.append(
                Difference(
                    DifferenceKind.CONTENT_CHANGED,
                    uuid_b,
                    left_fingerprint=left_fp,
                    right_fingerprint=right_fp,
                )
            )
            continue

        raise UndecodedError(
            f"identifier {uuid_b.hex()} peeled into {len(items)} elements; "
            "the checklists are not sets or the digests cannot be trusted"
        )

    differences.sort(key=lambda d: d.uuid)
    return differences


def format_result(result: ReconcileResult, *, verbose: bool = False) -> str:
    """Render a reconciliation result as stable, direction-explicit text."""
    lines: list[str] = []
    lines.append(
        f"left  ({result.left_name}): {result.left_unique} unique elements"
    )
    lines.append(
        f"right ({result.right_name}): {result.right_unique} unique elements"
    )
    lines.append("configuration: " + json.dumps(result.left_config, sort_keys=True))
    if not result.complete:
        lines.append("STATUS: INCOMPLETE — no difference list is claimed")
        lines.append(f"  {result.message}")
        if result.peeled_clues:
            lines.append(
                "  peeled clues (diagnostic only, not a complete list): "
                f"{len(result.peeled_clues)}"
            )
            if verbose:
                for clue in result.peeled_clues:
                    direction = "left" if clue.sign > 0 else "right"
                    lines.append(
                        f"    clue {direction} uuid={clue.uuid.hex()} "
                        f"fp={clue.fingerprint.hex()}"
                    )
        if result.residual is not None:
            r = result.residual
            lines.append(
                f"  residual: {r.nonzero_cells}/{r.total_cells} nonzero cells, "
                f"sum|count|={r.sum_abs_counts}, max|count|={r.max_abs_count}"
            )
            lines.append(
                "  residual cell indexes (first 10): "
                + ",".join(str(i) for i in r.sample_cell_indexes)
            )
        return "\n".join(lines)

    lines.append("STATUS: COMPLETE — table fully peeled to zero")
    lines.append(
        f"differences: {len(result.differences)} "
        f"(LEFT_ONLY={len(result.left_only)}, "
        f"RIGHT_ONLY={len(result.right_only)}, "
        f"CONTENT_CHANGED={len(result.changed)})"
    )
    for diff in result.differences:
        if diff.kind is DifferenceKind.LEFT_ONLY:
            lines.append(
                f"  LEFT_ONLY        uuid={diff.uuid_hex} "
                f"left_fingerprint={diff.left_fingerprint.hex()} "
                "(present on left only)"
            )
        elif diff.kind is DifferenceKind.RIGHT_ONLY:
            lines.append(
                f"  RIGHT_ONLY       uuid={diff.uuid_hex} "
                f"right_fingerprint={diff.right_fingerprint.hex()} "
                "(present on right only)"
            )
        else:
            lines.append(
                f"  CONTENT_CHANGED  uuid={diff.uuid_hex} "
                f"left_fingerprint={diff.left_fingerprint.hex()} "
                f"right_fingerprint={diff.right_fingerprint.hex()} "
                "(same identifier, different content)"
            )
    return "\n".join(lines)


__all__ = [
    "Difference",
    "DifferenceKind",
    "ReconcileResult",
    "compare_digests",
    "format_result",
    "pair_differences",
]
