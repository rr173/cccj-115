"""Hand-written expectations: core lineage scenarios.

Covers plain columns, arithmetic/boolean expressions and CASE, star
expansion with JOIN same-name columns, preceding-CTE pass-through and
UNION ALL aligned by position with different branch column orders.
"""

from helpers import (
    AmbiguousNameError, DuplicateNameError, LineageTestCase,
    UnknownNameError, UnsupportedSyntaxError,
)


class TestPlainColumns(LineageTestCase):
    def test_plain_column_and_expression_and_constant(self):
        sql = """
        SELECT id,
               amount * 2 AS doubled,
               'flag' AS tag,
               amount + 100 / 2 AS adjusted,
               status IS NULL AS missing
        FROM sales.orders
        """
        records = self.analyze_text(sql)
        out = self.by_ordinal(records, "statement")
        self.assertEqual([r["output_name"] for r in records],
                         ["id", "doubled", "tag", "adjusted", "missing"])

        # 1. plain column: one base source
        self.assertEqual(self.source_set(out[1]),
                         {("sales", "orders", "id")})
        self.assertEqual(out[1]["expr"]["node"], "column")
        self.assertEqual(out[1]["expr"]["name"], "id")

        # 2. arithmetic expression with an alias
        self.assertEqual(self.source_set(out[2]),
                         {("sales", "orders", "amount")})
        self.assertEqual(out[2]["expr"],
                         {"node": "binary", "op": "*",
                          "left": {"node": "column", "qualifier": "orders",
                                   "name": "amount",
                                   "qualifier_quoted": False,
                                   "name_quoted": False},
                          "right": {"node": "literal", "value": 2,
                                    "type": "number"}})

        # 3. constant-only column: present, explicitly empty sources
        self.assertEqual(out[3]["base_sources"], [])
        self.assertEqual(out[3]["expr"],
                         {"node": "literal", "value": "flag",
                          "type": "string"})

        # 4. nested arithmetic keeps the normalized tree shape
        expr4 = out[4]["expr"]
        self.assertEqual(expr4["node"], "binary")
        self.assertEqual(expr4["op"], "+")
        self.assertEqual(expr4["right"]["op"], "/")
        self.assertEqual(self.source_set(out[4]),
                         {("sales", "orders", "amount")})

        # 5. boolean postfix expression
        self.assertEqual(out[5]["expr"]["node"], "is_null")
        self.assertFalse(out[5]["expr"]["negated"])
        self.assertEqual(self.source_set(out[5]),
                         {("sales", "orders", "status")})

    def test_case_expression_collects_every_branch_source(self):
        sql = """
        SELECT CASE WHEN amount > 10 THEN status ELSE 'X' END AS bucket,
               CASE WHEN amount < 0 THEN 'neg'
                    WHEN status = 'A' THEN region
                    ELSE cust_id END AS mixed
        FROM sales.orders
        """
        records = self.analyze_text(sql)
        out = self.by_ordinal(records, "statement")
        bucket = out[1]
        self.assertEqual(self.source_set(bucket),
                         {("sales", "orders", "amount"),
                          ("sales", "orders", "status")})
        self.assertEqual(bucket["expr"]["node"], "case")
        self.assertIsNotNone(bucket["expr"]["else_"])
        self.assertEqual(bucket["expr"]["whens"][0]["cond"]["op"], ">")

        mixed = out[2]
        self.assertEqual(self.source_set(mixed),
                         {("sales", "orders", "amount"),
                          ("sales", "orders", "status"),
                          ("sales", "orders", "region"),
                          ("sales", "orders", "cust_id")})
        self.assertEqual(len(mixed["expr"]["whens"]), 2)


