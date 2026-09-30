"""查询编排器 — SQL 生成版核心业务逻辑

三条处理路径：
1. new_query: 完整的新查询（LLM 抽取意图 → Matcher 解析表/列/指标 → QueryState → LLM 生成 ClickHouse SQL）
2. followup_patch: 基于上轮状态的修改（检测 → 补丁 → 合并 → 重新生成 SQL）
3. confirmation: 用户确认低置信度候选（表 / 指标 / join 关系）

职责边界：
- LLM Layer 1 只抽取意图片段，SQL 结构组装由 generate_sql 完成
- 表/列/指标名字解析由 Matcher 确定性完成（倒排索引 + rapidfuzz），score 40-80 触发确认
- join 关系由 schema 配置推断，不靠 LLM 猜
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.types import MatcherType
from memory.memory_writer import MemoryWriter
from memory.user_preference_store import UserPreferenceStore
from service.followup_resolver import FollowupDecision, detect_followup
from service.llm_extractions import Extraction, extract_llm_async
from service.query_state_merger import merge_query_state
from service.session_manager import SessionManager
from service.session_models import QueryState
from service.sql_ast_analyzer import build_analysis_context
from service.sql_generator import (
    generate_sql,
    parse_order_text,
    parse_window_text,
    time_range_to_ch_expr,
)
from service.task_manager import TaskManager

logger = logging.getLogger(__name__)



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

    async def process(self, req, service, catalog=None, notify_fn=None) -> dict:
        """主入口：路由到三条处理路径

        Args:
            req: NL2DSLRequest
            service: MatcherService
            catalog: 兼容参数（SQL 版 schema 挂在 service.schema 上）
            notify_fn: async callable(chat_id, message) for Telegram notifications

        Returns:
            NL2SQLResponse dict
        """
        ctx = self.session.create_or_get(req.session_id, req.user_id, req.project_id)
        prev_qs = ctx.last_query_state

        # 1. 确认流拦截
        if ctx.pending_task_id:
            task = self.task.get_task(ctx.pending_task_id)
            if task and task.status == "waiting_confirmation":
                return await self._process_confirmation(req, ctx, task, service)

        # 2. LLM 抽取意图
        session_context = self.session.get_enhanced_context(ctx.session_id, query_text=req.text)
        timing = {}
        total_start = time.time()

        layer1_start = time.time()
        extraction_json = await extract_llm_async(req.text, session_context=session_context)
        timing["layer1_llm_extraction_s"] = round(time.time() - layer1_start, 3)
        logger.debug("Layer1: user=%s, intent=%s", req.text,
                      json.dumps(extraction_json.model_dump(), ensure_ascii=False))

        # 3. Follow-up 检测
        followup_decision = detect_followup(req.text, prev_qs, pending_task=False)
        logger.debug("Turn decision: mode=%s, confidence=%.2f, reason=%s",
                      followup_decision.mode, followup_decision.confidence, followup_decision.reason)

        # 4. Early exit（完全无查询信号且非 follow-up）
        has_signal = any([
            extraction_json.table_extractions,
            extraction_json.metric_extractions,
            extraction_json.column_extractions,
            extraction_json.filter_extractions,
            extraction_json.group_by_extractions,
            extraction_json.window_extractions,
            extraction_json.order_extractions,
        ])
        if not has_signal and not followup_decision.is_followup:
            self.session.add_message(ctx.session_id, "user", req.text, metadata={"error": "missing_query_signal"})
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message="您目前没有输入任何表、指标或查询条件，无法生成 SQL 哦",
            )

        # 5. Follow-up 路径
        if followup_decision.is_followup and prev_qs is not None:
            return await self._process_followup(
                req, ctx, extraction_json, followup_decision, prev_qs, service,
            )

        # 6. 新查询路径
        return await self._process_new_query(
            req, ctx, extraction_json, followup_decision, service,
            timing, total_start, notify_fn=notify_fn,
        )

    # ==================== 新查询路径 ====================

    async def _process_new_query(
        self, req, ctx, extraction_json, followup_decision,
        service, timing, total_start, notify_fn=None,
    ) -> dict:
        """处理完整的新查询"""
        prev_qs = ctx.last_query_state

        # Layer2: Matcher 解析
        layer2_start = time.time()

        table_result, table_bias = await self._resolve_field(
            service, MatcherType.TABLE, extraction_json.table_extractions,
            None, "table", req.project_id, ctx.user_id, query_text=req.text,
        )
        metric_result, metric_bias = await self._resolve_field(
            service, MatcherType.METRIC, extraction_json.metric_extractions,
            None, "metric", req.project_id, ctx.user_id, query_text=req.text,
        )

        # 时间
        n_days, time_explain = service.resolve_time(extraction_json)
        time_range = _time_range_from(n_days, time_explain)

        # 列解析（group_by / detail / filter column / window group）
        column_explain: Dict[str, Any] = {}
        group_by_cols, _ = self._resolve_texts_to_columns(
            service, [e.text for e in extraction_json.group_by_extractions], column_explain,
        )
        detail_cols, _ = self._resolve_texts_to_columns(
            service, [e.text for e in extraction_json.column_extractions], column_explain,
        )
        filters = self._resolve_filters(service, extraction_json.filter_extractions, column_explain)

        # window / order 意图（含方向词的 window 文本降级走全局 TopN 解析）
        window = None
        demoted_order_text = None
        if extraction_json.window_extractions:
            w_text = extraction_json.window_extractions[0].text
            w = parse_window_text(w_text)
            if w:
                w_cols, _ = self._resolve_texts_to_columns(service, [w["group_text"]], column_explain)
                if w_cols:
                    window = {"group_by": w_cols[0], "limit": w["limit"]}
                else:
                    column_explain["window"] = {"dropped": True, "raw": w["raw"]}
            else:
                demoted_order_text = w_text

        order_by = None
        order_source = extraction_json.order_extractions[0].text if extraction_json.order_extractions else demoted_order_text
        if order_source:
            o = parse_order_text(order_source)
            if o:
                o_res, _ = await self._resolve_field(
                    service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                    None, "metric", req.project_id, ctx.user_id, query_text=req.text,
                )
                # 排序词解析不出指标时（如"各品类销售额最高"切成"品类销售额"），回退到主指标
                order_metric = o_res.value or metric_result.value
                order_by = {
                    "metric": order_metric,
                    "metric_expr": service.get_metric_expr(order_metric) if order_metric else None,
                    "direction": o["direction"],
                    "limit": o["limit"],
                }

        timing["layer2_resolution_s"] = round(time.time() - layer2_start, 3)

        resolver_explain = self._build_resolver_explain(
            table_result, table_bias, metric_result, metric_bias,
            group_by_cols, detail_cols, filters, time_explain, column_explain,
            window=window, order_by=order_by,
        )

        # 确认流判断（表 / 指标）
        pending_fields = {}
        if table_result.needs_confirmation:
            pending_fields["tables"] = table_result.candidates
        if metric_result.needs_confirmation:
            pending_fields["metrics"] = metric_result.candidates

        if pending_fields:
            return await self._create_confirmation_task(
                req, ctx, extraction_json, pending_fields, resolver_explain,
                table_result, metric_result, time_range,
                group_by_cols, detail_cols, filters, window, order_by,
                notify_fn=notify_fn,
            )

        # 组装 QueryState
        metric_ids = [metric_result.value] if metric_result.value else []
        tables = [table_result.value] if table_result.value else []
        query_state = self._assemble_query_state(
            req.project_id, tables, metric_ids, group_by_cols, detail_cols,
            filters, time_range, window, order_by, turn_type="new_query",
        )

        # 主表推断（用户可能不提表名）
        base_table, table_infer_explain = service.infer_main_table(
            tables, metric_ids, group_by_cols + detail_cols + [f["column"] for f in filters],
        )
        if not base_table:
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message="无法确定要查询的表，请说明表名或使用已知指标（如销售额/订单量）",
                explain={"resolver_explain": {**resolver_explain, "table_inference": table_infer_explain}},
            )
        query_state.tables = [base_table]
        resolver_explain["table_inference"] = table_infer_explain

        # join 推断 + 生成 SQL
        sql, gen_explain, join_error = await self._generate_sql_from_state(
            req.text, query_state, service,
        )
        if join_error:
            # join 缺失 → 确认流（候选 = 主表邻接表）
            return await self._create_join_confirmation(
                req, ctx, extraction_json, followup_decision, prev_qs,
                query_state, service, join_error, resolver_explain,
            )

        timing["total_s"] = round(time.time() - total_start, 3)
        resolver_explain["sql_generation"] = gen_explain

        # 会话记录
        self.session.add_message(ctx.session_id, "user", req.text, metadata={
            "tables": query_state.tables, "metrics": query_state.metrics,
        })
        self.session.update_query_state(ctx.session_id, query_state)
        self._record_preferences(ctx, query_state)

        # 异步记忆学习
        prev_state_dict = prev_qs.to_dict() if prev_qs else None
        asyncio.create_task(self.memory.maybe_save(
            project_id=req.project_id, user_query=req.text,
            extraction=extraction_json.model_dump(), resolver_explain=resolver_explain,
            current_state=query_state.to_dict(), prev_state=prev_state_dict,
        ))

        if req.chat_id and notify_fn:
            await notify_fn(req.chat_id, f"SQL 已生成，请查收 👇")

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            sql=sql,
            resolved_intent=query_state.to_dict(),
            explain={
                "turn_explain": self._build_turn_explain("new_query", query_state, decision=followup_decision),
                "resolver_explain": resolver_explain, "timing": timing,
            },
            session_id=ctx.session_id,
        )

    # ==================== Follow-up 路径 ====================

    async def _process_followup(
        self, req, ctx, extraction_json, decision, prev_qs, service,
    ) -> dict:
        """处理 follow-up 补丁查询"""
        patch_result = await self._build_followup_patch(
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
        sql, gen_explain, join_error = await self._generate_sql_from_state(req.text, merged_state, service)
        if join_error:
            return await self._create_join_confirmation(
                req, ctx, extraction_json, decision, prev_qs,
                merged_state, service, join_error, patch_result.resolver_explain,
            )

        self.session.add_message(ctx.session_id, "user", req.text,
                                  metadata={"turn_mode": "followup_patch", "patch": patch_result.patch})
        self.session.update_query_state(ctx.session_id, merged_state)
        self._record_preferences(ctx, merged_state)

        return self._make_response(
            extraction_json=extraction_json.model_dump(),
            sql=sql,
            resolved_intent=merged_state.to_dict(),
            explain={
                "turn_explain": self._build_turn_explain("followup_patch", merged_state,
                                                          decision=decision, patch=patch_result.patch),
                "resolver_explain": {**patch_result.resolver_explain, "sql_generation": gen_explain},
            },
            session_id=ctx.session_id,
        )

    # ==================== 确认路径 ====================

    async def _process_confirmation(self, req, ctx, task, service) -> dict:
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
            if field == "tables":
                qs.tables = [value]
            elif field == "metrics":
                qs.metrics = [value]
            elif field == "join":
                extra_table = value
                if extra_table not in qs.tables:
                    qs.tables.append(extra_table)
        _mark_confirmation_state(qs, task.user_selection)

        sql, gen_explain, join_error = await self._generate_sql_from_state(task.raw_query, qs, service)
        if join_error:
            hint_msg = f"仍无法确定 join 关系: {join_error['missing']}"
            return self._make_response(
                extraction_json=task.extraction, status="needs_confirmation",
                session_id=ctx.session_id, message=hint_msg,
                task_id=task.task_id,
            )

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
            sql=sql,
            resolved_intent=qs.to_dict(),
            explain={
                "confirmed": task.user_selection, "method": "user_confirmation",
                "turn_explain": self._build_turn_explain("confirmation", qs, confirmed_fields=task.user_selection),
                "sql_generation": gen_explain,
            },
            session_id=ctx.session_id, status="success",
            message=f"已按您的选择生成 SQL: {task.user_selection}",
        )

    # ==================== 确认任务创建 ====================

    async def _create_confirmation_task(
        self, req, ctx, extraction_json, pending_fields, resolver_explain,
        table_result, metric_result, time_range,
        group_by_cols, detail_cols, filters, window, order_by, notify_fn=None,
    ) -> dict:
        """创建确认任务并返回 needs_confirmation 响应"""
        partial_state = self._assemble_query_state(
            req.project_id,
            [table_result.value] if table_result.value else [],
            [metric_result.value] if metric_result.value else [],
            group_by_cols, detail_cols, filters, time_range, window, order_by,
            turn_type="confirmation",
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

        field_display = {"tables": "表", "metrics": "指标"}.get(field_name, field_name)
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

    async def _create_join_confirmation(
        self, req, ctx, extraction_json, decision, prev_qs,
        query_state, service, join_error, resolver_explain,
    ) -> dict:
        """join 路径缺失时确认流：候选 = 主表的邻接表"""
        peers = service.schema.join_graph.get(query_state.tables[0], [])
        candidates = [{"value": p["peer"], "score": 100.0} for p in peers]

        if not candidates:
            return self._make_response(
                extraction_json=extraction_json.model_dump(), status="early_exit",
                session_id=ctx.session_id,
                message=f"无法确定 {join_error['missing']} 与主表的关联关系，请在 schema 配置中补充 joins",
                explain={"resolver_explain": resolver_explain},
            )

        task = self.task.create_task(
            session_id=ctx.session_id, raw_query=req.text,
            extraction=extraction_json.model_dump(), user_id=ctx.user_id,
            candidates={"join": candidates},
            partial_query_state=query_state,
        )
        self.session.update_pending_task(ctx.session_id, task.task_id)

        lines = [f"表 {query_state.tables[0]} 与 {', '.join(join_error['missing'])} 之间未配置直接 join，请选择关联表:"]
        lines += [f"  {i}. {c['value']}" for i, c in enumerate(candidates[:5], 1)]
        lines.append("回复编号或名称即可")

        return self._make_response(
            extraction_json=extraction_json.model_dump(), status="needs_confirmation",
            session_id=ctx.session_id, message="\n".join(lines),
            task_id=task.task_id, candidates={"join": candidates},
            explain={"resolver_explain": resolver_explain},
        )

    # ==================== SQL 生成 ====================

    async def _generate_sql_from_state(self, query_text, qs: QueryState, service):
        """从 QueryState 生成 ClickHouse SQL

        Returns:
            (sql, gen_explain, join_error) — join_error 非 None 表示 join 缺失需确认
        """
        base_table = qs.tables[0] if qs.tables else None
        if not base_table:
            raise ValueError("QueryState has no base table")

        # 派生列集合：group_by / detail / filter column / window group / 额外表
        filter_cols = [f["column"] for f in (qs.filters or []) if f.get("column")]
        derived_columns = sorted(set(
            list(qs.group_by or []) + list(qs.detail_columns or []) + filter_cols
            + ([qs.window["group_by"]] if qs.window and qs.window.get("group_by") else [])
        ))
        qs.columns = derived_columns

        # join 推断
        needed_tables = set(qs.tables[1:]) | {c.split(".")[0] for c in derived_columns}
        join_steps, join_explain = service.infer_joins(base_table, sorted(needed_tables))
        if join_explain.get("missing_join"):
            return "", {"join_explain": join_explain}, {"missing": join_explain["missing_join"]}

        # 时间表达式
        time_column = service.schema.time_column_of(base_table)
        time_expr = time_range_to_ch_expr(qs.time_range, f"{base_table}.{time_column}" if time_column else "")

        # order_by 的 metric_expr 补全
        order_by = None
        if qs.order_by:
            order_by = dict(qs.order_by)
            if not order_by.get("metric_expr"):
                order_by["metric_expr"] = service.get_metric_expr(order_by.get("metric", ""))

        intent = {
            "base_table": base_table,
            "metrics": [{"id": m, "expr": service.get_metric_expr(m)} for m in (qs.metrics or [])],
            "detail_columns": list(qs.detail_columns or []),
            "group_by": list(qs.group_by or []),
            "filters": list(qs.filters or []),
            "time_expr": time_expr,
            "joins": join_steps,
            "window": qs.window,
            "order_by": order_by,
        }

        sql, gen_explain = await generate_sql(
            query_text, intent, service.to_schema_prompt(),
            list(service.schema.tables.keys()),
            analysis_context=build_analysis_context(service.schema),
        )
        gen_explain["join_explain"] = join_explain
        return sql, gen_explain, None

    # ==================== 统一 resolve + bias + cross-encoder ====================

    async def _resolve_field(self, service, matcher_type, extractions, default, field_name,
                             project_id, user_id, query_text=None):
        """统一 resolve + user preference bias +（可选）cross-encoder 终选"""
        result = service.resolve_with_candidates(matcher_type, extractions, default=default)
        bias = self._apply_bias(result, project_id=project_id, user_id=user_id, field_name=field_name)
        if query_text:
            await self._apply_cross_encoder(result, query_text, field_name, bias)
        return result, bias

    async def _apply_cross_encoder(self, result, query_text, field_name, bias_explain):
        """LLM cross-encoder 受限终选（RERANKER_ENABLED 开启时触发）

        触发条件（二选一）：
        - 低置信确认带（needs_confirmation 或 score<80）
        - 高分但 top1/top2 分差过小（并列歧义同样危险）

        - relevance>=85 且 margin>=15 → 静默采纳（免一次确认打断）
        - 否则保持确认流，仅按相关性重排候选（最优排第一）
        - explain 记录在 bias_explain["cross_encoder_rerank"]
        """
        from service import reranker

        if not reranker.is_reranker_enabled():
            return
        if len(result.candidates) < 2:
            return

        if not result.needs_confirmation and result.score >= 80.0:
            top2_score = float(result.candidates[1].get("score", 0.0))
            if top2_score < float(result.score) - 5.0:
                return  # 高置信且领先明显，无需终选

        reranked, rexplain = await reranker.rerank_candidates(query_text, field_name, result.candidates)
        bias_explain["cross_encoder_rerank"] = rexplain
        if not rexplain.get("applied"):
            return

        top = reranked[0]
        result.candidates = reranked
        if rexplain.get("auto_accept"):
            result.value = top["value"]
            result.score = float(top["relevance"])
            result.method = f"{result.method}+cross_encoder"
            result.needs_confirmation = False

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

    # ==================== 列 / 过滤解析 ====================

    def _resolve_texts_to_columns(self, service, texts: list[str], explain_sink: dict) -> tuple[list[str], list[dict]]:
        """自然语言片段 → 限定列名列表（歧义容忍：40-80 取 top1，<40 丢弃）"""
        resolved: list[str] = []
        entries: list[dict] = []
        for text in texts:
            if not text or not text.strip():
                continue
            r = service.resolve_with_candidates(MatcherType.COLUMN, [Extraction(text=text)])
            entry = {"text": text, "method": r.method, "score": r.score}
            if r.value:
                entry["column"] = r.value
                resolved.append(r.value)
            elif r.candidates:
                # 无匹配但有召回候选 → 取 top1（列歧义容忍，避免频繁打断用户）
                top = r.candidates[0]["value"]
                entry.update({"column": top, "note": "recall_top1"})
                resolved.append(top)
            else:
                entry["dropped"] = True
            entries.append(entry)
        if entries:
            explain_sink["columns"] = entries
        return resolved, entries

    def _resolve_filters(self, service, filter_extractions, explain_sink: dict) -> list[dict]:
        """filter_extractions → 结构化 filters（column 走 matcher 解析）"""
        filters: list[dict] = []
        entries: list[dict] = []
        for fe in filter_extractions:
            col_text = fe.column or fe.text
            r = service.resolve_with_candidates(MatcherType.COLUMN, [Extraction(text=col_text)])
            qualified = r.value or (r.candidates[0]["value"] if r.candidates else None)
            entry = {"text": fe.text, "column_text": col_text, "column": qualified,
                     "op": fe.op, "value": fe.value}
            if qualified and fe.value is not None:
                filters.append({"column": qualified, "op": fe.op, "value": fe.value})
            else:
                entry["dropped"] = True
            entries.append(entry)
        if entries:
            explain_sink["filters"] = entries
        return filters

    # ==================== Follow-up Patch ====================

    async def _build_followup_patch(
        self, req_text, extraction_json, decision, service,
        *, project_id, user_id,
    ) -> FollowupPatchResult:
        """构建 follow-up 补丁，返回结果对象（不再用异常控制流）"""
        patch: dict[str, Any] = {}
        resolver_explain: Dict[str, Any] = {}
        column_explain: Dict[str, Any] = {}
        confirmation = None

        # Table
        if extraction_json.table_extractions:
            r, _ = await self._resolve_field(service, MatcherType.TABLE,
                extraction_json.table_extractions, None, "table", project_id, user_id,
                query_text=req_text)
            resolver_explain["table"] = {"method": r.method, "score": r.score}
            if r.needs_confirmation and confirmation is None:
                confirmation = ("tables", r)
            else:
                patch["tables"] = [r.value]

        # Metric
        if extraction_json.metric_extractions:
            r, _ = await self._resolve_field(service, MatcherType.METRIC,
                extraction_json.metric_extractions, None, "metric", project_id, user_id,
                query_text=req_text)
            resolver_explain["metric"] = {"method": r.method, "score": r.score}
            if r.needs_confirmation and confirmation is None:
                confirmation = ("metrics", r)
            else:
                patch["metrics"] = [r.value]

        # GroupBy（列解析）
        if extraction_json.group_by_extractions:
            cols, _ = self._resolve_texts_to_columns(
                service, [e.text for e in extraction_json.group_by_extractions], column_explain)
            if cols:
                patch["group_by"] = cols

        # 明细列
        if extraction_json.column_extractions:
            cols, _ = self._resolve_texts_to_columns(
                service, [e.text for e in extraction_json.column_extractions], column_explain)
            if cols:
                patch["detail_columns"] = cols

        # 过滤
        if extraction_json.filter_extractions:
            filters = self._resolve_filters(service, extraction_json.filter_extractions, column_explain)
            if filters:
                patch["filters"] = filters

        # Time
        if extraction_json.time_extractions:
            n_days, time_explain = service.resolve_time(extraction_json)
            resolver_explain["time"] = time_explain
            patch["time_range"] = _time_range_from(n_days, time_explain)
        elif decision.patch_hints.get("time_range"):
            patch["time_range"] = decision.patch_hints["time_range"]
            resolver_explain["time"] = {"method": "followup_hint", "value": decision.patch_hints["time_range"]}

        # Window（含方向词时降级为全局 TopN）
        if extraction_json.window_extractions:
            w_text = extraction_json.window_extractions[0].text
            w = parse_window_text(w_text)
            if w:
                w_cols, _ = self._resolve_texts_to_columns(service, [w["group_text"]], column_explain)
                if w_cols:
                    patch["window"] = {"group_by": w_cols[0], "limit": w["limit"]}
            else:
                o = parse_order_text(w_text)
                if o:
                    o_res, _ = await self._resolve_field(
                        service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                        None, "metric", project_id, user_id, query_text=req_text,
                    )
                    patch["order_by"] = {
                        "metric": o_res.value,
                        "metric_expr": service.get_metric_expr(o_res.value),
                        "direction": o["direction"],
                        "limit": o["limit"],
                    }

        # Order
        if extraction_json.order_extractions:
            o = parse_order_text(extraction_json.order_extractions[0].text)
            if o:
                o_res, _ = await self._resolve_field(
                    service, MatcherType.METRIC, [Extraction(text=o["metric_text"])],
                    None, "metric", project_id, user_id, query_text=req_text)
                patch["order_by"] = {
                    "metric": o_res.value,
                    "metric_expr": service.get_metric_expr(o_res.value),
                    "direction": o["direction"],
                    "limit": o["limit"],
                }

        if column_explain:
            resolver_explain["columns"] = column_explain.get("columns", [])
            resolver_explain["filters_parse"] = column_explain.get("filters", [])

        return FollowupPatchResult(
            patch=patch, resolver_explain=resolver_explain,
            patch_hints=decision.patch_hints, confirmation_needed=confirmation,
        )

    # ==================== 状态组装 / 偏好 ====================

    _SQL_STATE_FIELDS = ("tables", "metrics", "detail_columns", "filters",
                         "time_range", "group_by", "order_by", "limit", "window")

    def _assemble_query_state(
        self, project_id, tables, metrics, group_by, detail_cols,
        filters, time_range, window, order_by, turn_type,
    ) -> QueryState:
        explicit = []
        field_sources: Dict[str, str] = {}
        values = {
            "tables": tables, "metrics": metrics, "detail_columns": detail_cols,
            "filters": filters, "time_range": time_range, "group_by": group_by,
            "order_by": order_by, "window": window,
        }
        for name in self._SQL_STATE_FIELDS:
            if values.get(name):
                explicit.append(name)
                field_sources[name] = "explicit"
        return QueryState(
            project_id=project_id,
            tables=tables, metrics=metrics, detail_columns=detail_cols,
            filters=filters, time_range=time_range, group_by=group_by,
            order_by=order_by, window=window,
            explicit_fields=explicit, field_sources=field_sources,
            turn_type=turn_type,
        )

    def _record_preferences(self, ctx, qs: QueryState) -> None:
        self.preferences.record_selection(
            qs.project_id, ctx.user_id,
            table=qs.tables[0] if qs.tables else None,
            metric=qs.metrics[0] if qs.metrics else None,
            columns=(qs.group_by or [])[:2],
        )

    # ==================== explain / 消息 ====================

    def _build_resolver_explain(
        self, tr, tb, mr, mb, group_by_cols, detail_cols, filters,
        time_explain, column_explain, *, window=None, order_by=None,
    ) -> dict:
        def _field_explain(result, bias):
            e = {"method": result.method, "score": result.score}
            if bias.get("applied"):
                e["user_preference_bias"] = bias
            if bias.get("cross_encoder_rerank"):
                e["cross_encoder_rerank"] = bias["cross_encoder_rerank"]
            return e
        return {
            "table": _field_explain(tr, tb),
            "metric": _field_explain(mr, mb),
            "group_by": group_by_cols,
            "detail_columns": detail_cols,
            "filters": filters,
            "time": time_explain,
            "columns_parse": column_explain,
            "window": window,
            "order_by": order_by,
        }

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

    def _format_candidates_message(self, pending_fields: dict) -> str:
        """格式化候选提示消息"""
        field_display_map = {"tables": "表", "metrics": "指标", "join": "关联表"}
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

    @staticmethod
    def _make_response(**kwargs) -> dict:
        """构建响应 dict（保持字段顺序）"""
        defaults = {"sql": "", "resolved_intent": {}, "explain": {}, "status": "success"}
        defaults.update(kwargs)
        return defaults


# ==================== 模块级工具函数 ====================

def _time_range_from(days: int, explain: Dict[str, Any]) -> Dict[str, Any]:
    """TimeMatcher explain → QueryState.time_range"""
    return {"type": explain.get("pattern", "last_n_days"), "n": days}


def _build_state_snapshot(qs: QueryState) -> Dict[str, Any]:
    return {
        "tables": qs.tables, "metrics": qs.metrics,
        "time_range": qs.time_range,
        "filters": qs.filters,
        "group_by": qs.group_by,
        "window": qs.window, "order_by": qs.order_by,
    }


def _serialize_followup_decision(decision: FollowupDecision) -> Dict[str, Any]:
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


def _mark_confirmation_state(qs: QueryState, confirmed_fields: Dict[str, str]) -> None:
    qs.turn_type = "confirmation"
    merged_explicit = list(dict.fromkeys([*(qs.explicit_fields or []), *confirmed_fields.keys()]))
    qs.explicit_fields = merged_explicit
    active_fields = [f for f in QueryOrchestrator._SQL_STATE_FIELDS if getattr(qs, f, None)]
    qs.inherited_fields = [f for f in active_fields if f not in merged_explicit]
    field_sources = dict(qs.field_sources or {})
    for f in active_fields:
        if f in confirmed_fields:
            field_sources[f] = "confirmed"
        elif f not in field_sources:
            field_sources[f] = "explicit" if f in merged_explicit else "inherited"
    qs.field_sources = field_sources
