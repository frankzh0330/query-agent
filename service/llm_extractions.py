from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

logger = logging.getLogger(__name__)

# ==================== 缓存机制 ====================
_query_cache: Dict[str, dict] = {}


def _query_hash(text: str) -> str:
    """生成查询的哈希值用于缓存"""
    return hashlib.md5(text.encode()).hexdigest()


def _is_cache_enabled() -> bool:
    """检查是否启用缓存"""
    return os.getenv("ENABLE_LLM_CACHE", "true").lower() == "true"


# ==================== 会话上下文 ====================
def _build_context_str(session_context: Optional[Dict[str, Any]]) -> str:
    """构建动态会话上下文字符串（注入到提示词）

    只保留有价值的动态上下文：
    1. QueryState — 上一轮 SQL 查询意图（上下文继承，最高价值）
    2. 最近用户消息 — 辅助理解
    记忆文件纠正内容在 _build_messages 中单独注入。
    """
    if not session_context:
        return ""

    parts = []

    # QueryState — 最高价值
    if session_context.get("last_query_state"):
        qs = session_context["last_query_state"]
        state_parts = []
        if qs.get("tables"):
            state_parts.append(f"表={','.join(qs['tables'])}")
        if qs.get("metrics"):
            state_parts.append(f"指标={','.join(qs['metrics'])}")
        if qs.get("columns"):
            state_parts.append(f"列={','.join(qs['columns'])}")
        if qs.get("time_range"):
            tr = qs["time_range"]
            state_parts.append(f"时间=近{tr.get('n', '?')}天")
        if qs.get("group_by"):
            state_parts.append(f"分组={','.join(qs['group_by'])}")
        if qs.get("filters"):
            filter_strs = [f"{f.get('column')}{f.get('op')}{f.get('value')}" for f in qs["filters"]]
            state_parts.append(f"过滤={','.join(filter_strs)}")
        if qs.get("window"):
            w = qs["window"]
            state_parts.append(f"窗口排名={w.get('group_by')}前{w.get('limit')}")

        if state_parts:
            parts.append(f"上次查询: {', '.join(state_parts)}")

    # 最近用户消息 — 辅助理解
    recent = session_context.get("recent_queries", [])
    if recent:
        history_lines = [f"Q{i}: {q}" for i, q in enumerate(recent[-3:], 1)]
        parts.append("=== 最近对话 ===\n" + "\n".join(history_lines) + "\n=== 历史结束 ===")

    return "\n".join(parts) if parts else ""


def _is_followup_turn(session_context: Optional[Dict[str, Any]]) -> bool:
    """是否处于更可能是 follow-up 的轮次。"""
    if not session_context:
        return False
    return bool(session_context.get("last_query_state")) and bool(session_context.get("turn_index", 0))


def _build_followup_instruction(session_context: Optional[Dict[str, Any]]) -> str:
    """为多轮 follow-up 场景追加更偏 patch extraction 的指令。"""
    if not _is_followup_turn(session_context):
        return ""

    return (
        "\n\n=== 多轮查询补充规则 ===\n"
        "如果当前问题看起来是在补充、修改或缩写上一轮查询，请优先抽取本轮明确提到的新信息。\n"
        "不要为了补全而重复输出用户本轮没有明确说出的表、指标、列、过滤条件。\n"
        "如果本轮只说了时间、过滤或分组变化，就只抽取这些变化。\n"
        "只有当用户本轮明确提到表或指标时，才填充对应 extractions。\n"
        "=== 规则结束 ===\n"
    )


