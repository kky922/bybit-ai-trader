from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import LOGS_DIR

logger = logging.getLogger("coin-bot.tradfi_universe")

CACHE_FILE = LOGS_DIR / "tradfi_universe_cache.json"

# 심볼 타입 분류 규칙: 접두사/접미사 패턴으로 원자재 vs 주식 구분
_COMMODITY_PATTERNS = ("XAU", "XAG", "WTI", "BRENT", "XPT", "XPD", "NGAS", "COPPER")
_SKIP_SYMBOLS: set[str] = set()


def _classify(symbol: str) -> str:
    upper = symbol.upper()
    for pat in _COMMODITY_PATTERNS:
        if pat in upper:
            return "commodity"
    return "stock"


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(payload["ts"])
        if datetime.now(timezone.utc) - ts <= timedelta(hours=1):
            symbols = payload.get("symbols")
            if symbols:
                return symbols
    except Exception:
        pass
    return None


def _save_cache(symbols: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "symbols": symbols}, indent=2),
        encoding="utf-8",
    )


def get_tradfi_symbols(force_refresh: bool = False) -> dict[str, dict]:
    """Return {symbol: {"type": "commodity"|"stock"}} dict.

    Bybit TradFi instruments are listed under category=linear with
    symbolType="stock" or symbolType="commodity".
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached

    url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
    req = urllib.request.Request(url, headers={"User-Agent": "coin-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit instruments-info error: {data.get('retMsg')}")

    symbols: dict[str, dict] = {}
    for item in data["result"]["list"]:
        sym_type = item.get("symbolType", "")
        if sym_type not in ("stock", "commodity"):
            continue
        sym = item.get("symbol", "")
        if not sym or sym in _SKIP_SYMBOLS:
            continue
        symbols[sym] = {
            "type": sym_type,
            "base": item.get("baseCoin", ""),
            "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty") or 0),
            "lot_step": float(item.get("lotSizeFilter", {}).get("qtyStep") or 0.01),
            "min_notional": float(item.get("lotSizeFilter", {}).get("minNotionalValue") or 1),
            "tick_size": float(item.get("priceFilter", {}).get("tickSize") or 0.01),
        }

    logger.info("TradFi universe: %d symbols (%d commodity, %d stock)",
                len(symbols),
                sum(1 for v in symbols.values() if v["type"] == "commodity"),
                sum(1 for v in symbols.values() if v["type"] == "stock"))

    _save_cache(symbols)
    return symbols


def get_commodity_symbols(force_refresh: bool = False) -> list[str]:
    symbols = get_tradfi_symbols(force_refresh)
    return [s for s, v in symbols.items() if v["type"] == "commodity"]


def get_stock_symbols(force_refresh: bool = False) -> list[str]:
    symbols = get_tradfi_symbols(force_refresh)
    return [s for s, v in symbols.items() if v["type"] == "stock"]
