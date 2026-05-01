"""会话数据模型"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class QueryState:
    """最近一次查询的完整语义状态（替代旧的 resolved_entities）

    覆盖查询的所有维度：event, metric, time, region, group_by, filters, chart_type。
    每次成功执行查询后更新，作为下一轮对话的默认补全来源。

    优先级：explicit user input > session state (last_query_state) > preference memory
    """

    project_id: int = 55

    # 主体
    event: Optional[str] = None
    metric: Optional[str] = None

    # 时间
    time_range: Optional[Dict[str, Any]] = None  # {"type": "last_n_days", "n": 7}

    # 维度 / 过滤
    region_filter: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    filters: list[Dict[str, Any]] = field(default_factory=list)
    # filters 格式: [{"field": "platform", "op": "=", "value": "ios"}, ...]

    # 展示 / 交互
    chart_type: Optional[str] = None
    interaction_mode: Optional[str] = None

    # 调试
    confidence: Optional[float] = None
    explicit_fields: list[str] = field(default_factory=list)
    inherited_fields: list[str] = field(default_factory=list)
    field_sources: Dict[str, str] = field(default_factory=dict)
    turn_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "event": self.event,
            "metric": self.metric,
            "time_range": self.time_range,
            "region_filter": self.region_filter,
            "group_by": self.group_by,
            "filters": self.filters,
            "chart_type": self.chart_type,
            "interaction_mode": self.interaction_mode,
            "confidence": self.confidence,
            "explicit_fields": self.explicit_fields,
            "inherited_fields": self.inherited_fields,
            "field_sources": self.field_sources,
            "turn_type": self.turn_type,
        }


@dataclass
class TaskContext:
    """单次查询任务的上下文

    当用户需要确认（如多个候选 event）时，创建 TaskContext 追踪状态。
    与 Session 解耦：通过 task_id 索引，不挂在 session 上。
    TTL 30 分钟，过期自动失效。
    """

    task_id: str
    session_id: str
    raw_query: str
    user_id: Optional[str] = None

    # LLM 提取结果
    extraction: Dict[str, Any] = field(default_factory=dict)

    # 正在构建中的 QueryState（尚未确认/执行）
    partial_query_state: Optional[QueryState] = None

    # 候选项: {"event": [{"value": "payment_submit", "score": 0.88}, ...]}
    candidates: Dict[str, list[Dict[str, Any]]] = field(default_factory=dict)

    # 用户已确认的选择: {"event": "payment_success"}
    user_selection: Dict[str, str] = field(default_factory=dict)

    # 状态：waiting_confirmation | confirmed | completed | cancelled | expired
    status: str = "waiting_confirmation"
    turn_type: str = "confirmation"
    resume_count: int = 0
    last_prompt: Optional[str] = None

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def is_expired(self, ttl_minutes: int = 30) -> bool:
        """检查任务是否过期"""
        age = (datetime.now() - self.created_at).total_seconds() / 60
        return age > ttl_minutes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "raw_query": self.raw_query,
            "extraction": self.extraction,
            "partial_query_state": self.partial_query_state.to_dict() if self.partial_query_state else None,
            "candidates": self.candidates,
            "user_selection": self.user_selection,
            "status": self.status,
            "turn_type": self.turn_type,
            "resume_count": self.resume_count,
            "last_prompt": self.last_prompt,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class Message:
    """单条消息"""
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class SessionContext:
    """会话上下文"""
    session_id: str
    user_id: Optional[str] = None
    project_id: int = 55
    messages: list[Message] = field(default_factory=list)

    # 最近一次"已确认 / 已执行成功"的查询状态
    last_query_state: Optional[QueryState] = None

    # 最近一次结果摘要（可选）
    last_result_summary: Optional[str] = None

    # 当前等待用户确认的任务 ID
    pending_task_id: Optional[str] = None
    last_turn_type: Optional[str] = None
    last_user_query: Optional[str] = None
    turn_index: int = 0

    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)

    def add_message(self, role: str, content: str, metadata: Dict[str, Any] | None = None) -> None:
        """添加消息到会话"""
        msg = Message(
            role=role,
            content=content,
            timestamp=datetime.now(),
            metadata=metadata or {}
        )
        self.messages.append(msg)
        self.last_active = datetime.now()
        if role == "user":
            self.last_user_query = content
            self.turn_index += 1

    def get_recent_messages(self, limit: int = 5) -> list[Message]:
        """获取最近的消息"""
        return self.messages[-limit:] if len(self.messages) > limit else self.messages

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "messages": [m.to_dict() for m in self.messages],
            "last_query_state": self.last_query_state.to_dict() if self.last_query_state else None,
            "last_result_summary": self.last_result_summary,
            "pending_task_id": self.pending_task_id,
            "last_turn_type": self.last_turn_type,
            "last_user_query": self.last_user_query,
            "turn_index": self.turn_index,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }


@dataclass
class DailyMemory:
    """每日记忆（类似 OpenClaw 的 memory/YYYY-MM-DD.md）"""
    date: str
    entries: list[str] = field(default_factory=list)
