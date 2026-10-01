"""任务管理器：独立于 Session，管理查询任务的创建/确认/过期"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from memory.storage.memory_file import TaskStorage
from service.session_models import TaskContext, QueryState

logger = logging.getLogger(__name__)


class TaskManager:
    """任务管理器

    管理查询任务的完整生命周期：创建 → 确认 → 完成 → 过期。

    关键设计：
    - 与 Session 解耦：通过 task_id 索引，不挂在 session 上
    - TTL 机制：默认 30 分钟过期
    - 幂等：同一 task_id 多次 confirm 不会重复执行
    """

    DEFAULT_TTL_MINUTES = 30

    def __init__(self, ttl_minutes: int = DEFAULT_TTL_MINUTES, storage: TaskStorage | None = None):
        self._tasks: Dict[str, TaskContext] = {}
        self._ttl_minutes = ttl_minutes
        self.storage = storage

    def create_task(
        self,
        session_id: str,
        raw_query: str,
        extraction: Dict[str, Any],
        user_id: Optional[str] = None,
        candidates: Optional[Dict[str, list[Dict[str, Any]]]] = None,
        partial_query_state: Optional[QueryState] = None,
    ) -> TaskContext:
        """创建一个待确认/待执行任务

        Args:
            session_id: 所属会话
            raw_query: 用户原始输入
            extraction: LLM 提取结果
            user_id: 用户ID
            candidates: 候选项 {"event": [{"value": "x", "score": 0.9}]}
            partial_query_state: 正在构建中的查询状态

        Returns:
            TaskContext
        """
        task_id = uuid.uuid4().hex[:12]

        task = TaskContext(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            raw_query=raw_query,
            extraction=extraction,
            partial_query_state=partial_query_state,
            candidates=candidates or {},
            status="waiting_confirmation",
        )

        self._tasks[task_id] = task
        self._persist_task(task)
        logger.info(f"Task created: {task_id} (session={session_id}, status=waiting_confirmation)")
        return task

    def get_task(self, task_id: str) -> Optional[TaskContext]:
        """读取任务

        如果任务已过期，自动标记为 expired 并返回 None。
        """
        task = self._tasks.get(task_id)
        if not task:
            restored = self._restore_task(task_id)
            if restored:
                task = restored
            else:
                return None

        if task.is_expired(self._ttl_minutes):
            task.status = "expired"
            self._persist_task(task)
            logger.info(f"Task expired: {task_id}")
            return None

        return task

    def confirm_task(self, task_id: str, field: str, value: str) -> Optional[TaskContext]:
        """用户确认某个候选

        幂等：重复 confirm 同一 field 不会报错，只更新值。

        Args:
            task_id: 任务ID
            field: 确认的字段（如 "event"）
            value: 用户选择的值（如 "payment_success"）

        Returns:
            更新后的 TaskContext，如果任务不存在或已过期则返回 None
        """
        task = self.get_task(task_id)
        if not task:
            logger.warning(f"Task not found or expired: {task_id}")
            return None

        # 1. 记录用户选择
        task.user_selection[field] = value

        # 2. patch partial_query_state
        if task.partial_query_state:
            if field == "tables":
                task.partial_query_state.tables = [value]
            elif field == "metrics":
                task.partial_query_state.metrics = [value]
            elif field == "join":
                if value not in task.partial_query_state.tables:
                    task.partial_query_state.tables.append(value)
            elif field == "column":
                task.partial_query_state.columns.append(value)
            elif field == "group_by_column":
                if value not in (task.partial_query_state.group_by or []):
                    task.partial_query_state.group_by = [*(task.partial_query_state.group_by or []), value]
            elif field == "detail_column":
                if value not in (task.partial_query_state.detail_columns or []):
                    task.partial_query_state.detail_columns = [*(task.partial_query_state.detail_columns or []), value]

        # 3. 检查是否所有需要确认的字段都已确认
        all_confirmed = all(
            field in task.user_selection
            for field in task.candidates.keys()
        )

        if all_confirmed:
            task.status = "confirmed"

        task.updated_at = datetime.now()
        self._persist_task(task)
        logger.info(f"Task confirmed: {task_id}, field={field}, value={value}, all_confirmed={all_confirmed}")
        return task

    def update_status(self, task_id: str, status: str) -> Optional[TaskContext]:
        """更新任务状态

        状态机：waiting_confirmation → confirmed → completed
                                     → cancelled
                                     → expired（自动）
        """
        task = self.get_task(task_id)
        if not task:
            return None

        valid_transitions = {
            "waiting_confirmation": ["confirmed", "cancelled"],
            "confirmed": ["completed", "cancelled"],
        }

        current = task.status
        if status in valid_transitions.get(current, []):
            task.status = status
            task.updated_at = datetime.now()
            self._persist_task(task)
            logger.info(f"Task status updated: {task_id} {current} → {status}")
        else:
            logger.warning(f"Invalid status transition: {task_id} {current} → {status}")

        return task

    def expire_task(self, task_id: str) -> None:
        """手动标记任务过期"""
        task = self._tasks.get(task_id)
        if task:
            task.status = "expired"
            task.updated_at = datetime.now()
            self._persist_task(task)
            logger.info(f"Task manually expired: {task_id}")

    def cleanup_expired(self) -> int:
        """清理所有过期任务，返回清理数量"""
        to_remove = []
        for task_id, task in self._tasks.items():
            if task.is_expired(self._ttl_minutes) or task.status == "expired":
                to_remove.append(task_id)

        for task_id in to_remove:
            del self._tasks[task_id]
            if self.storage:
                self.storage.delete(task_id)

        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} expired tasks")

        return len(to_remove)

    def list_tasks(self, session_id: Optional[str] = None) -> list[TaskContext]:
        """列出任务，可按 session_id 过滤"""
        tasks = list(self._tasks.values())
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        return tasks

    def _persist_task(self, task: TaskContext) -> None:
        if not self.storage:
            return
        self.storage.append(
            task.task_id,
            {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "user_id": task.user_id,
                "raw_query": task.raw_query,
                "extraction": task.extraction,
                "partial_query_state": task.partial_query_state.to_dict() if task.partial_query_state else None,
                "candidates": task.candidates,
                "user_selection": task.user_selection,
                "status": task.status,
                "turn_type": task.turn_type,
                "resume_count": task.resume_count,
                "last_prompt": task.last_prompt,
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
            },
        )

    def _restore_task(self, task_id: str) -> Optional[TaskContext]:
        if not self.storage:
            return None
        latest = self.storage.read_latest(task_id)
        if not latest:
            return None

        partial_state = latest.get("partial_query_state")
        task = TaskContext(
            task_id=latest["task_id"],
            session_id=latest["session_id"],
            raw_query=latest["raw_query"],
            user_id=latest.get("user_id"),
            extraction=latest.get("extraction", {}),
            partial_query_state=QueryState(**partial_state) if partial_state else None,
            candidates=latest.get("candidates", {}),
            user_selection=latest.get("user_selection", {}),
            status=latest.get("status", "waiting_confirmation"),
            turn_type=latest.get("turn_type", "confirmation"),
            resume_count=latest.get("resume_count", 0),
            last_prompt=latest.get("last_prompt"),
            created_at=datetime.fromisoformat(latest["created_at"]),
            updated_at=datetime.fromisoformat(latest["updated_at"]),
        )
        self._tasks[task_id] = task
        return task
