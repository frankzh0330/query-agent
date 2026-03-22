from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

from gateway.base import BaseGateway

logger = logging.getLogger(__name__)


class TelegramGateway(BaseGateway):
    """
    Telegram Bot Gateway

    使用 long polling 方式接收消息
    """

    def __init__(self, bot_token: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._running = False
        self._offset: int = 0  # 用于 long polling

    async def start(self) -> None:
        """启动 Gateway，开始轮询消息"""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured, gateway will not start")
            return

        self._running = True
        logger.info("TelegramGateway started")

        while self._running:
            try:
                await self._poll_updates()
            except Exception as e:
                logger.error(f"Error polling updates: {e}")
                await asyncio.sleep(5)  # 出错后等待 5 秒

    async def stop(self) -> None:
        """停止 Gateway"""
        self._running = False
        logger.info("TelegramGateway stopped")

    async def _poll_updates(self) -> None:
        """轮询获取更新"""
        url = f"{self.base_url}/getUpdates"
        params = {
            "offset": self._offset + 1,
            "timeout": 30,  # long polling timeout
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=35)
            data = response.json()

            if not data.get("ok"):
                logger.error(f"Failed to get updates: {data}")
                return

            updates = data.get("result", [])
            for update in updates:
                self._offset = update.get("update_id", self._offset)
                await self.handle_message(update)

    async def handle_message(self, update: Dict[str, Any]) -> None:
        """处理消息"""
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")

        if not chat_id or not text:
            return

        logger.info(f"Received message from {chat_id}: {text}")

        # 调用 nl2dsl
        try:
            from app import NL2DSLRequest, nl2dsl
            req = NL2DSLRequest(text=text)
            result = nl2dsl(req)

            # 发送响应
            response_text = self._format_response(result)
            await self.send_response(str(chat_id), {"text": response_text})

        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await self.send_response(str(chat_id), {"text": f"处理失败: {e}"})

    def _format_response(self, result: Any) -> str:
        """格式化响应消息"""
        semantic = result.semantic
        exec_dsl = result.exec_dsl

        return (
            f"**查询结果**\n"
            f"地区: {', '.join(semantic.get('region_filter', []))}\n"
            f"指标: {semantic.get('metric', {}).get('metric_id', 'N/A')}\n"
            f"事件: {semantic.get('event', {}).get('event_name', 'N/A')}\n"
            f"时间: 近 {semantic.get('time_range', {}).get('n', 0)} 天\n\n"
            f"```\n{json.dumps(exec_dsl, ensure_ascii=False, indent=2)}\n```"
        )

    async def send_response(self, recipient: str, response: Dict[str, Any]) -> None:
        """发送响应"""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": recipient,
            "text": response.get("text", ""),
            "parse_mode": "Markdown",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

            if not data.get("ok"):
                logger.error(f"Failed to send message: {data}")
            else:
                logger.info(f"Message sent to {recipient}")


# =========================
# Gateway 管理器
# =========================

class GatewayManager:
    """管理多个 Gateway 实例"""

    def __init__(self):
        self._gateways: Dict[str, BaseGateway] = {}

    def register(self, name: str, gateway: BaseGateway) -> None:
        """注册 Gateway"""
        self._gateways[name] = gateway
        logger.info(f"Gateway registered: {name}")

    async def start_all(self) -> None:
        """启动所有 Gateway"""
        tasks = [gw.start() for gw in self._gateways.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """停止所有 Gateway"""
        tasks = [gw.stop() for gw in self._gateways.values()]
        await asyncio.gather(*tasks, return_exceptions=True)


# 全局 Gateway 管理器
gateway_manager = GatewayManager()
