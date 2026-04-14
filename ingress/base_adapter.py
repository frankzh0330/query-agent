"""Ingress Adapter 基类"""

from abc import ABC, abstractmethod
from typing import Any

from ingress.models import StandardMessage


class BaseIngressAdapter(ABC):
    """Ingress Adapter 基类

    所有渠道适配器都应继承该基类
    """

    @abstractmethod
    def adapt(self, raw_message: Any) -> StandardMessage:
        """将原始消息转换为标准格式"""
        pass

    @abstractmethod
    def extract_message_id(self, raw_message: Any) -> str:
        """提取消息ID（用于去重）"""
        pass
