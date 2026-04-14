"""Ingress Adapter 层 - 统一处理外部消息输入"""

from ingress.models import StandardMessage
from ingress.base_adapter import BaseIngressAdapter
from ingress.cleaner import MessageCleaner
from ingress.deduplicator import MessageDeduplicator
from ingress.telegram_adapter import TelegramAdapter

__all__ = [
    "StandardMessage",
    "BaseIngressAdapter",
    "MessageCleaner",
    "MessageDeduplicator",
    "TelegramAdapter",
]
