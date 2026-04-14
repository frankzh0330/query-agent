from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseGateway(ABC):
    """Gateway 基类"""

    def __init__(self, channel: str, bus=None):
        """
        Args:
            channel: 渠道标识，如 "telegram", "whatsapp"
            bus: MessageBus 实例，用于消息入队
        """
        self.channel = channel
        self.bus = bus

    @abstractmethod
    async def start(self) -> None:
        """启动 Gateway"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止 Gateway"""
        pass

    @abstractmethod
    async def handle_message(self, message: Any) -> None:
        """处理消息（Ingress 清洗 + 入队）"""
        pass

    @abstractmethod
    async def send_response(self, recipient: str, response: Dict[str, Any]) -> None:
        """发送响应"""
        pass

    def format_response(self, nl2dsl_result: Dict, query_result: Any = None) -> str:
        """格式化响应文本（子类可覆盖以适配不同渠道）"""
        return str(nl2dsl_result)
