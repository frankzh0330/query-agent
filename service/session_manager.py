"""会话管理器

支持 JSONL 持久化和文件恢复，集成长期记忆。

架构：
- Working Memory: 内存中的 SessionContext（带 JSONL 持久化）
- Long-Term Memory: 记忆文件（纠正/约束）

优先级链：explicit user input > session state (last_query_state) > memory corrections
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from memory.long_term_memory import LongTermMemory
from memory.storage.memory_file import SessionStorage
from service.session_models import Message, QueryState, SessionContext

logger = logging.getLogger(__name__)


class SessionManager:
    """会话管理器（JSONL 持久化 + 文件恢复）

    - Session 数据追加写入 JSONL 文件，服务重启后可恢复
    - 集成 LongTermMemory 读取纠正/约束记忆
    """

    def __init__(self, data_path: str = "data", max_messages: int = 20):
        self._sessions: Dict[str, SessionContext] = {}
        self._max_messages = max_messages
        self.storage = SessionStorage(f"{data_path}/sessions")
        self.long_term = LongTermMemory(f"{data_path}/memory")

    def create_or_get(
        self, session_id: str | None, user_id: str | None, project_id: int
    ) -> SessionContext:
        """创建或恢复会话"""
        # 内存命中
        if session_id and session_id in self._sessions:
            ctx = self._sessions[session_id]
            ctx.last_active = datetime.now()
            logger.debug(f"Retrieved existing session: {session_id}")
            return ctx

        # 尝试从 JSONL 文件恢复
        if session_id:
            ctx = self._load_from_storage(session_id, user_id, project_id)
            if ctx:
                self._sessions[session_id] = ctx
                logger.info(f"Restored session from file: {session_id}")
                return ctx

        # 创建新会话
        new_id = session_id or str(uuid.uuid4())
        ctx = SessionContext(
            session_id=new_id, user_id=user_id, project_id=project_id
        )
        self._sessions[new_id] = ctx
        logger.info(
            f"Created new session: {new_id} (user_id={user_id}, project_id={project_id})"
        )
        return ctx

    def _load_from_storage(
        self, session_id: str, user_id: str | None, project_id: int
    ) -> Optional[SessionContext]:
        """从 JSONL 文件恢复会话"""
        records = self.storage.read_tail(session_id, n=self._max_messages)
        if not records:
            return None

        ctx = SessionContext(
            session_id=session_id, user_id=user_id, project_id=project_id
        )
        for r in records:
            if r.get("type") == "message":
                try:
                    ctx.messages.append(
                        Message(
                            role=r["role"],
                            content=r["content"],
                            timestamp=datetime.fromisoformat(r["timestamp"]),
                            metadata=r.get("metadata", {}),
                        )
                    )
                except (KeyError, ValueError) as e:
                    logger.warning(f"Skipping invalid message record: {e}")
            elif r.get("type") == "query_state":
                try:
                    ctx.last_query_state = QueryState(**r["data"])
                except (TypeError, ValueError) as e:
                    logger.warning(f"Skipping invalid query_state record: {e}")

        ctx.last_active = datetime.now()
        return ctx

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        """添加消息到会话（内存 + 持久化）"""
        if session_id not in self._sessions:
            logger.warning(f"Session not found: {session_id}")
            return

        ctx = self._sessions[session_id]
        ctx.add_message(role, content, metadata)

        # 持久化到 JSONL
        self.storage.append(
            session_id,
            {
                "type": "message",
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "metadata": metadata or {},
            },
        )

        # 截断内存中的消息（文件保留完整记录）
        if len(ctx.messages) > self._max_messages:
            removed = len(ctx.messages) - self._max_messages
            ctx.messages = ctx.messages[-self._max_messages :]
            logger.debug(f"Trimmed {removed} old messages from session: {session_id}")

    def update_query_state(self, session_id: str, query_state: QueryState) -> None:
        """更新最近一次查询状态（内存 + 持久化）"""
        if session_id not in self._sessions:
            logger.warning(f"Session not found: {session_id}")
            return

        self._sessions[session_id].last_query_state = query_state
        self.storage.append(
            session_id,
            {
                "type": "query_state",
                "data": query_state.to_dict(),
                "timestamp": datetime.now().isoformat(),
            },
        )
        logger.debug(f"Updated last_query_state for session {session_id}")

    def get_query_state(self, session_id: str) -> Optional[QueryState]:
        """获取最近一次查询状态"""
        ctx = self._sessions.get(session_id)
        return ctx.last_query_state if ctx else None

    def get_context(self, session_id: str) -> Dict[str, Any]:
        """获取会话上下文（用于 LLM）"""
        if session_id not in self._sessions:
            return {}

        ctx = self._sessions[session_id]
        recent = ctx.get_recent_messages(limit=5)

        result: Dict[str, Any] = {
            "recent_queries": [m.content for m in recent if m.role == "user"],
            "project_id": ctx.project_id,
        }

        if ctx.last_query_state:
            result["last_query_state"] = ctx.last_query_state.to_dict()

        return result

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

    # ============ 增强上下文（集成长期记忆）============

    def get_enhanced_context(self, session_id: str) -> Dict[str, Any]:
        """获取增强上下文（会话 + 记忆文件）

        Returns:
            包含短期上下文（会话历史 + last_query_state）
            和长期记忆（纠正/约束）的字典
        """
        session = self._sessions.get(session_id)
        if not session:
            return {}

        context = self.get_context(session_id)

        # 按 project_id 分桶加载记忆文件内容（纠正/约束）
        memory_content = self.long_term.load_memory_context(
            project_id=session.project_id
        )
        if memory_content:
            context["memory_corrections"] = memory_content
            logger.debug(f"Enhanced context for {session_id}: injected {len(memory_content)} chars memory")

        return context
