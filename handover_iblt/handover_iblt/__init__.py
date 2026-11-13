"""Reconciling handover of specimen checklists with invertible Bloom filters.

Two parties each derive a small, fixed-size digest from their local checklist
and exchange only that digest.  Peeling the cell-wise difference of the two
digests recovers a bounded number of differing entries, which are reported as
direction-tagged rows: only sender, only receiver, or same identifier with a
changed content fingerprint.

Nothing in this package reads, prints or stores specimen payloads; an element
is exactly a 16-byte UUID plus a 32-byte SHA-256 fingerprint.
"""

from __future__ import annotations

from .config import (
    PROTOCOL_VERSION,
    DigestConfig,
    default_config,
)
from .builder import BuildOutcome, build_from_checklist
from .digest import (
    Digest,
    build_digest_from_store,
    compare_digest_files,
    load_digest,
    parse_digest_bytes,
    save_digest,
)
from .errors import (
    ChecklistError,
    DigestFormatError,
    DigestMismatchError,
    HandoverError,
    PeelingError,
    UndecodedError,
)
from .reconcile import (
    Difference,
    DifferenceKind,
    ReconcileResult,
    ResidualReport,
    compare_digests,
    format_result,
)
from .store import ChecklistStore

__all__ = [
    "BuildOutcome",
    "ChecklistConflictError",
    "ChecklistError",
    "ChecklistStore",
    "Difference",
    "DifferenceKind",
    "Digest",
    "DigestConfig",
    "DigestFormatError",
    "DigestMismatchError",
    "HandoverError",
    "PROTOCOL_VERSION",
    "PeelingError",
    "ReconcileResult",
    "ResidualReport",
    "UndecodedError",
    "build_digest_from_store",
    "build_from_checklist",
    "compare_digest_files",
    "compare_digests",
    "default_config",
    "format_result",
    "load_digest",
    "parse_digest_bytes",
    "save_digest",
]
