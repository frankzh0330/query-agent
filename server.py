from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app import app, set_matcher_service
from gateway.telegram_gateway import TelegramGateway, gateway_manager
from matcher.matcher_service import MatcherService
from service.catalog_scheduler import CatalogScheduler
from service.catalog_sync import CatalogSync

logger = logging.getLogger(__name__)


# ====================
# WebSocket 启动配置
# ====================

@asynccontextmanager
async def websocket_lifespan(app: FastAPI):
    """
    WebSocket 服务生命周期管理

    启动时:
    1. 初始化 MatcherService（构建索引）
    2. 注册到 app
    3. 启动 Telegram Gateway
    4. 启动 Catalog 定时同步调度器
    """
    logger.info("=== Starting WebSocket Server ===")

    # 1. 初始化 MatcherService（构建索引）
    matcher_service = MatcherService(catalog_path="catalog")
    set_matcher_service(matcher_service)
    logger.info("MatcherService initialized and registered")

    # 2. 启动 Telegram Gateway（后台任务）
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        telegram_gw = TelegramGateway(bot_token=telegram_token)
        gateway_manager.register("telegram", telegram_gw)

        # 在后台运行 Gateway
        async def run_gateway():
            await telegram_gw.start()

        asyncio.create_task(run_gateway())
        logger.info("TelegramGateway started in background")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set, skipping TelegramGateway")

    # 3. 启动 Catalog 定时同步调度器
    catalog_api_base = os.getenv("CATALOG_API_BASE")
    if catalog_api_base:
        catalog_sync = CatalogSync(api_base=catalog_api_base, catalog_dir="catalog")
        catalog_scheduler = CatalogScheduler(catalog_sync, matcher_service)
        catalog_scheduler.start()
        logger.info(f"CatalogScheduler started with API: {catalog_api_base}")
    else:
        catalog_scheduler = None
        logger.info("CATALOG_API_BASE not set, skipping CatalogScheduler")

    yield

    # 清理
    logger.info("=== Stopping WebSocket Server ===")
    await gateway_manager.stop_all()
    if catalog_scheduler:
        catalog_scheduler.shutdown()


# 创建带生命周期的 FastAPI 应用
app_with_ws = FastAPI(
    title="query-agent: WebSocket + HTTP",
    lifespan=websocket_lifespan,
)

# 挂载原有的 HTTP 路由
app_with_ws.mount("/api", app)


# ====================
# 启动入口
# ====================

def main():
    """启动 WebSocket 服务器"""
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    logger.info(f"Starting server on {host}:{port}")

    uvicorn.run(
        app_with_ws,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
