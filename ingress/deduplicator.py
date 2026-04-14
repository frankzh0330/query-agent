"""消息去重器 - 基于滑动窗口的内存去重"""

import time
from collections import deque
from typing import Deque, Set, Tuple


class MessageDeduplicator:
    """消息去重器（滑动窗口，内存存储）

    使用场景：
    - 防止重复处理相同的消息
    - 基于 message_id 判断去重
    """

    def __init__(self, window_size: int = 10000, ttl_seconds: int = 3600):
        """初始化去重器

        Args:
            window_size: 窗口大小（最大记录消息数）
            ttl_seconds: 消息过期时间（秒）
        """
        self.window_size = window_size
        self.ttl_seconds = ttl_seconds
        self.seen_messages: Set[str] = set()
        self.message_timestamps: Deque[Tuple[str, float]] = deque()

    def is_duplicate(self, message_id: str) -> bool:
        """检查消息是否重复

        Args:
            message_id: 消息唯一标识

        Returns:
            True 如果是重复消息，False 如果是新消息
        """
        # 清理过期消息
        self._cleanup_expired()

        if message_id in self.seen_messages:
            return True

        # 记录新消息
        self.seen_messages.add(message_id)
        self.message_timestamps.append((message_id, time.time()))

        # 限制窗口大小
        if len(self.message_timestamps) > self.window_size:
            old_id, _ = self.message_timestamps.popleft()
            self.seen_messages.discard(old_id)

        return False

    def _cleanup_expired(self) -> None:
        """清理过期的消息记录"""
        now = time.time()
        cutoff = now - self.ttl_seconds

        while self.message_timestamps:
            _, timestamp = self.message_timestamps[0]
            if timestamp < cutoff:
                old_id, _ = self.message_timestamps.popleft()
                self.seen_messages.discard(old_id)
            else:
                break

    def reset(self) -> None:
        """重置去重器"""
        self.seen_messages.clear()
        self.message_timestamps.clear()

    def size(self) -> int:
        """获取当前缓存的消息数量"""
        return len(self.seen_messages)
