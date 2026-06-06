from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from trading.exchange import BybitExchange

CACHE_FILE = config.LOGS_DIR / "universe_cache.json"
logger = logging.getLogger("universe")


def _load_cache() -> list[str] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(payload["ts"])
        if datetime.now(timezone.utc) - ts <= timedelta(hours=1):
            symbols = payload.get("symbols", [])
            # 빈 심볼 리스트면 캐시 무효화 (API 로드 실패 등)
            if not symbols:
                logger.warning("universe cache is empty, invalidating")
                return None
            return symbols
    except Exception:
        return None
    return None


def _load_via_urllib(excluded: set[str]) -> list[str]:
    """urllib stdlib로 Bybit tickers API 호출 (ccxt SSL 호환성 문제 우회)"""
    symbols: list[str] = []
    # instruments-info에는 거래량 데이터가 없으므로 tickers 사용
    url = "https://api.bybit.com/v5/market/tickers?category=spot"
    req = urllib.request.Request(url, headers={"User-Agent": "coin-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
    for item in data["result"]["list"]:
        token = item.get("symbol", "")
        if not token.endswith("USDT") or token in excluded:
            continue
        vol = float(item.get("turnover24h") or 0.0)
        if vol < config.MIN_24H_VOLUME_USD:
            continue
        symbols.append(token)
    return symbols


def _load_via_ccxt(exchange: BybitExchange, excluded: set[str]) -> list[str]:
    """ccxt 기반 심볼 로드"""
    symbols: list[str] = []
    exchange.exchange.load_markets()
    for sym, market in exchange.exchange.markets.items():
        if not market.get("spot") or market.get("quote") != "USDT":
            continue
        token = f"{market['base']}USDT"
        if token in excluded:
            continue
        quote_volume = float((market.get("info") or {}).get("turnover24h") or 0.0)
        if quote_volume < config.MIN_24H_VOLUME_USD:
            continue
        symbols.append(token)
    return symbols


def get_tradable_symbols(exchange: BybitExchange, *, retries: int = 3) -> list[str]:
    cached = _load_cache()
    if cached is not None:
        return cached

    excluded = {"USDCUSDT", "USDTUSDT", "DAIUSDT", "WBTCUSDT", "WETHUSDT", "XAUTUSDT", "USDEUSDT"}
    symbols: list[str] = []

    for attempt in range(1, retries + 1):
        symbols = []
        # 1) urllib 직접 호출 (SSL 호환성 문제 우회)
        try:
            symbols = _load_via_urllib(excluded)
            if symbols:
                logger.info("universe loaded via urllib: %d symbols (attempt %d)", len(symbols), attempt)
                break
            logger.warning("urllib returned 0 symbols (attempt %d/%d)", attempt, retries)
        except Exception as exc:
            logger.warning("urllib failed (attempt %d/%d): %s", attempt, retries, exc)

        # 2) ccxt 폴백
        if not symbols:
            try:
                symbols = _load_via_ccxt(exchange, excluded)
                if symbols:
                    logger.info("universe loaded via ccxt: %d symbols (attempt %d)", len(symbols), attempt)
                    break
                logger.warning("ccxt returned 0 symbols (attempt %d/%d)", attempt, retries)
            except Exception as exc:
                logger.warning("ccxt failed (attempt %d/%d): %s", attempt, retries, exc)

        if not symbols:
            logger.warning("got 0 symbols from all methods (attempt %d/%d), retrying...", attempt, retries)
            if attempt < retries:
                import time
                time.sleep(5 * attempt)

    result = sorted(set(symbols))
    logger.info("tradable symbols loaded: %d symbols", len(result))

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "symbols": result},
            indent=2,
        ),
        encoding="utf-8",
    )
    return result