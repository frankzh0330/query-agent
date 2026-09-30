from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class BusMessage(BaseModel):
    """统一消息协议，用于 Gateway ↔ Worker 之间的通信"""

    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    channel: str  # "telegram" | "whatsapp" | "lark" | ...
    chat_id: str  # 回复目标（Telegram chat_id 等）
    user_id: Optional[str] = None
    text: str
    project_id: int = 55
    created_at: float = Field(default_factory=time.time)


class BusResult(BaseModel):
    """Worker 处理结果"""

    msg: BusMessage
    success: bool
    nl2sql_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
