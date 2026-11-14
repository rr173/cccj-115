"""Shared helpers for the hand-written expectation test suite.

All expected lineage facts in these tests are written by hand; no SQL
parser other than the package under test is used to derive them.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_lineage import (  # noqa: E402
    AmbiguousNameError, Catalog, DuplicateNameError, LexicalError,
    LineageError, ParseError, ResolutionError, UnknownNameError,
    UnsupportedSyntaxError, analyze_files, parse_sql_text,
)
from sql_lineage.analyzer import Analyzer  # noqa: E402
from sql_lineage.report import LineageReportReader, iter_records  # noqa: E402

CATALOG_DOC = {
    "relations": [
        {"schema": "sales", "table": "orders",
         "columns": ["id", "cust_id", "amount", "status", "region"]},
        {"schema": "sales", "table": "customers",
         "columns": ["id", "name", "tier", "region"]},
        {"schema": "mkt", "table": "customers",
         "columns": ["id", "segment", "region"]},
        {"schema": "sales", "table": "products",
         "columns": ["id", "name", "price"]},
        {"schema": "hr", "table": "staff",
         "columns": ["id", "name", "salary"]},
    ]
}


class LineageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.catalog = Catalog(CATALOG_DOC["relations"])

    def tearDown(self):
        self.tmp.cleanup()

    def write_sql(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def analyze_text(self, sql, file_name="q.sql"):
        analyzer = Analyzer(self.catalog, file_name)
        try:
            asts = parse_sql_text(sql)
            return analyzer.analyze_file(asts, {})
        except LineageError as exc:
            if exc.file is None:
                exc.file = file_name
            if exc.statement is None:
                exc.statement = analyzer.statement_index or 1
            raise
    def analyze_paths(self, paths, out=None):
        return analyze_files(paths, self.catalog, out)

    # -- expectation helpers -------------------------------------------

    @staticmethod
    def by_ordinal(records, kind=None):
        filtered = [r for r in records
                    if (kind is None or r["record_kind"] == kind)]
        return {r["output_ordinal"]: r for r in filtered}

    @staticmethod
    def source_set(record):
        return {(s["schema"], s["table"], s["column"])
                for s in record["base_sources"]}

    @staticmethod
    def condition_types(record):
        return [(c["type"], c.get("join")) for c in record["row_conditions"]]

    def assertError(self, exc_type, sql, *, statement=None):
        with self.assertRaises(exc_type) as ctx:
            self.analyze_text(sql)
        exc = ctx.exception
        self.assertIsNotNone(exc.line)
        self.assertIsNotNone(exc.column)
        if statement is not None:
            self.assertEqual(exc.statement, statement)
        return exc

    def assert_atomic_failure(self, paths, out_path, original_bytes):
        with self.assertRaises(LineageError):
            self.analyze_paths(paths, out=out_path)
        with open(out_path, "rb") as fh:
            self.assertEqual(fh.read(), original_bytes)
        leftovers = [n for n in os.listdir(os.path.dirname(out_path))
                     if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])
