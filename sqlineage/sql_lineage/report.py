"""Streaming JSONL report reader.

The report is one JSON object per output column per line.  This module
lets callers consume a report one line at a time without loading the
whole file, and also provides a small filtering helper.
"""

import json


class LineageReportReader:
    """Iterate lineage records from a JSONL file one record at a time."""

    def __init__(self, path, encoding="utf-8"):
        self.path = path
        self.encoding = encoding
        self._fh = None
        self._line_no = 0

    def __enter__(self):
        self._fh = open(self.path, "r", encoding=self.encoding)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __iter__(self):
        if self._fh is None:
            self._fh = open(self.path, "r", encoding=self.encoding)
        for line in self._fh:
            self._line_no += 1
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON on line {self._line_no} of "
                    f"{self.path}: {exc.msg}") from exc

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def line_number(self):
        """1-based number of the last line consumed."""
        return self._line_no


def iter_records(path):
    """Generator yielding records from ``path`` without loading it all."""
    with LineageReportReader(path) as reader:
        for record in reader:
            yield record


def filter_records(path, file=None, statement=None, record_kind=None):
    """Stream records, optionally restricting by identity fields."""
    for record in iter_records(path):
        if file is not None and record["file"] != file:
            continue
        if statement is not None and record["statement"] != statement:
            continue
        if record_kind is not None and record["record_kind"] != record_kind:
            continue
        yield record
