"""High-level builder: checklist file -> SQLite store -> digest file.

``build_from_checklist`` streams the checklist into a SQLite store (identity
uniqueness enforced by SQL), folds every stored identity into a fixed-size
IBLT, and optionally writes the digest.

Only committed state is left behind:

* a failing checklist leaves no digest at all;
* the identity store is an explicit local artifact: when a ``store_path`` is
  supplied it is kept (and never silently replaced), because it is the local
  record the party may need later.  Without one, SQLite runs in memory.
* the digest is written to a temporary file and moved into place, so an
  existing digest is either fully replaced or left untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import DigestConfig
from .digest import Digest, build_digest_from_store, digest_size
from .errors import ChecklistError
from .store import ChecklistStore


@dataclass
class BuildOutcome:
    digest: Digest
    store_path: str | None
    digest_path: str | None
    digest_bytes: int
    source_lines: int
    unique_elements: int
    duplicate_lines: int

    def report_lines(self) -> list[str]:
        lines = [
            f"checklist lines read: {self.source_lines}",
            f"unique elements:      {self.unique_elements}",
            f"duplicate lines:      {self.duplicate_lines}",
            f"identity store:       {self.store_path or '(in-memory SQLite)'}",
            f"digest:               {self.digest_path or '(not written)'}",
            f"digest size:          {self.digest_bytes} bytes "
            f"({self.digest.config.capacity} cells x 64 bytes + 96)",
        ]
        return lines


def build_from_checklist(checklist_path: str, config: DigestConfig,
                         *, store_path: str | None = None,
                         digest_path: str | None = None,
                         overwrite: bool = False,
                         batch_size: int = 2000) -> BuildOutcome:
    """Build a digest from a checklist file.

    Raises :class:`FileExistsError` rather than replacing an existing store or
    digest unless ``overwrite`` is set for the digest.
    """
    if store_path is not None and os.path.exists(store_path):
        raise FileExistsError(
            f"refusing to overwrite existing identity store: {store_path}"
        )
    if digest_path is not None and os.path.exists(digest_path) and not overwrite:
        raise FileExistsError(
            f"refusing to overwrite existing digest: {digest_path}"
        )

    store = ChecklistStore(store_path)
    digest: Digest | None = None
    try:
        with open(checklist_path, "r", encoding="utf-8", newline="") as fh:
            stats = store.ingest_lines(fh, batch_size=batch_size)
        if stats["unique_elements"] != store.count():
            raise ChecklistError(
                "identity store count differs from ingestion statistics"
            )
        digest = build_digest_from_store(store, config)
        if digest.source_lines != stats["source_lines"]:
            raise ChecklistError("digest does not record the ingested line count")
        if digest.unique_elements != stats["unique_elements"]:
            raise ChecklistError("digest element count does not match the store")
        if digest_path is not None:
            _write_atomically(digest, digest_path, overwrite=overwrite)
        size = digest_size(config)
        return BuildOutcome(
            digest=digest,
            store_path=store_path,
            digest_path=digest_path,
            digest_bytes=size if digest_path is not None else 0,
            source_lines=stats["source_lines"],
            unique_elements=stats["unique_elements"],
            duplicate_lines=stats["duplicate_lines"],
        )
    except Exception:
        store.close()
        _cleanup_store(store_path)
        raise
    else:
        store.close()


def _write_atomically(digest: Digest, path: str, *, overwrite: bool) -> None:
    tmp_path = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp_path, "xb") as fh:
            fh.write(digest.to_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp_path, path)
        else:
            # Non-overwrite path: the destination must still be absent.
            os.link(tmp_path, path)
            os.unlink(tmp_path)
    except FileExistsError:
        raise FileExistsError(
            f"refusing to overwrite existing digest: {path}"
        ) from None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _cleanup_store(store_path: str | None) -> None:
    if store_path is None:
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(store_path + suffix)
        except OSError:
            pass
