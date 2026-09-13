import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from app.domain.models import AppError, ErrorCode
from app.domain.semantics import SCHEMA

ALLOWED_FUNCTIONS = {
    "AND",
    "OR",
    "SUM",
    "COUNT",
    "AVG",
    "MIN",
    "MAX",
    "NULLIF",
    "COALESCE",
    "ROUND",
    "ABS",
    "CAST",
    "TRY_CAST",
    "DATE_TRUNC",
    "TIMESTAMP_TRUNC",
    "LOWER",
    "UPPER",
    "CASE",
    "IF",
}
FORBIDDEN_NODES = {
    "Insert",
    "Update",
    "Delete",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Copy",
    "Attach",
    "Detach",
    "Pragma",
    "Into",
    "Lock",
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Union",
    "Intersect",
    "Except",
    "Lateral",
    "Unnest",
    "Pivot",
    "TableSample",
    "Window",
    "Parameter",
    "Placeholder",
    "Dot",
    "Hint",
    "Fetch",
    "Values",
}


@dataclass(frozen=True)
class ValidatedSQL:
    original: str
    normalized: str
    query_hash: str
    max_rows: int


def reject(message: str) -> None:
    raise AppError(ErrorCode.SQL_REJECTED, message)


def validate_sql(sql: str, max_rows: int = 200) -> ValidatedSQL:
    if len(sql) > 12000 or not sql.strip():
        reject("SQL 为空或超过长度限制")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except (ParseError, ValueError, RecursionError):
        raise AppError(ErrorCode.SQL_REJECTED, "SQL 无法解析") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        reject("仅允许单条 SELECT 或安全 CTE")
    tree = statements[0]
    assert isinstance(tree, exp.Select)
    nodes = list(tree.walk())
    if len(nodes) > 500 or any(type(node).__name__ in FORBIDDEN_NODES for node in nodes):
        reject("SQL 包含不允许的操作或过于复杂")
    if any(node.args.get("recursive") for node in tree.find_all(exp.With)):
        reject("不允许递归 CTE")
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    if cte_names & SCHEMA.keys():
        reject("CTE 不得覆盖白名单表名")
    tables = list(tree.find_all(exp.Table))
    if not tables:
        reject("查询必须使用业务白名单表")
    for table in tables:
        if not isinstance(table.this, exp.Identifier) or table.catalog or table.db:
            reject("不允许外部数据源或跨库查询")
        if table.name.lower() not in SCHEMA and table.name.lower() not in cte_names:
            reject("查询包含非白名单表")
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            reject("必须显式列出字段，禁止 SELECT *")
    for function in tree.find_all(exp.Func):
        name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name()
        if name not in ALLOWED_FUNCTIONS:
            reject("查询包含非白名单函数")
    for join in tree.find_all(exp.Join):
        if not join.args.get("on") or str(join.args.get("kind", "")).upper() == "CROSS":
            reject("不允许无关联条件的连接")
    if len(list(tree.find_all(exp.Join))) > 7:
        reject("连接数量超限")
    for limit in [*tree.find_all(exp.Limit), *tree.find_all(exp.Offset)]:
        number = limit.expression
        if not isinstance(number, exp.Literal) or not number.is_int or int(number.this) < 0:
            reject("LIMIT/OFFSET 必须是非负整数")
        if isinstance(limit, exp.Offset) and int(number.this) > 10000:
            reject("OFFSET 超限")
    try:
        tree = qualify(
            tree,
            dialect="duckdb",
            schema={table: columns for table, columns in SCHEMA.items()},
            validate_qualify_columns=True,
            quote_identifiers=True,
            identify=True,
        )
    except (OptimizeError, ValueError, KeyError):
        raise AppError(ErrorCode.SQL_REJECTED, "列不存在、列有歧义或别名无效") from None
    current_limit = tree.args.get("limit")
    row_limit = min(int(current_limit.expression.this), max_rows) if current_limit else max_rows
    tree = tree.limit(row_limit, copy=False)
    normalized = tree.sql(dialect="duckdb", comments=False)
    return ValidatedSQL(sql, normalized, hashlib.sha256(normalized.encode()).hexdigest(), row_limit)
