from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from common.bearer_client import BearerClient

logger = logging.getLogger(__name__)

# 全局客户端实例
_bearer_client: Optional[BearerClient] = None


def get_bearer_client() -> BearerClient:
    """获取 Bearer 客户端单例"""
    global _bearer_client
    if _bearer_client is None:
        base_url = os.getenv("BEARER_URL", "http://localhost:8080")
        token = os.getenv("BEARER_TOKEN", "XXX")
        _bearer_client = BearerClient(base_url=base_url, token=token)
        logger.info(f"BearerClient initialized: base_url={base_url}")
    return _bearer_client


async def execute_query(exec_dsl: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行查询请求

    :param exec_dsl: 执行 DSL
    :return: 查询结果
    """
    client = get_bearer_client()
    try:
        result = await client.query(exec_dsl)
        logger.info(f"Query executed successfully")
        return result
    except Exception as e:
        logger.error(f"Query execution failed: {e}")
        raise


async def check_bearer_health() -> bool:
    """检查 Bearer 服务健康状态"""
    client = get_bearer_client()
    return await client.health_check()
