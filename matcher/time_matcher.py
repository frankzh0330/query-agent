from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class TimeMatchResult:
    """时间匹配结果"""
    days: int
    time_type: str  # "last_n_days", "yesterday", "today", etc.
    explain: Dict[str, Any]


class TimeMatcher:
    """
    时间匹配器，支持多种时间表达式
    """

    DEFAULT_LAST_N_DAYS = 7

    # 时间模式定义
    PATTERNS = {
        "last_n_days": r"(最近|近|过去|last|past)\s*(\d+)\s*(天|days?|d)",
        "yesterday": r"昨天|yesterday",
        "today": r"今天|today",
        "this_week": r"本周|this week",
        "last_week": r"上周|last week",
        "this_month": r"本月|this month",
        "last_month": r"上月|last month",
    }

    def match(self, query: str) -> TimeMatchResult:
        """
        匹配时间表达式

        :param query: 时间表达式文本
        :return: TimeMatchResult
        """
        query = query.strip().lower()

        # 1. last_n_days 模式
        match = re.search(self.PATTERNS["last_n_days"], query, re.IGNORECASE)
        if match:
            try:
                days = int(match.group(2))
                return TimeMatchResult(
                    days=days,
                    time_type="last_n_days",
                    explain={
                        "method": "regex_match",
                        "input": query,
                        "pattern": "last_n_days",
                        "days": days,
                    },
                )
            except ValueError:
                pass

        # 2. yesterday
        if re.search(self.PATTERNS["yesterday"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=1,
                time_type="yesterday",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "yesterday",
                    "days": 1,
                },
            )

        # 3. today
        if re.search(self.PATTERNS["today"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=1,
                time_type="today",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "today",
                    "days": 1,
                },
            )

        # 4. this_week (7 days)
        if re.search(self.PATTERNS["this_week"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=7,
                time_type="this_week",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "this_week",
                    "days": 7,
                },
            )

        # 5. last_week (7 days)
        if re.search(self.PATTERNS["last_week"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=7,
                time_type="last_week",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "last_week",
                    "days": 7,
                },
            )

        # 6. this_month (30 days)
        if re.search(self.PATTERNS["this_month"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=30,
                time_type="this_month",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "this_month",
                    "days": 30,
                },
            )

        # 7. last_month (30 days)
        if re.search(self.PATTERNS["last_month"], query, re.IGNORECASE):
            return TimeMatchResult(
                days=30,
                time_type="last_month",
                explain={
                    "method": "regex_match",
                    "input": query,
                    "pattern": "last_month",
                    "days": 30,
                },
            )

        # 默认返回
        return TimeMatchResult(
            days=self.DEFAULT_LAST_N_DAYS,
            time_type="default",
            explain={
                "method": "fallback",
                "input": query,
                "default_days": self.DEFAULT_LAST_N_DAYS,
            },
        )


def resolve_last_n_days(extraction_json) -> tuple[int, Dict[str, Any]]:
    """
    从 LLM 提取结果中解析时间范围（duck typing：需要 time_extractions 属性）

    :param extraction_json: LLM 提取结果（SQLIntentJson）
    :return: (days, explain)
    """
    matcher = TimeMatcher()

    for extraction in getattr(extraction_json, "time_extractions", []) or []:
        text = extraction.text if hasattr(extraction, "text") else extraction.get("text", "")
        result = matcher.match(text)
        if result.time_type != "default":
            return result.days, result.explain

    return matcher.DEFAULT_LAST_N_DAYS, {
        "method": "fallback",
        "reason": "no valid time pattern found",
        "default": matcher.DEFAULT_LAST_N_DAYS,
    }


def time_range_from_explain(days: int, explain: Dict[str, Any]) -> Dict[str, Any]:
    """把 TimeMatcher 的解析结果折叠为 QueryState.time_range 结构"""
    return {"type": explain.get("pattern", "last_n_days"), "n": days}


# =========================
# 本地测试
# =========================

if __name__ == "__main__":
    matcher = TimeMatcher()

    test_queries = [
        "近7天",
        "最近30天",
        "过去3天",
        "last 7 days",
        "past 14 days",
        "昨天",
        "yesterday",
        "今天",
        "today",
        "本周",
        "this week",
        "上周",
        "last week",
        "本月",
        "this month",
        "上月",
        "last month",
        "无效的输入",
    ]

    for q in test_queries:
        result = matcher.match(q)
        print("=" * 60)
        print(f"query: {q}")
        print(f"days: {result.days}, type: {result.time_type}")
        print(f"explain: {result.explain}")
