from __future__ import annotations

from typing import Any, Dict, Optional

from common.http_client import HttpClient


class BearerClient(HttpClient):
    """Bearer API 客户端"""

    def __init__(self, base_url: str = "http://localhost:8080", token: str = "XXX"):
        super().__init__(base_url, token)

    async def query(self, exec_dsl: Dict[str, Any]) -> Dict[str, Any]:
        """发送查询请求"""
        return await self.post("/query", exec_dsl)

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            result = await self.get("/health")
            return result.get("status") == "ok"
        except Exception:
            return False
