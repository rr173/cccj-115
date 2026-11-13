"""Exception hierarchy for the handover reconciliation module."""

from __future__ import annotations


class HandoverError(Exception):
    """Base class for every error raised by this package."""


class ChecklistError(HandoverError):
    """A local checklist line cannot be turned into an element.

    Raised while the digest is being built; nothing is written in that case.
    """


class ChecklistConflictError(ChecklistError):
    """One identifier occurs with two different fingerprints in one checklist.

    Both input locations are reported and digest construction is refused.
    """


class DigestFormatError(HandoverError):
    """A digest file is missing, truncated, malformed or has been altered."""


class DigestMismatchError(HandoverError):
    """Two digests use different configurations and cannot be compared.

    Neither digest is modified by a failed comparison.
    """


class PeelingError(HandoverError):
    """Peeling stopped; the remaining cells are reported, not a “complete”
    difference list."""


class UndecodedError(PeelingError):
    """The difference table could not be fully peeled.

    ``residual_cells`` and ``residual_elements`` describe only the unpacked
    rest; the raised error never masquerades as a successful reconciliation.
    """
