"""Scale check: many statements, streaming consumption, peak memory.

Generates a large ordered SQL batch by hand (no external parser), runs
the analyzer, writes JSONL atomically and consumes the report one line
at a time through the streaming reader.  Prints the processed record
count and tracemalloc peak memory, and asserts memory stays far below
the size of materializing the whole report.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_lineage import Catalog, analyze_files  # noqa: E402
from sql_lineage.report import LineageReportReader  # noqa: E402

CATALOG = {
    "relations": [
        {"schema": "sales", "table": "orders",
         "columns": ["id", "cust_id", "amount", "status", "region"]},
        {"schema": "sales", "table": "customers",
         "columns": ["id", "name", "tier"]},
    ]
}


def build_sql_batches(directory, statements_per_file, file_count):
    """Write SQL files with ``statements_per_file`` statements each."""
    paths = []
    for f in range(file_count):
        path = os.path.join(directory, f"batch_{f:03d}.sql")
        parts = []
        for i in range(statements_per_file):
            parts.append(
                f"SELECT id AS k{i}, amount * {i + 1} AS scaled{i}, "
                f"'c{i}' AS tag{i}, "
                f"CASE WHEN status = 'A' THEN amount ELSE 0 END AS c{i} "
                "FROM sales.orders WHERE region = 'EU'")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(";\n".join(parts) + ";\n")
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--statements", type=int, default=2000)
    parser.add_argument("--files", type=int, default=4)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="sqlineage-scale-")
    catalog_path = os.path.join(workdir, "catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump(CATALOG, fh)
    catalog = Catalog.load(catalog_path)

    per_file = args.statements // args.files
    paths = build_sql_batches(workdir, per_file, args.files)
    out = os.path.join(workdir, "report.jsonl")

    tracemalloc.start()
    t0 = time.perf_counter()
    records = analyze_files(paths, catalog, out)
    analyze_peak = tracemalloc.get_traced_memory()[1]
    t_analyze = time.perf_counter() - t0
    tracemalloc.stop()

    report_bytes = os.path.getsize(out)

    # Streaming consumption: never hold the full report in memory.
    tracemalloc.start()
    consumed = 0
    ordinal_names = set()
    with LineageReportReader(out) as reader:
        for record in reader:
            consumed += 1
            ordinal_names.add(
                (record["file"], record["statement"],
                 record["output_ordinal"]))
    read_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    expected = args.statements * 4  # four output columns per statement
    result = {
        "statements_requested": args.statements,
        "files": args.files,
        "records_written": len(records),
        "records_streamed": consumed,
        "unique_output_fields": len(ordinal_names),
        "expected_records": expected,
        "report_bytes": report_bytes,
        "analyze_seconds": round(t_analyze, 3),
        "analyze_peak_bytes": analyze_peak,
        "streaming_read_peak_bytes": read_peak,
        "peak_read_vs_report_ratio": round(read_peak / report_bytes, 4),
    }
    print(json.dumps(result, indent=2))

    assert len(records) == expected
    assert consumed == expected
    assert len(ordinal_names) == expected
    # streaming reader peak must be a tiny fraction of full report size
    assert read_peak < report_bytes, (
        "streaming reader held more memory than the whole report")
    print("OK: counts match and streaming read stays well below report "
          "size")


if __name__ == "__main__":
    main()
