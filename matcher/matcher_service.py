from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from common.types import MatcherType
from matcher.column_matcher import ColumnMatcher, build_column_matcher_from_schema
from matcher.schema_loader import SQLSchema, load_sql_schema
from matcher.sql_metric_matcher import SQLMetricMatcher, build_sql_metric_matcher_from_schema
from matcher.table_matcher import TableMatcher, build_table_matcher_from_schema
from matcher.time_matcher import TimeMatcher, resolve_last_n_days

logger = logging.getLogger(__name__)


@dataclass
class ResolvedResult:
    """解析结果（含候选列表和置信度）"""
    value: str                      # 最终值（matched / inferred / default；列名为 "table.column"）
    score: float                    # 置信度 (0-100)
    method: str                     # "exact" | "fuzzy" | "default" | "inferred_from_metric" ...
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False  # 是否需要用户确认


# 需要确认的置信度区间
_CONFIRM_SCORE_HIGH = 80.0
_CONFIRM_SCORE_LOW = 40.0


class MatcherService:
    """Matcher 服务：表 / 列 / 业务指标 / 时间 的确定性解析

    匹配核心不变（倒排索引召回 + RapidFuzz 重排），目录从 event/metric/dimension
    换成 schema YAML 中的表/列/业务指标。
    """

    def __init__(self, catalog_path: str = "catalog"):
        logger.info("Initializing MatcherService (sql schema)...")

        self.schema: SQLSchema = load_sql_schema(catalog_path)

        logger.info("Building matcher indexes...")
        self.table_matcher = build_table_matcher_from_schema(self.schema.tables)
        self.column_matcher = build_column_matcher_from_schema(self.schema.columns)
        self.metric_matcher = build_sql_metric_matcher_from_schema(self.schema.metrics)
        self.time_matcher = TimeMatcher()
        logger.info("MatcherService initialized successfully!")

    # ==================== 带候选的解析方法 ====================

    def resolve_with_candidates(
        self, matcher_type: MatcherType, extractions: list, default: Optional[str] = None
    ) -> ResolvedResult:
        """从 LLM 提取结果中解析，返回完整候选和置信度

        判断规则（确定性代码，不依赖 LLM）：
        - score >= 80 → 直接用，不需确认
        - score 在 40-80 → 需要用户确认
        - score < 40 → 直接用 default，不确认（候选太模糊没价值）
        """
        if not extractions:
            return ResolvedResult(
                value=default or "", score=0.0, method="no_extractions",
                candidates=[], needs_confirmation=False,
            )

        first = extractions[0]
        query_text = first.text if hasattr(first, "text") else first.get("text", "")

        matcher = self._get_matcher(matcher_type)
        result = matcher.match(query_text)
        candidates = self._extract_candidates(result)

        if result.matched:
            if result.score >= _CONFIRM_SCORE_HIGH:
                return ResolvedResult(
                    value=result.matched, score=result.score,
                    method="exact" if result.score == 100.0 else "fuzzy",
                    candidates=candidates, needs_confirmation=False,
                )
            elif result.score >= _CONFIRM_SCORE_LOW:
                return ResolvedResult(
                    value=result.matched, score=result.score,
                    method="fuzzy_low_confidence",
                    candidates=candidates, needs_confirmation=True,
                )
            else:
                return ResolvedResult(
                    value=default or result.matched, score=result.score,
                    method="score_too_low",
                    candidates=candidates, needs_confirmation=False,
                )
        else:
            if candidates and result.score >= _CONFIRM_SCORE_LOW:
                return ResolvedResult(
                    value=default or "", score=result.score,
                    method="below_threshold",
                    candidates=candidates, needs_confirmation=True,
                )
            return ResolvedResult(
                value=default or "", score=result.score,
                method="no_match",
                candidates=candidates, needs_confirmation=False,
            )

    def resolve_time(self, extraction: Any) -> Tuple[int, Dict[str, Any]]:
        """解析时间范围 → (days, explain)；days 由 sql_generator 翻译为 CH 表达式"""
        return resolve_last_n_days(extraction)

    # ==================== 表推断 ====================

    def infer_main_table(
        self,
        resolved_tables: List[str],
        resolved_metrics: List[str],
        resolved_columns: List[str],
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """主表推断（用户经常不提表名）：

        1. 显式解析出的表 → 直接用
        2. 无表但有指标 → 从指标聚合表达式提取表（revenue = sum(orders.amount) → orders）
        3. 无表无指标但有列 → 按列归属表投票
        """
        if resolved_tables:
            return resolved_tables[0], {"method": "explicit", "tables": resolved_tables}

        # 1) 指标显式声明的归属表（count() 类无表引用表达式只能靠这个）
        # 2) 从指标聚合表达式中提取表（revenue = sum(orders.amount) → orders）
        for m_id in resolved_metrics:
            m_info = self.schema.metrics.get(m_id, {})
            declared = m_info.get("table")
            if declared and declared in self.schema.tables:
                return declared, {"method": "inferred_from_metric", "metric": m_id,
                                  "source": "declared_table"}
            expr = m_info.get("expr", "")
            for t_name in self.schema.tables:
                if f"{t_name}." in expr:
                    return t_name, {
                        "method": "inferred_from_metric",
                        "metric": m_id, "expr": expr,
                    }

        if resolved_columns:
            table_votes: Dict[str, int] = {}
            for qualified in resolved_columns:
                t = qualified.split(".")[0]
                table_votes[t] = table_votes.get(t, 0) + 1
            best = max(table_votes.items(), key=lambda kv: kv[1])[0]
            return best, {"method": "inferred_from_columns", "votes": table_votes}

        return None, {"method": "no_signal"}

    # ==================== join 推断 ====================

    def infer_joins(
        self, base_table: str, qualified_columns: List[str]
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        """根据查询涉及的列推断需要 join 的表

        列在非主表上（如 orders 查询涉及 users.region）→ 查 join 配置生成步骤。
        join 是无向匹配，输出统一归一化为 {left: base, right: peer, condition}。
        找不到 join 路径的表由调用方走确认流。
        """
        needed_tables = {q.split(".")[0] for q in qualified_columns}
        steps: List[Dict[str, str]] = []
        for t in sorted(needed_tables):
            if t == base_table:
                continue
            j = self.schema.find_join(base_table, t)
            if j:
                cond = j.get("condition") or j.get(True) or j.get("on") or ""
                peer = j["right"] if j["left"] == base_table else j["left"]
                steps.append({"left": base_table, "right": peer, "condition": cond})

        missing = sorted(
            t for t in needed_tables
            if t != base_table and t not in {s["right"] for s in steps}
        )
        explain = {
            "base_table": base_table,
            "needed_tables": sorted(needed_tables),
            "steps": steps,
            "missing_join": missing,
        }
        return steps, explain

    # ==================== 查询辅助 ====================

    def get_metric_expr(self, metric_id: str) -> str:
        return self.schema.metrics.get(metric_id, {}).get("expr", metric_id)

    def to_schema_prompt(self) -> str:
        """渲染 schema 摘要（SQL 生成 prompt 的上下文）"""
        return self.schema.to_schema_prompt()

    def _get_matcher(self, matcher_type: MatcherType):
        if matcher_type == MatcherType.TABLE:
            return self.table_matcher
        elif matcher_type == MatcherType.COLUMN:
            return self.column_matcher
        elif matcher_type == MatcherType.METRIC:
            return self.metric_matcher
        raise ValueError(f"Unknown matcher_type: {matcher_type}")

    @staticmethod
    def _extract_candidates(result) -> List[Dict[str, Any]]:
        """从 MatchResult.explain 中提取 top5 候选"""
        rerank_explain = result.explain.get("rerank_explain", {})
        top5 = rerank_explain.get("top5", [])
        recall_explain = result.explain.get("recall_explain", {})
        top_recall = recall_explain.get("top_candidates", [])

        if top5:
            return [
                {"value": c["name"], "score": c["score"]}
                for c in top5
            ]
        if top_recall:
            return [
                {"value": c["name"], "score": float(c.get("hit_count", 0))}
                for c in top_recall[:5]
            ]
        return []
