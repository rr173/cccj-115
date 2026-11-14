"""Hand-written expectations: catalog resolution, aggregates and literals.

Covers unqualified-table unique/ambiguous/zero-match resolution,
same-table-name-in-different-schema handling, missing columns, aggregate
functions, GROUP BY / HAVING / WHERE condition retention, quoted
identifiers versus single-quoted strings, and keyword-bearing comments.
"""

from helpers import (
    AmbiguousNameError, DuplicateNameError, LexicalError, LineageTestCase,
    UnknownNameError, UnsupportedSyntaxError,
)


class TestCatalogResolution(LineageTestCase):
    def test_unqualified_table_unique_match(self):
        records = self.analyze_text(
            "SELECT id FROM orders WHERE status = 'A'")
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "id")})

    def test_unqualified_table_ambiguous(self):
        # customers exists in both sales and mkt
        exc = self.assertError(AmbiguousNameError,
                               "SELECT id FROM customers")
        self.assertIn("sales.customers", exc.message)
        self.assertIn("mkt.customers", exc.message)

    def test_unqualified_table_zero_matches(self):
        self.assertError(UnknownNameError,
                         "SELECT id FROM nonexistent_table")

    def test_same_table_name_different_schemas_are_distinct(self):
        a = self.analyze_text("SELECT segment FROM mkt.customers")
        b = self.analyze_text("SELECT tier FROM sales.customers")
        self.assertEqual(self.source_set(a[0]),
                         {("mkt", "customers", "segment")})
        self.assertEqual(self.source_set(b[0]),
                         {("sales", "customers", "tier")})

    def test_missing_qualified_column(self):
        exc = self.assertError(
            UnknownNameError,
            "SELECT o.nope FROM sales.orders o")
        self.assertIn("nope", exc.message)

    def test_qualifier_not_in_scope(self):
        exc = self.assertError(
            UnknownNameError,
            "SELECT x.id FROM sales.orders o")
        self.assertIn("x", exc.message)

    def test_case_sensitive_identifiers(self):
        # catalog column is lower-case id; quoted "Id" is a different name
        self.assertError(
            UnknownNameError,
            'SELECT "Id" FROM sales.orders')
        # quoted exact spelling works
        records = self.analyze_text(
            'SELECT "id" AS k FROM sales.orders')
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "id")})
        # table names are case-sensitive too
        self.assertError(
            UnknownNameError,
            'SELECT id FROM sales."Orders"')


class TestAggregatesAndFilters(LineageTestCase):
    def test_aggregates_trace_arguments_and_empty_count_star(self):
        sql = """
        SELECT region,
               SUM(amount) AS total,
               COUNT(*) AS rows_n,
               COUNT(DISTINCT cust_id) AS buyers,
               AVG(amount) AS avg_amt,
               MIN(status) AS first_status,
               MAX(amount) AS max_amt
        FROM sales.orders
        GROUP BY region
        HAVING SUM(amount) > 100
        """
        records = self.analyze_text(sql)
        out = self.by_ordinal(records, "statement")
        self.assertEqual(self.source_set(out[2]),
                         {("sales", "orders", "amount")})
        self.assertEqual(out[2]["expr"]["node"], "aggregate")
        self.assertEqual(out[2]["expr"]["name"], "SUM")
        # COUNT(*) has no base source but the output column is retained
        self.assertEqual(out[3]["base_sources"], [])
        self.assertTrue(out[3]["expr"]["star"])
        # COUNT(DISTINCT ...) flags distinct and keeps the source
        self.assertTrue(out[4]["expr"]["distinct"])
        self.assertEqual(self.source_set(out[4]),
                         {("sales", "orders", "cust_id")})
        # HAVING is recorded as a row-affecting condition on every output
        for rec in records:
            types = self.condition_types(rec)
            self.assertIn(("having", None), types)
        # GROUP BY expressions are retained separately and do not alter
        # output lineage
        self.assertEqual(len(out[1]["group_by_exprs"]), 1)
        self.assertEqual(out[1]["group_by_exprs"][0]["name"], "region")

    def test_aggregate_in_where_rejected(self):
        self.assertError(
            UnsupportedSyntaxError,
            "SELECT id FROM sales.orders WHERE SUM(amount) > 1")

    def test_nested_aggregate_rejected(self):
        self.assertError(
            UnsupportedSyntaxError,
            "SELECT SUM(COUNT(*)) FROM sales.orders")

    def test_where_conditions_reach_every_output(self):
        sql = """
        SELECT id, amount
        FROM sales.orders
        WHERE status = 'PAID' AND region IN ('EU', 'APAC')
           AND amount BETWEEN 10 AND 100
           AND cust_id IS NOT NULL
        """
        records = self.analyze_text(sql)
        for rec in records:
            where = [c for c in rec["row_conditions"]
                     if c["type"] == "where"]
            self.assertEqual(len(where), 1)
            tree = where[0]["expr"]
            self.assertEqual(tree["node"], "binary")
            self.assertEqual(tree["op"], "AND")

    def test_not_like_and_or_normalized(self):
        sql = """
        SELECT id
        FROM sales.orders
        WHERE NOT (status LIKE 'A%' OR status = 'Z')
        """
        records = self.analyze_text(sql)
        where = records[0]["row_conditions"][0]["expr"]
        self.assertEqual(where["node"], "unary")
        self.assertEqual(where["op"], "NOT")
        self.assertEqual(where["operand"]["node"], "binary")
        self.assertEqual(where["operand"]["op"], "OR")
        like = where["operand"]["left"]
        self.assertEqual(like["node"], "like")
        self.assertFalse(like["negated"])


