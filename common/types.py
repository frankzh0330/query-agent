from __future__ import annotations

from enum import Enum


class MatcherType(str, Enum):
    """Matcher 类型枚举"""
    TABLE = "table"
    COLUMN = "column"
    METRIC = "metric"
    TIME = "time"
