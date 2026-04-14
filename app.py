from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from common.types import MatcherType
from dsl.renderer import render_exec_dsl
from dsl.semantic_models import Event, GroupBy, Metric, SemanticDSL, TimeRange
from dsl.validators import validate_region, validate_region_consistency
from service.bearer_service import execute_query
from service.llm_extractions import extract_llm_async
from service.session_manager import SessionManager
from service.session_models import QueryState
from service.task_manager import TaskManager

app = FastAPI(title="query-agent: NL to Semantic DSL")
logger = logging.getLogger(__name__)

# ====================
# 全局服务实例（由 WebSocket 启动时初始化）
# ====================

_matcher_service: Optional["MatcherService"] = None
session_manager = SessionManager(data_path="data")
task_manager = TaskManager()


def set_matcher_service(service: "MatcherService") -> None:
    """设置 matcher 服务（WebSocket 启动时调用）"""
    global _matcher_service
    _matcher_service = service
    logger.info("MatcherService registered")


def get_matcher_service() -> "MatcherService":
    """获取 matcher 服务"""
    if _matcher_service is None:
        raise RuntimeError("MatcherService not initialized. Start WebSocket server first.")
    return _matcher_service


# ====================
# 请求/响应模型
# ====================

class NL2DSLRequest(BaseModel):
    text: str
    project_id: int = Field(default=55)
    session_id: str | None = None  # 会话ID（可选）
    user_id: str | None = None  # 用户ID（可选）
    chat_id: str | None = None  # Telegram 聊天ID，用于发送进度通知


class NL2DSLResponse(BaseModel):
    extraction_json: Dict[str, Any]
    semantic: Dict[str, Any]
    exec_dsl: Dict[str, Any]
    explain: Dict[str, Any]
    session_id: str | None = None  # 返回会话ID
    status: str = "success"  # "success" | "early_exit" | "needs_confirmation"
    message: str | None = None  # 提示消息
    task_id: str | None = None  # 确认任务ID
    candidates: Dict[str, Any] | None = None  # 候选列表


class BearerQueryRequest(BaseModel):
    exec_dsl: Dict[str, Any]


class BearerQueryResponse(BaseModel):
    result: Dict[str, Any]
    success: bool
    error: Optional[str] = None


# ====================
# Telegram 通知辅助函数
# ====================