def _extract_json(content: str) -> str:
    """
    从内容中提取第一个完整的 JSON 对象

    处理 LLM 可能返回的额外数据：
    1. 移除 markdown 代码块标记
    2. 提取第一个完整的 JSON 对象
    3. 忽略 JSON 后的额外内容
    """
    content = content.strip()

    # 移除 markdown 代码块标记
    if content.startswith("```"):
        end_marker = content.find("\n```", 3)
        if end_marker != -1:
            content = content[3:end_marker].strip()
        else:
            lines = content.split("\n", 1)
            if len(lines) > 1:
                content = lines[1].strip()
            if content.startswith("json"):
                content = content[4:].strip()

    if content.startswith("```json"):
        content = content[7:].strip()
    elif content.startswith("```"):
        content = content[3:].strip()

    start = content.find("{")
    if start == -1:
        start = content.find("[")
    if start == -1:
        return content

    if content[start] == "{":
        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(content)):
            char = content[i]

            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return content[start:i + 1]

    return content[start:]


# ==================== 数据模型 ====================

class Extraction(BaseModel):
    text: str


class FilterExtraction(BaseModel):
    """过滤条件抽取：column/value 为自然语言片段，由 matcher 解析"""
    text: str
    column: Optional[str] = None   # 如 "会员等级"（待 ColumnMatcher 解析）
    op: str = "="                  # = | != | > | < | >= | <= | in | like
    value: Optional[str] = None    # 如 "vip"（LLM 直接给出或从 text 截取）


class SQLIntentJson(BaseModel):
    """Layer 1 LLM 抽取结果 — SQL 查询意图片段（不做任何解析/翻译）"""
    table_extractions: List[Extraction] = Field(default_factory=list)
    metric_extractions: List[Extraction] = Field(default_factory=list)
    column_extractions: List[Extraction] = Field(default_factory=list)
    filter_extractions: List[FilterExtraction] = Field(default_factory=list)
    group_by_extractions: List[Extraction] = Field(default_factory=list)
    time_extractions: List[Extraction] = Field(default_factory=list)
    order_extractions: List[Extraction] = Field(default_factory=list)    # "销售额最高的前3"
    window_extractions: List[Extraction] = Field(default_factory=list)   # "每个地区前3"


# ==================== Prompt ====================

FEW_SHOT_EXAMPLES = r"""
示例:
近7天各地区的销售额 -> {"metric_extractions":[{"text":"销售额"}],"group_by_extractions":[{"text":"地区"}],"time_extractions":[{"text":"近7天"}]}
VIP用户的订单量 -> {"metric_extractions":[{"text":"订单量"}],"filter_extractions":[{"text":"VIP用户","column":"会员等级","op":"=","value":"vip"}]}
订单表前10条金额大于100的记录 -> {"table_extractions":[{"text":"订单表"}],"column_extractions":[{"text":"金额"}],"filter_extractions":[{"text":"金额大于100","column":"金额","op":">","value":"100"}]}
每个地区销售额前3的会员 -> {"metric_extractions":[{"text":"销售额"}],"window_extractions":[{"text":"每个地区前3"}]}
客单价最高的前5 -> {"metric_extractions":[{"text":"客单价"}],"order_extractions":[{"text":"最高的前5"}]}
"""

