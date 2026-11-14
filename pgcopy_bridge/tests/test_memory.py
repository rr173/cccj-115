"""Memory must not scale with total row count.

Two measurements, both as *subprocesses* so RSS is meaningful:

* ``bench/scale_report.py`` at 2_000 and 40_000 rows.  Peak Python
  allocation during decode must stay in the same order of magnitude and the
  increment must be a tiny fraction of the 20x data growth.
* a direct symptom test: decoding one tuple of a large file and stopping
  early must not have consumed the rest of the file.

The absolute numbers are printed in the test output so they can be checked
by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench", "scale_report.py")


def _run_report(tmp_path, rows, name):
    report = tmp_path / f"{name}.json"
    env = {**os.environ, "PYTHONPATH": ROOT}
    r = subprocess.run(
        [sys.executable, "-s", BENCH, "--rows", str(rows),
         "--report", str(report)],
        capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    return json.loads(report.read_text())


def test_peak_memory_flat_as_rows_grow(tmp_path):
    small = _run_report(tmp_path, 2_000, "small")
    large = _run_report(tmp_path, 40_000, "large")

    assert small["decoded_tuples"] == 2_000
    assert large["decoded_tuples"] == 40_000
    assert large["encoded_tuples"] == 40_000

    small_peak = small["phases"][1]["tracemalloc_peak_mb"]
    large_peak = large["phases"][1]["tracemalloc_peak_mb"]
    # Encode must be flat too: line-by-line reading and writing.
    assert large["phases"][0]["tracemalloc_peak_mb"] < \
        small["phases"][0]["tracemalloc_peak_mb"] + 2.0
    print(f"decode peak: {small_peak} MB at 2k rows, {large_peak} MB at 40k")
    # 20x the rows; allow a fixed allowance, not a proportional one.  The
    # generator streams each line, so no full-file structure is retained.
    assert large_peak < small_peak + 2.0
    assert large_peak < 8.0

    small_rss = small["phases"][1]["maxrss_mb"]
    large_rss = large["phases"][1]["maxrss_mb"]
    print(f"process maxrss: {small_rss} MB vs {large_rss} MB")
    assert large_rss - small_rss < 32.0


def test_decoder_is_lazy_on_a_large_file(tmp_path):
    # Build a moderately large COPY with hand-written tuples via the
    # encoder, then read only the first two tuples and check the stream
    # position is far from EOF.
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "materials"))
    import _common as B
    from pgcopy_bridge import catalogue_from_mapping
    from pgcopy_bridge.copy_stream import CopyDecoder

    catalogue = catalogue_from_mapping(
        {"columns": [{"name": "a", "type": "int4"},
                     {"name": "b", "type": "text"}]})
    path = tmp_path / "many.copy"
    with open(path, "wb") as fh:
        fh.write(B.header())
        for i in range(20_000):
            fh.write(B.tuple_([B.int4(i), B.text("y" * 20)]))
        fh.write(B.end_marker())
    size = path.stat().st_size

    with open(path, "rb") as fh:
        stream = _CountingStream(fh)
        decoder = CopyDecoder(stream, catalogue, chunk_size=4096)
        gen = decoder.iter_tuples()
        first = next(gen)
        second = next(gen)
        assert first == [0, "y" * 20]
        assert second == [1, "y" * 20]
        assert stream.position < size / 10


class _CountingStream:
    def __init__(self, fh):
        self._fh = fh
        self.position = 0

    def read(self, n):
        data = self._fh.read(n)
        self.position += len(data)
        return data
