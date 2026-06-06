from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import requests

import config
from analysis.technical import adx, atr, ema
from trading.exchange import BybitExchange

logger = logging.getLogger("coin-bot.market_data")

_FNG_VALUE: float | None = None
_FNG_CACHED_AT: datetime | None = None


class MarketData:
    def __init__(self, exchange: BybitExchange):
        self.exchange = exchange

    def get(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        return self.exchange.fetch_ohlcv(symbol, timeframe, limit)


def fetch_fear_greed_index() -> float | None:
    """Fetch Crypto Fear & Greed Index from alternative.me (free, no API key needed).
    Returns: 0-100 value, or None on failure.
    Refreshes at most once per hour (cached)."""
    global _FNG_VALUE, _FNG_CACHED_AT
    now = datetime.now(timezone.utc)
    if _FNG_CACHED_AT is not None and (now - _FNG_CACHED_AT).total_seconds() < 3600:
        return _FNG_VALUE

    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _FNG_VALUE = float(data["data"][0]["value"])
        _FNG_CACHED_AT = now
        logger.info("Fear & Greed Index: %.0f (%s)", _FNG_VALUE, data["data"][0].get("value_classification", "?"))
        return _FNG_VALUE
    except Exception as e:
        logger.warning("Fear & Greed fetch failed: %s", e)
        return _FNG_VALUE


def get_regime_sizing(btc_4h: pd.DataFrame, fng: float | None = None) -> dict:
    """Return regime-adjusted position sizing params.
    Strong Bull → 더 공격적, Bear → 차단 (이미 별도 처리)
    Returns: {max_positions, equity_pct, kelly_fraction}"""
    if btc_4h is None or len(btc_4h) < 50:
        return {"max_positions": config.MAX_CONCURRENT_POSITIONS,
                "equity_pct": config.MAX_EQUITY_PCT_PER_POSITION,
                "kelly_fraction": config.KELLY_FRACTION}

    close = float(btc_4h["close"].iloc[-1])
    ema50 = float(ema(btc_4h["close"], config.EMA_SLOW).iloc[-1])
    btc_atr = atr(btc_4h, 14)
    btc_adx = adx(btc_4h, config.ADX_PERIOD)

    upper_band = ema50 + config.BTC_BAND_ATR_MULTI * btc_atr
    lower_band = ema50 - config.BTC_BAND_ATR_MULTI * btc_atr

    # Strong Bull: close > upper_band AND ADX >= 25 AND (FNG > 50 or unknown)
    if close > upper_band and btc_adx >= config.ADX_TRENDING_THRESHOLD:
        return {
            # 소액 집중모드 1차: strong bull이라도 포지션 수/비중을 자동 증폭하지 않는다.
            # 후보 품질을 먼저 검증한 뒤 Phase B에서 확대 여부를 판단한다.
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
            "equity_pct": config.MAX_EQUITY_PCT_PER_POSITION,
            "kelly_fraction": config.KELLY_FRACTION,
        }
    # Bear: entries blocked in entry_signal.py, but tighten here too
    elif close < lower_band:
        return {
            "max_positions": max(config.MAX_CONCURRENT_POSITIONS - 1, 1),  # -1
            "equity_pct": config.MAX_EQUITY_PCT_PER_POSITION * 0.75,  # 15%
            "kelly_fraction": config.KELLY_FRACTION * 0.5,
        }
    # Neutral: current defaults
    else:
        return {
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
            "equity_pct": config.MAX_EQUITY_PCT_PER_POSITION,
            "kelly_fraction": config.KELLY_FRACTION,
        }


# --- Regime 캐싱 (15분마다 갱신) ---
_REGIME_CACHE: dict = {"sizing": None, "updated_at": None}


def get_cached_regime_sizing(btc_4h: pd.DataFrame, fng: float | None = None) -> dict:
    """Cache regime sizing for 15min to avoid redundant computation."""
    global _REGIME_CACHE
    now = datetime.now(timezone.utc)
    if _REGIME_CACHE["sizing"] and _REGIME_CACHE["updated_at"]:
        if (now - _REGIME_CACHE["updated_at"]).total_seconds() < 900:  # 15min
            return _REGIME_CACHE["sizing"]
    _REGIME_CACHE["sizing"] = get_regime_sizing(btc_4h, fng)
    _REGIME_CACHE["updated_at"] = now
    return _REGIME_CACHE["sizing"]


def detect_market_regime(btc_4h: pd.DataFrame) -> str:
    """Classify current market regime based on BTC 4H data.
    Returns one of: 'bull', 'neutral', 'bear', 'unknown'."""
    if btc_4h is None or len(btc_4h) < 50:
        return "unknown"

    close = float(btc_4h["close"].iloc[-1])
    ema50 = float(ema(btc_4h["close"], config.EMA_SLOW).iloc[-1])
    btc_atr = atr(btc_4h, 14)
    btc_adx = adx(btc_4h, config.ADX_PERIOD)

    # 변동성 밴드: EMA50 ± 2×ATR
    upper_band = ema50 + config.BTC_BAND_ATR_MULTI * btc_atr
    lower_band = ema50 - config.BTC_BAND_ATR_MULTI * btc_atr

    logger.info(
        "Regime: close=%.2f ema50=%.2f upper=%.2f lower=%.2f adx=%.1f",
        close, ema50, upper_band, lower_band, btc_adx,
    )

    if close > upper_band and btc_adx >= config.ADX_TRENDING_THRESHOLD:
        return "bull"
    elif close < lower_band:
        # Lower band break = bear regardless of ADX.
        # When ADX < 25, the trend is weak but the price structure is still bearish
        # (below EMA50 - 2*ATR). Requiring ADX >= 25 masked genuine downtrends
        # with low momentum (e.g., BTC -2.7% below EMA50, ADX=15.5 on 2026-05-23).
        return "bear"
    else:
        return "neutral"
