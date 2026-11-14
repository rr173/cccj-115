"""Scale test: stream many rows and report peak memory.

    python3 bench/scale_report.py --rows 200000

Writes a JSONL stream of the requested size to a temporary file, converts it
to COPY and back, and reports the peak resident memory and *tracemalloc*
peak for each phase.  The point is that peak memory is governed by the read
buffer and one field/tuple, not by the row count: the report prints the
measured values so a reader can compare runs with different --rows.

The report is written to bench/scale_report.json next to this file (or to
the path given by --report).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pgcopy_bridge import Limits, load_catalogue  # noqa: E402
from pgcopy_bridge.library import decode_file, encode_file  # noqa: E402

CATALOGUE = [
    {"name": "seq", "type": "int8"},
    {"name": "label", "type": "text"},
    {"name": "amount", "type": "numeric"},
    {"name": "stamp", "type": "timestamp"},
    {"name": "grid", "type": "int4[]"},
    {"name": "blob", "type": "bytea"},
]


def write_stream(path: str, rows: int) -> None:
    with open(path, "wb") as fh:
        for i in range(rows):
            record = {
                "seq": i,
                "label": f"row-{i:09d}-" + "x" * 40,
                "amount": f"{i}.{i % 10000:04d}",
                "stamp": i * 1_000_000,
                "grid": {"shape": [4], "lower_bounds": [0],
                         "elements": [i, None, -i, i % 7]},
                "blob": (i * 2654435761 % (2 ** 32)).to_bytes(
                    4, "big").hex(),
            }
            fh.write(json.dumps(record, separators=(",", ":"))
                     .encode() + b"\n")


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def phase(name, func):
    tracemalloc.start()
    before_rss = rss_mb()
    start = time.perf_counter()
    result = func()
    seconds = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "phase": name,
        "seconds": round(seconds, 3),
        "tracemalloc_peak_mb": round(peak / (1024 * 1024), 2),
        "maxrss_mb": round(rss_mb(), 2),
        "maxrss_before_mb": round(before_rss, 2),
        "result": result,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--chunk", type=int, default=65536)
    parser.add_argument("--report", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "scale_report.json"))
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        cat_path = os.path.join(tmp, "cat.json")
        with open(cat_path, "w", encoding="utf-8") as fh:
            json.dump({"columns": CATALOGUE}, fh)
        catalogue = load_catalogue(cat_path)
        jsonl = os.path.join(tmp, "rows.jsonl")
        copy = os.path.join(tmp, "rows.copy")
        back = os.path.join(tmp, "rows.back.jsonl")

        write_stream(jsonl, args.rows)
        input_bytes = os.path.getsize(jsonl)

        enc = phase("encode", lambda: encode_file(
            jsonl, cat_path, copy, chunk_size=args.chunk).tuples)
        dec = phase("decode", lambda: decode_file(
            copy, cat_path, back, chunk_size=args.chunk).tuples)
        copy_bytes = os.path.getsize(copy)
        back_lines = sum(1 for _ in open(back, "rb"))

    report = {
        "rows": args.rows,
        "chunk_size": args.chunk,
        "input_jsonl_bytes": input_bytes,
        "output_copy_bytes": copy_bytes,
        "encoded_tuples": enc["result"],
        "decoded_tuples": dec["result"],
        "decoded_lines": back_lines,
        "phases": [enc, dec],
        "note": "tracemalloc_peak_mb measures Python allocations during the "
                "phase; maxrss_mb is process-wide. Neither should grow "
                "proportionally with --rows.",
    }
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
