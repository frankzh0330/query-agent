from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseGateway(ABC):
    """Gateway 基类"""

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
        """处理消息"""
        pass

    @abstractmethod
    async def send_response(self, recipient: str, response: Dict[str, Any]) -> None:
        """发送响应"""
        pass
