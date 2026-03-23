"""会话管理器"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict

from service.session_models import SessionContext

logger = logging.getLogger(__name__)


class SessionManager:
    """会话管理器（内存存储，可扩展 Redis）"""

    def __init__(self, max_messages: int = 20):
        self._sessions: Dict[str, SessionContext] = {}
        self._max_messages = max_messages

    def create_or_get(self, session_id: str | None, user_id: str | None, project_id: int) -> SessionContext:
        """创建或获取会话"""
        if session_id and session_id in self._sessions:
            ctx = self._sessions[session_id]
            ctx.last_active = datetime.now()
            logger.debug(f"Retrieved existing session: {session_id}")
            return ctx

        # 新会话
        new_id = session_id or str(uuid.uuid4())
        ctx = SessionContext(session_id=new_id, user_id=user_id, project_id=project_id)
        self._sessions[new_id] = ctx
        logger.info(f"Created new session: {new_id} (user_id={user_id}, project_id={project_id})")
        return ctx

    def add_message(self, session_id: str, role: str, content: str, metadata: Dict[str, Any] | None = None) -> None:
        """添加消息到会话"""
        if session_id not in self._sessions:
            logger.warning(f"Session not found: {session_id}")
            return

        ctx = self._sessions[session_id]
        ctx.add_message(role, content, metadata)

        # 限制消息数量
        if len(ctx.messages) > self._max_messages:
            removed = len(ctx.messages) - self._max_messages
            ctx.messages = ctx.messages[-self._max_messages:]
            logger.debug(f"Trimmed {removed} old messages from session: {session_id}")

    def get_context(self, session_id: str) -> Dict[str, Any]:
        """获取会话上下文（用于 LLM）"""
        if session_id not in self._sessions:
            return {}

        ctx = self._sessions[session_id]

        # 最近 5 条消息
        recent = ctx.get_recent_messages(limit=5)

        return {
            "recent_queries": [m.content for m in recent if m.role == "user"],
            "resolved_entities": ctx.resolved_entities,
            "project_id": ctx.project_id,
        }

    def update_entities(self, session_id: str, entities: Dict[str, Any]) -> None:
        """更新已解析的实体"""
        if session_id not in self._sessions:
            logger.warning(f"Session not found: {session_id}")
            return

        self._sessions[session_id].resolved_entities.update(entities)
        logger.debug(f"Updated entities for session {session_id}: {entities}")

    def get_session(self, session_id: str) -> SessionContext | None:
        """获取会话"""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[SessionContext]:
        """列出所有会话"""
        return list(self._sessions.values())

    def cleanup_inactive(self, max_age_minutes: int = 60) -> int:
        """清理不活跃的会话"""
        now = datetime.now()
        to_remove = []
        for sid, ctx in self._sessions.items():
            age = (now - ctx.last_active).total_seconds() / 60
            if age > max_age_minutes:
                to_remove.append(sid)

        for sid in to_remove:
            del self._sessions[sid]

        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} inactive sessions")

        return len(to_remove)
