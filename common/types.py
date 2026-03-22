from __future__ import annotations

from enum import Enum


class MatcherType(str, Enum):
    """Matcher 类型枚举"""
    EVENT = "event"
    METRIC = "metric"
    DIMENSION = "dimension"
    TIME = "time"
