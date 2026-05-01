"""查询编排器 — 从 app.py 提取的核心业务逻辑

三条处理路径：
1. new_query: 完整的新查询（LLM 提取 → Matcher → DSL）
2. followup_patch: 基于上轮状态的修改（检测 → 补丁 → 合并 → DSL）
3. confirmation: 用户确认低置信度候选

消除重复：
- _resolve_field: 统一 resolve + user preference bias
- _build_semantic: 统一 SemanticDSL 构建
- _format_candidates_message: 统一候选消息格式化
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.types import MatcherType
from dsl.renderer import render_exec_dsl
from dsl.semantic_models import Event, GroupBy, Metric, SemanticDSL, TimeRange
from dsl.validators import validate_region, validate_region_consistency
from memory.memory_writer import MemoryWriter
from memory.user_preference_store import UserPreferenceStore
from service.followup_resolver import detect_followup
from service.llm_extractions import extract_llm_async
from service.query_state_merger import merge_query_state
from service.session_manager import SessionManager
from service.session_models import QueryState
from service.task_manager import TaskManager

logger = logging.getLogger(__name__)

# ==================== 常量 ====================

DEFAULT_METRIC = "pv"
DEFAULT_EVENT = "app_launch"
DEFAULT_DIMENSION = "country"
DEFAULT_N_DAYS = 7

_REGION_TOKENS = ("美国", "德国", "欧洲", "新加坡", "加州", "纽约", "ROW")


# ==================== 异常 ====================

class FollowupConfirmationNeeded(Exception):
    def __init__(self, field_name: str, resolved_result):
        self.field_name = field_name
        self.resolved_result = resolved_result
        super().__init__(field_name)


# ==================== 数据类 ====================

@dataclass
class FollowupPatchResult:
    patch: dict[str, Any]
    resolver_explain: Dict[str, Any]
    patch_hints: Dict[str, Any]
    confirmation_needed: Optional[tuple[str, Any]] = None


# ==================== 编排器 ====================

class QueryOrchestrator:
    """查询编排器：封装三条处理路径的业务逻辑"""

    def __init__(
        self,
        session_manager: SessionManager,
        task_manager: TaskManager,
        memory_writer: MemoryWriter,
        user_preference_store: UserPreferenceStore,
    ):
        self.session = session_manager
        self.task = task_manager
        self.memory = memory_writer
        self.preferences = user_preference_store

    # ==================== 主入口 ====================

    async def process(self, req, service, catalog, notify_fn=None) -> dict:
        """主入口：路由到三条处理路径

        Args:
            req: NL2DSLRequest
            service: MatcherService
            catalog: catalog 数据
            notify_fn: async callable(chat_id, message) for Telegram notifications

        Returns:
            NL2DSLResponse dict
        """
        ctx = self.session.create_or_get(req.session_id, req.user_id, req.project_id)
        prev_qs = ctx.last_query_state

        # 1. 确认流拦截
        if ctx.pending_task_id:
            task = self.task.get_task(ctx.pending_task_id)
            if task and task.status == "waiting_confirmation":
                return await self._process_confirmation(req, ctx, task, service, catalog)

        # 2. LLM 提取
        session_context = self.session.get_enhanced_context(ctx.session_id, query_text=req.text)
        timing = {}
        total_start = time.time()

        layer1_start = time.time()
        extraction_json = await extract_llm_async(req.text, session_context=session_context)
        timing["layer1_llm_extraction_s"] = round(time.time() - layer1_start, 3)
        logger.debug("Layer1: user=%s, extraction=%s", req.text,
                      json.dumps(extraction_json.model_dump(), ensure_ascii=False))

        # 3. Follow-up 检测
        followup_decision = detect_followup(req.text, prev_qs, pending_task=False)
        logger.debug("Turn decision: mode=%s, confidence=%.2f, reason=%s",
                      followup_decision.mode, followup_decision.confidence, followup_decision.reason)

        # 4. Early exit（无 event 且非 follow-up）
        if not extraction_json.event_extractions and not followup_decision.is_followup:
            self.session.add_message(ctx.session_id, "user", req.text, metadata={"error": "missing_event"})
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id, message="您目前没有输入任何event_name,无法查询哦",
            )

        # 5. Follow-up 路径
        if followup_decision.is_followup and prev_qs is not None:
            return await self._process_followup(
                req, ctx, extraction_json, followup_decision, prev_qs, service, catalog,
            )

        # 6. 新查询路径
        return await self._process_new_query(
            req, ctx, extraction_json, followup_decision, service, catalog,
            timing, total_start, notify_fn=notify_fn,
        )

    # ==================== 新查询路径 ====================

    async def _process_new_query(
        self, req, ctx, extraction_json, followup_decision,
        service, catalog, timing, total_start, notify_fn=None,
    ) -> dict:
        """处理完整的新查询"""
        prev_qs = ctx.last_query_state

        # Layer2: Matcher 解析
        layer2_start = time.time()
        region_filter = extraction_json.region_filter or ["ROW"]
        region_explain = {"stage": "llm_direct", "region_filter": region_filter}

        metric_result, metric_bias = self._resolve_field(
            service, MatcherType.METRIC, extraction_json.metric_extractions,
            DEFAULT_METRIC, "metric", req.project_id, ctx.user_id,
        )
        event_result, event_bias = self._resolve_field(
            service, MatcherType.EVENT, extraction_json.event_extractions,
            DEFAULT_EVENT, "event", req.project_id, ctx.user_id,
        )
        group_by_result, group_by_bias = self._resolve_field(
            service, MatcherType.DIMENSION, extraction_json.group_by_extractions,
            DEFAULT_DIMENSION, "group_by", req.project_id, ctx.user_id,
        )
        n_days, time_explain = service.resolve_time(extraction_json)
        logger.debug("Layer2 resolved: event=%s(%.0f/%s), metric=%s(%.0f/%s), n_days=%d",
                      event_result.value, event_result.score, event_result.method,
                      metric_result.value, metric_result.score, metric_result.method, n_days)
        timing["layer2_resolution_s"] = round(time.time() - layer2_start, 3)

        resolver_explain = self._build_resolver_explain(
            region_explain, metric_result, metric_bias,
            event_result, event_bias, group_by_result, group_by_bias, time_explain,
        )

        # 确认流判断
        pending_fields = {}
        if event_result.needs_confirmation:
            pending_fields["event"] = event_result.candidates
        if metric_result.needs_confirmation:
            pending_fields["metric"] = metric_result.candidates

        if pending_fields:
            return await self._create_confirmation_task(
                req, ctx, extraction_json, pending_fields, resolver_explain,
                event_result.value, metric_result.value, n_days,
                region_filter, group_by_result.value, notify_fn=notify_fn,
            )

        # 高置信度 → 构建 DSL
        metric_id = metric_result.value
        event_name = event_result.value
        group_by_dim = group_by_result.value

        if req.chat_id and notify_fn:
            region_display = region_filter[0] if region_filter else "ROW"
            await notify_fn(req.chat_id,
                f"解析结果为: {region_display}|{event_name}|{metric_id}|近{n_days}天\n正在查询中，请稍后...")

        semantic, exec_dsl, timing = self._build_and_render_dsl(
            req.project_id, region_filter, metric_id, event_name,
            n_days, [group_by_dim], [], catalog, timing,
        )

        timing["total_s"] = round(time.time() - total_start, 3)
        logger.debug("Timing: %s", json.dumps(timing, ensure_ascii=False))

        # 会话记录
        self.session.add_message(ctx.session_id, "user", req.text, metadata={
            "region_filter": region_filter, "metric_id": metric_id, "event_name": event_name,
        })
        query_state = self._build_new_query_state(
            req.project_id, event_name, metric_id, n_days, region_filter, [group_by_dim],
        )
        self.session.update_query_state(ctx.session_id, query_state)
        self._record_preferences(ctx, query_state)

        # 异步记忆学习
        prev_state_dict = prev_qs.to_dict() if prev_qs else None
        asyncio.create_task(self.memory.maybe_save(
            project_id=req.project_id, user_query=req.text,
            extraction=extraction_json.model_dump(), resolver_explain=resolver_explain,
            current_state=query_state.to_dict(), prev_state=prev_state_dict,
        ))

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            semantic=semantic.model_dump(), exec_dsl=exec_dsl,
            explain={
                "turn_explain": self._build_turn_explain("new_query", query_state, decision=followup_decision),
                "resolver_explain": resolver_explain, "timing": timing,
            },
            session_id=ctx.session_id,
        )

    # ==================== Follow-up 路径 ====================

    async def _process_followup(
        self, req, ctx, extraction_json, decision, prev_qs, service, catalog,
    ) -> dict:
        """处理 follow-up 补丁查询"""
        patch_result = self._build_followup_patch(
            req.text, extraction_json, decision, service,
            project_id=req.project_id, user_id=ctx.user_id,
        )

        if patch_result.confirmation_needed:
            field_name, resolved = patch_result.confirmation_needed
            return await self._create_followup_confirmation(
                req, ctx, extraction_json, decision, prev_qs, field_name, resolved,
            )

        logger.debug("Follow-up patch built: patch=%s, session_id=%s",
                      json.dumps(patch_result.patch, ensure_ascii=False), ctx.session_id)

        merged_state = merge_query_state(prev_qs, patch_result.patch, req.project_id)
        semantic, exec_dsl, _ = self._build_and_render_dsl_from_state(merged_state, catalog)

        self.session.add_message(ctx.session_id, "user", req.text,
                                  metadata={"turn_mode": "followup_patch", "patch": patch_result.patch})
        self.session.update_query_state(ctx.session_id, merged_state)
        self._record_preferences(ctx, merged_state)

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            semantic=semantic.model_dump(), exec_dsl=exec_dsl,
            explain={
                "turn_explain": self._build_turn_explain("followup_patch", merged_state,
                                                          decision=decision, patch=patch_result.patch),
                "resolver_explain": patch_result.resolver_explain,
            },
            session_id=ctx.session_id,
        )

    # ==================== 确认路径 ====================

    async def _process_confirmation(self, req, ctx, task, service, catalog) -> dict:
        """处理用户对确认任务的回复"""
        user_input = req.text.strip()
        confirmed_values = {}

        for field_name, candidates in task.candidates.items():
            matched = _match_user_input_to_candidates(user_input, candidates)
            if matched:
                self.task.confirm_task(task.task_id, field_name, matched)
                confirmed_values[field_name] = matched
                logger.info("User confirmed %s=%s for task %s", field_name, matched, task.task_id)

        if not confirmed_values:
            hint_msg = self._format_unmatched_message(task.candidates)
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id, candidates=task.candidates,
            )

        all_confirmed = all(f in task.user_selection for f in task.candidates)
        if not all_confirmed:
            remaining = {f: c for f, c in task.candidates.items() if f not in task.user_selection}
            hint_msg = f"已确认: {confirmed_values}\n" + self._format_candidates_message(remaining)
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id, candidates=remaining,
            )

        # 全部确认
        self.session.update_pending_task(ctx.session_id, None)
        self.task.update_status(task.task_id, "confirmed")

        qs = task.partial_query_state
        for field, value in task.user_selection.items():
            if field in ("event", "metric"):
                setattr(qs, field, value)
        _mark_confirmation_state(qs, task.user_selection)

        semantic, exec_dsl, _ = self._build_and_render_dsl_from_state(qs, catalog)

        self.session.add_message(ctx.session_id, "user", task.raw_query)
        self.session.add_message(ctx.session_id, "user", user_input, metadata={"confirmed": task.user_selection})
        self.session.update_query_state(ctx.session_id, qs)
        self._record_preferences(ctx, qs)
        self.task.update_status(task.task_id, "completed")

        asyncio.create_task(self.memory.maybe_save(
            project_id=qs.project_id, user_query=task.raw_query,
            extraction=task.extraction, resolver_explain={"user_confirmed": task.user_selection},
            current_state=qs.to_dict(), confirmed_selection=task.user_selection,
        ))

        return self._make_response(
            extraction_json=task.extraction,
            semantic=semantic.model_dump(), exec_dsl=exec_dsl,
            explain={
                "confirmed": task.user_selection, "method": "user_confirmation",
                "turn_explain": self._build_turn_explain("confirmation", qs, confirmed_fields=task.user_selection),
            },
            session_id=ctx.session_id, status="success",
            message=f"已按您的选择生成查询: {task.user_selection}",
        )

    # ==================== 确认任务创建 ====================

    async def _create_confirmation_task(
        self, req, ctx, extraction_json, pending_fields, resolver_explain,
        event_val, metric_val, n_days, region_filter, group_by_val, notify_fn=None,
    ) -> dict:
        """创建确认任务并返回 needs_confirmation 响应"""
        partial_state = QueryState(
            project_id=req.project_id, event=event_val, metric=metric_val,
            time_range={"type": "last_n_days", "n": n_days}, region_filter=region_filter,
            group_by=[group_by_val], filters=[],
        )
        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates=pending_fields, partial_query_state=partial_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        confirm_msg = self._format_candidates_message(pending_fields) + "\n回复编号或名称即可"
        if req.chat_id and notify_fn:
            await notify_fn(req.chat_id, confirm_msg)

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message=confirm_msg,
            task_id=task.task_id, candidates=pending_fields,
            explain={"resolver_explain": resolver_explain},
        )

    async def _create_followup_confirmation(
        self, req, ctx, extraction_json, decision, prev_qs, field_name, resolved_result,
    ) -> dict:
        """Follow-up 中触发确认流"""
        partial_state = merge_query_state(prev_qs, {}, req.project_id)
        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates={field_name: resolved_result.candidates},
            partial_query_state=partial_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        field_display = "事件" if field_name == "event" else "指标"
        lines = [f"请选择{field_display}:"]
        lines += [f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)" for i, c in enumerate(resolved_result.candidates[:5], 1)]
        lines.append("回复编号或名称即可")

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message="\n".join(lines),
            task_id=task.task_id, candidates={field_name: resolved_result.candidates},
            explain={"turn_explain": {
                "mode": "followup_patch", "reason": decision.reason,
                "decision": _serialize_followup_decision(decision),
            }},
        )

    # ==================== 去重：统一 resolve + bias ====================

    def _resolve_field(self, service, matcher_type, extractions, default, field_name, project_id, user_id):
        """统一 resolve + user preference bias"""
        result = service.resolve_with_candidates(matcher_type, extractions, default=default)
        bias = self._apply_bias(result, project_id=project_id, user_id=user_id, field_name=field_name)
        return result, bias

    def _apply_bias(self, result, *, project_id, user_id, field_name):
        """应用用户偏好加权"""
        reranked, bias_explain = self.preferences.rerank_candidates(
            project_id, user_id, field_name, result.candidates,
        )
        if not bias_explain.get("applied"):
            return bias_explain
        top_candidate = reranked[0] if reranked else None
        result.candidates = reranked
        if top_candidate and top_candidate["value"] != result.value:
            result.value = top_candidate["value"]
            result.score = float(top_candidate["score"])
            result.method = f"{result.method}+user_bias"
        elif top_candidate:
            result.score = max(float(result.score), float(top_candidate["score"]))
        return bias_explain

    # ==================== 去重：统一 Follow-up Patch ====================

    def _build_followup_patch(
        self, req_text, extraction_json, decision, service,
        *, project_id, user_id,
    ) -> FollowupPatchResult:
        """构建 follow-up 补丁，返回结果对象（不再用异常控制流）"""
        patch: dict[str, Any] = {}
        resolver_explain: Dict[str, Any] = {}
        confirmation = None

        # Metric
        if extraction_json.metric_extractions:
            r, _ = self._resolve_field(service, MatcherType.METRIC,
                extraction_json.metric_extractions, DEFAULT_METRIC, "metric", project_id, user_id)
            resolver_explain["metric"] = {"method": r.method, "score": r.score}
            if r.needs_confirmation and confirmation is None:
                confirmation = ("metric", r)
            else:
                patch["metric"] = r.value

        # Event
        if extraction_json.event_extractions:
            r, _ = self._resolve_field(service, MatcherType.EVENT,
                extraction_json.event_extractions, DEFAULT_EVENT, "event", project_id, user_id)
            resolver_explain["event"] = {"method": r.method, "score": r.score}
            if r.needs_confirmation and confirmation is None:
                confirmation = ("event", r)
            else:
                patch["event"] = r.value

        # GroupBy
        if extraction_json.group_by_extractions:
            r, _ = self._resolve_field(service, MatcherType.DIMENSION,
                extraction_json.group_by_extractions, DEFAULT_DIMENSION, "group_by", project_id, user_id)
            resolver_explain["group_by"] = {"method": r.method, "score": r.score}
            patch["group_by"] = [r.value]

        # Time
        if extraction_json.time_extractions:
            n_days, time_explain = service.resolve_time(extraction_json)
            resolver_explain["time"] = time_explain
            patch["time_range"] = {"type": "last_n_days", "n": n_days}
        elif decision.patch_hints.get("time_range"):
            patch["time_range"] = decision.patch_hints["time_range"]
            resolver_explain["time"] = {"method": "followup_hint", "value": decision.patch_hints["time_range"]}

        # Region
        if _query_mentions_region(req_text):
            patch["region_filter"] = extraction_json.region_filter or ["ROW"]
            resolver_explain["region"] = {"method": "llm_direct", "region_filter": patch["region_filter"]}

        return FollowupPatchResult(
            patch=patch, resolver_explain=resolver_explain,
            patch_hints=decision.patch_hints, confirmation_needed=confirmation,
        )

    # ==================== 去重：统一 DSL 构建 ====================

    def _build_semantic(self, qs: QueryState) -> SemanticDSL:
        """从 QueryState 构建 SemanticDSL"""
        return SemanticDSL(
            project_id=qs.project_id,
            region_filter=qs.region_filter or [],
            metric=Metric(metric_id=qs.metric or DEFAULT_METRIC),
            event=Event(event_name=qs.event or DEFAULT_EVENT),
            time_range=TimeRange(type="last_n_days", n=(qs.time_range or {}).get("n", DEFAULT_N_DAYS)),
            group_by=[GroupBy(dimension_id=d) for d in (qs.group_by or [DEFAULT_DIMENSION])],
            filters=qs.filters or [],
        )

    def _build_and_render_dsl(self, project_id, region_filter, metric_id, event_name,
                               n_days, group_by, filters, catalog, timing=None) -> tuple:
        """构建 SemanticDSL + 渲染 exec_dsl"""
        semantic = SemanticDSL(
            project_id=project_id, region_filter=region_filter,
            metric=Metric(metric_id=metric_id), event=Event(event_name=event_name),
            time_range=TimeRange(type="last_n_days", n=n_days),
            group_by=[GroupBy(dimension_id=d) for d in group_by], filters=filters or [],
        )
        exec_dsl = render_exec_dsl(semantic, catalog)
        validate_region(exec_dsl)
        validate_region_consistency(semantic, exec_dsl)
        return semantic, exec_dsl, timing

    def _build_and_render_dsl_from_state(self, qs: QueryState, catalog) -> tuple:
        """从 QueryState 构建 + 渲染"""
        semantic = self._build_semantic(qs)
        exec_dsl = render_exec_dsl(semantic, catalog)
        validate_region(exec_dsl)
        validate_region_consistency(semantic, exec_dsl)
        return semantic, exec_dsl, None

    # ==================== 去重：统一候选消息格式化 ====================

    def _format_candidates_message(self, pending_fields: dict) -> str:
        """格式化候选提示消息"""
        field_display_map = {"event": "事件", "metric": "指标"}
        lines = []
        for field_name, cands in pending_fields.items():
            lines.append(f"请选择{field_display_map.get(field_name, field_name)}:")
            for i, c in enumerate(cands[:5], 1):
                lines.append(f"  {i}. {c['value']} (匹配度 {c['score']:.0f}%)")
        return "\n".join(lines)

    def _format_unmatched_message(self, candidates: dict) -> str:
        """格式化未匹配候选的消息"""
        lines = ["未能识别您的选择，请回复编号或名称:"]
        lines.append(self._format_candidates_message(candidates))
        return "\n".join(lines)

    # ==================== 辅助方法 ====================

    def _record_preferences(self, ctx, qs: QueryState) -> None:
        self.preferences.record_selection(
            qs.project_id, ctx.user_id, event=qs.event, metric=qs.metric, group_by=qs.group_by,
        )

    def _build_resolver_explain(self, region_explain, mr, mb, er, eb, gr, gb, time_explain) -> dict:
        def _field_explain(result, bias):
            e = {"method": result.method, "score": result.score}
            if bias.get("applied"):
                e["user_preference_bias"] = bias
            return e
        return {
            "region": region_explain,
            "metric": _field_explain(mr, mb),
            "event": _field_explain(er, eb),
            "group_by": _field_explain(gr, gb),
            "time": time_explain,
        }

    def _build_new_query_state(self, project_id, event, metric, n_days, region_filter, group_by) -> QueryState:
        return QueryState(
            project_id=project_id, event=event, metric=metric,
            time_range={"type": "last_n_days", "n": n_days}, region_filter=region_filter,
            group_by=group_by, filters=[],
            explicit_fields=["event", "metric", "time_range", "region_filter", "group_by"],
            field_sources={f: "explicit" for f in ["event", "metric", "time_range", "region_filter", "group_by"]},
            turn_type="new_query",
        )

    def _build_turn_explain(self, mode, qs, *, decision=None, patch=None, confirmed_fields=None) -> dict:
        explain = {
            "mode": mode,
            "explicit_fields": qs.explicit_fields,
            "inherited_fields": qs.inherited_fields,
            "field_sources": qs.field_sources,
            "state_snapshot": _build_state_snapshot(qs),
        }
        if decision is not None:
            explain["decision"] = _serialize_followup_decision(decision)
        if patch is not None:
            explain["applied_patch"] = patch
            explain["applied_patch_fields"] = list(patch.keys())
        if confirmed_fields:
            explain["confirmed_fields"] = confirmed_fields
        return explain

    @staticmethod
    def _make_response(**kwargs) -> dict:
        """构建响应 dict（保持字段顺序）"""
        defaults = {"semantic": {}, "exec_dsl": {}, "explain": {}, "status": "success"}
        defaults.update(kwargs)
        return defaults


# ==================== 模块级工具函数 ====================

def _build_state_snapshot(qs: QueryState) -> Dict[str, Any]:
    return {
        "event": qs.event, "metric": qs.metric,
        "time_range": qs.time_range, "region_filter": qs.region_filter,
        "group_by": qs.group_by,
    }


def _serialize_followup_decision(decision) -> Dict[str, Any]:
    return {
        "mode": decision.mode, "confidence": decision.confidence,
        "reason": decision.reason, "matched_signals": decision.matched_signals,
        "patch_hints": decision.patch_hints, "normalized_text": decision.normalized_text,
    }


def _match_user_input_to_candidates(user_input: str, candidates: list[dict]) -> str | None:
    text = user_input.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]["value"]
    text_lower = text.lower()
    for c in candidates:
        if c["value"].lower() == text_lower:
            return c["value"]
    for c in candidates:
        if text_lower in c["value"].lower() or c["value"].lower() in text_lower:
            return c["value"]
    return None


def _query_mentions_region(text: str) -> bool:
    return any(token in text for token in _REGION_TOKENS)


def _mark_confirmation_state(qs: QueryState, confirmed_fields: Dict[str, str]) -> None:
    qs.turn_type = "confirmation"
    merged_explicit = list(dict.fromkeys([*(qs.explicit_fields or []), *confirmed_fields.keys()]))
    qs.explicit_fields = merged_explicit
    active_fields = [f for f in ("event", "metric", "time_range", "region_filter", "group_by") if getattr(qs, f, None)]
    qs.inherited_fields = [f for f in active_fields if f not in merged_explicit]
    field_sources = dict(qs.field_sources or {})
    for f in active_fields:
        if f in confirmed_fields:
            field_sources[f] = "confirmed"
        elif f not in field_sources:
            field_sources[f] = "explicit" if f in merged_explicit else "inherited"
    qs.field_sources = field_sources
