from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from bus.message_schema import BusMessage

logger = logging.getLogger(__name__)


class AgentWorker:
    """
    Agent Worker：从请求队列取出消息 → 执行 NL2SQL（生成并静态校验 SQL）→ 返回结果。

    设计要点：
    - 同步的 extract_llm() 通过 asyncio.to_thread() 包装，避免阻塞事件循环
    - 无状态处理，可横向扩展为多个 Worker 实例
    """

    async def process(self, msg: BusMessage) -> Dict[str, Any]:
        """
        处理单条消息

        Args:
            msg: 统一消息协议

        Returns:
            {"nl2sql": result.model_dump()}
        """
        from app import NL2SQLRequest, get_matcher_service, orchestrator, _send_telegram_notification

        logger.info(f"Worker processing: msg={msg.msg_id}, channel={msg.channel}, text={msg.text[:60]}")

        # 用 chat_id 作为 session_id，同一聊天的消息共享会话上下文
        req = NL2SQLRequest(
            text=msg.text,
            user_id=msg.user_id,
            project_id=msg.project_id,
            chat_id=msg.chat_id,
            session_id=f"tg_{msg.chat_id}",
        )

        service = get_matcher_service()
        result = await orchestrator.process(req, service, service.schema, notify_fn=_send_telegram_notification)
        logger.debug("Worker nl2sql done: status=%s, session=%s", result.get("status"), result.get("session_id"))

        # early_exit / needs_confirmation 场景：无 SQL 产出，直接返回
        if result.get("status") != "success":
            return {
                "nl2sql": result,
                "early_exit": result.get("status") == "early_exit",
                "message": result.get("message"),
            }

        return {
            "nl2sql": result,
            "early_exit": False,
        }

    async def run_loop(self, bus) -> None:
        """
        持续从 bus 取消息并处理（用于 Redis 模式）

        DirectCallBus 模式下不需要此方法。
        """
        logger.info("AgentWorker started in queue mode")
        while True:
            try:
                msg = await bus.dequeue_request(timeout=30)
                if msg is None:
                    continue

                logger.info(
                    "[Worker] ← Dequeued msg_id=%s user=%s chat=%s text=%.80s",
                    msg.msg_id, msg.user_id, msg.chat_id, msg.text,
                )
                result = await self.process(msg)
                logger.info(
                    "[Worker] → Enqueuing result msg_id=%s early_exit=%s has_sql=%s",
                    msg.msg_id, result.get("early_exit"), bool(result.get("nl2sql", {}).get("sql")),
                )
                from bus.message_schema import BusResult
                bus_result = BusResult(
                    msg=msg,
                    success=True,
                    nl2sql_result=result.get("nl2sql"),
                    error=result.get("message") if result.get("early_exit") else None,
                )
                await bus.enqueue_result(bus_result)

            except Exception as e:
                logger.exception(f"Worker loop error: {e}")
                await asyncio.sleep(1)
