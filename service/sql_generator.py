"""ClickHouse SQL 生成器

设计（区别于模板渲染）：
- Matcher 完成表/列/指标的确定性解析后，把「已解析实体 + schema 上下文 + 时间表达式」
  组装进 prompt，由 LLM 直出 ClickHouse SQL。
- 生成结果经 sqlglot 校验，失败时带错误信息修复重试（上限 MAX_REPAIR_ROUNDS）。
- LLM 只负责 SQL 结构组装（GROUP BY / JOIN / 窗口），表名列名必须是解析出的规范名。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from service.llm_extractions import (
    _extract_json,
    _get_model_name,
    _supports_tool_calling,
    get_llm_client,
)
from service.sql_ast_analyzer import AnalysisContext, analyze_sql
from service.sql_validator import ValidationResult, validate_sql

load_dotenv()

logger = logging.getLogger(__name__)

MAX_REPAIR_ROUNDS = 2

# ==================== 时间表达式（time_range → ClickHouse 条件） ====================

def time_range_to_ch_expr(time_range: Optional[Dict[str, Any]], time_column: str) -> str:
    """把 QueryState.time_range 翻译为 ClickHouse WHERE 条件片段"""
    if not time_column:
        return ""
    tr = time_range or {}
    t = tr.get("type", "last_n_days")
    n = tr.get("n", 7)
    col = time_column

    if t == "yesterday":
        return f"{col} >= today() - 1 AND {col} < today()"
    if t == "today":
        return f"{col} >= today()"
    if t == "this_week":
        return f"{col} >= toMonday(today())"
    if t == "last_week":
        return f"{col} >= toMonday(today()) - INTERVAL 7 DAY AND {col} < toMonday(today())"
    if t == "this_month":
        return f"{col} >= toStartOfMonth(today())"
    if t == "last_month":
        return f"{col} >= toStartOfMonth(today()) - INTERVAL 1 MONTH AND {col} < toStartOfMonth(today())"
    # 默认 last_n_days
    return f"{col} >= now() - INTERVAL {n} DAY"


# ==================== window / order 文本解析 ====================

_WINDOW_PATTERNS = [
    re.compile(r"每[个一](?P<group>.+?)(?:的)?前\s*(?P<limit>\d+)"),
    re.compile(r"[各每](?P<group>.+?)(?:的)?前\s*(?P<limit>\d+)"),
]

# 英文形式（仅当文本不含中文时启用，见 parse_window_text / parse_order_text）
_EN_WINDOW_PATTERNS = [
    # "top 3 per region" / "top 3 for each category"
    # 允许 N 与介词之间夹名词短语： "top 3 categories in each region"
    re.compile(r"(?:top|first)\s+(?P<limit>\d+)\s+(?:[a-z_]+\s+){0,3}?(?:per|for each|in each|within each)\s+(?P<group>[a-z_ ]+?)\s*$", re.I),
    re.compile(r"(?:top|first)\s+(?P<limit>\d+)\s+(?:per|by)\s+(?P<group>[a-z_ ]+?)\s*$", re.I),
    # "each region top 3" / "per category, first 5"
    re.compile(r"(?:per|for each|in each|within each|each|every)\s+(?P<group>[a-z_ ]+?)\s*,?\s*(?:top|first)\s+(?P<limit>\d+)", re.I),
]

# (pattern, direction)；顺序敏感：先带 "by/of" 的完整形式，再退化形式
_EN_ORDER_PATTERNS = [
    # 允许 N 与 by 之间夹名词短语： "top 5 product categories by revenue"
    (re.compile(r"(?:top|first|highest|best)\s+(?P<limit>\d+)\s+(?:[a-z_]+\s+){0,3}?(?:by|of|in)\s+(?P<metric>[a-z_ ]+)", re.I), "DESC"),
    (re.compile(r"(?:bottom|lowest|worst|last)\s+(?P<limit>\d+)\s+(?:[a-z_]+\s+){0,3}?(?:by|of|in)\s+(?P<metric>[a-z_ ]+)", re.I), "ASC"),
    (re.compile(r"(?P<metric>[a-z_ ]+?)\s*,?\s*(?:top|first)\s+(?P<limit>\d+)", re.I), "DESC"),
    (re.compile(r"(?P<metric>[a-z_ ]+?)\s*,?\s*(?:bottom|last)\s+(?P<limit>\d+)", re.I), "ASC"),
    (re.compile(r"(?:highest|largest|biggest|most)\s+(?P<metric>[a-z_ ]+)(?P<limit>)", re.I), "DESC"),
    (re.compile(r"(?:lowest|smallest|least)\s+(?P<metric>[a-z_ ]+)(?P<limit>)", re.I), "ASC"),
]

_CJK = re.compile(r"[一-龥]")

_ORDER_PATTERNS = [
    # "销售额最高的前5" / "按销售额从高到低前5" / "金额最大的3个"
    re.compile(r"(?P<metric>.+?)(?:最高|最大|最多|从高到低|从大到小|降序)(?:的)?前?\s*(?P<limit>\d+)?"),
    re.compile(r"(?:按)?(?P<metric>.+?)(?:最低|最小|最少|从低到高|从小到大|升序)(?:的)?前?\s*(?P<limit>\d+)?"),
    # 宽松兜底："销售额前5" / "订单量前10"（无排序词，默认降序）
    re.compile(r"(?P<metric>[一-龥A-Za-z0-9_]{2,12})(?:的)?前\s*(?P<limit>\d+)"),
]


def parse_window_text(text: str) -> Optional[Dict[str, Any]]:
    """'每个地区前3' → {"group_text": "地区", "limit": 3}

    捕获组含方向词（最高/最低等）说明是全局 TopN 誤入 window 通道 → 返回 None 交由 order 路径
    """
    patterns = _WINDOW_PATTERNS if _CJK.search(text or "") else _EN_WINDOW_PATTERNS
    for pat in patterns:
        m = pat.search(text or "")
        if m:
            group = m.group("group").strip()
            if any(w in group for w in ("最高", "最低", "最大", "最小", "最多", "最少")):
                return None
            return {
                "group_text": group,
                "limit": int(m.group("limit")),
                "raw": text,
            }
    return None


def parse_order_text(text: str) -> Optional[Dict[str, Any]]:
    """'销售额最高的前5' → {"metric_text": "销售额", "limit": 5, "direction": "DESC"}"""
    if text and not _CJK.search(text):
        for pat, direction in _EN_ORDER_PATTERNS:
            m = pat.search(text.strip())
            if m and m.group("metric").strip():
                return {
                    "metric_text": m.group("metric").strip(),
                    "limit": int(m.group("limit")) if m.group("limit") else None,
                    "direction": direction,
                    "raw": text,
                }
        return None

    for idx, pat in enumerate(_ORDER_PATTERNS):
        m = pat.search(text or "")
        if m and m.group("metric").strip():
            # 前两个模式按词义定向；兜底模式默认降序（TopN 场景更常见）
            direction = "ASC" if idx == 1 else "DESC"
            metric_text = m.group("metric").strip()
            # 去掉前缀量词（"各品类销售额" → "销售额"）
            for prefix in ("各", "每", "按", "所有"):
                if metric_text.startswith(prefix) and len(metric_text) > len(prefix):
                    metric_text = metric_text[len(prefix):]
                    break
            return {
                "metric_text": metric_text,
                "limit": int(m.group("limit")) if m.group("limit") else None,
                "direction": direction,
                "raw": text,
            }
    return None


# ==================== Prompt ====================

_GENERATE_SYSTEM_PROMPT = r"""你是 ClickHouse SQL 生成器。根据用户问题和"已解析实体"生成一条 ClickHouse SQL。

