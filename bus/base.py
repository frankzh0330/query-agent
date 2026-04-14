from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from bus.message_schema import BusMessage, BusResult


class MessageBus(ABC):
    """消息总线抽象基类"""

    @abstractmethod
    async def enqueue_request(self, msg: BusMessage) -> None:
        """将消息放入请求队列"""
        ...

    @abstractmethod
    async def dequeue_request(self, timeout: float = 30) -> Optional[BusMessage]:
        """从请求队列取出消息（阻塞等待）"""
        ...

    @abstractmethod
    async def enqueue_result(self, result: BusResult) -> None:
        """将处理结果放入结果队列"""
        ...

    @abstractmethod
    async def dequeue_result(self, timeout: float = 30) -> Optional[BusResult]:
        """从结果队列取出结果（阻塞等待）"""
        ...
