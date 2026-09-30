"""会话数据模型"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class QueryState:
    """最近一次 SQL 查询的完整意图状态

    覆盖 SQL 生成所需的全部维度：tables / metrics / columns / filters /
    time_range / group_by / order_by / limit / window。
    每次成功生成 SQL 后更新，作为下一轮 follow-up 的默认补全来源。

    优先级：explicit user input > session state (last_query_state) > preference memory

    字段格式约定：
      tables   = ["orders"]
      metrics  = ["revenue", "order_count"]           # schema metrics id
      columns  = ["users.region"]                     # 涉及的限定列 "table.column"
      detail_columns = ["orders.order_id"]            # 明细展示列
      filters  = [{"column": "users.vip_level", "op": "=", "value": "vip"}]
      time_range = {"type": "last_n_days", "n": 7}    # type: last_n_days|yesterday|today|this_week|last_week|this_month|last_month
      order_by = {"metric": "revenue", "metric_expr": "sum(orders.amount)", "direction": "DESC", "limit": 5}
      window   = {"group_by": "users.region", "limit": 3}
    """

    project_id: int = 55

    # 主体
    tables: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    detail_columns: list[str] = field(default_factory=list)

    # 过滤 / 时间 / 分组
    filters: list[Dict[str, Any]] = field(default_factory=list)
    time_range: Optional[Dict[str, Any]] = None
    group_by: list[str] = field(default_factory=list)

    # 排序 / TopN / 窗口
    order_by: Optional[Dict[str, Any]] = None
    limit: Optional[int] = None
    window: Optional[Dict[str, Any]] = None

    # 调试
    confidence: Optional[float] = None
    explicit_fields: list[str] = field(default_factory=list)
    inherited_fields: list[str] = field(default_factory=list)
    field_sources: Dict[str, str] = field(default_factory=dict)
    turn_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "tables": self.tables,
            "metrics": self.metrics,
            "columns": self.columns,
            "detail_columns": self.detail_columns,
            "filters": self.filters,
            "time_range": self.time_range,
            "group_by": self.group_by,
            "order_by": self.order_by,
            "limit": self.limit,
            "window": self.window,
            "confidence": self.confidence,
            "explicit_fields": self.explicit_fields,
            "inherited_fields": self.inherited_fields,
            "field_sources": self.field_sources,
            "turn_type": self.turn_type,
        }


@dataclass
class TaskContext:
    """单次查询任务的上下文

    当用户需要确认（如表/指标/列多个候选）时，创建 TaskContext 追踪状态。
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

    # 候选项: {"table": [{"value": "orders", "score": 0.88}, ...]}
    candidates: Dict[str, list[Dict[str, Any]]] = field(default_factory=dict)

    # 用户已确认的选择: {"table": "orders"}
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

    # 最近一次"已确认 / 已成功生成 SQL"的查询状态
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