class TestStarAndJoins(LineageTestCase):
    def test_star_expands_in_from_order_and_keeps_join_duplicates(self):
        sql = """
        SELECT *
        FROM sales.orders o
        INNER JOIN sales.customers c ON o.cust_id = c.id
        """
        records = self.analyze_text(sql)
        names = [r["output_name"] for r in records]
        # orders columns first (5), then customers columns (4)
        self.assertEqual(names,
                         ["id", "cust_id", "amount", "status", "region",
                          "id", "name", "tier", "region"])
        # same-named columns are distinct output columns with distinct
        # base sources
        first_id, second_id = records[0], records[5]
        self.assertEqual(first_id["output_ordinal"], 1)
        self.assertEqual(second_id["output_ordinal"], 6)
        self.assertEqual(self.source_set(first_id),
                         {("sales", "orders", "id")})
        self.assertEqual(self.source_set(second_id),
                         {("sales", "customers", "id")})
        first_region, second_region = records[4], records[8]
        self.assertEqual(self.source_set(first_region),
                         {("sales", "orders", "region")})
        self.assertEqual(self.source_set(second_region),
                         {("sales", "customers", "region")})
        # every expanded column carries the JOIN ON condition
        for rec in records:
            self.assertIn(("join_on", "INNER"),
                          self.condition_types(rec))

    def test_join_on_sources_appear_in_condition_and_columns(self):
        sql = """
        SELECT o.amount AS amount
        FROM sales.orders o
        LEFT JOIN sales.customers c ON o.cust_id = c.id AND c.tier = 'GOLD'
        WHERE o.status = 'PAID'
        """
        records = self.analyze_text(sql)
        rec = records[0]
        conds = rec["row_conditions"]
        on = next(c for c in conds if c["type"] == "join_on")
        self.assertEqual(on["join"], "LEFT")
        # ON condition AST references both join keys and the literal
        flat = repr(on["expr"])
        self.assertIn("cust_id", flat)
        self.assertIn("'GOLD'", flat.replace('"', "'"))
        where = next(c for c in conds if c["type"] == "where")
        self.assertEqual(where["expr"]["op"], "=")
        # the output column itself only traces to orders.amount
        self.assertEqual(self.source_set(rec),
                         {("sales", "orders", "amount")})

    def test_unqualified_duplicate_column_is_ambiguous(self):
        sql = """
        SELECT region
        FROM sales.orders o
        INNER JOIN sales.customers c ON o.cust_id = c.id
        """
        exc = self.assertError(AmbiguousNameError, sql)
        self.assertIn("region", exc.message)
        self.assertIn("ambiguous", exc.message)

    def test_qualified_column_resolves_to_named_side(self):
        sql = """
        SELECT o.region AS order_region, c.region AS customer_region
        FROM sales.orders o
        INNER JOIN sales.customers c ON o.cust_id = c.id
        """
        records = self.analyze_text(sql)
        self.assertEqual(self.source_set(records[0]),
                         {("sales", "orders", "region")})
        self.assertEqual(self.source_set(records[1]),
                         {("sales", "customers", "region")})


