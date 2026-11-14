"""Hand-written expectations: rejected syntax, corrupt input, atomic
publishing, multi-file scoping and the streaming JSONL reader.
"""

import json
import os

from helpers import (
    DuplicateNameError, LexicalError, LineageError, LineageTestCase,
    LineageReportReader, ParseError, UnsupportedSyntaxError,
)


UNSUPPORTED_SAMPLES = [
    ("using join",
     "SELECT id FROM sales.orders o JOIN sales.customers c "
     "USING (id)"),
    ("natural join",
     "SELECT id FROM sales.orders NATURAL JOIN sales.customers"),
    ("right join",
     "SELECT id FROM sales.orders o RIGHT JOIN sales.customers c "
     "ON o.cust_id = c.id"),
    ("full join",
     "SELECT id FROM sales.orders o FULL JOIN sales.customers c "
     "ON o.cust_id = c.id"),
    ("cross join",
     "SELECT id FROM sales.orders CROSS JOIN sales.customers"),
    ("comma join",
     "SELECT id FROM sales.orders, sales.customers"),
    ("recursive cte",
     "WITH RECURSIVE r AS (SELECT id FROM sales.orders) "
     "SELECT * FROM r"),
    ("distinct select",
     "SELECT DISTINCT id FROM sales.orders"),
    ("order by",
     "SELECT id FROM sales.orders ORDER BY id"),
    ("plain union",
     "SELECT id FROM sales.orders UNION SELECT id FROM sales.products"),
    ("in subquery",
     "SELECT id FROM sales.orders WHERE id IN (SELECT id FROM products)"),
    ("qualified star",
     "SELECT o.* FROM sales.orders o"),
    ("simple case form",
     "SELECT CASE status WHEN 'A' THEN 1 ELSE 0 END FROM sales.orders"),
    ("three-part name",
     "SELECT id FROM catalog.sales.orders"),
]


class TestUnsupportedAndCorrupt(LineageTestCase):
    def test_each_unsupported_construct_is_rejected_with_location(self):
        for label, sql in UNSUPPORTED_SAMPLES:
            with self.subTest(label=label):
                try:
                    self.analyze_text(sql)
                except (UnsupportedSyntaxError, ParseError, LexicalError):
                    pass
                else:
                    self.fail(f"{label} was accepted but must be rejected")

    def test_unsupported_error_carries_line_column_and_cause(self):
        sql = "SELECT id FROM sales.orders\nRIGHT JOIN sales.customers c\n" \
              "ON 1 = 1"
        exc = self.assertError(UnsupportedSyntaxError, sql, statement=1)
        self.assertEqual(exc.line, 2)
        self.assertIn("RIGHT JOIN", exc.message)

    def test_corrupt_inputs(self):
        # unterminated string
        self.assertError(LexicalError,
                         "SELECT 'unterminated FROM sales.orders")
        # unterminated block comment
        self.assertError(LexicalError,
                         "SELECT id /* never closed FROM sales.orders")
        # unexpected character
        self.assertError(LexicalError, "SELECT @ FROM sales.orders")
        # malformed grammar
        self.assertError(ParseError, "SELECT FROM sales.orders")
        # empty statement
        self.assertError(LexicalError,
                         "SELECT id FROM sales.orders;;")
        # empty file
        path = self.write_sql("empty.sql", "   -- only a comment\n")
        with self.assertRaises(LineageError) as ctx:
            self.analyze_paths([path])
        self.assertEqual(ctx.exception.file, "empty.sql")

    def test_statement_index_reported_on_failure(self):
        sql = (
            "SELECT id FROM sales.orders; "
            "SELECT id FROM sales.orders; "
            "SELECT BROKEN( FROM sales.orders"
        )
        exc = self.assertError(ParseError, sql, statement=3)
        self.assertEqual(exc.file, "q.sql")


