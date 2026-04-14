"""Telegram 渠道适配器"""

from datetime import datetime
from typing import Any, Dict

from ingress.base_adapter import BaseIngressAdapter
from ingress.cleaner import MessageCleaner
from ingress.deduplicator import MessageDeduplicator
from ingress.models import StandardMessage


# 全局去重器实例（跨所有 TelegramAdapter 共享）
_global_deduplicator = MessageDeduplicator()


class TelegramAdapter(BaseIngressAdapter):
    """Telegram 渠道适配器

    将 Telegram update 转换为 StandardMessage
    """

    def __init__(self, deduplicator: MessageDeduplicator | None = None):
        """初始化适配器

        Args:
            deduplicator: 可选的去重器，默认使用全局共享实例
        """
        self.cleaner = MessageCleaner()
        self.deduplicator = deduplicator or _global_deduplicator

    def adapt(self, raw_update: Dict[str, Any]) -> StandardMessage:
        """转换 Telegram update 为标准消息

        Args:
            raw_update: Telegram Bot API 的 update 对象

        Returns:
            StandardMessage: 标准化消息
        """
        message = raw_update.get("message", {})

        # 提取基础信息
        message_id = self.extract_message_id(raw_update)
        user_id = str(message.get("from", {}).get("id", ""))
        chat_id = str(message.get("chat", {}).get("id", ""))
        raw_text = message.get("text", "")

        # 清洗文本
        cleaned_text = self.cleaner.clean(raw_text)

        # 去重检查
        is_duplicate = self.deduplicator.is_duplicate(message_id)

        return StandardMessage(
            message_id=message_id,
            user_id=user_id,
            chat_id=chat_id,
            channel="telegram",
            text=cleaned_text,
            raw_text=raw_text,
            timestamp=datetime.now(),
            metadata={"raw_update": raw_update},
            is_cleaned=(cleaned_text != raw_text),
            is_duplicate=is_duplicate,
        )

    def extract_message_id(self, raw_update: Dict[str, Any]) -> str:
        """提取 Telegram 消息唯一ID

        Args:
            raw_update: Telegram Bot API 的 update 对象

        Returns:
            消息唯一标识
        """
        update_id = raw_update.get("update_id", 0)
        message = raw_update.get("message", {})
        message_id = message.get("message_id", 0)
        return f"telegram_{update_id}_{message_id}"