class TestCTEPassThrough(LineageTestCase):
    def test_preceding_cte_is_visible_and_traces_to_base_columns(self):
        sql = """
        WITH paid AS (
            SELECT id AS order_id, cust_id, amount
            FROM sales.orders
            WHERE status = 'PAID'
        ),
        joined AS (
            SELECT p.order_id, c.name AS customer_name, p.amount
            FROM paid p
            INNER JOIN sales.customers c ON p.cust_id = c.id
        )
        SELECT order_id, customer_name
        FROM joined
        """
        records = self.analyze_text(sql)
        stmt = self.by_ordinal(records, "statement")
        self.assertEqual(self.source_set(stmt[1]),
                         {("sales", "orders", "id")})
        self.assertEqual(self.source_set(stmt[2]),
                         {("sales", "customers", "name")})
        # WHERE of the first CTE and ON of the second survive to the
        # final output columns
        conds = self.condition_types(stmt[1])
        self.assertIn(("where", None), conds)
        self.assertIn(("join_on", "INNER"), conds)

    def test_cte_cannot_reference_later_cte(self):
        sql = """
        WITH later AS (SELECT id FROM first_one),
             first_one AS (SELECT id FROM sales.orders)
        SELECT * FROM later
        """
        self.assertError(UnknownNameError, sql)

    def test_cte_cannot_reference_itself(self):
        sql = """
        WITH looped AS (SELECT id FROM looped)
        SELECT * FROM looped
        """
        self.assertError(UnknownNameError, sql)

    def test_duplicate_cte_name_rejected(self):
        sql = """
        WITH x AS (SELECT id FROM sales.orders),
             x AS (SELECT id FROM sales.orders)
        SELECT * FROM x
        """
        self.assertError(DuplicateNameError, sql)

    def test_cte_explicit_column_name_list(self):
        sql = """
        WITH pair (a, b) AS (
            SELECT id, amount FROM sales.orders
        )
        SELECT a, b FROM pair
        """
        records = self.analyze_text(sql)
        stmt = self.by_ordinal(records, "statement")
        self.assertEqual(list(stmt), [1, 2])
        self.assertEqual(stmt[1]["output_name"], "a")
        self.assertEqual(self.source_set(stmt[1]),
                         {("sales", "orders", "id")})
        self.assertEqual(self.source_set(stmt[2]),
                         {("sales", "orders", "amount")})

    def test_cte_column_name_count_mismatch_rejected(self):
        sql = """
        WITH pair (a, b, c) AS (SELECT id, amount FROM sales.orders)
        SELECT * FROM pair
        """
        from helpers import ResolutionError
        self.assertError(ResolutionError, sql)


class TestUnionAll(LineageTestCase):
    def test_union_aligns_by_position_not_alias(self):
        # branch 2 swaps the column order and uses different aliases;
        # positional alignment still maps ordinal 1 -> amount/id pair
        sql = """
        SELECT id AS first_col, amount AS second_col
        FROM sales.orders
        UNION ALL
        SELECT price AS renamed_second, id AS renamed_first
        FROM sales.products
        """
        records = self.analyze_text(sql)
        self.assertEqual(len(records), 2)
        r1, r2 = records
        # output names come from branch 1
        self.assertEqual(r1["output_name"], "first_col")
        self.assertEqual(r2["output_name"], "second_col")
        # branch mapping preserves per-branch names and branch numbers
        bm1 = {b["branch"]: b for b in r1["branch_mapping"]}
        self.assertEqual(bm1[1]["output_name"], "first_col")
        self.assertEqual(bm1[2]["output_name"], "renamed_second")
        self.assertEqual(
            {tuple(sorted(s.items())) for b in r1["branch_mapping"]
             for s in b["base_sources"]},
            {(("column", "id"), ("schema", "sales"), ("table", "orders")),
             (("column", "price"), ("schema", "sales"),
              ("table", "products"))})
        # union of base sources on the record itself
        self.assertEqual(self.source_set(r1),
                         {("sales", "orders", "id"),
                          ("sales", "products", "price")})
        self.assertEqual(self.source_set(r2),
                         {("sales", "orders", "amount"),
                          ("sales", "products", "id")})
        self.assertEqual(r1["expr"]["node"], "union_all")
        self.assertEqual(len(r1["expr"]["branches"]), 2)

    def test_union_column_count_mismatch_rejects_whole_statement(self):
        sql = """
        SELECT id, amount FROM sales.orders
        UNION ALL
        SELECT id FROM sales.products
        """
        from helpers import ResolutionError
        exc = self.assertError(ResolutionError, sql)
        self.assertIn("UNION ALL branch 2", exc.message)

    def test_union_three_branches_keep_conditions_per_branch(self):
        sql = """
        SELECT id FROM sales.orders WHERE status = 'A'
        UNION ALL
        SELECT id FROM sales.orders WHERE status = 'B'
        UNION ALL
        SELECT cust_id FROM sales.orders WHERE region = 'EU'
        """
        records = self.analyze_text(sql)
        r = records[0]
        self.assertEqual([b["branch"] for b in r["branch_mapping"]],
                         [1, 2, 3])
        for branch in r["branch_mapping"]:
            self.assertEqual(
                [c["type"] for c in branch["row_conditions"]], ["where"])
