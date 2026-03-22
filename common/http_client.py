from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class HttpClient:
    """HTTP 客户端基类"""

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def post(self, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
        """发送 POST 请求"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}{path}", json=json, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送 GET 请求"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}{path}", params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
