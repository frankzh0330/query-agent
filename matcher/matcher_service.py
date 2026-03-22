from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from common.types import MatcherType
from matcher.catalog_loader import Catalog, load_catalog
from matcher.dimension_matcher import build_dimension_matcher_from_catalog
from matcher.event_matcher import build_event_matcher_from_catalog
from matcher.metric_matcher import build_metric_matcher_from_catalog
from matcher.time_matcher import TimeMatcher, resolve_last_n_days

logger = logging.getLogger(__name__)


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
