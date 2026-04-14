"""Ingress 数据模型"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict


@dataclass
class StandardMessage:
    """标准化消息格式

    所有外部消息（Telegram, WhatsApp 等）都转换为该格式
    """
    # 基础信息
    message_id: str  # 消息唯一标识 (用于去重)
    user_id: str  # 用户ID
    chat_id: str  # 聊天ID
    channel: str  # 渠道标识: "telegram", "whatsapp" 等
    text: str  # 清洗后的文本内容

    # 元数据
    raw_text: str  # 原始文本（用于调试）
    timestamp: datetime  # 消息时间戳
    metadata: Dict[str, Any] = field(default_factory=dict)  # 其他元数据

    # 清洗标记
    is_cleaned: bool = False  # 是否经过清洗
    is_duplicate: bool = False  # 是否是重复消息

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "message_id": self.message_id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "channel": self.channel,
            "text": self.text,
            "raw_text": self.raw_text,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "is_cleaned": self.is_cleaned,
            "is_duplicate": self.is_duplicate,
        }
