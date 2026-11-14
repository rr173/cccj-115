"""Failure types that always carry a byte offset and a tuple/field location.

All parsing code raises a :class:`PgCopyError` subclass.  The rendered
message format is stable because tests match on it:

    <kind>: <message> (byte <offset>, line <n>, field <i> <name>)

``offset`` is a 0-based byte offset in the COPY stream, ``line`` is the
1-based tuple number for COPY input and the 1-based JSONL line for encode
input, and ``field`` is the 1-based column.  Any part that is not known at
the failure point is omitted.
"""

from __future__ import annotations


class PgCopyError(Exception):
    """Base class for every error this package reports."""

    kind = "pgcopy"

    def __init__(
        self,
        message: str,
        *,
        offset: int | None = None,
        line: int | None = None,
        field: int | None = None,
        field_name: str | None = None,
        extra: str | None = None,
    ) -> None:
        self.message = message
        self.offset = offset
        self.line = line
        self.field = field
        self.field_name = field_name
        self.extra = extra
        super().__init__(self.render())

    def render(self) -> str:
        loc: list[str] = []
        if self.offset is not None:
            loc.append(f"byte {self.offset}")
        if self.line is not None:
            loc.append(f"line {self.line}")
        if self.field is not None:
            name = f" {self.field_name}" if self.field_name else ""
            loc.append(f"field {self.field}{name}")
        text = f"{self.kind}: {self.message}"
        if self.extra:
            text += f" [{self.extra}]"
        if loc:
            text += f" ({', '.join(loc)})"
        return text

    def __str__(self) -> str:
        return self.render()


class DescriptorError(PgCopyError):
    """The column catalogue is malformed or unsupported."""

    kind = "descriptor"


class CopyFormatError(PgCopyError):
    """The COPY binary stream itself violates the protocol."""

    kind = "copy"


class ValueFormatError(PgCopyError):
    """A field body does not encode the declared PostgreSQL type."""

    kind = "value"


class LimitsError(PgCopyError):
    """A declared size exceeds the caller-supplied safety limit."""

    kind = "limit"


class RecordError(PgCopyError):
    """A JSONL logical record is malformed or does not match the catalogue."""

    kind = "record"


__all__ = [
    "PgCopyError",
    "DescriptorError",
    "CopyFormatError",
    "ValueFormatError",
    "LimitsError",
    "RecordError",
]
