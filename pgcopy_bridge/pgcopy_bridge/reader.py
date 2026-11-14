"""Byte reader that never assumes one ``read()`` returns a whole field.

The reader wraps any binary stream (including a pipe on stdin, which may
return one byte at a time) and exposes exact-count reads plus an absolute
byte position.  It buffers at most ``chunk_size`` bytes for small reads; a
single field body larger than the chunk size is accumulated directly into a
bytearray of its declared (already limit-checked) length.
"""

from __future__ import annotations

from .errors import CopyFormatError

__all__ = ["ByteReader", "Where"]


class Where:
    """Location of the value currently being read, for error messages."""

    __slots__ = ("line", "field", "field_name")

    def __init__(self, line: int = -1, field: int = -1,
                 field_name: str | None = None) -> None:
        self.line = line
        self.field = field
        self.field_name = field_name

    def child(self, field: int, field_name: str) -> "Where":
        return Where(self.line, field, field_name)


class ByteReader:
    def __init__(self, raw, chunk_size: int = 65536) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        self._raw = raw
        self._chunk = chunk_size
        self._buf = b""
        self._pos = 0
        self._offset = 0

    # -- position -------------------------------------------------------
    def tell(self) -> int:
        """Absolute offset of the next unread byte."""
        return self._offset

    def buffered(self) -> int:
        return len(self._buf) - self._pos

    # -- internals ------------------------------------------------------
    def _compact(self) -> None:
        if self._pos:
            self._buf = self._buf[self._pos:]
            self._pos = 0

    def _truncated(self, what: str, start: int, where: Where,
                   wanted: int, got: int) -> CopyFormatError:
        return CopyFormatError(
            f"truncated input: {what} needs {wanted} byte(s), "
            f"only {got} available",
            offset=start,
            line=where.line,
            field=where.field if where.field >= 0 else None,
            field_name=where.field_name,
            extra=f"stream ends at byte {self._offset}",
        )

    def _check(self, have: int, n: int, what: str, start: int,
               where: Where) -> None:
        if have < n:
            raise self._truncated(what, start, where, n, have)

    # -- reading --------------------------------------------------------
    def read_exact(self, n: int, what: str, where: Where) -> bytes:
        """Read exactly ``n`` bytes or raise a located truncation error."""
        if n == 0:
            return b""
        start = self._offset
        have = len(self._buf) - self._pos
        if have >= n:
            out = self._buf[self._pos:self._pos + n]
            self._pos += n
            self._offset += n
            return out

        pieces: list[bytes] | None = None
        got = 0
        if have:
            pieces = [self._buf[self._pos:]]
            got = have
            self._pos = len(self._buf)
            self._offset += have
        self._buf = b""
        self._pos = 0
        while got < n:
            want = min(self._chunk, n - got)
            data = self._raw.read(want)
            if not data:
                self._check(got, n, what, start, where)
            if pieces is None:
                pieces = []
            pieces.append(data)
            got += len(data)
            self._offset += len(data)
        return b"".join(pieces)

    def skip(self, n: int, what: str, where: Where) -> None:
        """Discard exactly ``n`` bytes without allocating them."""
        start = self._offset
        remaining = n
        have = self.buffered()
        if have:
            take = min(have, remaining)
            self._pos += take
            self._offset += take
            remaining -= take
        self._buf = b""
        self._pos = 0
        while remaining:
            want = min(self._chunk, remaining)
            data = self._raw.read(want)
            if not data:
                raise self._truncated(what, start, where, n, n - remaining)
            remaining -= len(data)
            self._offset += len(data)

    def has_more(self) -> bool:
        """True if at least one more byte is available (buffers it)."""
        if self.buffered():
            return True
        data = self._raw.read(self._chunk)
        if not data:
            return False
        self._buf = data
        self._pos = 0
        return True

    def read_byte(self, what: str, where: Where) -> bytes:
        return self.read_exact(1, what, where)
