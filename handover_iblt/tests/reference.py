"""Brute-force reference for differential tests.

The reference works on explicit element sets and is written independently of
the IBLT: it only knows ``(uuid_bytes, fingerprint_bytes)``.

The tool's job is a *symmetric difference on identifiers grouped by UUID*, so
the reference gives:

* ``left_only``  — UUID and fingerprint present only on the left;
* ``right_only`` — UUID and fingerprint present only on the right;
* ``changed``    — UUID present on both sides with different fingerprints.

Duplicates are collapsed by exact identity before the comparison, exactly as
the documented deduplication rule requires.
"""

from __future__ import annotations


def normalise(elements):
    """Collapse identical identities; keep one fingerprint per UUID.

    Raises ``ValueError`` if one UUID carries two fingerprints, mirroring the
    checklist-conflict rule.
    """
    by_uuid: dict[bytes, bytes] = {}
    for uuid_b, fp_b in elements:
        previous = by_uuid.get(uuid_b)
        if previous is not None and previous != fp_b:
            raise ValueError("conflicting fingerprints for one uuid")
        by_uuid[uuid_b] = fp_b
    return by_uuid


def expected_differences(left_elements, right_elements):
    left = normalise(left_elements)
    right = normalise(right_elements)
    left_only = [
        (uuid_b, left[uuid_b])
        for uuid_b in sorted(left)
        if uuid_b not in right
    ]
    right_only = [
        (uuid_b, right[uuid_b])
        for uuid_b in sorted(right)
        if uuid_b not in left
    ]
    changed = [
        (uuid_b, left[uuid_b], right[uuid_b])
        for uuid_b in sorted(left)
        if uuid_b in right and left[uuid_b] != right[uuid_b]
    ]
    return {"left_only": left_only, "right_only": right_only,
            "changed": changed}


def flatten(result):
    """Turn a tool-style result into the reference's three lists."""
    left_only = [(bytes.fromhex(d.uuid_hex), d.left_fingerprint)
                 for d in result.left_only]
    right_only = [(bytes.fromhex(d.uuid_hex), d.right_fingerprint)
                  for d in result.right_only]
    changed = [
        (bytes.fromhex(d.uuid_hex), d.left_fingerprint, d.right_fingerprint)
        for d in result.changed
    ]
    return {
        "left_only": sorted(left_only),
        "right_only": sorted(right_only),
        "changed": sorted(changed),
    }
