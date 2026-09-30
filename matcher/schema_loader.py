"""SQL Schema 加载器

从本地 YAML 加载表/列/join/指标元数据（演示态）。
生产方向：从 metadata service 定时同步后调用本模块重建索引（见 README Production Notes）。

产出结构：
  tables:   {table_name: {aliases, description, time_column, columns: {col: {aliases, type}}}}
  joins:    [{left, right, on}]
  metrics:  {metric_id: {aliases, expr, description}}
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA_FILE = "sql_schema.yaml"


@dataclass(frozen=True)
class SQLSchema:
    tables: Dict[str, Dict]
    columns: Dict[str, Dict]          # "table.column" -> {table, column, aliases, type}
    joins: List[Dict] = field(default_factory=list)
    metrics: Dict[str, Dict] = field(default_factory=dict)
    table_alias_lookup: Dict[str, str] = field(default_factory=dict)
    column_alias_lookup: Dict[str, str] = field(default_factory=dict)
    metric_alias_lookup: Dict[str, str] = field(default_factory=dict)
    join_graph: Dict[str, List[Dict]] = field(default_factory=dict)  # table -> [{peer, condition}]

    def normalize_enum_value(self, qualified_column: str, value):
        """把自然语言取值规范化为列声明的枚举值（确定性，不依赖 LLM）

        返回 (value, method)：
          method = "not_enum"   列未声明 enum_values，原样返回
                 = "exact"      已是规范值
                 = "normalized" 大小写/空格/连字符差异（"Credit Card" -> "credit_card"）
                 = "fuzzy"      近似匹配（RapidFuzz >= 90）
                 = "unmatched"  没有对应枚举值，原样返回（调用方可据此告警）
        """
        enums = (self.columns.get(qualified_column) or {}).get("enum_values") or []
        if not enums or not isinstance(value, str):
            return value, "not_enum"
        if value in enums:
            return value, "exact"

        def _key(v: str) -> str:
            return "".join(ch for ch in v.lower() if ch.isalnum())

        by_key = {_key(e): e for e in enums}
        k = _key(value)
        if k in by_key:
            return by_key[k], "normalized"
        from rapidfuzz import fuzz, process

        best = process.extractOne(k, list(by_key.keys()), scorer=fuzz.ratio)
        if best and best[1] >= 90:
            return by_key[best[0]], "fuzzy"
        return value, "unmatched"

    def columns_of_table(self, table: str) -> Dict[str, Dict]:
        return self.tables.get(table, {}).get("columns", {})

    def time_column_of(self, table: str) -> str:
        return self.tables.get(table, {}).get("time_column", "")

    def find_join(self, left: str, right: str) -> Dict | None:
        """查找两表之间的 join 定义（无向）"""
        for j in self.joins:
            pair = {j["left"], j["right"]}
            if left in pair and right in pair:
                return j
        return None

    def join_path_from(self, base_table: str, needed_tables: set[str]) -> List[Dict]:
        """从主表出发，为所需表收集 join 步骤（单跳；多跳留待生产扩展）"""
        steps = []
        for t in sorted(needed_tables):
            if t == base_table:
                continue
            j = self.find_join(base_table, t)
            if j:
                steps.append(j)
        return steps

    def to_schema_prompt(self, max_tables: int = 10) -> str:
        """渲染给 LLM 的 schema 摘要（供 SQL 生成 prompt 使用）"""
        lines = []
        for t_name, t_info in list(self.tables.items())[:max_tables]:
            cols = ", ".join(
                f"{c} {c_info.get('type', 'String')}"
                for c, c_info in t_info.get("columns", {}).items()
            )
            desc = t_info.get("description", "")
            lines.append(f"- {t_name} ({desc}): {cols}")
        for j in self.joins:
            cond = j.get("condition") or j.get("on") or ""
            lines.append(f"- JOIN: {j['left']} ↔ {j['right']} ON {cond}")
        for m_id, m_info in self.metrics.items():
            lines.append(f"- METRIC {m_id} = {m_info['expr']}")
        return "\n".join(lines)


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def load_sql_schema(base_dir: str = "catalog") -> SQLSchema:
    """加载 SQL schema YAML 并构建别名查找表"""
    path = os.path.join(base_dir, DEFAULT_SCHEMA_FILE)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    schema_node = raw.get("schema", {})
    tables: Dict[str, Dict] = schema_node.get("tables", {}) or {}
    joins: List[Dict] = schema_node.get("joins", []) or []
    metrics: Dict[str, Dict] = schema_node.get("metrics", {}) or {}

    # 展开列为 "table.column" 文档（同名列可区分归属表）
    columns: Dict[str, Dict] = {}
    for t_name, t_info in tables.items():
        for c_name, c_info in (t_info.get("columns", {}) or {}).items():
            qualified = f"{t_name}.{c_name}"
            aliases = [c_name] + list(c_info.get("aliases", []) or [])
            columns[qualified] = {
                "table": t_name,
                "column": c_name,
                "type": c_info.get("type", "String"),
                "enum_values": list(c_info.get("enum_values", []) or []),
                "aliases": list(dict.fromkeys(aliases)),
            }

    table_alias_lookup: Dict[str, str] = {}
    for t_name, t_info in tables.items():
        for alias in [t_name] + list(t_info.get("aliases", []) or []):
            nk = _norm(alias)
            if nk:
                table_alias_lookup[nk] = t_name

    column_alias_lookup: Dict[str, str] = {}
    for qualified, c_info in columns.items():
        for alias in c_info["aliases"]:
            nk = _norm(alias)
            if nk:
                # 同名别名先到先得（确定性），歧义由 ColumnMatcher 候选列表暴露
                column_alias_lookup.setdefault(nk, qualified)

    metric_alias_lookup: Dict[str, str] = {}
    for m_id, m_info in metrics.items():
        for alias in [m_id] + list(m_info.get("aliases", []) or []):
            nk = _norm(alias)
            if nk:
                metric_alias_lookup[nk] = m_id

    # join 邻接表（无向）
    # 注意：YAML 1.1 会把裸 `on` 解析为布尔 True，因此约定 key 为 condition（兼容 on）
    join_graph: Dict[str, List[Dict]] = {}
    for j in joins:
        cond = j.get("condition") or j.get(True) or j.get("on") or ""
        join_graph.setdefault(j["left"], []).append({"peer": j["right"], "condition": cond})
        join_graph.setdefault(j["right"], []).append({"peer": j["left"], "condition": cond})
        j["condition"] = cond

    logger.debug(
        "SQL schema loaded: %d tables, %d columns, %d metrics",
        len(tables), len(columns), len(metrics),
    )
    return SQLSchema(
        tables=tables,
        columns=columns,
        joins=joins,
        metrics=metrics,
        table_alias_lookup=table_alias_lookup,
        column_alias_lookup=column_alias_lookup,
        metric_alias_lookup=metric_alias_lookup,
        join_graph=join_graph,
    )
