# sql_lineage — 静态 SQL 字段级血缘

只做**静态分析**的独立 Python 模块：读取一组**有顺序**的 SQL 文件，结合 JSON
数据目录，为每个 SELECT 输出列生成字段级血缘，写成 JSONL 报告（一行一条输出
字段）。不连接数据库、不执行 SQL、不读取任何业务行；所有血缘事实只来自手写的
词法/语法分析器产生的语法树，绝不通过搜索 SQL 文本猜测来源。

## 1. 快速开始

```bash
# 分析（按给定文件顺序处理，成功后原子替换输出文件）
python -m sql_lineage run \
  --catalog demo/catalog.json \
  --sql demo/01_main.sql \
  --out report.jsonl

# 逐行流式读取报告，不加载整套报告
python -m sql_lineage read report.jsonl
python -m sql_lineage read report.jsonl --file 01_main.sql --statement 1
```

```python
from sql_lineage import Catalog, analyze_files
from sql_lineage import LineageReportReader

catalog = Catalog.load("catalog.json")
records = analyze_files(["a.sql", "b.sql"], catalog, "report.jsonl")

with LineageReportReader("report.jsonl") as reader:
    for record in reader:          # 每次只持有一行
        print(reader.line_number, record["output_name"],
              record["base_sources"])
```

## 2. JSON 数据目录格式

```json
{
  "relations": [
    {
      "schema": "sales",
      "table": "orders",
      "columns": ["id", "cust_id", "amount", "status"],
      "description": "可选字段，会被忽略但允许存在"
    }
  ]
}
```

* `schema`、`table`、`columns` 中的名称全部**大小写敏感**，与 SQL 中
  （含双引号标识符）的拼写逐字符比较；
* 重复的 `(schema, table)`、关系内重复列名、空列列表、非法 JSON 都会直接报错；
* 未加 schema 的表名按目录中**唯一匹配**解析：零个匹配报 unknown_name，
  多个匹配报 ambiguous_name 并列出全部候选；
* 带 schema 的限定表名必须精确命中，否则报 unknown_name。

## 3. 支持的 SQL 子集与明确拒绝项

| 类别 | 支持 | 明确拒绝（unsupported_syntax，不静默近似） |
| --- | --- | --- |
| WITH | 多个非递归 CTE、CTE 显式列名列表、后定义 CTE 引用先定义 CTE | `WITH RECURSIVE`、引用后续 CTE、CTE 自引用（unknown_name）、与已有关系重名 |
| 查询结构 | `SELECT` / `FROM` / `WHERE` / `GROUP BY` / `HAVING` | `ORDER BY`、`SELECT DISTINCT` |
| 关联 | `INNER JOIN`、`LEFT [OUTER] JOIN`，必须带 `ON` | `RIGHT`/`FULL`/`CROSS`/`NATURAL JOIN`、`JOIN ... USING`、逗号分隔表列表、括号表列表 |
| 名称 | 显式表别名、列别名（`AS` 或裸别名）、双引号标识符 | 子查询缺别名（parse_error）、三段式 catalog.schema.table |
| 列展开 | 裸 `SELECT *`，按 FROM 输入顺序展开，JOIN 两侧同名字段分别保留 | 限定星号 `t.*` |
| 表达式 | `+ - * / ||`、`= <> != < > <= >=`、`AND OR NOT`、`IS [NOT] NULL`、`[NOT] LIKE`、`[NOT] IN (常量列表)`、`[NOT] BETWEEN`、括号、一元正负号 | `IN (子查询)` |
| 条件表达式 | 搜索型 `CASE WHEN ... THEN ... ELSE ... END` | 简单型 `CASE x WHEN ...` |
| 聚合 | `COUNT/SUM/AVG/MIN/MAX`，`COUNT(*)`，聚合内 `DISTINCT` | WHERE/ON/GROUP BY 中出现聚合、嵌套聚合、非聚合函数上的 DISTINCT |
| 集合 | `UNION ALL`（按位置对齐分支） | 去重型 `UNION`、分支列数不一致（整条语句拒绝） |
| 词法 | 单引号字符串（`''` 转义）、双引号标识符（`""` 转义）、`--` 行注释、`/* */` 块注释、整数/小数/科学计数法 | 未闭合字符串/注释、非法字符、空语句、空文件 |

注释与字符串内的关键字不参与语法；双引号是标识符、单引号是字符串，二者严格
区分。

## 4. 解析与作用域规则

* 每个 SELECT 输出列有稳定的从 1 开始的 `output_ordinal` 与 `output_name`
  （别名优先，无别名的裸列用列名，其余用 `expr_N`）。