_BASE_SYSTEM_PROMPT = r"""你是"抽取器"。只做：从用户问题中抽取 SQL 查询意图的原文片段（extractions）。
注意：你不生成 SQL，不翻译名字——表名/指标名/列名的解析由下游 matcher 完成，你只负责切分和归类。

抽取规则：
- table_extractions：用户明确提到的表
  - 订单/订单表/下单 → 抽取原文
  - 用户/会员 → 抽取原文
  - 只有明确提到表或表相关描述才抽取，不要猜测

- metric_extractions：业务指标（通常是聚合值）
  - 销售额/营收/GMV → "销售额"
  - 订单量/单量/订单数 → "订单量"
  - 客单价 → "客单价"
  - 用户数/买家数 → "用户数"

- column_extractions：普通列（非指标、用于明细展示或隐含过滤分组）
  - "订单号"、"金额"、"状态"、"地区"、"品类" → 抽取原文

- filter_extractions：过滤条件
  - "VIP用户" → {"text":"VIP用户","column":"会员等级","op":"=","value":"vip"}
  - "金额大于100" → {"text":"金额大于100","column":"金额","op":">","value":"100"}
  - "状态是已支付" → {"text":"状态是已支付","column":"状态","op":"=","value":"已支付"}
  - op 只能是: = | != | > | < | >= | <= | in | like
  - column 抽原文（如"会员等级"），value 给规范化值（如"vip"、"100"）

- group_by_extractions：分组维度
  - "各地区"、"按渠道拆"、"每个品类" → 抽取维度原文（"地区"、"渠道"、"品类"）

- time_extractions：时间范围
  - 近7天/最近7天 → "近7天"；昨天 → "昨天"；本周 → "本周"；本月 → "本月"

- order_extractions：显式排序/TopN（针对全结果集）
  - "销售额最高的前5"、"按金额从大到小" → 抽取原文

- window_extractions：分组内排名（每个X内的前N）
  - "每个地区前3"、"每个品类销售额第一" → 抽取原文
  - 注意区分："销售额前5"（全局 TopN → order_extractions）vs "每个地区前3"（分组内 → window_extractions）

""" + FEW_SHOT_EXAMPLES

# Prompt-based 专用 prompt（含格式指令）
_BASE_SYSTEM_PROMPT_TEXT = _BASE_SYSTEM_PROMPT + r"""
输出要求：
- 不要输出任何解释文字
- 不要使用 Markdown 代码块（不要出现 ```）
- 直接输出 JSON，必须以 { 开头，以 } 结尾
"""


# ==================== Tool Schema (Function Calling) ====================

EXTRACT_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_sql_intent",
        "description": "从用户自然语言查询中抽取 SQL 查询意图片段",
        "parameters": {
            "type": "object",
            "properties": {
                "table_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "表抽取，如 订单表、用户表",
                },
                "metric_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "业务指标抽取，如 销售额、订单量、客单价",
                },
                "column_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "普通列抽取，如 订单号、地区、品类",
                },
                "filter_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "column": {"type": "string"},
                            "op": {"type": "string", "enum": ["=", "!=", ">", "<", ">=", "<=", "in", "like"]},
                            "value": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                    "description": "过滤条件抽取，如 VIP用户、金额大于100",
                },
                "group_by_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "分组维度抽取，如 地区、渠道、品类",
                },
                "time_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "时间范围抽取，如 近7天、昨天、本月",
                },
                "order_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "全局排序/TopN 抽取，如 销售额最高的前5",
                },
                "window_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "分组内排名抽取，如 每个地区前3",
                },
            },
            "required": [],
        },
    },
}


# ==================== LLM Client ====================

_llm_client: OpenAI | None = None


def get_llm_client() -> OpenAI:
    """获取 OpenAI 兼容客户端（模块级单例，复用连接池）"""
    global _llm_client
    if _llm_client is not None:
        return _llm_client

    backend = os.getenv("LLM_BACKEND", "zhipu").lower()

    if backend == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        _llm_client = OpenAI(
            api_key="ollama",
            base_url=base_url,
        )
    else:  # zhipu (默认)
        api_key = os.getenv("ZHIPU_API_KEY", os.getenv("ZHIPUAI_API_KEY", ""))
        base_url = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
        _llm_client = OpenAI(api_key=api_key, base_url=base_url)

    return _llm_client


def _get_model_name() -> str:
    """获取模型名称"""
    backend = os.getenv("LLM_BACKEND", "zhipu").lower()
    if backend == "ollama":
        return os.getenv("OLLAMA_MODEL", "glm4")
    return os.getenv("ZHIPU_MODEL", "glm-4")


def _supports_tool_calling() -> bool:
    """检查是否启用 function calling

    默认 true，所有 OpenAI 兼容后端均可使用。
    设 TOOL_CALLING_ENABLED=false 可强制走 prompt-based 路径。
    """
    return os.getenv("TOOL_CALLING_ENABLED", "true").lower() == "true"


# ==================== 构建消息 ====================

