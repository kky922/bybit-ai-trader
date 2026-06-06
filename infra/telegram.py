from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

import config

logger = logging.getLogger("telegram")

_SEND_TIMEOUT = 8  # DNS + connect + response 전체 하드 타임아웃


class CoinTelegram:
    def __init__(self) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_error_sent = 0.0

    async def _send(self, text: str) -> bool:
        if not self.enabled:
            return False
        # Use explicit DNS resolver (8.8.8.8) instead of system DNS (Tailscale)
        # to avoid Tailscale DNS flakiness: "Could not contact DNS servers"
        resolver = aiohttp.AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
        connector = aiohttp.TCPConnector(ssl=True, resolver=resolver)
        timeout = aiohttp.ClientTimeout(total=_SEND_TIMEOUT, connect=4, sock_connect=4)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("telegram send failed: status=%d body=%s", resp.status, body[:200])
                    return resp.status == 200
        except Exception as exc:
            logger.warning("telegram send error: %s", exc)
            return False

    def send_sync(self, text: str) -> None:
        # asyncio.wait_for로 이벤트 루프 레벨에서도 타임아웃 보장 (DNS hang 방지)
        async def _run() -> None:
            try:
                await asyncio.wait_for(self._send(text), timeout=_SEND_TIMEOUT + 2)
            except asyncio.TimeoutError:
                logger.warning("telegram send_sync timeout")
            except Exception as exc:
                logger.warning("telegram send_sync error: %s", exc)

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.warning("telegram asyncio.run error: %s", exc)

    def notify_error(self, text: str) -> None:
        now = time.time()
        if now - self.last_error_sent < 300:
            return
        self.last_error_sent = now
        self.send_sync(f"❌ <b>Coin Bot Error</b>\n{text[:500]}")
