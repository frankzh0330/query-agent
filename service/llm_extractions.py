from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain_community.chat_models import ChatZhipuAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

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
    """构建会话上下文字符串（注入到提示词）"""
    if not session_context:
        return ""

    context_str = ""

    # 对话历史
    if session_context.get("recent_queries"):
        context_str += "\n=== 对话历史 ===\n"
        for i, q in enumerate(session_context["recent_queries"], 1):
            context_str += f"Q{i}: {q}\n"
        context_str += "=== 历史结束 ===\n"

    # 已解析实体
    if session_context.get("resolved_entities"):
        e = session_context["resolved_entities"]
        entity_parts = []
        if e.get("region"):
            entity_parts.append(f"地区={e['region']}")
        if e.get("metric"):
            entity_parts.append(f"指标={e['metric']}")
        if e.get("event"):
            entity_parts.append(f"事件={e['event']}")

        if entity_parts:
            context_str += f"\n已确认: {', '.join(entity_parts)}\n"

    return context_str


def _extract_json(content: str) -> str:
    """
    从内容中提取第一个完整的 JSON 对象

    处理 Ollama 可能返回的额外数据：
    1. 移除 markdown 代码块标记
    2. 提取第一个完整的 JSON 对象
    3. 忽略 JSON 后的额外内容
    """
    content = content.strip()

    # 移除 markdown 代码块标记
    if content.startswith("```"):
        # 找到第二个 ``` 的位置
        end_marker = content.find("\n```", 3)
        if end_marker != -1:
            content = content[3:end_marker].strip()
        else:
            # 没有 closing ```，取第一行之后的内容
            lines = content.split("\n", 1)
            if len(lines) > 1:
                content = lines[1].strip()
            # 移除可能的 ```json 前缀
            if content.startswith("json"):
                content = content[4:].strip()

    # 移除可能的 ```json 或 ``` 前缀
    if content.startswith("```json"):
        content = content[7:].strip()
    elif content.startswith("```"):
        content = content[3:].strip()

    # 找到第一个 { 的位置
    start = content.find("{")
    if start == -1:
        # 尝试找数组开头
        start = content.find("[")
    if start == -1:
        return content  # 没有找到 JSON 开头，返回原内容

    # 从 { 开始提取，找到匹配的 }
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
                    return content[start:i+1]

    # 如果没有找到完整的对象，尝试直接解析
    return content[start:]

# Few-shot 示例（精简版，减少 token 消耗）
FEW_SHOT_EXAMPLES = r"""
示例:
德国 PV -> {{"metric_extractions":[{{"text":"PV"}}],"region_filter":["EUTTP"]}}
加州 UV -> {{"metric_extractions":[{{"text":"UV"}}],"region_filter":["USTTP"]}}
欧洲 近7天 app_launch -> {{"metric_extractions":[{{"text":"PV"}}],"time_extractions":[{{"text":"近7天"}}],"event_extractions":[{{"text":"app_launch"}}],"region_filter":["EUTTP"]}}
"""


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


_BASE_SYSTEM_PROMPT = r"""
你是"提取器"。只做：从用户问题中抽取结构化片段（extractions）。

只输出 JSON，key 必须且仅能包含：
metric_extractions, time_extractions, event_extractions, region_filter, group_by_extractions

每个元素格式：
{{"text":"..."}}

抽取规则：
- metric_extractions：从用户问题中提取指标
  - 明确提到 PV/浏览量/访问量/点击量 → "PV"
  - 明确提到 UV/独立访客/访客数/用户数 → "UV"
  - 没有明确提到指标 → 默认提取为 "PV"（这是最常用的指标）
  - metric_extractions 不能为空，必须有值

- region_filter：直接返回区域代码列表，注意不是国家名而是区域代码！
  - 欧洲/欧盟国家（德国、法国、意大利等）→ ["EUTTP"]
  - 美国/美国州（加州、纽约等）→ ["USTTP"]
  - 其他/未知/新加坡等 → ["ROW"]
  - 无区域信息 → ["ROW"]

输出要求：
- 不要输出任何解释文字
- 不要使用 Markdown 代码块（不要出现 ```）
- 直接输出 JSON，必须以 {{ 开头，以 }} 结尾

""" + FEW_SHOT_EXAMPLES


def _build_prompt_template(session_context: Optional[Dict[str, Any]] = None) -> ChatPromptTemplate:
    """构建带会话上下文的提示词模板"""
    context_str = _build_context_str(session_context)

    if context_str:
        # 添加上下文提示
        context_instruction = "\n请参考上述对话历史和已确认信息，理解用户当前问题的完整意图。\n"
        system_prompt = _BASE_SYSTEM_PROMPT + context_str + context_instruction
    else:
        system_prompt = _BASE_SYSTEM_PROMPT

    return ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "用户问题：\n{query}\n\n返回 JSON："),
    ])


# 保持向后兼容
EXTRACTIONS_PROMPT = _build_prompt_template(None)


def get_llm():
    """
    获取 LLM 实例，支持 Ollama 和智谱 AI

    通过 LLM_BACKEND 环境变量切换:
    - ollama: 使用本地 Ollama 模型
    - zhipu: 使用智谱 AI API (默认)
    """
    backend = os.getenv("LLM_BACKEND", "zhipu").lower()

    if backend == "ollama":
        from langchain_community.llms import Ollama
        model = os.getenv("OLLAMA_MODEL", "glm4")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        # Ollama 优化参数
        return Ollama(
            model=model,
            base_url=base_url,
            temperature=0.2,
            num_ctx=2048,  # 减少上下文窗口，提升速度
            num_predict=256,  # 限制最大生成长度
            repeat_penalty=1.1,  # 减少重复
        )
    else:  # zhipu (默认)
        model = os.getenv("ZHIPU_MODEL", "glm-4")
        return ChatZhipuAI(model=model, temperature=0.2)


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
            print(f"extract_llm: cache hit for query: {query}")
            return ExtractionsJson(**_query_cache[cache_key])

    # 构建带上下文的提示词
    prompt_template = _build_prompt_template(session_context)

    llm = get_llm()
    chain = prompt_template | llm
    resp = chain.invoke({"query": query})
    content = getattr(resp, "content", resp)

    # 使用增强的 JSON 提取逻辑
    content = _extract_json(content)

    try:
        data = json.loads(content)
        print("extract_extractions_llm: {}".format(data))
        result = ExtractionsJson.model_validate(data)

        # 保存到缓存
        if _is_cache_enabled():
            _query_cache[cache_key] = result.model_dump()

        return result
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"LLM extractions 输出不合法: {e}\nRaw:\n{content}")
