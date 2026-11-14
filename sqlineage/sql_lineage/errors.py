"""Exception hierarchy for static SQL lineage analysis.

All failures abort the whole analysis run; no partial report is ever
published (the orchestration layer keeps the previous target bytes).
"""


class LineageError(Exception):
    """Base class for every analysis error.

    ``file`` / ``statement`` / ``line`` / ``column`` are filled in by the
    analysis driver when the error propagates out of a file.  ``reason``
    carries a short machine-readable code so callers can classify errors.
    """

    reason = "lineage_error"

    def __init__(self, message, line=None, column=None,
                 file=None, statement=None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column
        self.file = file
        self.statement = statement

    def with_location(self, file, statement):
        """Return the error annotated with file/statement context."""
        if self.file is None:
            self.file = file
        if self.statement is None:
            self.statement = statement
        return self

    def to_dict(self):
        return {
            "error": self.reason,
            "message": self.message,
            "file": self.file,
            "statement": self.statement,
            "line": self.line,
            "column": self.column,
        }


class LexicalError(LineageError):
    reason = "lexical_error"


class ParseError(LineageError):
    reason = "parse_error"


class UnsupportedSyntaxError(ParseError):
    reason = "unsupported_syntax"


class CatalogError(LineageError):
    reason = "catalog_error"


class ResolutionError(LineageError):
    reason = "resolution_error"


class AmbiguousNameError(ResolutionError):
    reason = "ambiguous_name"


class UnknownNameError(ResolutionError):
    reason = "unknown_name"


class DuplicateNameError(LineageError):
    reason = "duplicate_name"
