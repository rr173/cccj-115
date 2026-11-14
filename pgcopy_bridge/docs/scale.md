# Scale and memory

`bench/scale_report.py` is the reproducible measurement behind the claim
that memory does not grow with the number of rows.

## How to run

```bash
cd pgcopy_bridge
python3 bench/scale_report.py --rows 200000            # writes bench/scale_report.json
python3 bench/scale_report.py --rows 2000 --chunk 1   # byte-at-a-time
```

It generates a JSONL stream with six columns (`int8`, `text`, `numeric`,
`timestamp`, `int4[]` with a NULL element, `bytea`), converts JSONL → COPY
→ JSONL, and records per phase: wall time, `tracemalloc` peak (Python
allocations) and process `maxrss` (from `resource.getrusage`).

## Reference result (Python 3.11.2, aarch64, this machine)

| rows | JSONL bytes | COPY bytes | phase | seconds | tracemalloc peak | maxrss |
| --- | --- | --- | --- | --- | --- | --- |
| 2 000 | 0.41 MB | 0.32 MB | encode | 0.40 | 0.31 MB | 13.6 MB |
| 2 000 | | | decode | 0.64 | 0.07 MB | 13.6 MB |
| 40 000 | 8.4 MB | 6.5 MB | encode | 8.5 | 0.34 MB | 13.7 MB |
| 40 000 | | | decode | 13.1 | 0.07 MB | 13.7 MB |
| 200 000 | 42.8 MB | 32.4 MB | encode | 43.1 | 0.32 MB | 13.6 MB |
| 200 000 | | | decode | 72.6 | 0.07 MB | 13.6 MB |

100x the rows changes peak Python allocation by at most a few tenths of a
megabyte and does not measurably change process RSS.  The 200 000-row run
is the one recorded in `bench/scale_report.json`.

## Why it is flat

* Decode: the reader hands out one protocol field at a time from a fixed
  64 KiB buffer; each tuple is decoded, serialised to one JSON line and
  written, then dropped.  `json.dumps` is called once per tuple.
* Encode: `LineReader` keeps only the current line (bounded by
  `--max-record-bytes`, default 128 MiB) and the encoder writes one
  tuple's bytes before moving on.
* Nothing accumulates a list of rows, a whole-file `bytes`, or a decoded
  document tree.

## What *does* scale with size

Only two things may grow with the data: the output file, and a single
field body, whose size is capped by `--max-field-bytes` (default 64 MiB)
and checked **from the declared wire length before allocation**.  An array
that declares 1000×1000 elements fails on the dimension header when
`--max-array-elements` is smaller, without reading the elements.

## Independent sanity checks in the suite

`tests/test_memory.py` runs the bench at 2 000 and 40 000 rows in
subprocesses and asserts `large_peak < small_peak + 2.0 MB` and
`large_rss - small_rss < 32 MB`, printing both values.  It also decodes two
tuples from a 20 000-row file and asserts the stream position is under 10%
of the file size, proving the decoder is lazy rather than reading ahead.
