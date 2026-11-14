"""Mask rules: whole-word IPv4, UUID and signed integer matching.

A rule applies only when the **entire** word matches; ``10.0.0.1;`` or
``x10.0.0.1`` remains literal.  The rule set is chosen per library and
persisted; templates record the concrete mask kind so a later library with a
different enabled set cannot silently reinterpret earlier items.

Rule details
------------
``INT``   optional ``+``/``-`` then one or more decimal digits, ASCII only.
          ``007`` is an integer (the sign is accepted; leading zeros are a
          value spelling, not a range issue).  ``1_000``, ``1.0``, ``0x10``,
          ``١٢`` (non-ASCII digits) and bare ``+``/``-`` are literal.
``UUID``  canonical 8-4-4-4-12 hexadecimal form with hyphens, case-insensitive.
          Braces, ``urn:`` prefixes, missing or extra hyphens, non-hex letters
          are literal.
``IPV4``  four dot-separated decimal octets, each 0..255, canonical spelling:
          no leading zeros beyond ``0`` itself, no sign, no whitespace.
          ``256.1.1.1``, ``1.2.3``, ``1.2.3.4.5``, ``01.2.3.4`` and
          ``1.2.3.4.`` are literal.  Range validity is checked numerically.

Ordering is fixed by ``MASK_KINDS`` and never derived from a ``set``.
"""

from __future__ import annotations

import re

IPV4 = "IPV4"
UUID = "UUID"
INT = "INT"

MASK_KINDS = (IPV4, UUID, INT)
SUPPORTED_MASK_KINDS = frozenset(MASK_KINDS)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_INT_RE = re.compile(r"[+-]?[0-9]+")
_IPV4_RE = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")


def is_int(word: str) -> bool:
    return _INT_RE.fullmatch(word) is not None


def is_uuid(word: str) -> bool:
    return _UUID_RE.fullmatch(word) is not None


def is_ipv4(word: str) -> bool:
    if _IPV4_RE.fullmatch(word) is None:
        return False
    return all(int(part) <= 255 for part in word.split("."))


_RULES = {IPV4: is_ipv4, UUID: is_uuid, INT: is_int}


def classify(word: str, enabled: frozenset[str]) -> str | None:
    """Return the first matching enabled mask kind in fixed order, else None."""
    for kind in MASK_KINDS:
        if kind in enabled and _RULES[kind](word):
            return kind
    return None


def validate_enabled(enabled) -> frozenset[str]:
    """Canonicalise an enabled-rule selection.

    ``None`` means "all rules".  Accepts an iterable of names or a
    comma-separated string.  Unknown names raise; duplicates collapse.
    """
    if enabled is None:
        return frozenset(MASK_KINDS)
    if isinstance(enabled, str):
        names = [] if enabled == "" else enabled.split(",")
    else:
        names = list(enabled)
    selected = set()
    for name in names:
        token = str(name).strip()
        if not token:
            continue
        if token not in SUPPORTED_MASK_KINDS:
            raise ValueError(f"unsupported mask rule {token!r}")
        selected.add(token)
    return frozenset(k for k in MASK_KINDS if k in selected)


def enabled_text(enabled: frozenset[str]) -> str:
    return ",".join(k for k in MASK_KINDS if k in enabled)


def enabled_from_text(text: str) -> frozenset[str]:
    return validate_enabled(text)


# Rendering constant for a learned wildcard; a literal word equal to this
# string stays a literal (see templates.Item.render).
WILDCARD_RENDER = "<*>"
