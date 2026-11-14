"""PostgreSQL ``COPY ... WITH (FORMAT binary)`` reader and writer.

Wire format, as implemented by PostgreSQL's ``CopyTo``/``CopyFrom``::

    11 bytes   signature  "PGCOPY\\n\\xff\\r\\n\\x00"
    int32      flags                      -- this subset accepts only 0
    int32      extension header length    -- skipped by declaration
    ...        extension header bytes     -- opaque
    repeated:
      int16    field count                -- -1 terminates the stream
      per field:
        int32  field length               -- -1 means SQL NULL
        ...    field body
    int16      -1 end marker

Everything is network byte order.  Numbers in this module are ``int`` and
are converted with explicit ``to_bytes``/``int.from_bytes``; no value ever
passes through ``float``.

The reader is incremental: it asks the byte source only for the bytes it
needs and works with a source that returns one byte per ``read()``.  It
never assumes that one call yields a whole protocol field.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalogue import Catalogue
from .errors import CopyFormatError, LimitsError, ValueFormatError
from .limits import DEFAULT_READ_CHUNK, Limits
from .reader import ByteReader, Where
from .values import decode_array, decode_scalar, encode_array, encode_scalar

__all__ = [
    "SIGNATURE",
    "CopyHeader",
    "CopyDecoder",
    "CopyEncoder",
    "decode_stream",
    "encode_stream",
]

SIGNATURE = b"PGCOPY\n\xff\r\n\x00"
FLAGS = 0
END_MARKER = -1


@dataclass(frozen=True)
class CopyHeader:
    signature: bytes
    flags: int
    extension_length: int
    #: absolute offset of the first tuple's field count
    data_offset: int


def _header_error(message: str, offset: int, extra: str | None = None) -> CopyFormatError:
    return CopyFormatError(message, offset=offset, extra=extra)


class CopyDecoder:
    """Streaming decoder over any binary file-like object."""

    def __init__(self, source, catalogue: Catalogue,
                 limits: Limits | None = None,
                 chunk_size: int = DEFAULT_READ_CHUNK) -> None:
        self._reader = ByteReader(source, chunk_size=chunk_size)
        self._catalogue = catalogue
        self._limits = limits or Limits()
        self._header: CopyHeader | None = None
        self._started = False
        self._finished = False
        self.tuples_read = 0

    # -- header ---------------------------------------------------------
    def read_header(self) -> CopyHeader:
        if self._header is not None:
            return self._header
        where = Where()
        sig = self._reader.read_exact(len(SIGNATURE), "file signature", where)
        if sig != SIGNATURE:
            raise _header_error(
                f"bad file signature {sig.hex()}; expected {SIGNATURE.hex()}",
                0)
        flags_bytes = self._reader.read_exact(4, "flags", where)
        flags = int.from_bytes(flags_bytes, "big", signed=True)
        if flags != FLAGS:
            raise _header_error(
                f"unsupported COPY flags {flags}; this subset accepts only 0",
                len(SIGNATURE))
        ext_bytes = self._reader.read_exact(
            4, "extension header length", where)
        ext_len = int.from_bytes(ext_bytes, "big", signed=True)
        if ext_len < 0:
            raise _header_error(
                f"negative extension header length {ext_len}",
                len(SIGNATURE) + 4)
        self._reader.skip(ext_len, "extension header", where)
        self._header = CopyHeader(
            signature=sig, flags=flags, extension_length=ext_len,
            data_offset=self._reader.tell())
        return self._header

    # -- tuples ---------------------------------------------------------
    def iter_tuples(self):
        """Yield tuples, each a list of values in catalogue order.

        ``None`` marks SQL NULL.  Every field body is fully validated, so a
        caller that only counts NULLs still rejects corrupt files.
        """
        self.read_header()
        if self._started:
            raise RuntimeError("iter_tuples may be iterated only once")
        self._started = True
        ncols = len(self._catalogue)
        while True:
            tuple_start = self._reader.tell()
            where = Where(line=self.tuples_read + 1)
            raw = self._reader.read_exact(2, "tuple field count", where)
            count = int.from_bytes(raw, "big", signed=True)
            if count == END_MARKER:
                self._finished = True
                extra_offset, extra_count = self._count_trailing()
                if extra_count:
                    raise _header_error(
                        f"{extra_count} extra byte(s) after the end marker",
                        extra_offset)
                return
            if count < 0:
                raise CopyFormatError(
                    f"tuple field count {count} is invalid (only -1 ends the "
                    f"stream)", offset=tuple_start, line=self.tuples_read + 1)
            if count != ncols:
                raise CopyFormatError(
                    f"tuple has {count} field(s) but the catalogue declares "
                    f"{ncols}", offset=tuple_start, line=self.tuples_read + 1)
            self.tuples_read += 1
            values = []
            for i, column in enumerate(self._catalogue.columns):
                fwhere = Where(line=self.tuples_read, field=i + 1,
                               field_name=column.name)
                len_at = self._reader.tell()
                raw_len = self._reader.read_exact(
                    4, f"field {i + 1} ({column.name}) length", fwhere)
                flen = int.from_bytes(raw_len, "big", signed=True)
                if flen == -1:
                    values.append(None)
                    continue
                if flen < -1:
                    raise CopyFormatError(
                        f"field length {flen} is less than -1",
                        offset=len_at, line=self.tuples_read, field=i + 1,
                        field_name=column.name)
                self._limits.check_field_bytes(
                    flen, offset=len_at, line=self.tuples_read,
                    field_index=i + 1, field_name=column.name)
                body_at = self._reader.tell()
                body = self._reader.read_exact(
                    flen, f"field {i + 1} ({column.name}) body", fwhere)
                ctx = _FieldCtx(body_at, self.tuples_read, i + 1,
                                column.name)
                if column.type.element is None:
                    values.append(
                        decode_scalar(column.type.name, body, ctx))
                else:
                    values.append(
                        decode_array(column.type.element.name,
                                     column.type.element.oid, body,
                                     self._limits, ctx))
            yield values

    def _count_trailing(self) -> tuple[int, int]:
        first = self._reader.tell()
        count = 0
        if not self._reader.has_more():
            return first, 0
        # Consume in bounded chunks purely to count; nothing is retained.
        while self._reader.has_more():
            n = min(self._reader.buffered(), DEFAULT_READ_CHUNK)
            self._reader.skip(n, "trailing bytes", Where())
            count += n
        return first, count

    # -- convenience ----------------------------------------------------
    def __iter__(self):
        return self.iter_tuples()


class _FieldCtx:
    __slots__ = ("offset", "line", "field", "field_name")

    def __init__(self, offset: int, line: int, field: int,
                 field_name: str) -> None:
        self.offset = offset
        self.line = line
        self.field = field
        self.field_name = field_name


class CopyEncoder:
    """Streaming writer producing a PostgreSQL-readable binary COPY file."""

    def __init__(self, sink, catalogue: Catalogue,
                 limits: Limits | None = None) -> None:
        self._sink = sink
        self._catalogue = catalogue
        self._limits = limits or Limits()
        self._started = False
        self.tuples_written = 0

    def write_header(self) -> None:
        if self._started:
            return
        self._started = True
        self._sink.write(SIGNATURE)
        self._sink.write(FLAGS.to_bytes(4, "big", signed=True))
        self._sink.write((0).to_bytes(4, "big", signed=True))

    def write_tuple(self, values: list, *, line: int) -> None:
        self.write_header()
        ncols = len(self._catalogue)
        if len(values) != ncols:
            raise ValueFormatError(
                f"record has {len(values)} value(s), catalogue declares "
                f"{ncols}", line=line)
        payload = bytearray()
        payload += ncols.to_bytes(2, "big", signed=True)
        for i, (column, value) in enumerate(
                zip(self._catalogue.columns, values)):
            if value is None:
                payload += (-1).to_bytes(4, "big", signed=True)
                continue
            ctx = _FieldCtx(None, line, i + 1, column.name)
            if column.type.element is None:
                body = encode_scalar(column.type.name, value, ctx)
            else:
                body = encode_array(column.type.element.name,
                                    column.type.element.oid, value,
                                    self._limits, ctx)
            if len(body) > self._limits.max_field_bytes:
                raise LimitsError(
                    f"encoded field is {len(body)} bytes, exceeds "
                    f"max_field_bytes {self._limits.max_field_bytes}",
                    line=line, field=i + 1, field_name=column.name)
            payload += len(body).to_bytes(4, "big", signed=True)
            payload += body
        self._sink.write(bytes(payload))
        self.tuples_written += 1

    def close(self) -> None:
        self.write_header()
        self._sink.write(END_MARKER.to_bytes(2, "big", signed=True))


# --------------------------------------------------------------------------
# Small helpers used by the library/CLI
# --------------------------------------------------------------------------

def decode_stream(source, catalogue: Catalogue, sink,
                  limits: Limits | None = None,
                  chunk_size: int = DEFAULT_READ_CHUNK,
                  on_tuple=None) -> int:
    """Decode a COPY stream to JSONL, writing one line per tuple.

    Returns the number of tuples written.  ``sink`` receives ``bytes``.
    ``on_tuple(values)`` is called for each decoded tuple, if given.
    """
    import json

    decoder = CopyDecoder(source, catalogue, limits=limits,
                          chunk_size=chunk_size)
    count = 0
    for values in decoder.iter_tuples():
        record = {column.name: value
                  for column, value in zip(catalogue.columns, values)}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
        sink.write(line.encode("utf-8") + b"\n")
        count += 1
        if on_tuple is not None:
            on_tuple(values)
    return count


def encode_stream(source, catalogue: Catalogue, sink,
                  limits: Limits | None = None,
                  chunk_size: int = DEFAULT_READ_CHUNK,
                  max_record_bytes: int = 128 * 1024 * 1024) -> int:
    """Encode JSONL ``source`` (binary, UTF-8) into a COPY stream on ``sink``.

    Returns the number of records written.
    """
    from .jsonl import iter_records

    encoder = CopyEncoder(sink, catalogue, limits=limits)
    encoder.write_header()
    count = 0
    for line_no, values in iter_records(
            source, catalogue, chunk_size=chunk_size,
            max_record_bytes=max_record_bytes):
        encoder.write_tuple(values, line=line_no)
        count += 1
    encoder.close()
    return count
