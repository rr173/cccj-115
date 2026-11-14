"""Atomic output publishing.

The contract is the same for both directions:

* everything is written to a temporary file *next to* the destination;
* the destination is replaced only after the whole input has been validated
  (including the COPY end marker / the last JSONL line);
* any failure removes the temporary file and leaves the existing destination
  byte-for-byte untouched.

For stdin input the input stream cannot be rewound, but that does not weaken
the contract: the entire stream is consumed into the temporary file first;
only after ``read()`` returns end-of-stream (or the last record is parsed)
does ``os.replace`` happen.  A failure mid-stream means the destination is
never touched.
"""

from __future__ import annotations

import contextlib
import os
import tempfile

__all__ = ["atomic_output"]


class _FileSink:
    __slots__ = ("_fh",)

    def __init__(self, fh) -> None:
        self._fh = fh

    def write(self, data: bytes) -> int:
        return self._fh.write(data)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        # The atomic_output context owns the file handle.
        pass


@contextlib.contextmanager
def atomic_output(path: str | os.PathLike[str]):
    """Yield a binary sink; publish ``path`` only if the block succeeds."""
    path = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp",
        dir=directory)
    published = False
    try:
        with os.fdopen(fd, "wb") as fh:
            sink = _FileSink(fh)
            try:
                yield sink
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                # If the body raised, this runs before os.replace below.
                pass
        os.replace(tmp, path)
        published = True
    finally:
        if not published:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
