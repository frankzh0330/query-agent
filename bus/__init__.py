from __future__ import annotations

import os

from bus.base import MessageBus
from bus.message_schema import BusMessage, BusResult


def create_bus() -> MessageBus:
    """
    工厂函数：根据 MESSAGE_BUS_BACKEND 环境变量创建消息总线

    - "direct"（默认）: DirectCallBus，内存直连，适用于开发和单进程部署
    - "redis": RedisMessageBus，通过 Redis LIST 队列，适用于多 Worker 部署
    """
    backend = os.getenv("MESSAGE_BUS_BACKEND", "direct").lower()

    if backend == "redis":
        from bus.redis_bus import RedisMessageBus
        return RedisMessageBus()
    else:
        from bus.direct_call_bus import DirectCallBus
        return DirectCallBus()


__all__ = [
    "MessageBus",
    "BusMessage",
    "BusResult",
    "create_bus",
]
