from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from bus.message_schema import BusResult

logger = logging.getLogger(__name__)


class ResponseDispatcher:
    """
    响应分派器：根据消息的 channel 字段路由结果到对应 Gateway。

    职责：
    1. 从结果队列取出 BusResult
    2. 根据 msg.channel 查找对应 Gateway
    3. 格式化响应并调用 gateway.send_response()
    """

    def __init__(self):
        self._gateways: Dict[str, Any] = {}  # channel -> gateway instance

    def register(self, channel: str, gateway) -> None:
        """注册 Gateway 到指定 channel"""
        self._gateways[channel] = gateway
        logger.info(f"Dispatcher registered gateway for channel: {channel}")

    async def dispatch(self, result: BusResult) -> None:
        """分派结果到对应 Gateway"""
        msg = result.msg
        gateway = self._gateways.get(msg.channel)

        if not gateway:
            logger.error(f"No gateway registered for channel: {msg.channel}")
            return

        try:
            if result.success and result.nl2dsl_result:
                # 检查 early_exit
                if result.error and not result.query_result:
                    # early_exit 场景：发送错误提示
                    await gateway.send_response(msg.chat_id, {"text": result.error})
                    return

                # 正常结果：让 Gateway 自己格式化
                response_text = gateway.format_response(
                    result.nl2dsl_result, result.query_result
                )
                await gateway.send_response(msg.chat_id, {"text": response_text})
            else:
                error_msg = f"处理失败: {result.error or '未知错误'}"
                await gateway.send_response(msg.chat_id, {"text": error_msg})

        except Exception as e:
            logger.exception(f"Dispatch error for msg={msg.msg_id}: {e}")

    async def run_loop(self, bus) -> None:
        """
        持续从 bus 取结果并分派（用于 Redis 模式）

        DirectCallBus 模式下不需要此方法。
        """
        logger.info("ResponseDispatcher started in queue mode")
        while True:
            try:
                result = await bus.dequeue_result(timeout=30)
                if result is None:
                    continue
                await self.dispatch(result)
            except Exception as e:
                logger.exception(f"Dispatcher loop error: {e}")
                await asyncio.sleep(1)
