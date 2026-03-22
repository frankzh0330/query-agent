#!/usr/bin/env python3
"""
Query Agent MCP 服务器

提供三个调试命令：
- /token <query> - 测试分词
- /recall <query> - 测试召回
- /rerank <query> - 测试重排
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from common.text_utils import normalize, tokenize_mixed
from matcher.event_matcher import build_event_matcher_from_catalog
from matcher.metric_matcher import build_metric_matcher_from_catalog
from matcher.dimension_matcher import build_dimension_matcher_from_catalog
from matcher.catalog_loader import load_catalog


# 全局 matcher 实例
_event_matcher = None
_metric_matcher = None
_dimension_matcher = None


def _init_matchers():
    """初始化所有 matcher"""
    global _event_matcher, _metric_matcher, _dimension_matcher
    if _event_matcher is None:
        catalog = load_catalog("catalog")
        _event_matcher = build_event_matcher_from_catalog(catalog.events)
        _metric_matcher = build_metric_matcher_from_catalog(catalog.metrics)
        _dimension_matcher = build_dimension_matcher_from_catalog(catalog.dimensions)


def _get_matcher(matcher_type: str):
    """根据类型获取 matcher"""
    _init_matchers()
    if matcher_type == "event":
        return _event_matcher
    elif matcher_type == "metric":
        return _metric_matcher
    elif matcher_type == "dimension":
        return _dimension_matcher
    else:
        raise ValueError(f"Unknown matcher_type: {matcher_type}")


# 创建 MCP 服务器
mcp = FastMCP("Query Agent")


@mcp.tool()
async def token(
    query: str,
    matcher_type: str = "event",
) -> dict[str, Any]:
    """
    测试分词功能

    Args:
        query: 要分词的查询文本
        matcher_type: matcher 类型 (event/metric/dimension)

    Returns:
        {
            "original": str,           # 原始查询
            "normalized": str,        # 规范化后的查询
            "tokens": list[str],      # 分词结果
            "expanded": list[str],    # 同义词扩展后的 tokens
        }
    """
    matcher = _get_matcher(matcher_type)

    # 1. 分词
    tokens = tokenize_mixed(query)

    # 2. 同义词扩展
    expanded_tokens = matcher._expand_query_tokens(tokens)

    return {
        "original": query,
        "normalized": normalize(query),
        "tokens": tokens,
        "expanded": expanded_tokens,
    }


@mcp.tool()
async def recall(
    query: str,
    matcher_type: str = "event",
) -> dict[str, Any]:
    """
    测试召回功能

    Args:
        query: 查询文本
        matcher_type: matcher 类型 (event/metric/dimension)

    Returns:
        {
            "query": str,
            "tokens": list[str],
            "expanded": list[str],
            "candidates": list,          # 候选 ID 列表
            "explain": dict,              # 召回解释
        }
    """
    matcher = _get_matcher(matcher_type)

    # 分词 + 扩展
    tokens = tokenize_mixed(query)
    expanded_tokens = matcher._expand_query_tokens(tokens)

    # 召回
    candidates, explain = matcher._recall_candidates(expanded_tokens)

    # 转换候选结果（包含名称）
    candidate_details = []
    for item in explain.get("top_candidates", []):
        doc_id = item["id"]
        if doc_id in candidates:
            candidate_details.append({
                "id": doc_id,
                "name": item["name"],
                "hit_count": item["hit_count"],
            })

    return {
        "query": query,
        "tokens": tokens,
        "expanded": expanded_tokens,
        "candidates": candidates,
        "candidate_details": candidate_details,
        "explain": explain,
    }


@mcp.tool()
async def rerank(
    query: str,
    matcher_type: str = "event",
) -> dict[str, Any]:
    """
    测试重排功能

    Args:
        query: 查询文本
        matcher_type: matcher 类型 (event/metric/dimension)

    Returns:
        {
            "query": str,
            "tokens": list[str],
            "expanded": list[str],
            "candidates": list,          # 候选 ID 列表
            "result": dict | None,        # 重排结果
        }
    """
    matcher = _get_matcher(matcher_type)

    # 分词 + 扩展
    tokens = tokenize_mixed(query)
    expanded_tokens = matcher._expand_query_tokens(tokens)

    # 召回
    candidates, recall_explain = matcher._recall_candidates(expanded_tokens)

    if not candidates:
        return {
            "query": query,
            "tokens": tokens,
            "expanded": expanded_tokens,
            "candidates": [],
            "result": None,
            "recall_explain": recall_explain,
        }

    # 重排
    rerank_result = matcher._rerank(query, candidates)

    if rerank_result is None:
        return {
            "query": query,
            "tokens": tokens,
            "expanded": expanded_tokens,
            "candidates": candidates,
            "result": None,
            "recall_explain": recall_explain,
        }

    best_id, best_score, rerank_explain = rerank_result

    return {
        "query": query,
        "tokens": tokens,
        "expanded": expanded_tokens,
        "candidates": candidates,
        "result": {
            "id": best_id,
            "name": matcher._get_name_by_id(best_id),
            "score": best_score,
            "explain": rerank_explain,
        },
        "recall_explain": recall_explain,
    }


if __name__ == "__main__":
    # 初始化 matchers
    _init_matchers()
    print("Query Agent MCP Server initialized", file=sys.stderr)

    # 运行服务器
    mcp.run()
