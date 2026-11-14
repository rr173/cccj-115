"""End-to-end CLI/integration check with hand-written inputs.

Scenario layout (all expectations written by hand):

* catalog with same-named tables in two schemas;
* file 1: CTE + JOIN + star + same-name columns;
* file 2: UNION ALL with differently ordered branches;
* file 3 (single statement): publishes a named query for file 4;
* file 4: consumes that named query;
* file 5: deliberately corrupt syntax -> the whole run must fail, the
  target must keep its original bytes and no temp file may remain.
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sql_lineage.report import LineageReportReader  # noqa: E402

CATALOG = {
    "relations": [
        {"schema": "sales", "table": "orders",
         "columns": ["id", "cust_id", "amount", "status"]},
        {"schema": "sales", "table": "customers",
         "columns": ["id", "name", "tier"]},
        {"schema": "mkt", "table": "customers",
         "columns": ["id", "segment"]},
        {"schema": "sales", "table": "products",
         "columns": ["id", "price"]},
    ]
}

FILES = {
    "01_join_star.sql": """
WITH paid AS (
  SELECT id, cust_id, amount
  FROM sales.orders
  WHERE status = 'PAID'
)
SELECT *
FROM paid p
INNER JOIN sales.customers c ON p.cust_id = c.id
LEFT JOIN sales.products pr ON p.id = pr.id
""",
    "02_union.sql": """
SELECT id AS a, amount AS b FROM sales.orders WHERE status = 'A'
UNION ALL
SELECT price AS b_again, id AS a_again FROM sales.products
""",
    "03_named_publish.sql": """
SELECT cust_id, SUM(amount) AS spent
FROM sales.orders
GROUP BY cust_id
HAVING SUM(amount) > 10
""",
    "04_named_consume.sql": """
SELECT cust_id, spent
FROM "03_named_publish"
WHERE spent > 100
""",
    "05_broken.sql": "SELECT id FROM sales.orders\nRIGHT JOIN x ON 1 = 1\n",
}


def write_inputs(directory):
    cat_path = os.path.join(directory, "catalog.json")
    with open(cat_path, "w") as fh:
        json.dump(CATALOG, fh)
    paths = {}
    for name, text in FILES.items():
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(text)
        paths[name] = path
    return cat_path, paths


def run_cli(catalog, sql_paths, out):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run(
        [sys.executable, "-m", "sql_lineage", "run",
         "--catalog", catalog, "--sql", *sql_paths, "--out", out],
        capture_output=True, text=True, env=env, cwd=ROOT)


def main():
    workdir = tempfile.mkdtemp(prefix="sqlineage-e2e-")
    catalog, paths = write_inputs(workdir)
    out = os.path.join(workdir, "lineage.jsonl")
    original = b"PRE-EXISTING-BYTES\n"
    with open(out, "wb") as fh:
        fh.write(original)

    good_order = [paths[n] for n in
                  ("01_join_star.sql", "02_union.sql",
                   "03_named_publish.sql", "04_named_consume.sql")]

    # 1. successful run publishes and every record is stream-readable
    ok = run_cli(catalog, good_order, out)
    assert ok.returncode == 0, ok.stderr
    records = list(LineageReportReader(out))
    print(f"[1] successful run: {len(records)} records")

    # hand-checked expectations --------------------------------------
    s1 = [r for r in records if r["file"] == "01_join_star.sql"
          and r["record_kind"] == "statement"]
    # paid(3) + customers(3) + products(2) = 8 star-expanded outputs
    assert len(s1) == 8, [r["output_name"] for r in s1]
    assert s1[0]["output_ordinal"] == 1 and s1[-1]["output_ordinal"] == 8
    # first output traces through the CTE back to base orders.id and
    # carries CTE WHERE + both JOIN ON conditions
    r = s1[0]
    assert {tuple(sorted(x.items())) for x in r["base_sources"]} == {
        (("column", "id"), ("schema", "sales"), ("table", "orders"))}
    types = [c["type"] for c in r["row_conditions"]]
    assert types == ["join_on", "join_on", "where"], types
    joins = [c.get("join") for c in r["row_conditions"]
             if c["type"] == "join_on"]
    assert joins == ["INNER", "LEFT"], joins

    # UNION: positional alignment, branch 2 swaps order/names
    s2 = [r for r in records if r["file"] == "02_union.sql"]
    assert len(s2) == 2
    u1, u2 = s2
    assert [b["branch"] for b in u1["branch_mapping"]] == [1, 2]
    b1 = {b["branch"]: b for b in u1["branch_mapping"]}
    assert b1[1]["output_name"] == "a"
    assert b1[2]["output_name"] == "b_again"
    src1 = {(s["schema"], s["table"], s["column"])
            for s in u1["base_sources"]}
    assert src1 == {("sales", "orders", "id"),
                    ("sales", "products", "price")}, src1
    src2 = {(s["schema"], s["table"], s["column"])
            for s in u2["base_sources"]}
    assert src2 == {("sales", "orders", "amount"),
                    ("sales", "products", "id")}, src2
    # branch 1's WHERE stays attached to branch 1 only
    bm1 = {b["branch"]: b for b in u1["branch_mapping"]}
    assert [c["type"] for c in bm1[1]["row_conditions"]] == ["where"]
    assert bm1[2]["row_conditions"] == []
    print("[2] union positional mapping and per-branch conditions OK")

    # named query from file 3 visible to file 4
    s4 = [r for r in records if r["file"] == "04_named_consume.sql"]
    assert len(s4) == 2
    assert {(s["schema"], s["table"], s["column"])
            for s in s4[1]["base_sources"]} == {
        ("sales", "orders", "amount")}
    # HAVING from file 3 and WHERE from file 4 both influence the rows
    cond = {c["type"] for c in s4[1]["row_conditions"]}
    assert {"having", "where"} <= cond, cond
    print("[3] cross-file named query lineage OK")

    # 2. reverse order: file 4 can no longer see the named query
    rev = run_cli(catalog, list(reversed(good_order)), out)
    assert rev.returncode == 1
    assert "unknown_name" in rev.stderr
    print("[4] reverse batch order rejected as unknown name")

    # 3. corrupt last file: failure, original bytes, no temp leftovers.
    # Re-arm the sentinel first, since step 1 legitimately replaced it.
    with open(out, "wb") as fh:
        fh.write(original)
    fail = run_cli(catalog, good_order + [paths["05_broken.sql"]], out)
    assert fail.returncode == 1, fail.stderr
    assert "unsupported_syntax" in fail.stderr
    assert "statement 1" in fail.stderr
    with open(out, "rb") as fh:
        assert fh.read() == original, "target bytes changed on failure"
    leftovers = [n for n in os.listdir(workdir) if n.startswith(".tmp-")]
    assert leftovers == [], leftovers
    print("[5] corrupt last file: target unchanged, no temp files")

    # 4. read subcommand streams the good report again
    good2 = run_cli(catalog, good_order, out)
    assert good2.returncode == 0
    read = subprocess.run(
        [sys.executable, "-m", "sql_lineage", "read", out],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=ROOT), cwd=ROOT)
    assert read.returncode == 0
    assert f"consumed {len(records)} records" in read.stderr
    print(f"[6] streaming read CLI consumed {len(records)} records")

    print("ALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
