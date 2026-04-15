from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from common.bearer_client import BearerClient

logger = logging.getLogger(__name__)

# 全局客户端实例
_bearer_client: Optional[BearerClient] = None


def _is_mock_mode() -> bool:
    """检查是否启用 Mock 模式"""
    return os.getenv("BEARER_MOCK", "false").lower() == "true"


def get_bearer_client() -> BearerClient:
    """获取 Bearer 客户端单例"""
    global _bearer_client
    if _bearer_client is None:
        base_url = os.getenv("BEARER_URL", "http://localhost:8080")
        token = os.getenv("BEARER_TOKEN", "XXX")
        _bearer_client = BearerClient(base_url=base_url, token=token)
        logger.info(f"BearerClient initialized: base_url={base_url}")
    return _bearer_client


def _mock_query_result(exec_dsl: Dict[str, Any]) -> Dict[str, Any]:
    """生成 Mock 查询结果"""
    # 从 exec_dsl 中提取一些信息用于生成假数据
    region_filter = exec_dsl.get("content", {}).get("option", {}).get("finder", {}).get("region_filter", ["ROW"])
    metric_id = exec_dsl.get("content", {}).get("queries", [{}])[0].get("event_indicator", "pv")
    event_name = exec_dsl.get("content", {}).get("queries", [{}])[0].get("event_name", "app_launch")

    # 生成假数据
    mock_data = {
        "status": "success",
        "data": {
            "result": [
                {
                    "date": "2024-03-24",
                    f"{metric_id}": 12345,
                    "region": region_filter[0] if region_filter else "ROW",
                },
                {
                    "date": "2024-03-23",
                    f"{metric_id}": 11234,
                    "region": region_filter[0] if region_filter else "ROW",
                },
                {
                    "date": "2024-03-22",
                    f"{metric_id}": 10234,
                    "region": region_filter[0] if region_filter else "ROW",
                },
            ]
        },
        "meta": {
            "event": event_name,
            "metric": metric_id,
            "regions": region_filter,
            "mock": True,
            "message": "This is mock data"
        }
    }

    logger.info(f"Mock query result: region={region_filter}, metric={metric_id}, event={event_name}")
    return mock_data


async def execute_query(exec_dsl: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行查询请求

    :param exec_dsl: 执行 DSL
    :return: 查询结果
    """
    # Mock 模式
    if _is_mock_mode():
        # logger.info("Using MOCK mode for Bearer query")
        # 模拟网络延迟
        await asyncio.sleep(0.5)
        result = _mock_query_result(exec_dsl)
        logger.info(f"Mock query result: {json.dumps(result, ensure_ascii=False)}")
        return result

    # 正常模式
    client = get_bearer_client()
    try:
        result = await client.query(exec_dsl)
        logger.info("Query executed successfully")
        return result
    except Exception as e:
        logger.error(f"Query execution failed: {e}")
        raise


async def check_bearer_health() -> bool:
    """检查 Bearer 服务健康状态"""
    if _is_mock_mode():
        logger.info("Mock mode: Bearer health check always returns True")
        return True

    client = get_bearer_client()
    return await client.health_check()
