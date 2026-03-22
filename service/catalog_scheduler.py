"""
目录定时同步调度器 - 使用 APScheduler 实现定时同步和热更新
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from matcher.catalog_loader import load_catalog
from matcher.dimension_matcher import build_dimension_matcher_from_catalog
from matcher.event_matcher import build_event_matcher_from_catalog
from matcher.metric_matcher import build_metric_matcher_from_catalog
from service.catalog_sync import CatalogSync

logger = logging.getLogger(__name__)


class CatalogScheduler:
    """目录定时同步调度器"""

    def __init__(self, sync: CatalogSync, matcher_service):
        self.sync = sync
        self.matcher_service = matcher_service
        self.scheduler = AsyncIOScheduler()

    async def incremental_sync(self):
        """增量同步并热更新索引"""
        logger.info("Starting incremental catalog sync...")
        try:
            event_count = await self.sync.incremental_sync_events()
            dim_count = await self.sync.incremental_sync_dimensions()
            logger.info(f"Incremental sync completed: {event_count} events, {dim_count} dimensions")

            # 热更新 matcher
            await self._reload_matchers()
            logger.info("Matcher indexes hot-reloaded successfully")
        except Exception as e:
            logger.error(f"Incremental sync failed: {e}", exc_info=True)

    async def full_sync(self):
        """全量同步并热更新索引"""
        logger.info("Starting full catalog sync...")
        try:
            event_count = await self.sync.full_sync_events()
            dim_count = await self.sync.full_sync_dimensions()
            logger.info(f"Full sync completed: {event_count} events, {dim_count} dimensions")

            # 热更新 matcher
            await self._reload_matchers()
            logger.info("Matcher indexes hot-reloaded successfully")
        except Exception as e:
            logger.error(f"Full sync failed: {e}", exc_info=True)

    async def _reload_matchers(self):
        """热更新：重新加载 catalog 并重建 matcher 索引"""
        new_catalog = load_catalog(self.sync.catalog_dir)

        # 重建 matcher
        self.matcher_service.catalog = new_catalog
        self.matcher_service.event_matcher = build_event_matcher_from_catalog(new_catalog.events)
        self.matcher_service.metric_matcher = build_metric_matcher_from_catalog(new_catalog.metrics)
        self.matcher_service.dimension_matcher = build_dimension_matcher_from_catalog(new_catalog.dimensions)

    def start(self):
        """启动定时任务"""
        # 每 1 小时增量同步
        self.scheduler.add_job(self.incremental_sync, "interval", hours=1, id="incremental_sync")
        logger.info("Scheduled incremental sync: every 1 hour")

        # 每 6 小时全量同步
        self.scheduler.add_job(self.full_sync, "interval", hours=6, id="full_sync")
        logger.info("Scheduled full sync: every 6 hours")

        self.scheduler.start()
        logger.info("CatalogScheduler started")

    def shutdown(self):
        """关闭调度器"""
        self.scheduler.shutdown(wait=False)
        logger.info("CatalogScheduler shutdown")