* CTE 与子查询的**列名、顺序、输出表达式**构成后续可见数据集；星号展开按
  FROM 顺序，JOIN 同名列作为不同输出列保留。
* 限定列 `t.col` 校验限定符在当前作用域内且列存在；未限定列在多个可见输入中
  重名时报 ambiguous_name，绝不任选一个。
* `GROUP BY` / `HAVING` 不改变输出列血缘，但它们的表达式树会保留在记录的
  `group_by_exprs` 与 `row_conditions` 中。
* `UNION ALL` 按位置而非别名对应；输出列名取第 1 分支，`branch_mapping`
  保留每个分支编号、该分支列名、表达式路径、来源与行条件。
* 同名别名只在本条语句作用域生效；跨语句、跨文件不串线。
* 常量表达式（如 `'x'`、`COUNT(*)`）没有基础表字段时，`base_sources`
  显式为 `[]`，输出列本身不会丢失。

## 5. JSONL 报告结构（每行一条输出字段）

```json
{
  "file": "01_main.sql",
  "statement": 1,
  "query_id": "q2",
  "record_kind": "statement",
  "record_name": null,
  "output_ordinal": 2,
  "output_name": "adjusted",
  "base_sources": [
    {"schema": "sales", "table": "orders", "column": "amount"}
  ],
  "expr": { "node": "binary", "op": "+", "left": {"..." : "..."},
            "right": {"node": "literal", "value": 1, "type": "number"} },
  "union_branch": null,
  "branch_mapping": [ {"branch": 1, "output_name": "...", "expr": {},
                       "base_sources": [], "row_conditions": []} ],
  "row_conditions": [
    {"type": "where", "expr": {"...": "..."}, "loc": [4, 7]},
    {"type": "join_on", "join": "INNER", "expr": {"...": "..."},
     "loc": [5, 31]},
    {"type": "having", "expr": {"...": "..."}, "loc": [8, 8]}
  ],
  "group_by_exprs": [ {"node": "column", "...": "..."} ],
  "loc": [3, 8]
}
```

* `record_kind`：`statement`（顶层语句输出）、`cte`（CTE 体输出）、
  `subquery`（派生表输出）；UNION 结果为 `statement`/`cte`，其分支细节在
  `branch_mapping` 中。
* `expr` 是规范化语法树（节点类型：`column/literal/binary/unary/is_null/
  like/between/in/case/function/aggregate/union_all`），叶子 `column`
  节点带解析后的作用域限定符。
* `row_conditions` 收集所有影响结果行集合的 WHERE、JOIN ON、HAVING 条件，
  并沿 CTE/子查询穿透到最终输出；`group_by_exprs` 单独保留 GROUP BY 表达式。
* 多文件成功后，每条顶层语句以 `文件名#语句序号` 发布命名查询，单语句文件
  额外提供文件 stem 短名，仅供**其后**的文件引用。

## 6. 失败语义与原子发布

* 任何文件中途解析/解析失败：报错携带 `error` 类别、`file`、`statement`、
  `line`、`column` 与原因；
* 失败时不会发布部分成功报告——目标文件保持**原字节**，同目录临时文件
  （`.tmp-<目标名>-*`）保证被清理；写入/重命名阶段失败同样清理；
* 重复文件名、关系/命名查询冲突、损坏 SQL、不支持语法均整体中止本次运行。

## 7. Python 读取接口

```python
from sql_lineage import LineageReportReader, iter_records, filter_records

with LineageReportReader("report.jsonl") as reader:
    for record in reader:
        ...                      # 逐行；reader.line_number 为已消费行号

for record in iter_records("report.jsonl"):     # 等价生成器
    ...

for record in filter_records("report.jsonl", file="a.sql", statement=1):
    ...
```

## 8. 测试

所有预期均为**手写**，不使用任何正式 SQL 解析器生成答案：

```bash
cd tests
PYTHONPATH=.. python3 -m unittest discover          # 50 项单元测试
PYTHONPATH=.. python3 e2e_check.py                  # 端到端/原子性/批次顺序
PYTHONPATH=.. python3 scale_check.py                # 大批量 + 峰值内存
```

覆盖：普通字段、表达式、星号与 JOIN 重名、前序 CTE 穿透、分支顺序不同的
UNION ALL、聚合与过滤条件、不同 schema 同名表、重复列歧义、引号与注释、
缺字段、列数冲突、不支持语法、损坏输入、跨文件作用域不串线，以及最后一个
文件故意语法错误时目标字节不变且无临时文件残留。
