"""Exception hierarchy for the LAS point-cloud library."""

from __future__ import annotations


class PointCloudError(Exception):
    """Base class for every error raised by this package."""


class LibraryError(PointCloudError):
    """The on-disk library is missing, malformed or configured incorrectly."""


class IngestionError(PointCloudError):
    """A source file cannot be ingested.

    The database is left unchanged: no point belonging to the rejected source
    remains queryable and the source does not appear in the source manifest.
    """


class QueryError(PointCloudError):
    """A query argument is invalid (for example a malformed decimal string)."""
