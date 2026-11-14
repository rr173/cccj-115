"""Multi-file orchestration and atomic report publishing.

Files are processed strictly in caller-given order.  Only files that have
been analyzed successfully earlier in the run expose named queries to
later files.  The JSONL report is written to a temporary file in the same
directory and renamed into place only after every file succeeded; on any
failure the target keeps its original bytes and no temp file remains.
"""

import json
import os
import tempfile

from .analyzer import Analyzer
from .catalog import Catalog
from .errors import DuplicateNameError, LineageError
from .lexer import split_statements, tokenize
from .parser import parse


def _read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_sql_text(sql):
    """Lex and parse one SQL file's text into ordered statement ASTs."""
    tokens = tokenize(sql)
    chunks = split_statements(tokens)
    asts = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            asts.append(parse(chunk))
        except LineageError as exc:
            if exc.statement is None:
                exc.statement = index
            raise
    return asts


def analyze_files(file_paths, catalog, output_path=None,
                  read_text=_read_text):
    """Analyze ``file_paths`` in order, optionally publishing JSONL.

    Returns the list of lineage records (also what would be written).
    Raises a :class:`LineageError` annotated with file/statement on the
    first failure; in that case ``output_path`` is left byte-for-byte
    untouched and any temporary file is removed.
    """
    if isinstance(catalog, str):
        catalog = Catalog.load(catalog)

    records = []
    named_queries = {}
    seen_file_names = set()
    published_names = set(catalog.by_name.keys())

    for file_path in file_paths:
        file_name = os.path.basename(file_path)
        if file_name in seen_file_names:
            raise DuplicateNameError(
                f"duplicate input file name {file_name!r}; file names must "
                "be unique within a run")
        seen_file_names.add(file_name)

        try:
            sql = read_text(file_path)
        except OSError as exc:
            raise LineageError(f"cannot read SQL file {file_path}: {exc}")

        analyzer = Analyzer(catalog, file_name)
        try:
            asts = parse_sql_text(sql)
            if not asts:
                raise LineageError(
                    f"SQL file {file_name} contains no statements")
            file_records = analyzer.analyze_file(asts, dict(named_queries))
        except LineageError as exc:
            if exc.file is None:
                exc.file = file_name
            if exc.statement is None:
                exc.statement = analyzer.statement_index or None
            if output_path is not None:
                _discard_pending_temp(output_path)
            raise

        records.extend(file_records)

        # Every top-level statement of a successfully analyzed file
        # publishes a named query for later files as ``name#ordinal``.
        # A single-statement file is additionally addressable by the
        # bare file stem.  Named queries never leak across statements
        # inside the same file.
        stem = os.path.splitext(file_name)[0]
        top = [r for r in file_records
               if r["record_kind"] == "statement"]
        ordinals = sorted({r["statement"] for r in top})
        for ordinal in ordinals:
            name = f"{stem}#{ordinal}"
            if name in published_names:
                raise DuplicateNameError(
                    f"named query {name!r} conflicts with an existing "
                    "catalog relation or earlier named query")
            stmt_records = [r for r in top if r["statement"] == ordinal]
            key = ("__named_query__", name)
            named_queries[name] = (
                key, _statement_outputs(stmt_records), None)
            published_names.add(name)
        if len(ordinals) == 1 and stem not in published_names:
            name = stem
            key = ("__named_query__", name)
            named_queries[name] = (
                key, _statement_outputs(
                    [r for r in top if r["statement"] == ordinals[0]]),
                None)
            published_names.add(name)

    if output_path is not None:
        _atomic_write_jsonl(output_path, records)
    return records


def _statement_outputs(stmt_records):
    # Reconstruct _Column-like lightweight objects for named queries
    # from the top-level statement records of one statement.
    from .analyzer import _Column
    columns = []
    for rec in sorted(stmt_records, key=lambda r: r["output_ordinal"]):
        columns.append(_Column(
            rec["output_name"],
            [dict(s) for s in rec["base_sources"]],
            rec["expr"],
            [dict(f) for f in rec["row_conditions"]],
            rec["loc"]))
    return columns


def _atomic_write_jsonl(output_path, records):
    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-" + os.path.basename(output_path) + "-",
        dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        _safe_unlink(tmp_path)
        raise


def _discard_pending_temp(output_path):
    # No temp file is created until the very end, but keep the hook so the
    # failure contract is explicit and future-proof.
    directory = os.path.dirname(os.path.abspath(output_path))
    prefix = ".tmp-" + os.path.basename(output_path) + "-"
    try:
        for name in os.listdir(directory):
            if name.startswith(prefix):
                _safe_unlink(os.path.join(directory, name))
    except OSError:
        pass


def _safe_unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
