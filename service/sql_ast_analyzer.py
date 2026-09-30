"""SQL AST 后置分析器（generate-then-validate 路线）

对 LLM 生成的 SQL 做 sqlglot AST 级分析，三类检查：

1. 结构校验
   - 列存在性（经 qualify 别名解析后对照 schema）
   - join 边必须在 schema joins 声明内，且 ON 列与声明条件一致（join_key_mismatch）
   - JOIN 缺 ON → 笛卡尔积错误
2. 实体保真（对抗语义漂移）
   - 意图中的表必须出现在 SQL 中
   - 指标聚合表达式（如 sum(orders.amount)）必须存在
   - 过滤谓词（列 op 值）必须存在
3. 静态成本（无数据库近似，真值靠 ClickHouse EXPLAIN，见 README Production Notes）
   - 扫描量估算（schema est_rows）、大扫描警告
   - 事实表有时间列却未被任何条件引用 → full_scan 警告
   - join 链长度、子查询嵌套深度

确定性代码，不依赖 LLM。错误回灌 SQL 生成修复循环；警告只进 explain。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify as sqlglot_qualify

logger = logging.getLogger(__name__)

DIALECT = "clickhouse"

_LARGE_SCAN_ROWS = 10_000_000
_FACT_TABLE_ROWS = 1_000_000
_MAX_JOINS = 3
_MAX_SUBQUERY_DEPTH = 2

_OP_MAP = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


@dataclass
class AnalysisContext:
    tables_columns: Dict[str, Set[str]]
    join_pairs: Set[frozenset]
    join_key_pairs: Set[frozenset]          # {("orders","user_id"), ("users","id")} 形式的合法 ON 列对
    time_columns: Dict[str, str]
    est_rows: Dict[str, int]


@dataclass
class AnalysisResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    cost: Dict[str, Any] = field(default_factory=dict)
    qualified: bool = False


def build_analysis_context(schema) -> Optional[AnalysisContext]:
    """从 SQLSchema 构建分析上下文（失败返回 None，分析跳过——兼容测试中的 mock schema）"""
    try:
        tables_columns = {
            t: set((info.get("columns") or {}).keys())
            for t, info in (schema.tables or {}).items()
        }
        join_pairs: Set[frozenset] = set()
        join_key_pairs: Set[frozenset] = set()
        for j in schema.joins or []:
            left, right = j["left"], j["right"]
            join_pairs.add(frozenset({left, right}))
            for key_pair in _parse_join_condition(j.get("condition", "")):
                join_key_pairs.add(key_pair)
        time_columns = {
            t: info["time_column"]
            for t, info in (schema.tables or {}).items()
            if info.get("time_column")
        }
        est_rows = {
            t: int(info.get("est_rows", 0) or 0)
            for t, info in (schema.tables or {}).items()
        }
        return AnalysisContext(tables_columns, join_pairs, join_key_pairs, time_columns, est_rows)
    except Exception as e:
        logger.warning("build_analysis_context failed (analysis skipped): %s", e)
        return None


def _parse_join_condition(condition: str) -> List[frozenset]:
    """'orders.user_id = users.id' → {frozenset({('orders','user_id'), ('users','id')})}"""
    if not condition:
        return []
    try:
        cond = sqlglot.parse_one(condition, dialect=DIALECT)
    except sqlglot.errors.ParseError:
        return []
    pairs = []
    if isinstance(cond, exp.Binary):
        cols = [c for c in (cond.left, cond.right) if isinstance(c, exp.Column)]
        if len(cols) == 2:
            pairs.append(frozenset({(c.table, c.name) for c in cols}))
    return pairs


def _norm(s: str) -> str:
    return (s or "").lower().replace(" ", "")


def _build_alias_map(ast: exp.Expression) -> Dict[str, str]:
    """alias → 真实表名（FROM orders o → {'o': 'orders'}）"""
    alias_map: Dict[str, str] = {}
    for t in ast.find_all(exp.Table):
        alias = t.alias
        if alias and alias != t.name:
            alias_map[alias] = t.name
    return alias_map


def _resolve_table(col: exp.Column, alias_map: Dict[str, str]) -> Optional[str]:
    qualifier = col.table
    if not qualifier:
        return None
    if qualifier in alias_map:
        return alias_map[qualifier]
    return qualifier


def _apply_alias_map(text: str, alias_map: Dict[str, str]) -> str:
    for alias, real in alias_map.items():
        text = text.replace(f"{alias}.", f"{real}.")
    return text


def _value_repr(value: Any) -> str:
    """过滤值 → 与 sqlglot Literal 渲染对齐的规范形（'vip' / 1000）"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    if s.replace(".", "", 1).isdigit():
        return s
    return "'" + s.lower() + "'"