class TestQuotingAndComments(LineageTestCase):
    def test_keyword_inside_string_is_inert(self):
        # the word SELECT and WHERE appear only inside the literal
        sql = "SELECT 'SELECT WHERE FROM' AS lit FROM sales.orders"
        records = self.analyze_text(sql)
        self.assertEqual(records[0]["base_sources"], [])
        self.assertEqual(records[0]["expr"]["type"], "string")
        self.assertEqual(records[0]["expr"]["value"],
                         "SELECT WHERE FROM")

    def test_escaped_quotes_in_strings_and_identifiers(self):
        from sql_lineage.lexer import tokenize
        strings = [t for t in tokenize("SELECT 'it''s' AS s FROM t")
                   if t.kind == "string"]
        self.assertEqual(strings[0].value, "it's")
        quoted = [t for t in tokenize('SELECT "we""ird" FROM t')
                  if t.kind == "quoted"]
        self.assertEqual(quoted[0].value, 'we"ird')
        # and the escaped identifier is usable as a real alias
        sql = 'SELECT w."we""ird" AS k FROM sales.orders AS w'
        # alias "we"ird" does not exist on orders -> unknown column,
        # proving it was parsed as one identifier rather than splitting
        self.assertError(UnknownNameError, sql)

    def test_keyword_inside_line_and_block_comments_is_inert(self):
        sql = """
        -- SELECT * FROM nothing; this whole line is a comment
        SELECT id /* WHERE hidden = 1 ; SELECT */
        FROM sales.orders -- trailing FROM junk
        """
        records = self.analyze_text(sql)
        self.assertEqual(len(records), 1)
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "id")})

    def test_dash_dash_inside_string_is_not_a_comment(self):
        sql = "SELECT 'a -- b' AS lit FROM sales.orders"
        records = self.analyze_text(sql)
        self.assertEqual(records[0]["expr"]["value"], "a -- b")

    def test_quoted_alias_is_usable_as_qualifier_case_sensitively(self):
        sql = """
        SELECT "O"."id" AS k
        FROM sales.orders AS "O"
        """
        records = self.analyze_text(sql)
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "id")})
        # lower-case o does not match the quoted "O"
        self.assertError(UnknownNameError,
                         'SELECT o.id FROM sales.orders AS "O"')


class TestSubqueries(LineageTestCase):
    def test_derived_table_requires_alias_and_passes_columns_through(self):
        sql = """
        SELECT inner_q.order_id AS k
        FROM (SELECT id AS order_id, amount FROM sales.orders) inner_q
        WHERE inner_q.amount > 5
        """
        records = self.analyze_text(sql)
        stmt = [r for r in records if r["record_kind"] == "statement"]
        subq = [r for r in records if r["record_kind"] == "subquery"]
        self.assertEqual(len(subq), 2)
        self.assertEqual(self.source_set(stmt[0]),
                         {("sales", "orders", "id")})
        # WHERE of the outer query plus the lineage of the inner column
        conds = self.condition_types(stmt[0])
        self.assertIn(("where", None), conds)

    def test_derived_table_without_alias_rejected(self):
        from helpers import ParseError
        self.assertError(
            ParseError,
            "SELECT x.id FROM (SELECT id FROM sales.orders)")
