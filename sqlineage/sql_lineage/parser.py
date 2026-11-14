"""Hand-written recursive-descent parser for the supported SQL subset.

The parser consumes tokens produced by :mod:`sql_lineage.lexer` and emits
plain-dict abstract syntax trees.  Every node carries a ``loc`` field
with the leading token's line/column so errors can be located precisely.

Unsupported but explicitly recognisable constructs (recursive CTEs,
RIGHT/FULL/CROSS/NATURAL joins, USING, DISTINCT, ORDER BY, comma joins,
...) raise :class:`UnsupportedSyntaxError` rather than being silently
misparsed.
"""

from .errors import ParseError, UnsupportedSyntaxError

AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})


def _node(kind, token, **fields):
    node = {"node": kind, **fields}
    if token is not None:
        node["loc"] = [token.line, token.column]
    return node


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    # -- token helpers ---------------------------------------------------

    @property
    def cur(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _loc(self):
        tok = self.cur
        return [tok.line, tok.column] if tok else None

    def eof(self):
        return self.pos >= len(self.tokens)

    def at_keyword(self, *words):
        tok = self.cur
        return tok is not None and tok.kind == "keyword" and tok.value in words

    def at_op(self, *syms):
        tok = self.cur
        return tok is not None and tok.kind == "op" and tok.value in syms

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_op(self, sym):
        tok = self.cur
        if tok is None or tok.kind != "op" or tok.value != sym:
            raise ParseError(
                f"expected {sym!r} but found "
                f"{('end of input' if tok is None else tok.value)!r}",
                tok.line if tok else None,
                tok.column if tok else None)
        return self.advance()

    def expect_keyword(self, word):
        tok = self.cur
        if tok is None or tok.kind != "keyword" or tok.value != word:
            raise ParseError(
                f"expected {word} but found "
                f"{('end of input' if tok is None else tok.value)!r}",
                tok.line if tok else None,
                tok.column if tok else None)
        return self.advance()

    def consume_keyword(self, word):
        if self.at_keyword(word):
            return self.advance()
        return None

    # -- entry point -----------------------------------------------------

    def parse_statement(self):
        node = self.parse_with_select()
        if not self.eof():
            tok = self.cur
            raise ParseError(
                f"unexpected token {tok.value!r} after complete statement",
                tok.line, tok.column)
        return node

    def parse_with_select(self):
        ctes = None
        if self.at_keyword("WITH"):
            ctes = self.parse_with()
        return self.parse_query_expr(ctes)

    def parse_with(self):
        kw = self.expect_keyword("WITH")
        if self.consume_keyword("RECURSIVE") is not None:
            raise UnsupportedSyntaxError(
                "recursive CTEs are not supported", kw.line, kw.column)
        ctes = []
        while True:
            name_tok = self._parse_identifier()
            column_names = None
            if self.at_op("("):
                # CTE column-name list: ( a, b, ... ) -- never a subquery
                # because the AS keyword must follow the close paren.
                self.advance()
                column_names = [self._parse_identifier()]
                while self.consume_op(","):
                    column_names.append(self._parse_identifier())
                self.expect_op(")")
            self.expect_keyword("AS")
            self.expect_op("(")
            body = self.parse_query_expr(None)
            self.expect_op(")")
            ctes.append({
                "name": name_tok.value,
                "quoted": name_tok.kind == "quoted",
                "loc": [name_tok.line, name_tok.column],
                "column_names": column_names,
                "body": body,
            })
            if not self.consume_op(","):
                break
        return ctes

    def consume_op(self, sym):
        if self.at_op(sym):
            return self.advance()
        return None

    def _parse_identifier(self):
        tok = self.cur
        if tok is None:
            raise ParseError("expected identifier but found end of input")
        if tok.kind not in ("ident", "quoted"):
            raise ParseError(
                f"expected identifier but found {tok.value!r}",
                tok.line, tok.column)
        return self.advance()

    # -- query expressions (UNION ALL) -----------------------------------

    def parse_query_expr(self, ctes):
        left = self.parse_select_core(ctes)
        if self.at_keyword("UNION"):
            kw = self.advance()
            if not self.consume_keyword("ALL"):
                raise UnsupportedSyntaxError(
                    "only UNION ALL is supported; UNION DISTINCT is not",
                    kw.line, kw.column)
            right = self.parse_query_expr(None)
            if right["node"] == "union":
                arms = right["arms"]
            else:
                arms = [right]
            return _node("union", kw, arms=[left] + arms)
        return left

    def parse_select_core(self, ctes):
        kw = self.expect_keyword("SELECT")
        if self.consume_keyword("DISTINCT") is not None:
            raise UnsupportedSyntaxError(
                "SELECT DISTINCT is not supported", kw.line, kw.column)
        self.consume_keyword("ALL")

        items = []
        while True:
            if self.at_op("*"):
                star = self.advance()
                items.append(_node("star", star))
            else:
                expr = self.parse_expr()
                alias = self._parse_optional_alias()
                item = _node("select_item", None, expr=expr, alias=alias)
                item["loc"] = list(expr["loc"])
                items.append(item)
            if not self.consume_op(","):
                break

        from_ = None
        if self.consume_keyword("FROM"):
            from_ = self.parse_table_expr()

        where = None
        if self.consume_keyword("WHERE"):
            where = self.parse_expr()

        group_by = None
        if self.at_keyword("GROUP"):
            gkw = self.advance()
            self.expect_keyword("BY")
            group_by = [self.parse_expr()]
            while self.consume_op(","):
                group_by.append(self.parse_expr())
            if self.at_keyword("ORDER"):
                self._reject_order(gkw)

        having = None
        if self.consume_keyword("HAVING"):
            having = self.parse_expr()

        if self.at_keyword("ORDER"):
            self._reject_order(self.advance())

        return _node("select", kw, ctes=ctes or [], items=items,
                     from_=from_, where=where, group_by=group_by,
                     having=having)

    def _reject_order(self, tok):
        raise UnsupportedSyntaxError(
            "ORDER BY is not supported", tok.line, tok.column)

    def _parse_optional_alias(self):
        # AS alias
        if self.consume_keyword("AS"):
            tok = self._parse_identifier()
            return {"name": tok.value, "quoted": tok.kind == "quoted",
                    "loc": [tok.line, tok.column]}
        # Bare alias: identifier not followed by an operator that would
        # make the expression ambiguous.
        tok = self.cur
        if tok is not None and tok.kind in ("ident", "quoted"):
            self.advance()
            return {"name": tok.value, "quoted": tok.kind == "quoted",
                    "loc": [tok.line, tok.column]}
        return None

    # -- FROM / JOIN -----------------------------------------------------

    def parse_table_expr(self):
        left = self.parse_table_factor()
        while True:
            join_type = "INNER"
            join_tok = self.cur
            if self.at_keyword("INNER"):
                self.advance()
                self.expect_keyword("JOIN")
            elif self.at_keyword("LEFT"):
                self.advance()
                self.consume_keyword("OUTER")
                self.expect_keyword("JOIN")
                join_type = "LEFT"
            elif self.at_keyword("JOIN"):
                self.advance()
            elif self.at_keyword("RIGHT", "FULL", "CROSS", "NATURAL"):
                bad = self.advance()
                raise UnsupportedSyntaxError(
                    f"{bad.value} JOIN is not supported",
                    bad.line, bad.column)
            elif self.at_op(","):
                bad = self.advance()
                raise UnsupportedSyntaxError(
                    "comma-separated table lists are not supported; "
                    "use an explicit JOIN ... ON",
                    bad.line, bad.column)
            else:
                break
            right = self.parse_table_factor()
            if self.at_keyword("USING"):
                bad = self.advance()
                raise UnsupportedSyntaxError(
                    "JOIN ... USING is not supported; use JOIN ... ON",
                    bad.line, bad.column)
            self.expect_keyword("ON")
            on = self.parse_expr()
            left = _node("join", join_tok, type=join_type, left=left,
                         right=right, on=on)
        return left

    def parse_table_factor(self):
        if self.at_op("("):
            open_tok = self.advance()
            if self.at_keyword("WITH", "SELECT"):
                sub = self.parse_query_expr(
                    self.parse_with() if self.at_keyword("WITH") else None)
                self.expect_op(")")
                alias = self._parse_required_alias(open_tok)
                return _node("subquery", open_tok, query=sub, alias=alias)
            raise UnsupportedSyntaxError(
                "parenthesised table lists are not supported",
                open_tok.line, open_tok.column)

        first = self._parse_identifier()
        parts = [first]
        while self.at_op("."):
            self.advance()
            parts.append(self._parse_identifier())
        if len(parts) > 2:
            raise UnsupportedSyntaxError(
                "three-part (catalog.schema.table) names are not supported",
                first.line, first.column)
        table = _node(
            "table", first,
            schema=(parts[0].value if len(parts) == 2 else None),
            name=(parts[1].value if len(parts) == 2 else parts[0].value),
            schema_quoted=(parts[0].kind == "quoted"),
            name_quoted=(parts[-1].kind == "quoted"))
        alias = self._parse_optional_alias()
        table["alias"] = alias
        return table

    def _parse_required_alias(self, token):
        alias = self._parse_optional_alias()
        if alias is None:
            raise ParseError(
                "derived tables (subqueries) must have an explicit alias",
                token.line, token.column)
        return alias

    # -- expressions -----------------------------------------------------

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        node = self.parse_and()
        while self.at_keyword("OR"):
            kw = self.advance()
            right = self.parse_and()
            node = _node("binary", kw, op="OR", left=node, right=right)
        return node

    def parse_and(self):
        node = parse_not = self.parse_not()
        while self.at_keyword("AND"):
            kw = self.advance()
            right = self.parse_not()
            node = _node("binary", kw, op="AND", left=node, right=right)
        return node

    def parse_not(self):
        if self.at_keyword("NOT"):
            kw = self.advance()
            return _node("unary", kw, op="NOT", operand=self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        node = self.parse_additive()
        while True:
            tok = self.cur
            if tok is not None and tok.kind == "op" and \
                    tok.value in ("=", "<>", "!=", "<", ">", "<=", ">="):
                self.advance()
                op = "!=" if tok.value == "<>" else tok.value
                node = _node("binary", tok, op=op, left=node,
                             right=self.parse_additive())
            elif self.at_keyword("IS"):
                self.advance()
                negated = self.consume_keyword("NOT") is not None
                self.expect_keyword("NULL")
                node = _node("is_null", tok, operand=node, negated=negated)
            elif self.at_keyword("LIKE") or (
                    self.at_keyword("NOT") and self._next_is_keyword("LIKE")):
                negated = self.consume_keyword("NOT") is not None
                kw = self.expect_keyword("LIKE")
                node = _node("like", kw, left=node,
                             right=self.parse_additive(), negated=negated)
            elif self.at_keyword("IN") or (
                    self.at_keyword("NOT") and self._next_is_keyword("IN")):
                negated = self.consume_keyword("NOT") is not None
                kw = self.expect_keyword("IN")
                node = self._parse_in(node, kw, negated)
            elif self.at_keyword("BETWEEN") or (
                    self.at_keyword("NOT")
                    and self._next_is_keyword("BETWEEN")):
                negated = self.consume_keyword("NOT") is not None
                kw = self.expect_keyword("BETWEEN")
                low = self.parse_additive()
                self.expect_keyword("AND")
                high = self.parse_additive()
                node = _node("between", kw, operand=node, low=low,
                             high=high, negated=negated)
            else:
                return node

    def _next_is_keyword(self, word):
        tok = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
        return tok is not None and tok.kind == "keyword" and tok.value == word

    def _parse_in(self, left, kw, negated):
        self.expect_op("(")
        if self.at_keyword("SELECT", "WITH"):
            raise UnsupportedSyntaxError(
                "IN (subquery) is not supported", kw.line, kw.column)
        values = [self.parse_expr()]
        while self.consume_op(","):
            values.append(self.parse_expr())
        self.expect_op(")")
        return _node("in", kw, left=left, values=values, negated=negated)

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.at_op("+", "-"):
            op_tok = self.advance()
            right = self.parse_multiplicative()
            node = _node("binary", op_tok, op=op_tok.value, left=node,
                         right=right)
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.at_op("*", "/", "||"):
            op_tok = self.advance()
            right = self.parse_unary()
            node = _node("binary", op_tok,
                         op=("CONCAT" if op_tok.value == "||" else op_tok.value),
                         left=node, right=right)
        return node

    def parse_unary(self):
        if self.at_op("+", "-"):
            op_tok = self.advance()
            return _node("unary", op_tok, op=op_tok.value,
                         operand=self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.cur
        if tok is None:
            raise ParseError("unexpected end of input in expression")

        if tok.kind == "number":
            self.advance()
            return _node("literal", tok, value=tok.value, type="number")

        if tok.kind == "string":
            self.advance()
            return _node("literal", tok, value=tok.value, type="string")

        if tok.kind == "keyword" and tok.value == "NULL":
            self.advance()
            return _node("literal", tok, value=None, type="null")

        if tok.kind == "keyword" and tok.value == "CASE":
            return self.parse_case()

        if tok.kind == "op" and tok.value == "(":
            self.advance()
            node = self.parse_expr()
            self.expect_op(")")
            return node

        if tok.kind in ("ident", "quoted"):
            # Function call?
            nxt = self.tokens[self.pos + 1] \
                if self.pos + 1 < len(self.tokens) else None
            if nxt is not None and nxt.kind == "op" and nxt.value == "(":
                return self.parse_function()
            return self.parse_column_ref()

        raise ParseError(
            f"unexpected token {tok.value!r} in expression",
            tok.line, tok.column)

    def parse_column_ref(self):
        first = self._parse_identifier()
        parts = [first]
        while self.at_op("."):
            self.advance()
            if self.at_op("*"):
                star = self.advance()
                raise UnsupportedSyntaxError(
                    "qualified star (alias.*) is not supported; use a "
                    "plain * or an explicit column list",
                    star.line, star.column)
            parts.append(self._parse_identifier())
        if len(parts) > 2:
            raise ParseError(
                "column references have at most a qualifier and a name",
                first.line, first.column)
        return _node(
            "column", first,
            qualifier=(parts[0].value if len(parts) == 2 else None),
            qualifier_quoted=(parts[0].kind == "quoted") if len(parts) == 2
            else False,
            name=parts[-1].value,
            name_quoted=parts[-1].kind == "quoted")

    def parse_function(self):
        name_tok = self.advance()
        fname = name_tok.value.upper()
        self.expect_op("(")
        distinct = False
        star = None
        args = []
        if self.at_op("*"):
            star = self.advance()
        elif not self.at_op(")"):
            if self.consume_keyword("DISTINCT"):
                distinct = True
            args.append(self.parse_expr())
            while self.consume_op(","):
                args.append(self.parse_expr())
        self.expect_op(")")
        kind = "aggregate" if fname in AGGREGATES else "function"
        if distinct and kind != "aggregate":
            raise UnsupportedSyntaxError(
                f"DISTINCT is only supported inside aggregate functions, "
                f"not {name_tok.value!r}",
                name_tok.line, name_tok.column)
        return _node(kind, name_tok, name=fname, args=args,
                     distinct=distinct, star=star is not None)

    def parse_case(self):
        kw = self.expect_keyword("CASE")
        if not self.at_keyword("WHEN"):
            raise UnsupportedSyntaxError(
                "the simple CASE (CASE <expr> WHEN ...) form is not "
                "supported; use searched CASE WHEN ...",
                kw.line, kw.column)
        whens = []
        while self.at_keyword("WHEN"):
            wtok = self.advance()
            cond = self.parse_expr()
            self.expect_keyword("THEN")
            result = self.parse_expr()
            whens.append(_node("when", wtok, cond=cond, result=result))
        else_expr = None
        if self.consume_keyword("ELSE"):
            else_expr = self.parse_expr()
        self.expect_keyword("END")
        return _node("case", kw, whens=whens, else_=else_expr)


def parse(tokens):
    """Parse one statement's token list into an AST."""
    return Parser(tokens).parse_statement()
