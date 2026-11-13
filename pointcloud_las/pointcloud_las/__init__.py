"""LAS 1.2 point-cloud ingestion and exact spatial retrieval on SQLite.

This package only stores and retrieves the original points and their
provenance; it does not compute tree heights or any other scientific metric.
"""

from .errors import (
    IngestionError,
    LibraryError,
    PointCloudError,
    QueryError,
)
from .library import PointCloudLibrary
from .models import IngestionResult, PointRecord, SourceInfo
from .schema import library_stats

__all__ = [
    "PointCloudLibrary",
    "PointRecord",
    "SourceInfo",
    "IngestionResult",
    "PointCloudError",
    "LibraryError",
    "IngestionError",
    "QueryError",
    "library_stats",
]

__version__ = "1.0.0"