class TestAtomicPublishing(LineageTestCase):
    def _good_file(self, name):
        return self.write_sql(name, "SELECT id, amount FROM sales.orders")

    def test_target_unchanged_and_no_temp_left_on_failure(self):
        good = self._good_file("good.sql")
        bad = self.write_sql(
            "bad.sql",
            "SELECT id FROM sales.orders\nRIGHT JOIN x ON 1=1")
        out = os.path.join(self.dir, "report.jsonl")
        payload = b'{"preexisting": true}\n'
        with open(out, "wb") as fh:
            fh.write(payload)
        self.assert_atomic_failure([good, bad], out, payload)

    def test_failure_in_last_file_keeps_original_bytes(self):
        good1 = self._good_file("a_good.sql")
        good2 = self._good_file("b_good.sql")
        bad = self.write_sql("c_bad.sql", "SELECT FROM ")
        out = os.path.join(self.dir, "r.jsonl")
        original = b"orig-bytes-unchanged\n"
        with open(out, "wb") as fh:
            fh.write(original)
        self.assert_atomic_failure([good1, good2, bad], out, original)

    def test_successful_run_replaces_target(self):
        good = self._good_file("ok.sql")
        out = os.path.join(self.dir, "out.jsonl")
        with open(out, "wb") as fh:
            fh.write(b"old\n")
        records = self.analyze_paths([good], out=out)
        with open(out, "rb") as fh:
            lines = [json.loads(l) for l in fh
                     if l.strip()]
        self.assertEqual(len(lines), len(records))
        self.assertNotIn(b"old", open(out, "rb").read())
        self.assertEqual(
            [n for n in os.listdir(self.dir) if n.startswith(".tmp-")],
            [])

    def test_duplicate_file_name_rejected(self):
        one = self.write_sql("dup.sql", "SELECT id FROM sales.orders")
        other_dir = os.path.join(self.dir, "nested")
        os.makedirs(other_dir)
        two = self.write_sql(os.path.join("nested", "dup.sql"),
                             "SELECT id FROM sales.orders")
        with self.assertRaises(DuplicateNameError):
            self.analyze_paths([one, two])

    def test_catalog_duplicate_relation_rejected(self):
        from helpers import Catalog
        with self.assertRaises(DuplicateNameError):
            Catalog([
                {"schema": "s", "table": "t", "columns": ["a"]},
                {"schema": "s", "table": "t", "columns": ["b"]},
            ])


class TestMultiFileScoping(LineageTestCase):
    def _two_batches(self):
        # file A defines a single-statement named query
        a = self.write_sql(
            "daily_totals.sql",
            "SELECT region, SUM(amount) AS total "
            "FROM sales.orders GROUP BY region")
        # file B consumes it
        b = self.write_sql(
            "report.sql",
            "SELECT region, total FROM daily_totals WHERE total > 10")
        return a, b

    def test_earlier_file_named_query_visible_to_later_file(self):
        a, b = self._two_batches()
        records = self.analyze_paths([a, b])
        report = [r for r in records
                  if r["file"] == "report.sql"
                  and r["record_kind"] == "statement"]
        self.assertEqual(len(report), 2)
        self.assertEqual(self.source_set(report[1]),
                         {("sales", "orders", "amount")})

    def test_reverse_order_does_not_see_the_named_query(self):
        a, b = self._two_batches()
        from helpers import UnknownNameError
        with self.assertRaises(UnknownNameError):
            self.analyze_paths([b, a])

    def test_alias_scope_does_not_leak_between_statements(self):
        # identical alias text in two statements must resolve
        # independently; a broken second statement must not be rescued
        # by the first statement's aliases
        path = self.write_sql(
            "multi.sql",
            "SELECT o.id AS k FROM sales.orders o; "
            "SELECT o.id FROM sales.customers o")
        records = self.analyze_paths([path])
        self.assertEqual(self.source_set(
            [r for r in records if r["statement"] == 2][0]),
            {("sales", "customers", "id")})

    def test_same_sql_different_batches_have_identical_lineage(self):
        a, b = self._two_batches()
        first = self.analyze_paths([a, b])
        # rerun from scratch: fresh analyzer state, same files
        second = self.analyze_paths([a, b])
        self.assertEqual(
            [json.dumps(r, sort_keys=True) for r in first],
            [json.dumps(r, sort_keys=True) for r in second])

    def test_relation_name_conflict_between_cte_and_table_rejected(self):
        path = self.write_sql(
            "conflict.sql",
            "WITH orders AS (SELECT id FROM sales.products) "
            "SELECT * FROM orders")
        with self.assertRaises(DuplicateNameError):
            self.analyze_paths([path])


class TestStreamingReader(LineageTestCase):
    def test_line_by_line_consumption_without_full_load(self):
        path = self.write_sql(
            "stream.sql",
            "SELECT id, amount, status FROM sales.orders")
        out = os.path.join(self.dir, "stream.jsonl")
        records = self.analyze_paths([path], out=out)
        consumed = 0
        with LineageReportReader(out) as reader:
            for record in reader:
                consumed += 1
                self.assertIn("base_sources", record)
        self.assertEqual(consumed, len(records))
        self.assertEqual(reader.line_number, len(records))

    def test_reader_reports_bad_jsonl_line(self):
        out = os.path.join(self.dir, "broken.jsonl")
        with open(out, "w") as fh:
            fh.write('{"ok": 1}\n{not json}\n')
        with LineageReportReader(out) as reader:
            it = iter(reader)
            next(it)
            with self.assertRaises(ValueError):
                next(it)