async def _send_telegram_notification(chat_id: str, message: str) -> None:
    """直接发送 Telegram 通知（用于查询进度反馈）"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning(f"Failed to send Telegram notification: {e}")


def _match_user_input_to_candidates(user_input: str, candidates: list[dict]) -> str | None:
    """将用户输入匹配到候选项（支持编号和名称）"""
    text = user_input.strip()

    # 尝试编号匹配
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]["value"]

    # 尝试名称匹配（忽略大小写）
    text_lower = text.lower()
    for c in candidates:
        if c["value"].lower() == text_lower:
            return c["value"]

    # 尝试部分匹配
    for c in candidates:
        if text_lower in c["value"].lower() or c["value"].lower() in text_lower:
            return c["value"]

    return None


async def _handle_confirmation(
    req: NL2DSLRequest,
    ctx: "SessionContext",
    task: "TaskContext",
    service: "MatcherService",
    catalog,
) -> NL2DSLResponse:
    """处理用户对确认任务的回复"""
    user_input = req.text.strip()
    confirmed_values = {}

    # 对每个待确认字段尝试匹配用户输入
    for field_name, candidates in task.candidates.items():
        matched = _match_user_input_to_candidates(user_input, candidates)
        if matched:
            task_manager.confirm_task(task.task_id, field_name, matched)
            confirmed_values[field_name] = matched
            logger.info(f"User confirmed {field_name}={matched} for task {task.task_id}")

    if not confirmed_values:
        # 用户输入没匹配到任何候选 → 提示重新选择
        message_lines = ["未能识别您的选择，请回复编号或名称:"]
        for field_name, cands in task.candidates.items():
            field_display = {"event": "事件", "metric": "指标"}.get(field_name, field_name)
            message_lines.append(f"请选择{field_display}:")
            for i, c in enumerate(cands[:5], 1):
                message_lines.append(f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)")

        hint_msg = "\n".join(message_lines)
        if req.chat_id:
            await _send_telegram_notification(req.chat_id, hint_msg)

        return NL2DSLResponse(
            extraction_json=task.extraction,
            semantic={},
            exec_dsl={},
            explain={},
            session_id=ctx.session_id,
            status="needs_confirmation",
            message=hint_msg,
            task_id=task.task_id,
            candidates=task.candidates,
        )

    # 检查是否所有待确认字段都已确认
    all_confirmed = all(
        field in task.user_selection
        for field in task.candidates.keys()
    )

    if not all_confirmed:
        # 还有字段未确认 → 继续等待
        remaining = {
            f: c for f, c in task.candidates.items()
            if f not in task.user_selection
        }
        message_lines = [f"已确认: {confirmed_values}"]
        for field_name, cands in remaining.items():
            field_display = {"event": "事件", "metric": "指标"}.get(field_name, field_name)
            message_lines.append(f"还需要选择{field_display}:")
            for i, c in enumerate(cands[:5], 1):
                message_lines.append(f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)")

        hint_msg = "\n".join(message_lines)
        return NL2DSLResponse(
            extraction_json=task.extraction,
            semantic={},
            exec_dsl={},
            explain={},
            session_id=ctx.session_id,
            status="needs_confirmation",
            message=hint_msg,
            task_id=task.task_id,
            candidates=remaining,
        )

    # 全部确认 → 用确认后的 query_state 继续构建 DSL
    ctx.pending_task_id = None
    task_manager.update_status(task.task_id, "confirmed")

    qs = task.partial_query_state
    # 用用户确认的值覆盖
    for field, value in task.user_selection.items():
        if field == "event":
            qs.event = value
        elif field == "metric":
            qs.metric = value

    # Layer3: 语义 DSL 构建
    semantic = SemanticDSL(
        project_id=qs.project_id,
        region_filter=qs.region_filter,
        metric=Metric(metric_id=qs.metric),
        event=Event(event_name=qs.event),
        time_range=TimeRange(type="last_n_days", n=qs.time_range["n"] if qs.time_range else 7),
        group_by=[GroupBy(dimension_id=d) for d in qs.group_by],
        filters=qs.filters,
    )

    # Layer4: DSL 渲染
    exec_dsl = render_exec_dsl(semantic, catalog)

    # Validate
    validate_region(exec_dsl)
    validate_region_consistency(semantic, exec_dsl)

    # 记录到会话
    session_manager.add_message(ctx.session_id, "user", task.raw_query)
    session_manager.add_message(ctx.session_id, "user", user_input, metadata={"confirmed": task.user_selection})
    session_manager.update_query_state(ctx.session_id, qs)
    task_manager.update_status(task.task_id, "completed")

    return NL2DSLResponse(
        extraction_json=task.extraction,
        semantic=semantic.model_dump(),
        exec_dsl=exec_dsl,
        explain={"confirmed": task.user_selection, "method": "user_confirmation"},
        session_id=ctx.session_id,
        status="success",
        message=f"已按您的选择生成查询: {task.user_selection}",
    )


# ====================
# HTTP API 端点
# ====================

@app.post("/nl2dsl", response_model=NL2DSLResponse)
async def nl2dsl(req: NL2DSLRequest) -> NL2DSLResponse:
    """
    NL 转 DSL 接口

    可通过 HTTP 调用，也可由 WebSocket 内部调用
    支持会话记忆：传入 session_id 可利用历史对话上下文
    """
    service = get_matcher_service()
    catalog = service.catalog

    # 获取或创建会话
    ctx = session_manager.create_or_get(req.session_id, req.user_id, req.project_id)

    # ============ 入口拦截：检查是否有待确认任务 ============
    if ctx.pending_task_id:
        task = task_manager.get_task(ctx.pending_task_id)
        if task and task.status == "waiting_confirmation":
            # 用户的这条消息是确认回复，不是新查询
            return await _handle_confirmation(req, ctx, task, service, catalog)

    # ============ 正常查询流程 ============

    # 获取增强会话上下文（包含长期记忆）
    session_context = session_manager.get_enhanced_context(ctx.session_id)

    timing = {}
    total_start = time.time()

    # Layer1: LLM 提取（带会话上下文）
    layer1_start = time.time()
    extraction_json = await extract_llm_async(req.text, session_context=session_context)
    timing["layer1_llm_extraction_s"] = round(time.time() - layer1_start, 3)

    # 打印 LLM 提取结果
    print(f"\n=== Layer1: LLM 提取结果 ===")
    print(f"用户输入: {req.text}, extraction_json: {json.dumps(extraction_json.model_dump(), ensure_ascii=False)}")

    # === 验证必填字段 event ===
    if not extraction_json.event_extractions:
        error_msg = "您目前没有输入任何event_name,无法查询哦"

        # 发送 Telegram 提示
        if req.chat_id:
            await _send_telegram_notification(req.chat_id, error_msg)

        # 记录用户消息（解析失败）
        session_manager.add_message(ctx.session_id, "user", req.text, metadata={"error": "missing_event"})

        # 返回 early_exit 状态
        return NL2DSLResponse(
            extraction_json=extraction_json.model_dump(),
            semantic={},
            exec_dsl={},
            explain={},
            session_id=ctx.session_id,
            status="early_exit",
            message=error_msg,
        )

    # Layer2: 使用 MatcherService 解析（带候选和置信度）
    layer2_start = time.time()
    region_filter = extraction_json.region_filter if extraction_json.region_filter else ["ROW"]
    region_explain = {"stage": "llm_direct", "region_filter": region_filter}

    print(f"\n=== Layer2: MatcherService 解析 ===")
    print(f"extraction_json: {extraction_json.model_dump()}")
    print(f"region_filter: {region_filter}")

    metric_result = service.resolve_with_candidates(
        MatcherType.METRIC, extraction_json.metric_extractions, default="pv"
    )
    print(f"metric: value={metric_result.value}, score={metric_result.score}, needs_confirm={metric_result.needs_confirmation}")

    event_result = service.resolve_with_candidates(
        MatcherType.EVENT, extraction_json.event_extractions, default="app_launch"
    )
    print(f"event: value={event_result.value}, score={event_result.score}, needs_confirm={event_result.needs_confirmation}")

    group_by_result = service.resolve_with_candidates(
        MatcherType.DIMENSION, extraction_json.group_by_extractions, default="country"
    )
    print(f"group_by: value={group_by_result.value}, score={group_by_result.score}, needs_confirm={group_by_result.needs_confirmation}")

    n_days, time_explain = service.resolve_time(extraction_json)
    print(f"n_days: {n_days}, time_explain: {time_explain}")
    timing["layer2_resolution_s"] = round(time.time() - layer2_start, 3)

    # ============ 确认流判断（确定性代码规则，不依赖 LLM） ============
    pending_fields = {}
    if event_result.needs_confirmation:
        pending_fields["event"] = event_result.candidates
    if metric_result.needs_confirmation:
        pending_fields["metric"] = metric_result.candidates

    if pending_fields:
        # 有低置信度字段 → 创建确认任务
        partial_state = QueryState(
            project_id=req.project_id,
            event=event_result.value,
            metric=metric_result.value,
            time_range={"type": "last_n_days", "n": n_days},
            region_filter=region_filter,
            group_by=[group_by_result.value],
            filters=[],
        )
        task = task_manager.create_task(
            session_id=ctx.session_id,
            raw_query=req.text,
            extraction=extraction_json.model_dump(),
            user_id=ctx.user_id,
            candidates=pending_fields,
            partial_query_state=partial_state,
        )
        ctx.pending_task_id = task.task_id

        # 构建候选提示消息
        message_lines = []
        for field_name, cands in pending_fields.items():
            field_display = {"event": "事件", "metric": "指标"}.get(field_name, field_name)
            message_lines.append(f"请选择{field_display}:")
            for i, c in enumerate(cands[:5], 1):
                message_lines.append(f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)")
        message_lines.append("回复编号或名称即可")

        confirm_msg = "\n".join(message_lines)

        if req.chat_id:
            await _send_telegram_notification(req.chat_id, confirm_msg)

        return NL2DSLResponse(
            extraction_json=extraction_json.model_dump(),
            semantic={},
            exec_dsl={},
            explain={"resolver_explain": {
                "region": region_explain,
                "metric": {"method": metric_result.method, "score": metric_result.score},
                "event": {"method": event_result.method, "score": event_result.score},
                "group_by": {"method": group_by_result.method, "score": group_by_result.score},
                "time": time_explain,
            }},
            session_id=ctx.session_id,
            status="needs_confirmation",
            message=confirm_msg,
            task_id=task.task_id,
            candidates=pending_fields,
        )

    # 高置信度 → 使用解析结果
    metric_id = metric_result.value
    event_name = event_result.value
    group_by_dim = group_by_result.value

    resolver_explain = {
        "region": region_explain,
        "metric": {"method": metric_result.method, "score": metric_result.score},
        "event": {"method": event_result.method, "score": event_result.score},
        "group_by": {"method": group_by_result.method, "score": group_by_result.score},
        "time": time_explain,
    }

    # === 查询前反馈 ===
    if req.chat_id:
        region_display = region_filter[0] if region_filter else "ROW"
        time_display = f"近{n_days}天"
        notification = f"解析结果为: {region_display}|{event_name}|{metric_id}|{time_display}\n正在查询中，请稍后..."
        await _send_telegram_notification(req.chat_id, notification)

    # Layer3: 语义 DSL 构建
    layer3_start = time.time()
    semantic = SemanticDSL(
        project_id=req.project_id,
        region_filter=region_filter,
        metric=Metric(metric_id=metric_id),
        event=Event(event_name=event_name),
        time_range=TimeRange(type="last_n_days", n=n_days),
        group_by=[GroupBy(dimension_id=group_by_dim)],
        filters=[],
    )
    timing["layer3_semantic_dsl_s"] = round(time.time() - layer3_start, 3)

    # Layer4: DSL 渲染
    layer4_start = time.time()
    exec_dsl = render_exec_dsl(semantic, catalog)
    timing["layer4_render_exec_dsl_s"] = round(time.time() - layer4_start, 3)

    # 打印 exec_dsl 用于调试
    print(f"\n=== Layer4: exec_dsl ===")
    print(f"{json.dumps(exec_dsl, ensure_ascii=False)}")
    # logger.info(f"Generated exec_dsl: {exec_dsl}")

    # Validate
    validate_start = time.time()
    validate_region(exec_dsl)
    validate_region_consistency(semantic, exec_dsl)
    timing["validation_s"] = round(time.time() - validate_start, 3)

    timing["total_s"] = round(time.time() - total_start, 3)
    # logger.info(f"nl2dsl timing: {timing}")
    print(f"\n=== Timing ===")
    print(f"timing: {json.dumps(timing, ensure_ascii=False)}")

    # 记录用户消息到会话
    session_manager.add_message(
        ctx.session_id,
        "user",
        req.text,
        metadata={
            "region_filter": region_filter,
            "metric_id": metric_id,
            "event_name": event_name,
        }
    )

    # 更新 last_query_state（只在查询成功时更新）
    query_state = QueryState(
        project_id=req.project_id,
        event=event_name,
        metric=metric_id,
        time_range={"type": "last_n_days", "n": n_days},
        region_filter=region_filter,
        group_by=[group_by_dim],
        filters=[],
    )
    session_manager.update_query_state(ctx.session_id, query_state)

    return NL2DSLResponse(
        extraction_json=extraction_json.model_dump(),
        semantic=semantic.model_dump(),
        exec_dsl=exec_dsl,
        explain={
            "resolver_explain": resolver_explain,
            "timing": timing,
        },
        session_id=ctx.session_id,
    )


@app.post("/query/bearer", response_model=BearerQueryResponse)
async def query_bearer(req: BearerQueryRequest):
    """
    执行 Bearer 查询接口

    可通过 HTTP 调用，也可由 WebSocket 内部调用
    """
    try:
        result = await execute_query(req.exec_dsl)
        return BearerQueryResponse(result=result, success=True)
    except Exception as e:
        logger.error(f"Bearer query failed: {e}")
        return BearerQueryResponse(result={}, success=False, error=str(e))


@app.get("/sessions")
def list_sessions():
    """列出所有会话（调试用）"""
    sessions = session_manager.list_sessions()
    return {
        "count": len(sessions),
        "sessions": [s.to_dict() for s in sessions],
    }


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    """获取单个会话（调试用）"""
    ctx = session_manager.get_session(session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")
    return ctx.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """删除会话（调试用）"""
    ctx = session_manager.get_session(session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Session not found")
    del session_manager._sessions[session_id]
    return {"deleted": session_id}