def _predicate_key(column: str, op: str, value: Any) -> str:
    return _norm(f"{column} {op} {_value_repr(value)}")


def _collect_predicates(ast: exp.Expression, alias_map: Dict[str, str]) -> Set[str]:
    """AST 中所有 列 op 字面量 谓词的规范 key 集合"""
    keys: Set[str] = set()
    for node in ast.find_all(exp.Binary):
        op = _OP_MAP.get(node.key)
        if not op:
            continue
        # sqlglot Binary 操作数在 this / expression 两个 arg 上
        left, right = node.this, node.args.get("expression")
        col, lit = None, None
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            col, lit = left, right
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            col, lit = right, left
        if col is None or lit is None:
            continue
        table = _resolve_table(col, alias_map)
        col_full = f"{table}.{col.name}" if table else col.name
        # lit.sql() 已是最终渲染形（'vip' / 1000），直接拼接，不再过 _value_repr
        keys.add(_norm(f"{col_full} {op} {lit.sql(dialect=DIALECT).lower()}"))
    return keys


def analyze_sql(sql: str, ctx: AnalysisContext, intent: Dict[str, Any]) -> AnalysisResult:
    """主入口：结构校验 + 实体保真 + 静态成本"""
    result = AnalysisResult()
    try:
        ast = sqlglot.parse_one(sql, dialect=DIALECT)
    except sqlglot.errors.ParseError as e:
        result.errors.append(f"parse_error: {e}")
        return result

    # qualify：别名解析 + 列校验（失败则降级用原始 AST 尽力分析）
    qualified = ast
    try:
        schema_map = {t: {c: "String" for c in cols} for t, cols in ctx.tables_columns.items()}
        qualified = sqlglot_qualify(
            ast.copy(), schema=schema_map, dialect=DIALECT,
            validate_qualify_columns=True,
            quote_identifiers=False, identify=False,
        )
        result.qualified = True
    except Exception as e:
        logger.debug("qualify failed, fallback to raw ast: %s", e)
        qualified = ast

    alias_map = _build_alias_map(qualified)

    # ==================== 表 ====================
    used_tables = {t.name for t in qualified.find_all(exp.Table)}
    unknown_tables = used_tables - set(ctx.tables_columns)
    if unknown_tables:
        result.errors.append(f"unknown_table: {sorted(unknown_tables)} 不在 schema 白名单")

    required_tables = {intent.get("base_table")} - {None}
    for j in intent.get("joins", []):
        required_tables.update({j.get("left"), j.get("right")})
    required_tables -= {None}
    missing_tables = required_tables - used_tables
    if missing_tables:
        result.errors.append(f"missing_table: 意图要求的表未出现在 SQL 中: {sorted(missing_tables)}")
    extra_tables = used_tables - required_tables
    if extra_tables:
        result.warnings.append({
            "code": "extra_table", "tables": sorted(extra_tables),
            "message": "SQL 引用了意图之外的表（可能是多余 join）",
        })

    # ==================== 列存在性 ====================
    for col in qualified.find_all(exp.Column):
        table = _resolve_table(col, alias_map)
        if table is None or table not in ctx.tables_columns:
            continue
        if col.name not in ctx.tables_columns[table]:
            result.errors.append(f"unknown_column: {table}.{col.name} 不在 schema 中")

    # ==================== join 一致性 ====================
    joins = list(qualified.find_all(exp.Join))
    for join in joins:
        on = join.args.get("on")
        if on is None:
            result.errors.append("cartesian_join: JOIN 缺少 ON 条件")
            continue
        on_cols = [c for c in on.find_all(exp.Column)]
        on_tables = {_resolve_table(c, alias_map) for c in on_cols}
        on_tables.discard(None)
        if len(on_tables) >= 2:
            pair = frozenset(list(on_tables)[:2])
            if pair not in ctx.join_pairs:
                result.errors.append(f"undeclared_join_edge: {sorted(pair)} 不在 schema joins 配置中")
                continue
            qualified_cols = [c for c in on_cols if _resolve_table(c, alias_map)]
            if len(qualified_cols) == 2:
                key_pair = frozenset(
                    (_resolve_table(c, alias_map), c.name) for c in qualified_cols
                )
                if key_pair not in ctx.join_key_pairs:
                    result.errors.append(
                        f"join_key_mismatch: ON {sorted(key_pair)} 与 schema 声明的 join 条件不一致"
                    )

    # ==================== 实体保真 ====================
    all_nodes_norm = {
        _norm(_apply_alias_map(n.sql(dialect=DIALECT), alias_map))
        for n in qualified.walk()
    }
    for m in intent.get("metrics", []):
        needle = _norm(m.get("expr", ""))
        if needle and needle not in all_nodes_norm:
            result.errors.append(
                f"missing_metric_expr: 指标 {m.get('id')} 的表达式 {m.get('expr')} 未出现在 SQL 中"
            )

    ast_predicates = _collect_predicates(qualified, alias_map)
    for f in intent.get("filters", []):
        needle = _predicate_key(f.get("column", ""), f.get("op", "="), f.get("value"))
        if needle and needle not in ast_predicates:
            result.errors.append(
                f"missing_filter: 过滤条件 {f.get('column')} {f.get('op')} {f.get('value')} 未出现在 SQL 中"
            )

    # ==================== 时间过滤（成本相关警告）====================
    base = intent.get("base_table")
    time_col = ctx.time_columns.get(base or "")
    if intent.get("time_expr") and base and time_col:
        has_time = any(
            c.name == time_col and _resolve_table(c, alias_map) == base
            for c in qualified.find_all(exp.Column)
        )
        if not has_time:
            result.warnings.append({
                "code": "missing_time_filter",
                "message": f"{base}.{time_col} 未被任何条件引用，可能全表扫描",
            })

    # ==================== GROUP BY 一致性（CK 宽松，仅警告）====================
    seen_group_warnings = set()
    for sel in qualified.find_all(exp.Select):
        group_cols = sel.args.get("group") or []
        if not group_cols:
            continue
        group_norm = {_norm(_apply_alias_map(c.sql(dialect=DIALECT), alias_map)) for c in group_cols}
        for e in sel.expressions:
            for col in e.find_all(exp.Column):
                if col.find_ancestor(exp.AggFunc) is not None:
                    continue
                col_norm = _norm(_apply_alias_map(col.sql(dialect=DIALECT), alias_map))
                if col_norm not in group_norm and col_norm not in seen_group_warnings:
                    seen_group_warnings.add(col_norm)
                    result.warnings.append({
                        "code": "non_grouped_column", "column": col_norm,
                        "message": f"非聚合列 {col_norm} 不在 GROUP BY 中（ClickHouse 宽松，但通常非本意）",
                    })

    # ==================== 静态成本 ====================
    scanned = sum(ctx.est_rows.get(t, 0) for t in used_tables)
    join_count = len(joins)
    subquery_count = len(list(qualified.find_all(exp.Subquery)))

    for t in sorted(used_tables):
        # 仅对主表检查时间列：join 进来的维表（如 users）不需要时间过滤
        if t != base or not ctx.time_columns.get(t, ""):
            continue
        t_time = ctx.time_columns[t]
        if ctx.est_rows.get(t, 0) > _FACT_TABLE_ROWS:
            referenced = any(
                c.name == t_time and _resolve_table(c, alias_map) == t
                for c in qualified.find_all(exp.Column)
            )
            if not referenced:
                result.warnings.append({
                    "code": "full_scan_on_fact_table", "table": t,
                    "message": f"大表 {t}({ctx.est_rows[t]} 行) 的时间列 {t_time} 未被过滤，存在全表扫描风险",
                })

    if scanned > _LARGE_SCAN_ROWS:
        result.warnings.append({
            "code": "large_scan", "estimated_rows": scanned,
            "message": f"估算扫描量约 {scanned:,} 行（无选择率折减），建议确认时间/过滤条件",
        })
    if join_count > _MAX_JOINS:
        result.warnings.append({"code": "too_many_joins", "join_count": join_count,
                                "message": f"join 数量 {join_count} 超过 {_MAX_JOINS}"})
    if subquery_count > _MAX_SUBQUERY_DEPTH:
        result.warnings.append({"code": "deep_nesting", "subquery_count": subquery_count,
                                "message": f"子查询嵌套 {subquery_count} 层，超过 {_MAX_SUBQUERY_DEPTH}"})

    result.cost = {
        "estimated_rows_scanned": scanned,
        "tables_scanned": sorted(used_tables),
        "join_count": join_count,
        "subquery_count": subquery_count,
        "qualified": result.qualified,
    }
    return result
