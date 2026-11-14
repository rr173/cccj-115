"""Library facade: decode, encode and inspect in one place."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

from .catalogue import Catalogue, load_catalogue
from .copy_stream import CopyDecoder, CopyEncoder
from .jsonl import iter_records
from .limits import DEFAULT_READ_CHUNK, Limits
from .publish import atomic_output

__all__ = ["DecodeResult", "InspectResult", "ColumnStats", "decode_file",
           "encode_file", "inspect_file", "decode_bytes", "encode_bytes"]


@dataclass
class ColumnStats:
    name: str
    type: str
    null_count: int = 0
    non_null_count: int = 0


@dataclass
class DecodeResult:
    tuples: int = 0
    columns: int = 0


@dataclass
class InspectResult:
    columns: list[ColumnStats]
    tuples: int
    limits: Limits
    header_offset: int = 0
    extension_length: int = 0
    source: str | None = None
    bytes_read: int = 0

    def format_report(self) -> str:
        lines = [
            f"source: {self.source}",
            f"copy header: data starts at byte {self.header_offset} "
            f"(extension header {self.extension_length} byte(s))",
            f"columns: {len(self.columns)}",
            f"tuples: {self.tuples}",
            "limits: " + ", ".join(f"{k}={v}" for k, v in self.limits.describe()),
            "",
            f"{'#':>2}  {'column':<24} {'type':<12} {'NULL':>10} {'non-NULL':>10}",
        ]
        for i, col in enumerate(self.columns, 1):
            lines.append(
                f"{i:>2}  {col.name:<24} {col.type:<12} "
                f"{col.null_count:>10} {col.non_null_count:>10}")
        return "\n".join(lines)


def _source_stream(path: str | None):
    if path is None or path == "-":
        import sys
        return sys.stdin.buffer, False
    return open(path, "rb", buffering=0), True


def decode_file(copy_path: str | None, catalogue_path: str, out_path: str,
                *, limits: Limits | None = None,
                chunk_size: int = DEFAULT_READ_CHUNK,
                catalogue: Catalogue | None = None) -> DecodeResult:
    """Decode a COPY file into JSONL, publishing ``out_path`` atomically."""
    cat = catalogue or load_catalogue(catalogue_path)
    lim = limits or Limits()
    source, close_source = _source_stream(copy_path)
    try:
        with atomic_output(out_path) as sink:
            decoder = CopyDecoder(source, cat, limits=lim,
                                  chunk_size=chunk_size)
            count = 0
            for values in decoder.iter_tuples():
                record = {column.name: value
                          for column, value in zip(cat.columns, values)}
                line = json.dumps(record, ensure_ascii=False,
                                  allow_nan=False, separators=(",", ":"))
                sink.write(line.encode("utf-8") + b"\n")
                count += 1
    finally:
        if close_source:
            source.close()
    return DecodeResult(tuples=count, columns=len(cat))


def encode_file(jsonl_path: str | None, catalogue_path: str,
                out_path: str, *, limits: Limits | None = None,
                chunk_size: int = DEFAULT_READ_CHUNK,
                max_record_bytes: int = 128 * 1024 * 1024,
                catalogue: Catalogue | None = None) -> DecodeResult:
    """Encode JSONL into a COPY file, publishing ``out_path`` atomically."""
    cat = catalogue or load_catalogue(catalogue_path)
    lim = limits or Limits()
    source, close_source = _source_stream(jsonl_path)
    try:
        with atomic_output(out_path) as sink:
            encoder = CopyEncoder(sink, cat, limits=lim)
            encoder.write_header()
            count = 0
            for line_no, values in iter_records(
                    source, cat, chunk_size=chunk_size,
                    max_record_bytes=max_record_bytes):
                encoder.write_tuple(values, line=line_no)
                count += 1
            encoder.close()
    finally:
        if close_source:
            source.close()
    return DecodeResult(tuples=count, columns=len(cat))


def inspect_file(copy_path: str | None, catalogue_path: str, *,
                 limits: Limits | None = None,
                 chunk_size: int = DEFAULT_READ_CHUNK,
                 catalogue: Catalogue | None = None) -> InspectResult:
    """Validate a COPY file fully and report structure only.

    Every value is decoded so numeric digit groups and array headers are
    checked; the decoded business values are discarded immediately and are
    never printed.
    """
    cat = catalogue or load_catalogue(catalogue_path)
    lim = limits or Limits()
    source, close_source = _source_stream(copy_path)
    stats = [ColumnStats(name=c.name, type=c.type.name) for c in cat.columns]
    try:
        decoder = CopyDecoder(source, cat, limits=lim, chunk_size=chunk_size)
        header = decoder.read_header()
        for values in decoder.iter_tuples():
            for col, value in zip(stats, values):
                if value is None:
                    col.null_count += 1
                else:
                    col.non_null_count += 1
    finally:
        if close_source:
            source.close()
    return InspectResult(
        columns=stats, tuples=decoder.tuples_read, limits=lim,
        header_offset=header.data_offset,
        extension_length=header.extension_length,
        source=copy_path if copy_path not in (None, "-") else "<stdin>",
        bytes_read=decoder._reader.tell(),
    )


def decode_bytes(data: bytes, catalogue: Catalogue,
                 *, limits: Limits | None = None,
                 chunk_size: int = 1):
    """Decode an in-memory COPY blob; returns a list of tuples."""
    decoder = CopyDecoder(io.BytesIO(data), catalogue, limits=limits,
                          chunk_size=chunk_size)
    return list(decoder.iter_tuples())


def encode_bytes(tuples, catalogue: Catalogue, *,
                 limits: Limits | None = None) -> bytes:
    """Encode tuples to an in-memory COPY blob."""
    buf = io.BytesIO()
    encoder = CopyEncoder(buf, catalogue, limits=limits)
    encoder.write_header()
    for i, values in enumerate(tuples, 1):
        encoder.write_tuple(list(values), line=i)
    encoder.close()
    return buf.getvalue()
