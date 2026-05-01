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
    1. QueryState — 上下文继承（最高价值）
    2. 最近用户消息 — 辅助理解

    不再注入 favorite_regions/metrics/events（LLM + Catalog 已够用）
    记忆文件纠正内容在 _build_messages 中单独注入。
    """
    if not session_context:
        return ""

    parts = []

    # QueryState — 最高价值
    if session_context.get("last_query_state"):
        qs = session_context["last_query_state"]
        state_parts = []
        if qs.get("region_filter"):
            state_parts.append(f"地区={','.join(qs['region_filter'])}")
        if qs.get("metric"):
            state_parts.append(f"指标={qs['metric']}")
        if qs.get("event"):
            state_parts.append(f"事件={qs['event']}")
        if qs.get("time_range"):
            tr = qs["time_range"]
            state_parts.append(f"时间=近{tr.get('n', '?')}天")
        if qs.get("group_by"):
            state_parts.append(f"分组={','.join(qs['group_by'])}")
        if qs.get("filters"):
            filter_strs = [f"{f['field']}{f['op']}{f['value']}" for f in qs["filters"]]
            state_parts.append(f"过滤={','.join(filter_strs)}")

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
        "不要为了补全而重复输出用户本轮没有明确说出的 event、metric、group_by。\n"
        "如果本轮只说了时间、地区、指标或分组变化，就只抽取这些变化。\n"
        "只有当用户本轮明确提到 event 时，才填充 event_extractions。\n"
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


class ExtractionsJson(BaseModel):
    metric_extractions: List[Extraction] = Field(default_factory=list)
    time_extractions: List[Extraction] = Field(default_factory=list)
    event_extractions: List[Extraction] = Field(default_factory=list)

    # 区域过滤：LLM 直接返回 ["EUTTP"], ["USTTP"], 或 ["ROW"]
    # 注意：region_filter 不能为空，默认为 ["ROW"]
    region_filter: List[str] = Field(default_factory=lambda: ["ROW"])

    group_by_extractions: List[Extraction] = Field(default_factory=list)


# ==================== Prompt ====================

FEW_SHOT_EXAMPLES = r"""
示例:
德国 PV -> {"metric_extractions":[{"text":"PV"}],"event_extractions":[],"region_filter":["EUTTP"]}
加州 UV -> {"metric_extractions":[{"text":"UV"}],"event_extractions":[],"region_filter":["USTTP"]}
欧洲 app_launch的PV -> {"metric_extractions":[{"text":"PV"}],"event_extractions":[{"text":"app_launch"}],"region_filter":["EUTTP"]}
欧洲 近7天 app_launch -> {"metric_extractions":[{"text":"PV"}],"time_extractions":[{"text":"近7天"}],"event_extractions":[{"text":"app_launch"}],"region_filter":["EUTTP"]}
"""

_BASE_SYSTEM_PROMPT = r"""你是"提取器"。只做：从用户问题中抽取结构化片段（extractions）。

抽取规则：
- metric_extractions：从用户问题中提取指标
  - 明确提到 PV/浏览量/访问量/点击量 → "PV"
  - 明确提到 UV/独立访客/访客数/用户数 → "UV"
  - 没有明确提到指标 → 默认提取为 "PV"（这是最常用的指标）
  - metric_extractions 不能为空，必须有值

- event_extractions：从用户问题中提取事件名称（重要！）
  - app_launch/启动/应用启动/打开app → "app_launch"
  - click/点击/点击事件 → "click"
  - view/浏览/浏览事件 → "view"
  - 其他事件名 → 直接提取原文
  - 注意：如果提到 app launch、click、view 等，这些是事件，不是时间！
  - 只有用户明确提到事件才提取，不要猜测或默认填充

- time_extractions：从用户问题中提取时间范围
  - 近7天/最近7天/7天内 → "近7天"
  - 昨天/昨天一天 → "昨天"
  - 今天/当天 → "今天"
  - 本周/这周 → "本周"
  - 本月/这个月 → "本月"
  - 注意：只有明确的时间描述才是 time，app_launch 不是时间！

- region_filter：直接返回区域代码列表，注意不是国家名而是区域代码！
  - 欧洲/欧盟国家（德国、法国、意大利等）→ ["EUTTP"]
  - 美国/美国州（加州、纽约等）→ ["USTTP"]
  - 其他/未知/新加坡等 → ["ROW"]
  - 无区域信息 → ["ROW"]

- group_by_extractions：从用户问题中提取分组维度

""" + FEW_SHOT_EXAMPLES

# Prompt-based 专用 prompt（含格式指令）
_BASE_SYSTEM_PROMPT_TEXT = _BASE_SYSTEM_PROMPT + r"""
输出要求：
- 不要输出任何解释文字
- 不要使用 Markdown 代码块（不要出现 ```）
- 直接输出 JSON，必须以 { 开头，以 } 结尾
"""


# ==================== Tool Schema (Function Calling) ====================

EXTRACT_ENTITIES_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_entities",
        "description": "从用户自然语言查询中提取结构化实体",
        "parameters": {
            "type": "object",
            "properties": {
                "metric_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "指标提取，如 PV、UV",
                },
                "time_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "时间范围提取，如 近7天、昨天",
                },
                "event_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "事件名提取，如 app_launch、click",
                },
                "region_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "区域代码列表，如 EUTTP、USTTP、ROW",
                },
                "group_by_extractions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "分组维度提取",
                },
            },
            "required": ["metric_extractions", "region_filter"],
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
    1. 记忆文件内容（纠正/约束，借鉴 cc_python section 9 的内容注入方式）
    2. 动态上下文（QueryState + 最近消息）
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

def _parse_result(data: dict) -> ExtractionsJson:
    """解析并验证提取结果"""
    return ExtractionsJson.model_validate(data)


def extract_llm(query: str, session_context: Optional[Dict[str, Any]] = None) -> ExtractionsJson:
    """LLM 提取实体（支持会话上下文）

    Args:
        query: 用户查询文本
        session_context: 会话上下文，包含历史对话和已解析实体
    """
    # 检查缓存（暂不支持带上下文的缓存）
    if _is_cache_enabled() and not session_context:
        cache_key = _query_hash(query)
        if cache_key in _query_cache:
            logger.info(f"extract_llm: cache hit for query: {query}")
            return ExtractionsJson(**_query_cache[cache_key])

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
        kwargs["tools"] = [EXTRACT_ENTITIES_TOOL]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "extract_entities"}}

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
            raise ValueError(f"LLM extractions 输出不合法: {e}\nRaw:\n{content}")

    # 保存到缓存（仅无上下文时）
    if _is_cache_enabled() and not session_context:
        _query_cache[cache_key] = result.model_dump()

    return result


async def extract_llm_async(query: str, session_context: Optional[Dict[str, Any]] = None) -> ExtractionsJson:
    """extract_llm 的异步包装，通过 asyncio.to_thread 避免阻塞事件循环"""
    return await asyncio.to_thread(extract_llm, query, session_context)
