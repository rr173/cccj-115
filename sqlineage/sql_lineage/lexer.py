"""Hand-written lexical analyser for the supported SQL subset.

The lexer understands:

* unquoted identifiers (``my_col``) and double-quoted identifiers
  (``"My Col"``), with ``""`` as the in-string quote escape;
* single-quoted string literals (``'it''s'``) with ``''`` escape;
* integer/decimal/exponential numeric literals;
* line comments (``-- ...``) and block comments (``/* ... */``);
* the symbol set used by the supported grammar.

Keywords are recognised case-insensitively and emitted as their upper
case spelling.  Comment and string contents are never rescanned, so a
keyword written inside a comment or literal stays inert.
"""

from dataclasses import dataclass

from .errors import LexicalError

KEYWORDS = frozenset({
    "WITH", "AS", "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS",
    "NATURAL", "ON", "USING", "UNION", "ALL", "CASE", "WHEN", "THEN",
    "ELSE", "END", "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE",
    "BETWEEN", "DISTINCT", "RECURSIVE", "ORDER",
})

# Multi-character operators first so the longest match wins.
_SYMBOLS = ("<>", "!=", "<=", ">=", "||", "(", ")", ",", ".", ";",
            "+", "-", "*", "/", "=", "<", ">")


@dataclass
class Token:
    kind: str          # keyword | ident | quoted | string | number | op
    value: object      # upper keyword / identifier text / literal value / symbol
    line: int
    column: int

    def __repr__(self):
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.column})"


def tokenize(sql):
    """Return the list of :class:`Token` objects for ``sql``."""
    tokens = []
    i, line, col = 0, 1, 1
    n = len(sql)

    def advance(ch):
        nonlocal i, line, col
        i += 1
        if ch == "\n":
            line += 1
            col = 1
        else:
            col += 1

    while i < n:
        ch = sql[i]

        if ch in " \t\r\n\f\v":
            advance(ch)
            continue

        start_line, start_col = line, col

        # Line comment.
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                advance(sql[i])
            continue

        # Block comment (non-nesting, like most SQL dialects).
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            advance(sql[i])
            advance(sql[i])
            while i < n and not (sql[i] == "*" and i + 1 < n
                                 and sql[i + 1] == "/"):
                advance(sql[i])
            if i >= n:
                raise LexicalError(
                    "unterminated block comment", start_line, start_col)
            advance(sql[i])
            advance(sql[i])
            continue

        # Single-quoted string literal.
        if ch == "'":
            advance(ch)
            parts = []
            while True:
                if i >= n:
                    raise LexicalError(
                        "unterminated string literal",
                        start_line, start_col)
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        parts.append("'")
                        advance(sql[i])
                        advance(sql[i])
                        continue
                    advance(sql[i])
                    break
                parts.append(sql[i])
                advance(sql[i])
            tokens.append(Token("string", "".join(parts),
                                start_line, start_col))
            continue

        # Double-quoted identifier.
        if ch == '"':
            advance(ch)
            parts = []
            while True:
                if i >= n:
                    raise LexicalError(
                        'unterminated quoted identifier',
                        start_line, start_col)
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        parts.append('"')
                        advance(sql[i])
                        advance(sql[i])
                        continue
                    advance(sql[i])
                    break
                parts.append(sql[i])
                advance(sql[i])
            tokens.append(Token("quoted", "".join(parts),
                                start_line, start_col))
            continue

        # Numeric literal.
        if ch.isdigit():
            start = i
            while i < n and sql[i].isdigit():
                advance(sql[i])
            if i < n and sql[i] == ".":
                advance(sql[i])
                while i < n and sql[i].isdigit():
                    advance(sql[i])
            if i < n and sql[i] in "eE":
                advance(sql[i])
                if i < n and sql[i] in "+-":
                    advance(sql[i])
                if i >= n or not sql[i].isdigit():
                    raise LexicalError(
                        "malformed exponent in numeric literal",
                        start_line, start_col)
                while i < n and sql[i].isdigit():
                    advance(sql[i])
            text = sql[start:i]
            value = float(text) if ("." in text or "e" in text or "E" in text) \
                else int(text)
            tokens.append(Token("number", value, start_line, start_col))
            continue

        # Bare identifier / keyword.
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                advance(sql[i])
            text = sql[start:i]
            upper = text.upper()
            if upper in KEYWORDS:
                tokens.append(Token("keyword", upper,
                                    start_line, start_col))
            else:
                tokens.append(Token("ident", text, start_line, start_col))
            continue

        # Symbol / operator.
        for sym in _SYMBOLS:
            if sql.startswith(sym, i):
                for c in sym:
                    advance(c)
                tokens.append(Token("op", sym, start_line, start_col))
                break
        else:
            raise LexicalError(
                f"unexpected character {ch!r}", start_line, start_col)

    return tokens


def split_statements(tokens):
    """Split a token stream at top-level semicolons.

    Returns a list of token lists.  Empty statements (``;;`` or a
    semicolon with nothing before it) are lexical errors; a single
    trailing semicolon is accepted.
    """
    statements, current = [], []
    for tok in tokens:
        if tok.kind == "op" and tok.value == ";":
            if not current:
                if statements:
                    raise LexicalError(
                        "empty statement", tok.line, tok.column)
                raise LexicalError(
                    "empty statement before first query",
                    tok.line, tok.column)
            statements.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        statements.append(current)
    return statements
