"""High-level facade tying ingestion, manifest and queries together."""

from __future__ import annotations

from typing import Iterator, Optional, Sequence

from .errors import LibraryError
from .ingestion import ingest, list_sources, hash_file
from .query import (
    count_points,
    export_points,
    iter_points,
    query_points,
)
from .schema import connect, create_library, library_stats
from .models import IngestionResult, PointRecord


class PointCloudLibrary:
    """A disk-backed library of LAS point records.

    Create with :meth:`create` (choosing an explicitly unreferenced library or
    a strictly referenced one) or reopen with the constructor.  Each call uses
    its own SQLite connection; WAL allows a long-running query connection to
    coexist with an ingestion on another connection.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn = connect(path)

    # -- lifecycle -------------------------------------------------------
    @classmethod
    def create(cls, path: str, *, unreferenced: bool = False) -> "PointCloudLibrary":
        """Create a new library.

        ``unreferenced=True`` creates an explicitly no-reference library: only
        LAS files without any coordinate reference description are admitted.
        The default (``False``) creates a referenced library in which every
        file must carry the same reference description.
        """
        create_library(path, "none" if unreferenced else "strict")
        return cls(path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "PointCloudLibrary":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def connection(self):
        if self._conn is None:
            raise LibraryError("library connection is closed")
        return self._conn

    # -- ingestion -------------------------------------------------------
    def ingest_file(
        self,
        path: str,
        *,
        read_batch: int = 10_000,
        _fail_after_batches: Optional[int] = None,
        _hard_crash_after_batches: Optional[int] = None,
    ) -> IngestionResult:
        return ingest(
            self.connection,
            path,
            read_batch=read_batch,
            _fail_after_batches=_fail_after_batches,
            _hard_crash_after_batches=_hard_crash_after_batches,
        )

    # -- manifest --------------------------------------------------------
    def sources(self) -> list:
        return list_sources(self.connection)

    def stats(self) -> dict:
        return library_stats(self.connection)

    def known_sha256(self, path: str) -> str:
        """Compute the content identity a file would get when ingested."""
        return hash_file(path)

    # -- queries ---------------------------------------------------------
    def count(
        self,
        bounds: Optional[dict] = None,
        *,
        classifications: Optional[Sequence[int]] = None,
        source_sha: Optional[str] = None,
        index_start: Optional[int] = None,
        index_end: Optional[int] = None,
    ) -> int:
        return count_points(
            self.connection,
            bounds,
            classifications=classifications,
            source_sha=source_sha,
            index_start=index_start,
            index_end=index_end,
        )

    def query(
        self,
        bounds: dict,
        *,
        classifications: Optional[Sequence[int]] = None,
        source_sha: Optional[str] = None,
        index_start: Optional[int] = None,
        index_end: Optional[int] = None,
    ) -> list:
        return query_points(
            self.connection,
            bounds,
            classifications=classifications,
            source_sha=source_sha,
            index_start=index_start,
            index_end=index_end,
        )

    def iter_query(
        self,
        bounds: Optional[dict] = None,
        *,
        classifications: Optional[Sequence[int]] = None,
        source_sha: Optional[str] = None,
        index_start: Optional[int] = None,
        index_end: Optional[int] = None,
        fetch_size: int = 1000,
    ) -> Iterator[PointRecord]:
        yield from iter_points(
            self.connection,
            bounds,
            classifications=classifications,
            source_sha=source_sha,
            index_start=index_start,
            index_end=index_end,
            fetch_size=fetch_size,
        )

    def export_csv(
        self,
        out_path: str,
        bounds: Optional[dict] = None,
        *,
        classifications: Optional[Sequence[int]] = None,
        source_sha: Optional[str] = None,
        index_start: Optional[int] = None,
        index_end: Optional[int] = None,
    ) -> int:
        return export_points(
            self.connection,
            out_path,
            bounds,
            classifications=classifications,
            source_sha=source_sha,
            index_start=index_start,
            index_end=index_end,
        )
