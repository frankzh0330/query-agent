"""Turn-based follow-up query detection."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from service.session_models import QueryState

# 明确的 follow-up 信号，通常表示“在上一轮基础上修改”
_FOLLOWUP_PREFIXES = (
    "换成",
    "改成",
    "改为",
    "再按",
    "再看",
    "那",
    "还是",
    "也",
    "继续",
    "顺便",
    "然后",
    "对比",
    "比较",
)

_REGION_TOKENS = ("美国", "德国", "欧洲", "新加坡", "row", "usttp", "euttp")
_GROUP_BY_TOKENS = ("渠道", "国家", "地区", "城市", "平台")
_METRIC_TOKENS = ("uv", "pv", "arpu", "gmv", "留存", "转化")
_FOLLOWUP_SUFFIXES = ("呢", "吧", "一下", "看看", "再来")
_COMPARISON_PATTERNS = ("和昨天比", "和今天比", "和上周比", "同比", "环比")

_TIME_ONLY_TERMS = {
    "今天": {"time_range": {"type": "last_n_days", "n": 1}},
    "昨天": {"time_range": {"type": "last_n_days", "n": 1}},
    "本周": {"time_range": {"type": "last_n_days", "n": 7}},
    "这周": {"time_range": {"type": "last_n_days", "n": 7}},
    "本月": {"time_range": {"type": "last_n_days", "n": 30}},
    "这个月": {"time_range": {"type": "last_n_days", "n": 30}},
}


@dataclass
class FollowupDecision:
    """当前轮次是否应视为上一轮的 follow-up。"""

    mode: str  # "new_query" | "followup_patch" | "confirmation_reply"
    confidence: float
    is_followup: bool
    patch_hints: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    matched_signals: list[str] = field(default_factory=list)
    normalized_text: str = ""


def detect_followup(
    text: str,
    last_query_state: QueryState | None,
    pending_task: bool = False,
) -> FollowupDecision:
    """判断本轮是否为 follow-up。

    该函数只做确定性规则判断，不依赖 LLM。
    """
    normalized = (text or "").strip()
    if not normalized:
        return FollowupDecision(
            mode="new_query",
            confidence=0.0,
            is_followup=False,
            reason="empty_input",
            normalized_text=normalized,
        )

    if pending_task and _looks_like_confirmation_reply(normalized):
        return FollowupDecision(
            mode="confirmation_reply",
            confidence=0.98,
            is_followup=False,
            reason="pending_task_confirmation_reply",
            matched_signals=["confirmation_reply"],
            normalized_text=normalized,
        )

    if last_query_state is None:
        return FollowupDecision(
            mode="new_query",
            confidence=0.05,
            is_followup=False,
            reason="no_previous_state",
            normalized_text=normalized,
        )

    if normalized in _TIME_ONLY_TERMS:
        return FollowupDecision(
            mode="followup_patch",
            confidence=0.95,
            is_followup=True,
            patch_hints=_TIME_ONLY_TERMS[normalized],
            reason="time_only_term",
            matched_signals=["time_only_term"],
            normalized_text=normalized,
        )

    if any(normalized.startswith(prefix) for prefix in _FOLLOWUP_PREFIXES):
        return FollowupDecision(
            mode="followup_patch",
            confidence=0.9,
            is_followup=True,
            patch_hints={},
            reason="followup_prefix",
            matched_signals=_collect_patch_signals(normalized),
            normalized_text=normalized,
        )

    if any(pattern in normalized for pattern in _COMPARISON_PATTERNS):
        return FollowupDecision(
            mode="followup_patch",
            confidence=0.88,
            is_followup=True,
            patch_hints={},
            reason="comparison_phrase",
            matched_signals=_collect_patch_signals(normalized),
            normalized_text=normalized,
        )

    contextual_signals = _collect_patch_signals(normalized)
    if _looks_like_contextual_patch(normalized, contextual_signals):
        return FollowupDecision(
            mode="followup_patch",
            confidence=0.82,
            is_followup=True,
            patch_hints={},
            reason="contextual_patch_like_input",
            matched_signals=contextual_signals,
            normalized_text=normalized,
        )

    if len(normalized) <= 12 and _looks_like_short_patch(normalized):
        return FollowupDecision(
            mode="followup_patch",
            confidence=0.75,
            is_followup=True,
            patch_hints={},
            reason="short_patch_like_input",
            matched_signals=_collect_patch_signals(normalized),
            normalized_text=normalized,
        )

    return FollowupDecision(
        mode="new_query",
        confidence=0.2,
        is_followup=False,
        reason="looks_like_new_query",
        normalized_text=normalized,
    )


def _looks_like_confirmation_reply(text: str) -> bool:
    if text.isdigit():
        return True

    lowered = text.lower()
    confirmation_terms = {"第一个", "第二个", "第1个", "第2个", "就是这个", "选这个"}
    if lowered in confirmation_terms:
        return True
    return False


def _looks_like_short_patch(text: str) -> bool:
    patch_keywords = (
        "昨天",
        "今天",
        "本周",
        "本月",
        "uv",
        "pv",
        "美国",
        "德国",
        "欧洲",
        "渠道",
        "国家",
        "地区",
    )
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in patch_keywords)


def _collect_patch_signals(text: str) -> list[str]:
    lowered = text.lower()
    signals: list[str] = []

    if any(token in lowered for token in _REGION_TOKENS):
        signals.append("region_token")
    if any(token in lowered for token in _GROUP_BY_TOKENS):
        signals.append("group_by_token")
    if any(token in lowered for token in _METRIC_TOKENS):
        signals.append("metric_token")
    if any(token in text for token in _FOLLOWUP_SUFFIXES):
        signals.append("followup_suffix")
    if any(token in text for token in ("按", "拆", "看", "对比", "比较")):
        signals.append("patch_verb")
    if any(token in text for token in _COMPARISON_PATTERNS):
        signals.append("comparison_phrase")

    return signals


def _looks_like_contextual_patch(text: str, signals: list[str]) -> bool:
    if not signals:
        return False

    short_enough = len(text) <= 18
    if short_enough and any(signal in signals for signal in ("region_token", "metric_token", "group_by_token")):
        return True

    if "comparison_phrase" in signals:
        return True

    if "followup_suffix" in signals and any(
        signal in signals for signal in ("region_token", "metric_token", "group_by_token", "patch_verb")
    ):
        return True

    return False
