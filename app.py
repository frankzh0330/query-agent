"""Query Agent API — FastAPI 端点定义

app.py 只负责：
1. FastAPI 实例 + 请求/响应模型
2. 全局服务实例化
3. HTTP 端点（委托给 QueryOrchestrator）
4. Telegram 通知（依赖环境变量）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from service.bearer_service import execute_query
from service.session_manager import SessionManager
from service.session_models import QueryState
from service.task_manager import TaskManager
from memory.memory_writer import MemoryWriter
from memory.storage.memory_file import TaskStorage
from memory.user_preference_store import UserPreferenceStore
from service.query_orchestrator import QueryOrchestrator

app = FastAPI(title="query-agent: NL to Semantic DSL")
logger = logging.getLogger(__name__)

# ====================
# 全局服务实例
# ====================

_matcher_service: Optional["MatcherService"] = None
session_manager = SessionManager(data_path="data")
task_manager = TaskManager(storage=TaskStorage(data_path="data/tasks"))
memory_writer = MemoryWriter(data_path="data/memory")
user_preference_store = UserPreferenceStore(data_path="data/user_preferences")
orchestrator = QueryOrchestrator(session_manager, task_manager, memory_writer, user_preference_store)


def set_matcher_service(service: "MatcherService") -> None:
    global _matcher_service
    _matcher_service = service
    logger.info("MatcherService registered")


def get_matcher_service() -> "MatcherService":
    if _matcher_service is None:
        raise RuntimeError("MatcherService not initialized. Start WebSocket server first.")
    return _matcher_service


# ====================
# 请求/响应模型
# ====================

class NL2DSLRequest(BaseModel):
    text: str
    project_id: int = Field(default=55)
    session_id: str | None = None
    user_id: str | None = None
    chat_id: str | None = None


class NL2DSLResponse(BaseModel):
    extraction_json: Dict[str, Any]
    semantic: Dict[str, Any]
    exec_dsl: Dict[str, Any]
    explain: Dict[str, Any]
    session_id: str | None = None
    status: str = "success"
    message: str | None = None
    task_id: str | None = None
    candidates: Dict[str, Any] | None = None


class BearerQueryRequest(BaseModel):
    exec_dsl: Dict[str, Any]


class BearerQueryResponse(BaseModel):
    result: Dict[str, Any]
    success: bool
    error: Optional[str] = None


# ====================
# Telegram 通知
# ====================

async def _send_telegram_notification(chat_id: str, message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning("Failed to send Telegram notification: %s", e)


# ====================
# HTTP 端点
# ====================

@app.post("/nl2dsl", response_model=NL2DSLResponse)
async def nl2dsl(req: NL2DSLRequest) -> NL2DSLResponse:
    """NL 转 DSL 接口"""
    service = get_matcher_service()
    catalog = service.catalog
    result = await orchestrator.process(req, service, catalog, notify_fn=_send_telegram_notification)
    return NL2DSLResponse(**result)


@app.post("/query/bearer", response_model=BearerQueryResponse)
async def query_bearer(req: BearerQueryRequest):
    """执行 Bearer 查询接口"""
    try:
        result = await execute_query(req.exec_dsl)
        return BearerQueryResponse(result=result, success=True)
    except Exception as e:
        logger.error("Bearer query failed: %s", e)
        return BearerQueryResponse(result={}, success=False, error=str(e))


@app.get("/sessions")
def list_sessions():
    sessions = session_manager.list_sessions()
    return {"count": len(sessions), "sessions": [s.to_dict() for s in sessions]}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    ctx = session_manager.get_session(session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")
    return ctx.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not session_manager.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}
