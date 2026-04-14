from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from bus.message_schema import BusMessage

logger = logging.getLogger(__name__)


class AgentWorker:
    """
    Agent Worker：从请求队列取出消息 → 执行 nl2dsl + execute_query → 返回结果。

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
            {"nl2dsl": result.model_dump(), "query": query_result}
        """
        from app import NL2DSLRequest, nl2dsl
        from service.bearer_service import execute_query

        req = NL2DSLRequest(
            text=msg.text,
            user_id=msg.user_id,
            project_id=msg.project_id,
            chat_id=msg.chat_id,
        )

        result = await nl2dsl(req)
        result_data = result.model_dump()

        # early_exit 场景（缺少 event_name 等）
        if result.status == "early_exit":
            return {
                "nl2dsl": result_data,
                "query": None,
                "early_exit": True,
                "message": result.message,
            }

        # 执行 Bearer 查询
        try:
            query_result = await execute_query(result.exec_dsl)
        except Exception as e:
            logger.exception(f"Bearer query failed for msg={msg.msg_id}: {e}")
            query_result = {"success": False, "error": str(e)}

        return {
            "nl2dsl": result_data,
            "query": query_result,
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

                result = await self.process(msg)
                from bus.message_schema import BusResult
                bus_result = BusResult(
                    msg=msg,
                    success=True,
                    nl2dsl_result=result.get("nl2dsl"),
                    query_result=result.get("query"),
                    error=result.get("message") if result.get("early_exit") else None,
                )
                await bus.enqueue_result(bus_result)

            except Exception as e:
                logger.exception(f"Worker loop error: {e}")
                await asyncio.sleep(1)
