"""Streaming table parser for the supported DwC-A dialect.

Memory boundary: one *logical* record is materialised at a time.  The reader
is a generator over a text wrapper of the ZIP member stream; neither the whole
member nor the whole archive is decoded into memory.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from typing import Iterator, Optional

from .errors import ArchiveDataError
from .models import (
    CellValue,
    DataRow,
    ORIGIN_DEFAULT,
    ORIGIN_EMPTY,
    ORIGIN_SOURCE,
)
from .models import TableSpec

# Producers sometimes store very long text blobs (e.g. embedded JSON/media
# references).  Lift csv's 128 KiB per-field guard to the interpreter limit.
csv.field_size_limit(sys.maxsize)


class _HeaderSkipper:
    """Wrap a newline='' text stream and swallow the first ``n`` physical
    lines before the csv module reads anything.

    The csv module consumes its input through the iterator protocol
    (``next()``), so the skipped lines are dropped in ``__next__`` rather
    than via ``readline``.  Header skipping counts physical lines
    delimited by the line terminator, *before* CSV quoting is considered --
    matching the DwC-A meaning of ``ignoreHeaderLines``.
    """

    def __init__(self, stream: io.TextIOBase, n: int):
        self._stream = stream
        self._remaining = n
        self._drained = False

    def _drain_header(self) -> None:
        # Runs on the first read only.  The header lines are consumed here;
        # the caller's own read then returns the first *data* line, so a
        # drain never swallows an extra record.
        if self._drained:
            return
        self._drained = True
        while self._remaining > 0:
            line = self._stream.readline()
            self._remaining -= 1
            if line == "":  # EOF inside the declared header
                break

    def readline(self) -> str:
        # CPython's C csv reader drives its input through readline().
        self._drain_header()
        return self._stream.readline()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        self._drain_header()
        return next(self._stream)

    def close(self) -> None:
        self._stream.close()


def _open_member(zf: zipfile.ZipFile, spec: TableSpec) -> io.TextIOBase:
    raw = zf.open(spec.member, mode="r")
    try:
        # newline='' is mandatory for the csv module: it keeps CR/LF exactly
        # as encoded, including line breaks embedded inside quoted fields.
        # Both LF and CRLF are then handled natively by csv.  utf-8-sig only
        # differs from utf-8 by stripping one leading BOM, so either declared
        # UTF-8 flavour reads correctly even when ignoreHeaderLines=0.
        codec = (
            "utf-8-sig"
            if spec.encoding in ("utf-8", "utf-8-sig")
            else spec.encoding
        )
        return io.TextIOWrapper(
            raw, encoding=codec, errors="strict", newline=""
        )
    except Exception:
        raw.close()
        raise


def _make_csv_reader(stream: io.TextIOBase, spec: TableSpec) -> csv.reader:
    if spec.fields_enclosed_by == "":
        return csv.reader(
            stream,
            delimiter=spec.fields_terminated_by,
            quoting=csv.QUOTE_NONE,
            quotechar=None,
            doublequote=True,
            strict=True,
        )
    return csv.reader(
        stream,
        delimiter=spec.fields_terminated_by,
        quotechar=spec.fields_enclosed_by,
        quoting=csv.QUOTE_MINIMAL,
        doublequote=True,
        strict=True,
    )


def _is_short(row: list[str], spec: TableSpec) -> bool:
    """A mapped column index is beyond the cells actually present."""
    return any(idx >= len(row) for idx in (f.index for f in spec.iter_indexed()))


def _build_cells(row: list[str], spec: TableSpec) -> dict[str, CellValue]:
    cells: dict[str, CellValue] = {}
    for fspec in spec.fields:
        if fspec.index is None:
            # No index => a declared default is mandatory (validated earlier).
            cells[fspec.term] = CellValue(fspec.default or "", ORIGIN_DEFAULT)
            continue
        if fspec.index >= len(row):
            # Out-of-range mapped column: do not silently invent an empty
            # cell; the row is quarantined and raw_cells keeps the evidence.
            continue
        text = row[fspec.index]
        if text == "" and fspec.default is not None:
            cells[fspec.term] = CellValue(fspec.default, ORIGIN_DEFAULT)
        elif text == "":
            cells[fspec.term] = CellValue("", ORIGIN_EMPTY)
        else:
            cells[fspec.term] = CellValue(text, ORIGIN_SOURCE)
    return cells


def _row_key(row: list[str], spec: TableSpec) -> Optional[str]:
    key_spec = spec.id_field if spec.is_core else spec.coreid_field
    assert key_spec is not None and key_spec.index is not None
    if key_spec.index >= len(row):
        return None
    return row[key_spec.index]


def iter_records(
    zf: zipfile.ZipFile, spec: TableSpec
) -> Iterator[DataRow]:
    """Yield logical records of one table in source order.

    Each yielded :class:`DataRow` carries the parsed term cells (with value
    origin), the raw decoded cells and -- when a mapped column is out of
    range -- is flagged via ``row.cells`` missing that term; callers detect
    the short row through :func:`row_is_short`.
    """
    stream = _open_member(zf, spec)
    try:
        try:
            reader_source = (
                _HeaderSkipper(stream, spec.ignore_header_lines)
                if spec.ignore_header_lines
                else stream
            )
            reader = _make_csv_reader(reader_source, spec)
            record_number = 0
            for row in reader:
                record_number += 1
                if len(row) == 0:
                    # csv yields [] for a blank trailing line; treat an
                    # all-empty physical line as no logical record.
                    record_number -= 1
                    continue
                key = _row_key(row, spec)
                cells = _build_cells(row, spec)
                parsed = DataRow(
                    table_id=spec.table_id,
                    member=spec.member,
                    record_number=record_number,
                    cells=cells,
                )
                parsed.raw_cells = list(row)  # type: ignore[attr-defined]
                parsed.row_key = key          # type: ignore[attr-defined]
                parsed.is_short = _is_short(row, spec)  # type: ignore[attr-defined]
                yield parsed
        except csv.Error as exc:
            raise ArchiveDataError(
                f"member {spec.member!r}: malformed CSV at logical record "
                f"{record_number + 1}: {exc}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise ArchiveDataError(
                f"member {spec.member!r}: bytes are not valid "
                f"{spec.encoding}: {exc}"
            ) from exc
    finally:
        stream.close()
