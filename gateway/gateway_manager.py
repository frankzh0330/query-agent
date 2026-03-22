from __future__ import annotations

import asyncio
import logging
from typing import Dict

from gateway.base import BaseGateway
from gateway.telegram_gateway import TelegramGateway

logger = logging.getLogger(__name__)


class GatewayManager:
    """管理多个 Gateway 实例"""

    def __init__(self):
        self._gateways: Dict[str, BaseGateway] = {}

    def register(self, name: str, gateway: BaseGateway) -> None:
        """注册 Gateway"""
        self._gateways[name] = gateway
        logger.info(f"Gateway registered: {name}")

    def register_telegram(self, bot_token: str = None) -> None:
        """注册 Telegram Gateway"""
        gateway = TelegramGateway(bot_token=bot_token)
        self.register("telegram", gateway)

    async def start_all(self) -> None:
        """启动所有 Gateway"""
        if not self._gateways:
            logger.warning("No gateways registered")
            return

        logger.info(f"Starting {len(self._gateways)} gateway(s)")
        tasks = [gw.start() for gw in self._gateways.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """停止所有 Gateway"""
        logger.info("Stopping all gateways")
        tasks = [gw.stop() for gw in self._gateways.values()]
        await asyncio.gather(*tasks, return_exceptions=True)


# 全局 Gateway 管理器
gateway_manager = GatewayManager()
