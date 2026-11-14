"""UTF-8 JSONL records: one tuple per line, fields in catalogue order.

Decoding writes exactly one compact JSON object per tuple, keys in catalogue
order.  Encoding accepts one JSON object per line, requires exactly the
catalogue's field names (order in the file is irrelevant, output order is
the catalogue's), and rejects unknown keys, missing keys and duplicate keys.

JSON is used only as a *carrier* for already-exact values:

* integers stay Python ``int`` (arbitrary precision);
* ``numeric`` is an object of integers, or an exact decimal string;
* timestamps/dates are integer counts, never calendar strings;
* JSON floating-point literals are rejected everywhere in a record, so no
  value can silently acquire a binary floating-point approximation;
* ``NaN``/``Infinity`` JSON bare literals are rejected; the only accepted
  spelling for numeric specials is the quoted string.
"""

from __future__ import annotations

import json

from .errors import RecordError

__all__ = ["LineReader", "iter_records", "parse_record"]


class LineReader:
    """Incremental line reader over any binary stream.

    Works with a source whose ``read()`` returns short chunks (including a
    single byte).  A single line is bounded by ``max_line_bytes``; the bound
    is applied while accumulating, so a gigantic line is rejected before it
    is materialised in full.
    """

    def __init__(self, source, chunk_size: int = 65536,
                 max_line_bytes: int = 128 * 1024 * 1024) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        self._source = source
        self._chunk = chunk_size
        self._max = max_line_bytes
        self._buf = b""
        self._pos = 0
        self._base = 0      # absolute offset of self._buf[0]
        self._read = 0      # bytes requested from the source (debug aid)
        self._line_no = 0

    def __iter__(self):
        return self

    def __next__(self) -> tuple[int, bytes, int]:
        while True:
            idx = self._buf.find(b"\n", self._pos)
            if idx >= 0:
                if idx - self._pos > self._max:
                    raise RecordError(
                        f"line exceeds max_record_bytes {self._max}",
                        offset=self._base + self._pos,
                        line=self._line_no + 1)
                start = self._base + self._pos
                line = self._buf[self._pos:idx]
                self._pos = idx + 1
                self._line_no += 1
                if line.endswith(b"\r"):
                    line = line[:-1]
                return self._line_no, line, start
            pending = self._buf[self._pos:]
            if len(pending) > self._max:
                raise RecordError(
                    f"line exceeds max_record_bytes {self._max}",
                    offset=self._base + self._pos, line=self._line_no + 1)
            self._buf = pending
            self._base += self._pos
            self._pos = 0
            data = self._source.read(self._chunk)
            if not data:
                if not self._buf:
                    raise StopIteration
                start = self._base
                line = self._buf
                self._buf = b""
                self._pos = 0
                self._base += len(line)
                self._line_no += 1
                if line.endswith(b"\r"):
                    line = line[:-1]
                return self._line_no, line, start
            self._buf += data


class _RejectFloat:
    """Marker for error text; raised via json's parse_float hook."""

    def __init__(self, text: str) -> None:
        self.text = text


def _parse_float(text: str):
    # json calls this for every float literal.  Returning a non-float marker
    # lets validation produce one precise error instead of guessing.
    return _RejectFloat(text)


def _parse_constant(text: str):
    return _RejectFloat(text)


def _pairs(pairs):
    seen = set()
    out = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
        out[key] = value
    return out


def _find_float(value, path: str) -> str | None:
    if isinstance(value, _RejectFloat):
        return f"{path}: JSON floating-point literal {value.text!r} is not " \
               f"allowed; use an integer or an exact decimal string"
    if isinstance(value, list):
        for i, item in enumerate(value):
            found = _find_float(item, f"{path}[{i}]")
            if found:
                return found
    elif isinstance(value, dict):
        for key, item in value.items():
            found = _find_float(item, f"{path}.{key}" if path else key)
            if found:
                return found
    return None


def parse_record(line: bytes, catalogue, line_no: int) -> list:
    """Parse and validate one JSONL line into a list in catalogue order."""
    if not line.strip():
        raise RecordError("blank line; every line must hold one tuple",
                          line=line_no)
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecordError(
            f"line is not valid UTF-8: {exc.reason} at byte {exc.start}",
            line=line_no) from None
    try:
        obj = json.loads(text, parse_float=_parse_float,
                         parse_constant=_parse_constant,
                         object_pairs_hook=_pairs)
    except json.JSONDecodeError as exc:
        raise RecordError(
            f"invalid JSON: {exc.msg} at column {exc.colno} (byte "
            f"{exc.pos})", line=line_no) from None
    except ValueError as exc:
        raise RecordError(str(exc), line=line_no) from None

    if not isinstance(obj, dict):
        raise RecordError(
            f"record must be a JSON object, got {type(obj).__name__}",
            line=line_no)
    found = _find_float(obj, "")
    if found:
        raise RecordError(found, line=line_no)

    expected = set(catalogue.names)
    actual = set(obj)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise RecordError(
            f"missing field(s) {missing}; catalogue has "
            f"{list(catalogue.names)}", line=line_no)
    if extra:
        raise RecordError(
            f"unknown field(s) {extra}; catalogue has "
            f"{list(catalogue.names)}", line=line_no)
    return [obj[column.name] for column in catalogue.columns]


def iter_records(source, catalogue, chunk_size: int = 65536,
                 max_record_bytes: int = 128 * 1024 * 1024):
    """Yield ``(line_no, values)`` for each non-empty logical record."""
    reader = LineReader(source, chunk_size=chunk_size,
                        max_line_bytes=max_record_bytes)
    for line_no, line, _offset in reader:
        yield line_no, parse_record(line, catalogue, line_no)
