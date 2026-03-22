"""
目录同步服务 - 从 HTTP API 获取 events/dimensions 数据并写入 YAML
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List

import httpx
import yaml


class CatalogSync:
    """目录同步服务"""

    def __init__(self, api_base: str, catalog_dir: str = "catalog"):
        self.api_base = api_base
        self.catalog_dir = catalog_dir
        self.state_file = os.path.join(catalog_dir, ".sync_state.json")

    async def fetch_events(self, since: str | None = None) -> List[Dict]:
        """获取 events 数据

        Args:
            since: 时间戳，只返回 update_time >= since 的数据
        """
        params = {}
        if since:
            params["since"] = since

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.api_base}/events", params=params)
            resp.raise_for_status()
            return resp.json()

    async def fetch_dimensions(self, since: str | None = None) -> List[Dict]:
        """获取 dimensions 数据

        Args:
            since: 时间戳，只返回 update_time >= since 的数据
        """
        params = {}
        if since:
            params["since"] = since

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.api_base}/dimensions", params=params)
            resp.raise_for_status()
            return resp.json()

    def _parse_aliases(self, alias_str: str) -> List[str]:
        """解析逗号分隔的别名"""
        if not alias_str:
            return []
        return [a.strip() for a in alias_str.split(",") if a.strip()]

    def _load_state(self) -> Dict:
        """加载同步状态"""
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                return json.load(f)
        return {"last_full_sync_events": None, "last_incremental_sync_events": None,
                "last_full_sync_dimensions": None, "last_incremental_sync_dimensions": None}

    def _save_state(self, state: Dict):
        """保存同步状态"""
        os.makedirs(self.catalog_dir, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def _load_existing_events(self) -> Dict:
        """加载现有 events.yaml"""
        path = os.path.join(self.catalog_dir, "events.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return data.get("events", {})
        return {}

    def _load_existing_dimensions(self) -> Dict:
        """加载现有 dimensions.yaml"""
        path = os.path.join(self.catalog_dir, "dimensions.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return data.get("dimensions", {})
        return {}

    def _write_events(self, events: Dict):
        """写入 events.yaml"""
        os.makedirs(self.catalog_dir, exist_ok=True)
        path = os.path.join(self.catalog_dir, "events.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"events": events}, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def _write_dimensions(self, dimensions: Dict):
        """写入 dimensions.yaml"""
        os.makedirs(self.catalog_dir, exist_ok=True)
        path = os.path.join(self.catalog_dir, "dimensions.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"dimensions": dimensions}, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    async def incremental_sync_events(self) -> int:
        """增量同步 events"""
        state = self._load_state()
        since = state.get("last_incremental_sync_events")

        items = await self.fetch_events(since=since)
        existing = self._load_existing_events()

        # 更新/新增
        for item in items:
            event_name = item.get("event_name") or item.get("name")
            if not event_name:
                continue

            if item.get("status") == 0:
                # 删除 status=0 的
                if event_name in existing:
                    del existing[event_name]
                continue

            existing[event_name] = {
                "aliases": self._parse_aliases(item.get("alias", "")),
                "show_name": item.get("show_name", event_name),
                "event_type": item.get("event_type", "origin"),
                "event_name": event_name,
            }

        # 写入 YAML
        self._write_events(existing)

        # 更新状态
        state["last_incremental_sync_events"] = datetime.now().isoformat()
        self._save_state(state)

        return len(items)

    async def full_sync_events(self) -> int:
        """全量同步 events"""
        items = await self.fetch_events()

        events = {}
        for item in items:
            event_name = item.get("event_name") or item.get("name")
            if not event_name:
                continue

            if item.get("status") == 0:
                continue  # 跳过禁用的

            events[event_name] = {
                "aliases": self._parse_aliases(item.get("alias", "")),
                "show_name": item.get("show_name", event_name),
                "event_type": item.get("event_type", "origin"),
                "event_name": event_name,
            }

        self._write_events(events)

        state = self._load_state()
        state["last_full_sync_events"] = datetime.now().isoformat()
        state["last_incremental_sync_events"] = datetime.now().isoformat()
        self._save_state(state)

        return len(events)

    async def incremental_sync_dimensions(self) -> int:
        """增量同步 dimensions"""
        state = self._load_state()
        since = state.get("last_incremental_sync_dimensions")

        items = await self.fetch_dimensions(since=since)
        existing = self._load_existing_dimensions()

        for item in items:
            dim_name = item.get("property_name") or item.get("name")
            if not dim_name:
                continue

            if item.get("status") == 0:
                if dim_name in existing:
                    del existing[dim_name]
                continue

            existing[dim_name] = {
                "aliases": self._parse_aliases(item.get("alias", "")),
                "property_type": item.get("property_type", "profile"),
                "property_name": dim_name,
                "property_compose_type": item.get("property_compose_type", "origin"),
            }

        self._write_dimensions(existing)

        state["last_incremental_sync_dimensions"] = datetime.now().isoformat()
        self._save_state(state)

        return len(items)

    async def full_sync_dimensions(self) -> int:
        """全量同步 dimensions"""
        items = await self.fetch_dimensions()

        dimensions = {}
        for item in items:
            dim_name = item.get("property_name") or item.get("name")
            if not dim_name:
                continue

            if item.get("status") == 0:
                continue

            dimensions[dim_name] = {
                "aliases": self._parse_aliases(item.get("alias", "")),
                "property_type": item.get("property_type", "profile"),
                "property_name": dim_name,
                "property_compose_type": item.get("property_compose_type", "origin"),
            }

        self._write_dimensions(dimensions)

        state = self._load_state()
        state["last_full_sync_dimensions"] = datetime.now().isoformat()
        state["last_incremental_sync_dimensions"] = datetime.now().isoformat()
        self._save_state(state)

        return len(dimensions)
