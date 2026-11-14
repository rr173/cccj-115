"""sql_lineage: static field-level lineage for a documented SQL subset.

Public API:

* :class:`Catalog` -- case-sensitive JSON data catalog;
* :func:`analyze_files` -- ordered multi-file analysis with atomic output;
* :func:`parse_sql_text` -- lex + parse entry point;
* :class:`LineageReportReader` / :func:`iter_records` -- streaming JSONL
  reader that does not load the whole report;
* exception hierarchy rooted at :class:`LineageError`.

The package only performs static analysis: it never connects to a
database, never executes SQL and never reads business rows.
"""

from .catalog import Catalog
from .errors import (
    AmbiguousNameError, CatalogError, DuplicateNameError, LexicalError,
    LineageError, ParseError, ResolutionError, UnsupportedSyntaxError,
    UnknownNameError,
)
from .report import LineageReportReader, filter_records, iter_records
from .runner import analyze_files, parse_sql_text

__all__ = [
    "Catalog", "analyze_files", "parse_sql_text",
    "LineageReportReader", "iter_records", "filter_records",
    "LineageError", "LexicalError", "ParseError", "UnsupportedSyntaxError",
    "CatalogError", "ResolutionError", "AmbiguousNameError",
    "UnknownNameError", "DuplicateNameError",
]

__version__ = "1.0.0"
