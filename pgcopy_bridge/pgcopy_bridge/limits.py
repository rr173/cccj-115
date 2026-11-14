"""Caller-tunable resource limits, checked before large allocations.

Every limit is a plain integer in the CLI (``--max-field-bytes`` etc.) and a
field on :class:`Limits` in the API.  The checks happen on *declared* sizes
from the wire header, before the corresponding body is read or built, so a
lying header is rejected without allocating it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LimitsError

#: Maximum declared byte count for one field body.
DEFAULT_MAX_FIELD_BYTES = 64 * 1024 * 1024
#: Maximum number of dimensions in one array value.
DEFAULT_MAX_ARRAY_DIMS = 6  # PostgreSQL's MAXDIM
#: Maximum number of elements in one array value.
DEFAULT_MAX_ARRAY_ELEMENTS = 4_000_000
#: Default read buffer when the caller does not pick one.
DEFAULT_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Limits:
    max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES
    max_array_dims: int = DEFAULT_MAX_ARRAY_DIMS
    max_array_elements: int = DEFAULT_MAX_ARRAY_ELEMENTS

    def __post_init__(self) -> None:
        for name in ("max_field_bytes", "max_array_dims",
                     "max_array_elements"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def check_field_bytes(self, n: int, *, offset: int, line: int = -1,
                          field_index: int = -1,
                          field_name: str | None = None) -> None:
        if n < 0:
            raise LimitsError(
                f"declared field length {n} is negative",
                offset=offset, line=line if line >= 0 else None,
                field=field_index if field_index >= 0 else None,
                field_name=field_name)
        if n > self.max_field_bytes:
            raise LimitsError(
                f"declared field length {n} exceeds max_field_bytes "
                f"{self.max_field_bytes}",
                offset=offset, line=line if line >= 0 else None,
                field=field_index if field_index >= 0 else None,
                field_name=field_name)

    def check_array_dims(self, n: int, *, offset: int, line: int = -1,
                         field_index: int = -1,
                         field_name: str | None = None) -> None:
        if n < 0 or n > self.max_array_dims:
            raise LimitsError(
                f"array declares {n} dimension(s), max_array_dims is "
                f"{self.max_array_dims}",
                offset=offset, line=line if line >= 0 else None,
                field=field_index if field_index >= 0 else None,
                field_name=field_name)

    def check_array_elements(self, n: int, *, offset: int, line: int = -1,
                             field_index: int = -1,
                             field_name: str | None = None) -> None:
        if n > self.max_array_elements:
            raise LimitsError(
                f"array declares {n} element(s), max_array_elements is "
                f"{self.max_array_elements}",
                offset=offset, line=line if line >= 0 else None,
                field=field_index if field_index >= 0 else None,
                field_name=field_name)

    def describe(self) -> list[tuple[str, int]]:
        return [
            ("max_field_bytes", self.max_field_bytes),
            ("max_array_dims", self.max_array_dims),
            ("max_array_elements", self.max_array_elements),
        ]
