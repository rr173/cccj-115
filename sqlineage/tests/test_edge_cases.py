"""Hand-written expectations for miscellaneous edge semantics."""

from helpers import LineageTestCase


class TestEdgeSemantics(LineageTestCase):
    def test_select_without_from_keeps_constant_columns(self):
        records = self.analyze_text(
            "SELECT 1 AS one, 'x' AS two, NULL AS three")
        self.assertEqual([r["output_name"] for r in records],
                         ["one", "two", "three"])
        for rec in records:
            self.assertEqual(rec["base_sources"], [])
        self.assertEqual(records[2]["expr"]["type"], "null")

    def test_union_of_constants_has_empty_sources(self):
        records = self.analyze_text(
            "SELECT 1 AS a UNION ALL SELECT 2 AS b UNION ALL "
            "SELECT 3 AS c")
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["base_sources"], [])
        self.assertEqual([b["branch"] for b in rec["branch_mapping"]],
                         [1, 2, 3])

    def test_concat_operator_normalized(self):
        records = self.analyze_text(
            "SELECT name || tier AS k FROM sales.customers")
        self.assertEqual(records[0]["expr"]["op"], "CONCAT")
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "customers", "name"),
                          ("sales", "customers", "tier")})

    def test_not_in_and_not_between_flags(self):
        records = self.analyze_text(
            "SELECT id FROM sales.orders "
            "WHERE status NOT IN ('A', 'B') "
            "AND amount NOT BETWEEN 1 AND 9")
        where = records[0]["row_conditions"][0]["expr"]
        # top AND, each side carrying its own negated flag
        left, right = where["left"], where["right"]
        self.assertTrue(left["negated"])
        self.assertEqual(left["node"], "in")
        self.assertTrue(right["negated"])
        self.assertEqual(right["node"], "between")

    def test_exponent_and_decimal_literals(self):
        records = self.analyze_text(
            "SELECT amount * 1.5e2 AS k FROM sales.orders")
        lit = records[0]["expr"]["right"]
        self.assertEqual(lit["type"], "number")
        self.assertEqual(lit["value"], 150.0)

    def test_synthetic_name_for_unnamed_expression(self):
        records = self.analyze_text(
            "SELECT id, amount + 1 FROM sales.orders")
        self.assertEqual(records[0]["output_name"], "id")
        self.assertEqual(records[1]["output_name"], "expr_2")

    def test_duplicate_output_names_keep_distinct_ordinals(self):
        # SQL allows repeated output names; the stable identity is the
        # ordinal, and each column keeps its own lineage.
        records = self.analyze_text(
            "SELECT id AS k, amount AS k FROM sales.orders")
        self.assertEqual([r["output_name"] for r in records], ["k", "k"])
        self.assertEqual([r["output_ordinal"] for r in records], [1, 2])
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "id")})
        self.assertEqual(self.source_set(records[1]),
                         {("sales", "orders", "amount")})

    def test_star_then_explicit_same_name_both_kept(self):
        records = self.analyze_text(
            "SELECT *, id FROM sales.orders")
        ids = [r for r in records if r["output_name"] == "id"]
        self.assertEqual(len(ids), 2)
        self.assertEqual([r["output_ordinal"] for r in ids], [1, 6])

    def test_ordinal_stability_under_star_then_expression(self):
        sql = """
        SELECT *, amount * 2 AS doubled
        FROM sales.orders
        """
        records = self.analyze_text(sql)
        ordinals = [r["output_ordinal"] for r in records]
        self.assertEqual(ordinals, [1, 2, 3, 4, 5, 6])
        self.assertEqual(records[-1]["output_name"], "doubled")

    def test_named_query_hashed_name_for_multi_statement_file(self):
        a = self.write_sql(
            "flows.sql",
            "SELECT id FROM sales.orders; SELECT amount FROM sales.orders")
        b = self.write_sql(
            "down.sql",
            "SELECT id FROM \"flows#1\"")
        records = self.analyze_paths([a, b])
        down = [r for r in records if r["file"] == "down.sql"]
        self.assertEqual(self.source_set(down[0]),
                         {("sales", "orders", "id")})

    def test_cte_filters_propagate_through_two_levels(self):
        sql = """
        WITH l1 AS (
            SELECT id, amount FROM sales.orders WHERE status = 'A'
        ),
        l2 AS (
            SELECT id, amount FROM l1 WHERE amount > 10
        )
        SELECT id FROM l2
        """
        records = self.analyze_text(sql)
        stmt = [r for r in records if r["record_kind"] == "statement"]
        where_count = sum(
            1 for c in stmt[0]["row_conditions"] if c["type"] == "where")
        self.assertEqual(where_count, 2)
