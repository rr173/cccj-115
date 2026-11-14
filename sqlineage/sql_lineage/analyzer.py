"""Static field-level lineage analyzer.

The analyzer walks parser ASTs against a :class:`~sql_lineage.catalog.Catalog`
and produces one lineage record per output column.  It never connects to a
database and never executes SQL: every lineage fact is derived from the
syntax tree, never from text search.
"""

from .errors import (
    AmbiguousNameError, DuplicateNameError, LineageError, ParseError,
    ResolutionError, UnknownNameError, UnsupportedSyntaxError,
)


# --------------------------------------------------------------------------
# Internal data model
# --------------------------------------------------------------------------

class _Column:
    """One column of an in-scope dataset."""

    def __init__(self, name, sources, expr, filters, loc):
        self.name = name              # visible column name
        self.sources = sources        # list[dict] base column refs
        self.expr = expr              # normalized AST producing the column
        self.filters = filters        # list[dict] conditions affecting rows
        self.loc = loc                # [line, col] of the producing item


class _Dataset:
    """A named relation visible in a FROM scope."""

    def __init__(self, alias, key, kind, columns, loc, join_type=None):
        self.alias = alias                    # visible qualifier text
        self.key = key                        # catalog key or cte/query id
        self.kind = kind                      # table | cte | named_query | subquery
        self.columns = columns                # list[_Column]
        self.loc = loc
        self.join_type = join_type            # None for the driving relation


def _loc(node):
    return node.get("loc")