def _build_messages(query: str, session_context: Optional[Dict[str, Any]],
                    use_tool_calling: bool) -> list[dict[str, str]]:
    """构建 OpenAI 兼容的 messages 列表

    注入结构（按顺序追加到 system prompt）：
    1. 记忆文件内容（纠正/约束）
    2. 动态上下文（QueryState + 最近消息）
    3. follow-up patch extraction 指令
    """
    system_prompt = _BASE_SYSTEM_PROMPT if use_tool_calling else _BASE_SYSTEM_PROMPT_TEXT

    # 层 1：记忆文件内容（纠正/约束）
    if session_context and session_context.get("memory_corrections"):
        corrections = session_context["memory_corrections"]
        system_prompt += (
            f"\n\n=== 已知约束和纠正 ===\n"
            f"{corrections}\n"
            f"=== 约束结束 ===\n"
            f"请遵循上述约束处理用户查询。\n"
        )

    # 层 2：动态上下文（QueryState + 最近消息）
    context_str = _build_context_str(session_context)
    if context_str:
        system_prompt += "\n\n请参考上述上下文理解用户当前问题。\n" + context_str

    # 层 3：follow-up patch extraction 指令
    followup_instruction = _build_followup_instruction(session_context)
    if followup_instruction:
        system_prompt += followup_instruction

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户问题：\n{query}"},
    ]

    if not use_tool_calling:
        messages[-1]["content"] += "\n\n返回 JSON："

    return messages


# ==================== 提取逻辑 ====================

def _parse_result(data: dict) -> SQLIntentJson:
    """解析并验证提取结果"""
    return SQLIntentJson.model_validate(data)


def extract_llm(query: str, session_context: Optional[Dict[str, Any]] = None) -> SQLIntentJson:
    """LLM 抽取 SQL 查询意图（支持会话上下文）

    Args:
        query: 用户查询文本
        session_context: 会话上下文，包含历史对话和已解析状态
    """
    # 检查缓存（暂不支持带上下文的缓存）
    if _is_cache_enabled() and not session_context:
        cache_key = _query_hash(query)
        if cache_key in _query_cache:
            logger.info(f"extract_llm: cache hit for query: {query}")
            return SQLIntentJson(**_query_cache[cache_key])

    client = get_llm_client()
    model = _get_model_name()
    use_tool_calling = _supports_tool_calling()
    messages = _build_messages(query, session_context, use_tool_calling)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    logger.debug(f"LLM extract: model={model}, tool_calling={use_tool_calling}, query={query[:80]}")

    if use_tool_calling:
        kwargs["tools"] = [EXTRACT_INTENT_TOOL]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "extract_sql_intent"}}

    resp = client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    message = choice.message

    # Function Calling 路径：从 tool_calls 提取结构化参数
    if use_tool_calling and message.tool_calls:
        args = json.loads(message.tool_calls[0].function.arguments)
        logger.debug(f"LLM extract (tool_call): {json.dumps(args, ensure_ascii=False)[:200]}")
        result = _parse_result(args)
    else:
        # Prompt-based 路径（fallback 或 Ollama）
        content = message.content if hasattr(message, "content") else str(message)
        content = _extract_json(content)
        logger.debug(f"LLM extract (prompt): raw={content[:200]}")
        try:
            data = json.loads(content)
            result = _parse_result(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"LLM extract parse failed: {e}, raw={content[:300]}")
            raise ValueError(f"LLM SQLIntentJson 输出不合法: {e}\nRaw:\n{content}")

    # 保存到缓存（仅无上下文时）
    if _is_cache_enabled() and not session_context:
        _query_cache[cache_key] = result.model_dump()

    return result


async def extract_llm_async(query: str, session_context: Optional[Dict[str, Any]] = None) -> SQLIntentJson:
    """extract_llm 的异步包装，通过 asyncio.to_thread 避免阻塞事件循环"""
    return await asyncio.to_thread(extract_llm, query, session_context)
