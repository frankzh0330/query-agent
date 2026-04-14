from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

from bus.message_schema import BusMessage
from gateway.base import BaseGateway
from ingress.telegram_adapter import TelegramAdapter

logger = logging.getLogger(__name__)


class TelegramGateway(BaseGateway):
    """
    Telegram Bot Gateway

    使用 long polling 方式接收消息。
    通过 MessageBus 将消息传递给 AgentWorker 处理，不再直接调用业务逻辑。
    """

    def __init__(self, bot_token: Optional[str] = None, bus=None):
        super().__init__(channel="telegram", bus=bus)
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
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 Gateway"""
        self._running = False
        logger.info("TelegramGateway stopped")

    async def _poll_updates(self) -> None:
        """轮询获取更新"""
        url = f"{self.base_url}/getUpdates"
        params = {
            "offset": self._offset + 1,
            "timeout": 30,
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
        """处理消息：Ingress 清洗 → 入队"""
        adapter = TelegramAdapter()
        std_msg = adapter.adapt(update)

        # 检查是否重复
        if std_msg.is_duplicate:
            logger.info(f"Duplicate message ignored: {std_msg.message_id}")
            return

        # 检查清洗后是否为空
        if not std_msg.text:
            logger.info(f"Empty message after cleaning: {std_msg.raw_text}")
            return

        logger.info(
            f"Received message: user={std_msg.user_id}, "
            f"chat={std_msg.chat_id}, text={std_msg.text}"
        )

        # 构建统一消息并通过 Bus 发送
        bus_msg = BusMessage(
            channel=self.channel,
            chat_id=std_msg.chat_id,
            user_id=std_msg.user_id,
            text=std_msg.text,
            project_id=55,
        )

        if self.bus:
            await self.bus.enqueue_request(bus_msg)
        else:
            logger.error("No bus configured, message dropped")

    def format_response(self, nl2dsl_result: Any, query_result: Any = None) -> str:
        """格式化响应消息为 Telegram 文本"""
        status = nl2dsl_result.get("status", "success")

        # 确认流：返回候选列表供用户选择
        if status == "needs_confirmation":
            return self._format_confirmation(nl2dsl_result)

        # 正常结果
        semantic = nl2dsl_result.get("semantic", {})
        metric = semantic.get("metric", {})
        event = semantic.get("event", {})
        time_range = semantic.get("time_range", {})

        lines = [
            "\U0001F4CA 查询结果",
            f"地区: {', '.join(semantic.get('region_filter', []))}",
            f"指标: {metric.get('metric_id', 'N/A')}",
            f"事件: {event.get('event_name', 'N/A')}",
            f"时间: 近 {time_range.get('n', 0)} 天",
        ]

        if query_result and query_result.get("success"):
            data = query_result.get("result", query_result)
            mock_data = data.get("data", {})
            records = mock_data.get("result", mock_data.get("data", []))

            if records:
                lines.append("\n\U0001F4C8 数据结果")
                for record in records[:5]:
                    parts = []
                    for k, v in record.items():
                        if k != "date":
                            parts.append(f"{k}={v}")
                    date_val = record.get('date', 'N/A')
                    if parts:
                        lines.append(f"{date_val}: {', '.join(parts)}")
                    else:
                        lines.append(f"{date_val}: {json.dumps(record, ensure_ascii=False)}")

                if len(records) > 5:
                    lines.append(f"...(还有 {len(records) - 5} 条记录)")
            else:
                lines.append("\n暂无数据")
        elif query_result and query_result.get("error"):
            lines.append(f"\n\U0000274C 查询失败: {query_result.get('error')}")
        else:
            lines.append("\n\U000026A0\uFE0F 未获取到查询结果")

        return "\n".join(lines)

    async def send_response(self, recipient: str, response: Dict[str, Any]) -> None:
        """发送响应"""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": recipient,
            "text": response.get("text", ""),
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

            if not data.get("ok"):
                logger.error(f"Failed to send message: {data}")
            else:
                logger.info(f"Message sent to {recipient}")

    def _format_confirmation(self, nl2dsl_result: Any) -> str:
        """格式化确认请求消息"""
        message = nl2dsl_result.get("message", "")
        candidates = nl2dsl_result.get("candidates", {})

        if not candidates:
            return message

        lines = []
        for field_name, cands in candidates.items():
            field_display = {"event": "事件", "metric": "指标"}.get(field_name, field_name)
            lines.append(f"请选择{field_display}:")
            for i, c in enumerate(cands[:5], 1):
                lines.append(f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)")
        lines.append("回复编号或名称即可")

        return "\n".join(lines)
