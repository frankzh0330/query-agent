"""会话数据模型"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict


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
    user_id: str | None = None
    project_id: int = 55
    messages: list[Message] = field(default_factory=list)
    # 已解析的实体（跨轮次记忆）
    resolved_entities: Dict[str, Any] = field(default_factory=dict)
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

    def get_recent_messages(self, limit: int = 5) -> list[Message]:
        """获取最近的消息"""
        return self.messages[-limit:] if len(self.messages) > limit else self.messages

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "messages": [m.to_dict() for m in self.messages],
            "resolved_entities": self.resolved_entities,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }


@dataclass
class DailyMemory:
    """每日记忆（类似 OpenClaw 的 memory/YYYY-MM-DD.md）"""
    date: str
    entries: list[str] = field(default_factory=list)  # "用户A查询了德国PV，地区=germany，指标=pv"
