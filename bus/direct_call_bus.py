from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bus.base import MessageBus
from bus.message_schema import BusMessage, BusResult

logger = logging.getLogger(__name__)


class DirectCallBus(MessageBus):
    """
    内存直连实现：enqueue_request 直接调用 Worker 处理。

    不经过队列，用于开发和单进程部署。
    行为等价于重构前的直接函数调用。
    """

    def __init__(self):
        self._worker = None  # 延迟绑定
        self._dispatcher = None  # 延迟绑定

    def bind_worker(self, worker) -> None:
        """绑定 AgentWorker"""
        self._worker = worker

    def bind_dispatcher(self, dispatcher) -> None:
        """绑定 ResponseDispatcher"""
        self._dispatcher = dispatcher

    async def enqueue_request(self, msg: BusMessage) -> None:
        """直接调用 worker 处理，结果交给 dispatcher"""
        if not self._worker:
            raise RuntimeError("Worker not bound to DirectCallBus")

        logger.info(f"DirectCallBus: processing msg={msg.msg_id} from {msg.channel}")

        try:
            result = await self._worker.process(msg)
            bus_result = BusResult(
                msg=msg,
                success=True,
                nl2dsl_result=result.get("nl2dsl"),
                query_result=result.get("query"),
                error=result.get("message") if result.get("early_exit") else None,
            )
        except Exception as e:
            logger.exception(f"Worker error for msg={msg.msg_id}: {e}")
            bus_result = BusResult(
                msg=msg,
                success=False,
                error=str(e),
            )

        # 直接分派结果
        if self._dispatcher:
            await self._dispatcher.dispatch(bus_result)

    async def dequeue_request(self, timeout: float = 30) -> Optional[BusMessage]:
        """DirectCallBus 不支持 dequeue，消息直接处理"""
        raise NotImplementedError("DirectCallBus processes messages inline, no dequeue")

    async def enqueue_result(self, result: BusResult) -> None:
        """DirectCallBus 结果直接分派，不走队列"""
        if self._dispatcher:
            await self._dispatcher.dispatch(result)

    async def dequeue_result(self, timeout: float = 30) -> Optional[BusResult]:
        """DirectCallBus 不支持 dequeue"""
        raise NotImplementedError("DirectCallBus dispatches results inline, no dequeue")
