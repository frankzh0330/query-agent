from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from common.types import MatcherType
from matcher.catalog_loader import Catalog, load_catalog
from matcher.dimension_matcher import build_dimension_matcher_from_catalog
from matcher.event_matcher import build_event_matcher_from_catalog
from matcher.metric_matcher import build_metric_matcher_from_catalog
from matcher.time_matcher import TimeMatcher, resolve_last_n_days

logger = logging.getLogger(__name__)


@dataclass
class ResolvedResult:
    """解析结果（含候选列表和置信度）"""
    value: str                      # 最终值（matched 或 default）
    score: float                    # 置信度 (0-100)
    method: str                     # "exact" | "fuzzy" | "default"
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False  # 是否需要用户确认


# 需要确认的置信度区间
_CONFIRM_SCORE_HIGH = 80.0
_CONFIRM_SCORE_LOW = 40.0


class MatcherService:
    """
    Matcher 服务类，统一管理所有 matcher

    替代 router.py 的全局变量模式，使用面向对象设计
    """

    def __init__(self, catalog_path: str = "catalog"):
        """初始化服务，构建所有 matcher 索引"""
        logger.info("Initializing MatcherService...")

        # 加载 catalog
        self.catalog: Catalog = load_catalog(catalog_path)

        # 构建 matcher 索引
        logger.info("Building matcher indexes...")
        self.event_matcher = build_event_matcher_from_catalog(self.catalog.events)
        self.metric_matcher = build_metric_matcher_from_catalog(self.catalog.metrics)
        self.dimension_matcher = build_dimension_matcher_from_catalog(self.catalog.dimensions)
        self.time_matcher = TimeMatcher()
        logger.info("MatcherService initialized successfully!")

    # ==================== 匹配方法 ====================

    def match_event(self, query: str, default: str = "app_launch") -> Tuple[str, Dict[str, Any]]:
        """匹配事件"""
        result = self.event_matcher.match(query)
        if result.matched:
            return result.matched, result.explain
        return default, {"method": "fallback", "default": default}

    def match_metric(self, query: str, default: str = "pv") -> Tuple[str, Dict[str, Any]]:
        """匹配指标"""
        result = self.metric_matcher.match(query)
        if result.matched:
            return result.matched, result.explain
        return default, {"method": "fallback", "default": default}

    def match_dimension(self, query: str, default: str = "country") -> Tuple[str, Dict[str, Any]]:
        """匹配维度"""
        result = self.dimension_matcher.match(query)
        if result.matched:
            return result.matched, result.explain
        return default, {"method": "fallback", "default": default}

    def resolve_time(self, extraction: Any) -> Tuple[int, Dict[str, Any]]:
        """解析时间范围"""
        return resolve_last_n_days(extraction)

    # ==================== 批量解析方法 ====================

    def resolve_from_extractions(
        self, matcher_type: MatcherType, extractions: list, default: str
    ) -> Tuple[str, Dict[str, Any]]:
        """
        从 LLM 提取结果中解析

        :param matcher_type: MatcherType 枚举 (EVENT/METRIC/DIMENSION)
        :param extractions: LLM 提取结果列表
        :param default: 默认值
        """
        if not extractions:
            return default, {"method": "no_extractions", "default": default}

        first = extractions[0]
        query_text = first.text if hasattr(first, "text") else first.get("text", "")

        if matcher_type == MatcherType.EVENT:
            return self.match_event(query_text, default)
        elif matcher_type == MatcherType.METRIC:
            return self.match_metric(query_text, default)
        elif matcher_type == MatcherType.DIMENSION:
            return self.match_dimension(query_text, default)
        else:
            raise ValueError(f"Unknown matcher_type: {matcher_type}")

    # ==================== 带候选的解析方法 ====================

    def resolve_with_candidates(
        self, matcher_type: MatcherType, extractions: list, default: str
    ) -> ResolvedResult:
        """从 LLM 提取结果中解析，返回完整候选和置信度

        判断规则（确定性代码，不依赖 LLM）：
        - score >= 80 → 直接用，不需确认
        - score 在 40-80 → 需要用户确认
        - score < 40 → 直接用 default，不确认（候选太模糊没价值）
        """
        if not extractions:
            return ResolvedResult(
                value=default, score=0.0, method="no_extractions",
                candidates=[], needs_confirmation=False,
            )

        first = extractions[0]
        query_text = first.text if hasattr(first, "text") else first.get("text", "")

        # 获取 MatchResult（含 score 和 top5 候选）
        matcher = self._get_matcher(matcher_type)
        result = matcher.match(query_text)

        # 从 explain 中提取 top5 候选
        candidates = self._extract_candidates(result)

        if result.matched:
            # 有匹配结果
            if result.score >= _CONFIRM_SCORE_HIGH:
                return ResolvedResult(
                    value=result.matched, score=result.score,
                    method="exact" if result.score == 100.0 else "fuzzy",
                    candidates=candidates, needs_confirmation=False,
                )
            elif result.score >= _CONFIRM_SCORE_LOW:
                # 低置信度但有候选 → 需要确认
                return ResolvedResult(
                    value=result.matched, score=result.score,
                    method="fuzzy_low_confidence",
                    candidates=candidates, needs_confirmation=True,
                )
            else:
                # 分数太低，用 default
                return ResolvedResult(
                    value=default, score=result.score,
                    method="score_too_low",
                    candidates=candidates, needs_confirmation=False,
                )
        else:
            # 完全没匹配
            if candidates and result.score >= _CONFIRM_SCORE_LOW:
                # 有候选但没过 threshold → 需要确认
                return ResolvedResult(
                    value=default, score=result.score,
                    method="below_threshold",
                    candidates=candidates, needs_confirmation=True,
                )
            return ResolvedResult(
                value=default, score=result.score,
                method="no_match",
                candidates=candidates, needs_confirmation=False,
            )

    def _get_matcher(self, matcher_type: MatcherType):
        if matcher_type == MatcherType.EVENT:
            return self.event_matcher
        elif matcher_type == MatcherType.METRIC:
            return self.metric_matcher
        elif matcher_type == MatcherType.DIMENSION:
            return self.dimension_matcher
        raise ValueError(f"Unknown matcher_type: {matcher_type}")

    @staticmethod
    def _extract_candidates(result) -> List[Dict[str, Any]]:
        """从 MatchResult.explain 中提取 top5 候选"""
        rerank_explain = result.explain.get("rerank_explain", {})
        top5 = rerank_explain.get("top5", [])
        recall_explain = result.explain.get("recall_explain", {})
        top_recall = recall_explain.get("top_candidates", [])

        # 优先用 rerank 的 top5（更精确），fallback 到 recall 的 top
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
