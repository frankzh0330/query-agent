"""ClickHouse SQL 静态校验（sqlglot，确定性护栏）

校验链：
1. parse 必须成功（语法）
2. 单条语句，只允许 SELECT / WITH（只读强制）
3. 所有表引用 ⊆ schema 白名单（防幻觉表名）
4. 无 LIMIT 时自动注入（防全量拉取）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

DEFAULT_DIALECT = "clickhouse"
DEFAULT_LIMIT = 100
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.With)


@dataclass
class ValidationResult:
    ok: bool
    sql: str                    # 修正后的 SQL（如注入 LIMIT）
    errors: list[str] = field(default_factory=list)

    @property
    def error(self) -> str:
        return "; ".join(self.errors)


def validate_sql(sql: str, allowed_tables: Iterable[str], dialect: str = DEFAULT_DIALECT) -> ValidationResult:
    """校验并规范化 SQL；返回 ok=False 时 errors 给出原因（供修复重试）"""
    errors: list[str] = []
    sql = (sql or "").strip().rstrip(";").strip()

    # 去掉 markdown 代码块残留
    if sql.startswith("```"):
        first_newline = sql.find("\n")
        sql = sql[first_newline + 1:] if first_newline != -1 else sql
        sql = sql.replace("```", "").strip()

    # 1. parse
    try:
        expressions = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        return ValidationResult(ok=False, sql=sql, errors=[f"parse_error: {e}"])

    expressions = [e for e in expressions if e is not None]
    if len(expressions) != 1:
        return ValidationResult(
            ok=False, sql=sql,
            errors=[f"expected_single_statement, got {len(expressions)}"],
        )

    stmt = expressions[0]
    # 2. 只读强制（WITH 包裹的 CTE 也放行，最终以内部 SELECT 为准）
    root = stmt
    if isinstance(root, exp.With):
        inner = [e for e in root.find_all(exp.Select)]
        if not inner:
            return ValidationResult(ok=False, sql=sql, errors=["with_without_select"])
        root = inner[-1]
    if not isinstance(root, _ALLOWED_ROOTS):
        return ValidationResult(
            ok=False, sql=sql,
            errors=[f"readonly_violation: root statement is {type(root).__name__}, only SELECT/WITH allowed"],
        )
    forbidden = root.find(exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.TruncateTable)
    if forbidden is not None:
        return ValidationResult(
            ok=False, sql=sql,
            errors=[f"readonly_violation: contains {type(forbidden).__name__}"],
        )

    # 3. 表白名单（CTE 名不算表引用）
    allowed = set(allowed_tables)
    cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    used_tables = set()
    for table in root.find_all(exp.Table):
        name = table.name
        if not name or name in cte_names:
            continue
        used_tables.add(name)
        if name not in allowed:
            errors.append(f"unknown_table: {name} not in schema whitelist {sorted(allowed)}")

    # 4. LIMIT 注入（有窗口排名的查询由外部保证 limit，这里兜底聚合/明细）
    if not root.args.get("limit"):
        root = root.limit(DEFAULT_LIMIT, copy=False)
        sql = root.sql(dialect=dialect)

    if errors:
        return ValidationResult(ok=False, sql=sql, errors=errors)
    return ValidationResult(ok=True, sql=sql)
