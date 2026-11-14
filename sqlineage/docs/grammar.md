# 支持的 SQL 子集（形式化语法速查）

记号：`[]` 可选、`{}` 重复零到多次、`|` 选一。名称大小写规则：关键字
大小写不敏感，标识符（含双引号形式）与目录名称大小写敏感。

```
statement      := [with_clause] query_expr
with_clause    := WITH cte {"," cte}
cte            := ident ["(" ident {"," ident} ")"] AS "(" query_expr ")"
query_expr     := select_core { "UNION" "ALL" select_core }
select_core    := SELECT [ALL] select_item {"," select_item}
                  [FROM table_expr]
                  [WHERE expr]
                  [GROUP BY expr {"," expr}]
                  [HAVING expr]
select_item    := "*" | expr [ [AS] ident ]
table_expr     := table_factor { join_kind table_factor ON expr }
join_kind      := [INNER | LEFT [OUTER]] JOIN
table_factor   := [schema "."] table [ [AS] ident ]
                | "(" query_expr ")" [AS] ident        -- 子查询必须有别名
expr           := or_expr
or_expr        := and_expr { OR and_expr }
and_expr       := not_expr { AND not_expr }
not_expr       := [NOT] comparison
comparison     := additive
                  { ("=" | "<>" | "!=" | "<" | ">" | "<=" | ">=") additive
                    | IS [NOT] NULL
                    | [NOT] LIKE additive
                    | [NOT] "(" expr {"," expr} ")"
                    | [NOT] BETWEEN additive AND additive }
additive       := multiplicative { ("+"|"-") multiplicative }
multiplicative := unary { ("*"|"/"|"||") unary }
unary          := ("+"|"-") unary | primary
primary        := literal | column_ref | function_call
                | "(" expr ")" | case_expr
case_expr      := CASE { WHEN expr THEN expr } [ELSE expr] END
function_call  := ident "(" [ "*" | [DISTINCT] expr {"," expr} ] ")"
column_ref     := [ident "."] ident
literal        := number | "'string'" | NULL
```

## 规范化表达式树节点

| AST node | 字段 |
| --- | --- |
| `column` | `qualifier`（解析后的作用域别名）、`name`、`*_quoted` |
| `literal` | `value`、`type`（number/string/null） |
| `binary` | `op`（`+ - * / CONCAT = != < > <= >= AND OR`）、`left`、`right` |
| `unary` | `op`（`+ - NOT`）、`operand` |
| `is_null` | `operand`、`negated` |
| `like` | `left`、`right`、`negated` |
| `in` | `left`、`values[]`、`negated` |
| `between` | `operand`、`low`、`high`、`negated` |
| `case` | `whens[]{cond,result}`、`else_` |
| `function` / `aggregate` | `name`、`args[]`、`distinct`、`star` |
| `union_all` | `branches[]`（按位置的各分支表达式） |

所有节点在报告输出时去掉内部 `loc`，仅记录级与条件级保留 `[line,column]`。
`||` 在树中规范化为 `CONCAT`，`<>` 规范化为 `!=`。