硬性规则：
1. 只输出一条 SELECT（或 WITH ... SELECT）语句，禁止 INSERT/UPDATE/DELETE/DDL。
2. 表名和列名必须与"已解析实体"中给出的规范名完全一致，禁止发明不存在的表或列。
3. 指标聚合直接使用"已解析指标"给出的表达式，不要改写。
4. 时间过滤直接使用"时间条件"给出的表达式，拼进 WHERE。
5. 需要 JOIN 时使用"JOIN 关系"给出的 ON 条件。
6. 过滤值为字符串时用单引号；数值不加引号。
7. 分组排名（窗口意图）优先用 ClickHouse 的 LIMIT n BY <分组列> 语法。
8. 聚合查询不要 SELECT 无关列；明细查询按"明细列"输出。
9. 全局 TopN：按指标 ORDER BY ... DESC LIMIT n。
10. 输出裸 SQL：不要 markdown 代码块，不要任何解释文字。
"""

_GENERATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "emit_sql",
        "description": "输出生成的 ClickHouse SQL",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "完整的 ClickHouse SELECT 语句"},
            },
            "required": ["sql"],
        },
    },
}


def _render_intent_block(intent: Dict[str, Any]) -> str:
    """把解析后的实体渲染进 prompt（规范名来源，LLM 不得偏离）"""
    lines = []

    if intent.get("base_table"):
        lines.append(f"主表: {intent['base_table']}")
    for m in intent.get("metrics", []):
        lines.append(f"指标: {m['id']} = {m['expr']}")
    if intent.get("detail_columns"):
        lines.append(f"明细列: {', '.join(intent['detail_columns'])}")
    if intent.get("group_by"):
        lines.append(f"分组列: {', '.join(intent['group_by'])}")
    for f in intent.get("filters", []):
        value = f.get("value", "")
        if isinstance(value, str) and not value.isdigit():
            value = f"'{value}'"
        lines.append(f"过滤: {f['column']} {f.get('op', '=')} {value}")
    if intent.get("time_expr"):
        lines.append(f"时间条件: {intent['time_expr']}")
    for j in intent.get("joins", []):
        cond = j.get("condition") or j.get("on") or ""
        lines.append(f"JOIN 关系: {j['left']} ↔ {j['right']} ON {cond}")
    if intent.get("window"):
        w = intent["window"]
        lines.append(f"窗口意图: {w.get('group_by')} 分组内前 {w.get('limit')}")
    if intent.get("order_by"):
        o = intent["order_by"]
        lines.append(f"排序意图: 按 {o.get('metric_expr')} {o.get('direction')}"
                     + (f" LIMIT {o['limit']}" if o.get("limit") else ""))

    return "\n".join(lines) if lines else "（无显式实体，仅按用户问题与 Schema 生成）"


def _build_generate_messages(query: str, intent: Dict[str, Any], schema_prompt: str,
                             use_tool_calling: bool, repair_error: Optional[str] = None) -> list[dict[str, str]]:
    system_prompt = _GENERATE_SYSTEM_PROMPT
    if repair_error:
        system_prompt += (
            f"\n\n=== 上一次生成被校验拒绝 ===\n{repair_error}\n"
            f"请修正以上问题后重新输出完整 SQL。\n=== 结束 ===\n"
        )

    user_content = (
        f"=== Schema（唯一可用表/列） ===\n{schema_prompt}\n\n"
        f"=== 已解析实体 ===\n{_render_intent_block(intent)}\n\n"
        f"=== 用户问题 ===\n{query}\n\n返回 SQL："
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    if not use_tool_calling:
        messages[-1]["content"] += "（直接输出 SQL 文本，不要代码块）"
    return messages


# ==================== 生成主入口 ====================

def _call_llm(messages: list[dict[str, str]]) -> str:
    """同步 LLM 调用，返回 SQL 文本"""
    client = get_llm_client()
    model = _get_model_name()
    use_tool = _supports_tool_calling()

    kwargs: Dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.1}
    if use_tool:
        kwargs["tools"] = [_GENERATE_TOOL_SCHEMA]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "emit_sql"}}

    resp = client.chat.completions.create(**kwargs)
    message = resp.choices[0].message

    if use_tool and message.tool_calls:
        args = json.loads(message.tool_calls[0].function.arguments)
        return args.get("sql", "").strip()

    content = message.content if hasattr(message, "content") else str(message)
    return _extract_sql_text(content)


def _extract_sql_text(content: str) -> str:
    """prompt 路径：剥掉 markdown 与解释，尽量取出 SQL"""
    sql = _extract_json(content)
    if sql.startswith("{"):
        try:
            data = json.loads(sql)
            if isinstance(data, dict) and "sql" in data:
                return str(data["sql"]).strip()
        except json.JSONDecodeError:
            pass
    # fallback：剥代码块原样返回
    text = (content or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else text
        text = text.replace("```", "").strip()
    return text.rstrip(";").strip()


async def generate_sql(
    query: str,
    intent: Dict[str, Any],
    schema_prompt: str,
    allowed_tables: list[str],
    analysis_context: Optional[AnalysisContext] = None,
) -> tuple[str, Dict[str, Any]]:
    """生成 + 校验 + AST 分析 + 修复循环

    Args:
        analysis_context: 传入时启用 AST 后置分析（列存在性/实体保真/join 一致性/静态成本），
                          分析错误与校验错误一并回灌修复循环

    Returns:
        (sql, explain)  — 校验最终失败时抛 ValueError（由编排层转 error 响应）
    """
    total_rounds = MAX_REPAIR_ROUNDS + 1
    repair_error: Optional[str] = None
    explain: Dict[str, Any] = {"rounds": []}

    for round_no in range(total_rounds):
        messages = _build_generate_messages(query, intent, schema_prompt, _supports_tool_calling(), repair_error)
        sql = await asyncio.to_thread(_call_llm, messages)

        result: ValidationResult = validate_sql(sql, allowed_tables)
        errors = list(result.errors)

        # AST 后置分析（确定性，只在语法校验通过后运行）
        analysis = None
        if result.ok and analysis_context is not None:
            analysis = analyze_sql(result.sql, analysis_context, intent)
            errors.extend(analysis.errors)

        explain["rounds"].append({
            "round": round_no + 1,
            "ok": result.ok and not errors,
            "errors": errors,
        })

        if result.ok and not errors:
            explain["final_sql"] = result.sql
            explain["repaired"] = round_no > 0
            if analysis is not None:
                explain["ast_analysis"] = {
                    "warnings": analysis.warnings,
                    "cost": analysis.cost,
                    "qualified": analysis.qualified,
                }
            return result.sql, explain

        repair_error = "; ".join(errors) if errors else result.error
        logger.warning("SQL validation/analysis failed (round %d): %s", round_no + 1, repair_error)

    raise ValueError(f"SQL validation failed after {total_rounds} rounds: {repair_error}")
