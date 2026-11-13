"""Exact decimal arithmetic for LAS scale/offset based comparisons.

LAS stores coordinates as signed 32-bit integers together with per-axis
``scale`` and ``offset`` doubles.  The physical coordinate of a point is::

    physical = scale * integer + offset

All boundary comparisons in this package are performed with :mod:`decimal`
so that points sitting exactly on a box edge are never lost to binary
floating-point rounding.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Tuple

from .errors import QueryError

# Optional sign, then digits with optional fraction (require at least one
# digit somewhere), then an optional base-10 exponent.  No whitespace, no
# "inf"/"nan", no underscores.
_DECIMAL_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


def float_to_decimal(value: float) -> Decimal:
    """Return the exact shortest decimal value of an IEEE-754 double.

    ``repr(float)`` produces the shortest decimal string that round-trips to
    the same double, so ``Decimal(repr(x))`` is the faithful decimal value
    stored in the LAS header.  It deliberately does *not* use ``Decimal(x)``,
    which would expand the full binary error (e.g. ``0.1`` ->
    ``0.1000000000000000055511151231257827021181583404541015625``).
    """
    if not isinstance(value, float):
        raise TypeError("float_to_decimal expects a float")
    text = repr(value)
    if text in ("inf", "-inf", "nan"):
        raise ValueError("scale/offset must be finite")
    return Decimal(text)


def parse_decimal(value, name: str) -> Decimal:
    """Parse a public decimal query argument.

    Only strings (and plain integers) are accepted: passing a Python ``float``
    is a programming error because its binary approximation would already have
    destroyed the exact boundary the caller asked for.
    """
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool) or isinstance(value, float):
        raise QueryError(
            f"{name} must be given as an exact decimal string, not {type(value).__name__}"
        )
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _DECIMAL_RE.match(text):
            raise QueryError(f"{name} is not a valid finite decimal string: {value!r}")
        parsed = Decimal(text)
    else:
        raise QueryError(f"{name} must be a decimal string, got {type(value).__name__}")
    if not parsed.is_finite():
        raise QueryError(f"{name} must be finite")
    return parsed


def physical_value(integer: int, scale: Decimal, offset: Decimal) -> Decimal:
    """Exact physical coordinate ``scale * integer + offset``."""
    return scale * Decimal(integer) + offset


def closed_int_bounds(
    lo: Decimal, hi: Decimal, scale: Decimal, offset: Decimal
) -> Tuple[int, int] | None:
    """Translate a closed physical interval ``[lo, hi]`` to integer indices.

    Returns ``(k_min, k_max)`` -- the inclusive range of integer coordinates
    whose physical value falls inside the closed interval -- or ``None`` when
    no int32 coordinate can satisfy it.  ``scale`` must be strictly positive.
    """
    if scale <= 0:
        raise QueryError("LAS scale must be strictly positive")
    if lo > hi:
        raise QueryError("lower bound must not be greater than upper bound")
    # 40 digits is far beyond any LAS header precision requirement and keeps
    # the context free of silently rounded divisions.
    with localcontext() as ctx:
        ctx.prec = 50
        k_min_exact = (lo - offset) / scale
        k_max_exact = (hi - offset) / scale
    k_min = int(k_min_exact.to_integral_value(rounding=ROUND_CEILING))
    k_max = int(k_max_exact.to_integral_value(rounding=ROUND_FLOOR))
    k_min = max(k_min, INT32_MIN)
    k_max = min(k_max, INT32_MAX)
    if k_min > k_max:
        return None
    return k_min, k_max