def _err(exc_type, node, message):
    loc = _loc(node) if isinstance(node, dict) else None
    return exc_type(message, loc[0] if loc else None,
                    loc[1] if loc else None)


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class Analyzer:
    def __init__(self, catalog, file_name):
        self.catalog = catalog
        self.file_name = file_name
        self.statement_index = 0
        # named-query name -> (key, columns, loc) from earlier files
        self.named_queries = {}
        self.records = []
        # counter so every select gets a unique query id within the run
        self._qcounter = 0

    def _new_qid(self):
        self._qcounter += 1
        return f"q{self._qcounter}"

    # -- public entry ----------------------------------------------------

    def analyze_file(self, asts, named_queries):
        """Analyze all statements of one file.

        ``named_queries`` maps name -> (key, columns, loc) produced by
        successfully analyzed earlier files.
        """
        self.named_queries = named_queries
        self.records = []
        for index, ast in enumerate(asts, start=1):
            self.statement_index = index
            try:
                self._analyze_statement(ast)
            except LineageError as exc:
                raise self._annotate(exc)
        return self.records

    def _annotate(self, exc):
        if exc.file is None:
            exc.file = self.file_name
            exc.statement = self.statement_index
        return exc

    # -- statements ------------------------------------------------------

    def _analyze_statement(self, ast):
        # Build CTE datasets in definition order; a CTE may only see the
        # CTEs defined before it.  References to later CTEs or to the
        # CTE itself fail resolution because they are absent from the
        # progressively grown scope.
        ctes = {}
        for cte in ast.get("ctes", []):
            name = cte["name"]
            if name in ctes:
                loc = cte["loc"]
                raise DuplicateNameError(
                    f"duplicate CTE name {name!r} in WITH clause",
                    loc[0], loc[1])
            if name in self.named_queries or name in self.catalog.by_name:
                loc = cte["loc"]
                raise DuplicateNameError(
                    f"CTE name {name!r} conflicts with an already visible "
                    "relation (catalog table or named query)",
                    loc[0], loc[1])
            dataset, _ = self._analyze_query(cte["body"],
                                             parent_ctes=dict(ctes),
                                             cte_def=cte)
            ctes[name] = dataset

        if ast["node"] == "union":
            self._analyze_union(ast, dict(ctes),
                                record_kind="statement",
                                record_name=None)
        else:
            _, records = self._analyze_select(
                ast, dict(ctes), record_kind="statement",
                record_name=None)
            self.records.extend(records)

    # -- query expressions ----------------------------------------------

    def _analyze_query(self, node, parent_ctes, cte_def=None,
                       subq_alias=None):
        """Analyze a select/union used as a CTE body or subquery.

        Returns (dataset, records emitted for the inner columns).
        """
        if node["node"] == "union":
            return self._analyze_union(
                node, parent_ctes, record_kind=(
                    "cte" if cte_def else "subquery"),
                record_name=(cte_def["name"] if cte_def else subq_alias),
                cte_def=cte_def)
        return self._analyze_select(
            node, parent_ctes,
            record_kind=("cte" if cte_def else "subquery"),
            record_name=(cte_def["name"] if cte_def else subq_alias),
            cte_def=cte_def)

    # -- SELECT ----------------------------------------------------------

    def _analyze_select(self, node, parent_ctes, record_kind, record_name,
                        cte_def=None, union_arm=None):
        qid = self._new_qid()
        scope, table_filters = self._build_scope(node.get("from_"),
                                                 parent_ctes)

        # WHERE condition: aggregate-free, contributes to every output row.
        where_filters = []
        if node.get("where") is not None:
            where = node["where"]
            self._assert_no_aggregate(where, "WHERE")
            where_filters.append(self._filter_dict(
                where, scope, "where", None))

        # GROUP BY expressions are validated against the scope but do not
        # change output lineage.
        group_filters = []
        if node.get("group_by"):
            for gexpr in node["group_by"]:
                self._assert_no_aggregate(gexpr, "GROUP BY")
                group_filters.append(self._filter_dict(
                    gexpr, scope, "group_by", None))

        having_filters = []
        if node.get("having") is not None:
            having = node["having"]
            having_filters.append(self._filter_dict(
                having, scope, "having", None))

        row_filters = table_filters + where_filters + group_filters + \
            having_filters

        output_columns = []
        output_records = []
        ordinal = 0
        for item in node["items"]:
            if item["node"] == "star":
                # SELECT * expands in FROM order; joined relations keep
                # their same-named columns as distinct output columns.
                # Duplicate output names are legal; the stable identity
                # is the output ordinal.
                for ds in scope:
                    for col in ds.columns:
                        ordinal += 1
                        out = self._column_from_star(
                            col, ds, ordinal, row_filters)
                        output_columns.append(out)
                        if record_kind in ("cte", "subquery",
                                           "statement") and union_arm is None:
                            output_records.append(self._record(
                                qid, record_kind, record_name, ordinal,
                                out.name, list(out.sources), out.expr,
                                out.filters,
                                group_by=[f["expr"]
                                          for f in group_filters],
                                union_arm=None, loc=out.loc))
                continue
            if item["node"] == "qualified_star":
                raise _err(UnsupportedSyntaxError, item,
                           "qualified star (alias.*) is not supported; "
                           "use a plain * or an explicit column list")
            ordinal += 1
            expr = item["expr"]
            self._assert_no_nested_aggregate(expr)
            analysis = self._analyze_expr(expr, scope)
            alias = item.get("alias")
            name = alias["name"] if alias else self._synthetic_name(
                expr, ordinal)
            loc = _loc(item)
            filters = list(row_filters)
            for src in analysis["sources"]:
                filters.extend(src.get("_filters", ()))
            clean_sources = [self._clean_source(s) for s in
                             analysis["sources"]]
            column = _Column(name, clean_sources, analysis["ast"],
                             self._dedupe_filters(filters), loc)
            output_columns.append(column)
            output_records.append(self._record(
                qid, record_kind, record_name, ordinal, name,
                clean_sources, analysis["ast"], filters,
                group_by=[f["expr"] for f in group_filters],
                union_arm=union_arm, loc=loc))

        dataset = _Dataset(alias=None, key=qid, kind="subquery",
                           columns=output_columns, loc=_loc(node))

        if cte_def is not None:
            self._apply_cte_column_names(cte_def, output_columns)

        # Emit records for CTE bodies and standalone subqueries.  Union
        # arms are emitted via the union machinery with arm context.
        if record_kind in ("cte", "subquery") and union_arm is None:
            self.records.extend(output_records)
        return dataset, output_records

    def _column_from_star(self, col, ds, ordinal, row_filters):
        loc = col.loc
        filters = list(row_filters) + list(col.filters)
        return _Column(col.name, list(col.sources), col.expr,
                       self._dedupe_filters(filters), loc)

    @staticmethod
    def _synthetic_name(expr, ordinal):
        if expr["node"] == "column":
            return expr["name"]
        return f"expr_{ordinal}"

    def _apply_cte_column_names(self, cte_def, columns):
        names = cte_def.get("column_names")
        if not names:
            return
        if len(names) != len(columns):
            loc = cte_def.get("loc")
            raise ResolutionError(
                f"CTE {cte_def['name']!r} declares {len(names)} column "
                f"names but its query produces {len(columns)} columns",
                loc[0] if loc else None, loc[1] if loc else None)
        for col, name_tok in zip(columns, names):
            col.name = name_tok.value

    # -- scope construction ---------------------------------------------

    def _build_scope(self, from_node, parent_ctes):
        """Return (scope datasets, filters contributed by joins)."""
        if from_node is None:
            return [], []
        if from_node["node"] == "join":
            left_scope, left_filters = self._build_scope(
                from_node["left"], parent_ctes)
            right_scope, right_filters = self._build_scope(
                from_node["right"], parent_ctes)
            on = from_node["on"]
            self._assert_no_aggregate(on, "JOIN ON")
            scope = left_scope + right_scope
            # Validate the ON expression against the joined scope.
            on_analysis = self._analyze_expr(on, scope)
            on_filter = {
                "type": "join_on",
                "join": from_node["type"],
                "expr": on_analysis["ast"],
                "loc": _loc(on),
            }
            for ds in right_scope:
                ds.join_type = from_node["type"]
            return scope, left_filters + right_filters + [on_filter]
        return [self._build_dataset(from_node, parent_ctes)], []

    def _build_dataset(self, factor, parent_ctes):
        if factor["node"] == "table":
            return self._build_table_dataset(factor, parent_ctes)
        if factor["node"] == "subquery":
            alias = factor["alias"]["name"]
            dataset, _ = self._analyze_query(
                factor["query"], parent_ctes, subq_alias=alias)
            dataset.alias = alias
            dataset.loc = _loc(factor)
            return dataset
        raise _err(ParseError, factor,
                   f"unsupported FROM factor {factor['node']!r}")

    def _build_table_dataset(self, factor, parent_ctes):
        loc = _loc(factor)
        try:
            if factor.get("schema") is not None:
                key = self.catalog.resolve_qualified(
                    factor["schema"], factor["name"])
            else:
                name = factor["name"]
                if name in parent_ctes:
                    return self._wrap_cte_dataset(parent_ctes[name], factor)
                if name in self.named_queries:
                    return self._wrap_named_query(
                        self.named_queries[name], factor)
                key = self.catalog.resolve_unqualified(name)
        except ResolutionError as exc:
            if exc.line is None:
                exc.line, exc.column = loc
            raise
        columns = []
        for col_name in self.catalog.columns_of(key):
            ref = {"schema": key[0], "table": key[1], "column": col_name}
            ast = {"node": "column", "qualifier": None,
                   "name": col_name, "name_quoted": False,
                   "qualifier_quoted": False}
            columns.append(_Column(col_name, [ref], ast, [], loc))
        alias_tok = factor.get("alias")
        alias = alias_tok["name"] if alias_tok else factor["name"]
        return _Dataset(alias, key, "table", columns, loc)

    def _wrap_cte_dataset(self, cte_dataset, factor):
        alias_tok = factor.get("alias")
        alias = alias_tok["name"] if alias_tok else factor["name"]
        loc = _loc(factor)
        columns = [_Column(c.name, list(c.sources), c.expr, list(c.filters),
                           c.loc or loc)
                   for c in cte_dataset.columns]
        return _Dataset(alias, cte_dataset.key, "cte", columns, loc)

    def _wrap_named_query(self, entry, factor):
        key, columns, _loc_ref = entry
        alias_tok = factor.get("alias")
        alias = alias_tok["name"] if alias_tok else factor["name"]
        loc = _loc(factor)
        cols = [_Column(c.name, list(c.sources), c.expr, list(c.filters),
                        c.loc or loc) for c in columns]
        return _Dataset(alias, key, "named_query", cols, loc)

    # -- column resolution ----------------------------------------------

    def _resolve_column(self, node, scope):
        name = node["name"]
        qualifier = node.get("qualifier")
        if qualifier is not None:
            matches = [ds for ds in scope if ds.alias == qualifier]
            if not matches:
                raise _err(UnknownNameError, node,
                           f"qualifier {qualifier!r} is not in scope; "
                           "visible relations: "
                           + ", ".join(sorted(ds.alias for ds in scope)))
            ds = matches[0]
            col = next((c for c in ds.columns if c.name == name), None)
            if col is None:
                raise _err(UnknownNameError, node,
                           f"column {qualifier}.{name} does not exist on "
                           f"relation {qualifier!r}")
            return ds, col
        candidates = []
        for ds in scope:
            for col in ds.columns:
                if col.name == name:
                    candidates.append((ds, col))
        if not candidates:
            raise _err(UnknownNameError, node,
                       f"unqualified column {name!r} does not exist in any "
                       "visible relation")
        if len(candidates) > 1:
            where = ", ".join(sorted(ds.alias for ds, _ in candidates))
            raise _err(AmbiguousNameError, node,
                       f"unqualified column {name!r} is ambiguous; visible "
                       f"in relations: {where}")
        return candidates[0]

    # -- expression analysis --------------------------------------------

    def _analyze_expr(self, node, scope):
        kind = node["node"]
        if kind == "literal":
            return {"ast": self._clean_ast(node), "sources": []}
        if kind == "column":
            ds, col = self._resolve_column(node, scope)
            ast = {"node": "column",
                   "qualifier": ds.alias,
                   "name": col.name,
                   "qualifier_quoted": bool(node.get("qualifier_quoted")),
                   "name_quoted": bool(node.get("name_quoted"))}
            return {
                "ast": ast,
                "sources": [
                    dict(src, _filters=tuple(col.filters))
                    for src in col.sources
                ],
            }
        if kind == "binary":
            left = self._analyze_expr(node["left"], scope)
            right = self._analyze_expr(node["right"], scope)
            ast = {"node": "binary", "op": node["op"],
                   "left": left["ast"], "right": right["ast"]}
            return {"ast": ast,
                    "sources": left["sources"] + right["sources"]}
        if kind == "unary":
            inner = self._analyze_expr(node["operand"], scope)
            return {"ast": {"node": "unary", "op": node["op"],
                            "operand": inner["ast"]},
                    "sources": inner["sources"]}
        if kind == "is_null":
            inner = self._analyze_expr(node["operand"], scope)
            return {"ast": {"node": "is_null", "operand": inner["ast"],
                            "negated": node["negated"]},
                    "sources": inner["sources"]}
        if kind == "like":
            left = self._analyze_expr(node["left"], scope)
            right = self._analyze_expr(node["right"], scope)
            return {"ast": {"node": "like", "left": left["ast"],
                            "right": right["ast"],
                            "negated": node["negated"]},
                    "sources": left["sources"] + right["sources"]}
        if kind == "between":
            operand = self._analyze_expr(node["operand"], scope)
            low = self._analyze_expr(node["low"], scope)
            high = self._analyze_expr(node["high"], scope)
            return {"ast": {"node": "between", "operand": operand["ast"],
                            "low": low["ast"], "high": high["ast"],
                            "negated": node["negated"]},
                    "sources": operand["sources"] + low["sources"]
                    + high["sources"]}
        if kind == "in":
            left = self._analyze_expr(node["left"], scope)
            values = [self._analyze_expr(v, scope) for v in node["values"]]
            return {
                "ast": {"node": "in", "left": left["ast"],
                        "values": [v["ast"] for v in values],
                        "negated": node["negated"]},
                "sources": left["sources"]
                + [s for v in values for s in v["sources"]],
            }
        if kind == "case":
            return self._analyze_case(node, scope)
        if kind == "function":
            return self._analyze_function(node, scope, aggregate=False)
        if kind == "aggregate":
            return self._analyze_function(node, scope, aggregate=True)
        raise _err(UnsupportedSyntaxError, node,
                   f"unsupported expression node {kind!r}")

    def _analyze_case(self, node, scope):
        sources = []
        whens = []
        for when in node["whens"]:
            cond = self._analyze_expr(when["cond"], scope)
            result = self._analyze_expr(when["result"], scope)
            whens.append({"node": "when", "cond": cond["ast"],
                          "result": result["ast"]})
            sources.extend(cond["sources"])
            sources.extend(result["sources"])
        ast = {"node": "case", "whens": whens, "else_": None}
        if node.get("else_") is not None:
            else_analysis = self._analyze_expr(node["else_"], scope)
            ast["else_"] = else_analysis["ast"]
            sources.extend(else_analysis["sources"])
        return {"ast": ast, "sources": sources}

    def _analyze_function(self, node, scope, aggregate):
        args = [self._analyze_expr(a, scope) for a in node["args"]]
        sources = [s for a in args for s in a["sources"]]
        ast = {"node": "aggregate" if aggregate else "function",
               "name": node["name"],
               "args": [a["ast"] for a in args],
               "distinct": node["distinct"], "star": node["star"]}
        return {"ast": ast, "sources": sources}

    # -- aggregate validation -------------------------------------------

    def _assert_no_aggregate(self, node, clause):
        for sub in self._walk(node):
            if sub["node"] == "aggregate":
                loc = _loc(sub)
                raise UnsupportedSyntaxError(
                    f"aggregate function {sub['name']} is not allowed in "
                    f"{clause}", loc[0], loc[1])

    def _assert_no_nested_aggregate(self, node):
        for sub in self._walk(node):
            if sub["node"] != "aggregate":
                continue
            for inner in self._walk(sub):
                if inner is sub:
                    continue
                if inner["node"] == "aggregate":
                    loc = _loc(inner)
                    raise UnsupportedSyntaxError(
                        "nested aggregate functions are not supported",
                        loc[0], loc[1])

    @staticmethod
    def _walk(node):
        if not isinstance(node, dict) or "node" not in node:
            return
        yield node
        for key, value in node.items():
            if key in ("loc",):
                continue
            if isinstance(value, dict) and "node" in value:
                yield from Analyzer._walk(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and "node" in item:
                        yield from Analyzer._walk(item)

    # -- filters / records ----------------------------------------------

    def _filter_dict(self, node, scope, ftype, join):
        analysis = self._analyze_expr(node, scope)
        out = {"type": ftype, "expr": analysis["ast"], "loc": _loc(node)}
        if join is not None:
            out["join"] = join
        return out

    @staticmethod
    def _dedupe_filters(filters):
        seen, out = set(), []
        for f in filters:
            marker = str(f)
            if marker not in seen:
                seen.add(marker)
                out.append(f)
        return out

    @staticmethod
    def _clean_source(src):
        return {"schema": src["schema"], "table": src["table"],
                "column": src["column"]}

    @staticmethod
    def _clean_ast(node):
        if isinstance(node, dict):
            return {k: Analyzer._clean_ast(v) for k, v in node.items()
                    if k != "loc"}
        if isinstance(node, list):
            return [Analyzer._clean_ast(v) for v in node]
        return node

    def _record(self, qid, record_kind, record_name, ordinal, name,
                sources, expr, filters, group_by=None, union_arm=None,
                loc=None):
        row_conditions = self._dedupe_filters(
            [f for f in filters if f["type"] != "group_by"])
        return {
            "file": self.file_name,
            "statement": self.statement_index,
            "query_id": qid,
            "record_kind": record_kind,
            "record_name": record_name,
            "output_ordinal": ordinal,
            "output_name": name,
            "base_sources": self._dedupe_sources(sources),
            "expr": expr,
            "union_branch": union_arm,
            "row_conditions": row_conditions,
            "group_by_exprs": group_by or [],
            "loc": list(loc) if loc else None,
        }

    @staticmethod
    def _dedupe_sources(sources):
        seen, out = set(), []
        for src in sources:
            key = (src["schema"], src["table"], src["column"])
            if key not in seen:
                seen.add(key)
                out.append({"schema": src["schema"],
                            "table": src["table"],
                            "column": src["column"]})
        return out

    # -- UNION ALL -------------------------------------------------------

    def _analyze_union(self, node, parent_ctes, record_kind, record_name,
                       cte_def=None):
        arms = node["arms"]
        arm_results = []
        width = None
        for arm_index, arm in enumerate(arms, start=1):
            if arm["node"] != "select":
                raise _err(UnsupportedSyntaxError, arm,
                           "UNION ALL arms must be plain SELECT statements "
                           "(nested UNION is flattened)")
            arm_ctes = dict(parent_ctes)
            dataset, records = self._analyze_select(
                arm, arm_ctes, record_kind="union_arm",
                record_name=record_name, union_arm=arm_index)
            if width is None:
                width = len(dataset.columns)
            elif len(dataset.columns) != width:
                loc = _loc(arm)
                raise ResolutionError(
                    f"UNION ALL branch {arm_index} produces "
                    f"{len(dataset.columns)} columns but branch 1 produces "
                    f"{width}", loc[0], loc[1])
            arm_results.append((dataset, records))

        # Output names come from branch 1; arms align positionally, never
        # by alias name.
        first_cols = arm_results[0][0].columns
        union_qid = self._new_qid()
        union_columns = []
        for ordinal, first_col in enumerate(first_cols, start=1):
            sources, branch_mapping, filters = [], [], []
            for arm_index, (dataset, records) in enumerate(arm_results,
                                                           start=1):
                col = dataset.columns[ordinal - 1]
                sources.extend(col.sources)
                filters.extend(col.filters)
                branch_mapping.append({
                    "branch": arm_index,
                    "output_name": col.name,
                    "expr": col.expr,
                    "base_sources": self._dedupe_sources(col.sources),
                    "row_conditions": self._dedupe_filters(col.filters),
                })
            name = first_col.name
            expr = {"node": "union_all",
                    "branches": [b["expr"] for b in branch_mapping]}
            union_columns.append(_Column(
                name, self._dedupe_sources(sources), expr,
                self._dedupe_filters(filters), first_col.loc))
            record = {
                "file": self.file_name,
                "statement": self.statement_index,
                "query_id": union_qid,
                "record_kind": record_kind,
                "record_name": record_name,
                "output_ordinal": ordinal,
                "output_name": name,
                "base_sources": self._dedupe_sources(sources),
                "expr": expr,
                "union_branch": None,
                "branch_mapping": branch_mapping,
                "row_conditions": self._dedupe_filters(filters),
                "group_by_exprs": [],
                "loc": _loc(node),
            }
            self.records.append(record)

        dataset = _Dataset(alias=None, key=union_qid, kind="subquery",
                           columns=union_columns, loc=_loc(node))
        if cte_def is not None:
            self._apply_cte_column_names(cte_def, union_columns)
        return dataset, None
