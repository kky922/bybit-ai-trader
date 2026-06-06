from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

import config
from analysis.technical import atr, ema, rsi, rsi_series, volume_ratio

logger = logging.getLogger("coin-bot.tradfi_entry")


@dataclass
class TradFiEntrySignal:
    symbol: str
    symbol_type: str  # "commodity" or "stock"
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float


def _fmt(**items: float | str) -> str:
    parts = []
    for k, v in items.items():
        parts.append(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}")
    return ",".join(parts)


def check_tradfi_entry(
    symbol: str,
    symbol_type: str,
    candles_4h: pd.DataFrame,
    candles_1h: pd.DataFrame,
    spy_4h: pd.DataFrame | None = None,
    *,
    regime: str = "neutral",
) -> tuple[TradFiEntrySignal | None, str]:
    """
    TradFi 진입 신호 체크.

    spy_4h: SPY 4H 캔들 (매크로 필터용). None이면 매크로 필터 스킵.
    regime: bridge regime ("bear", "bear_crash", "bull", "neutral").
            bear/bear_crash 시 TRADFI_BEAR_ATR_STOP_MULTI로 SL 완화,
            TRADFI_BEAR_ATR_TP_MULTI로 TP 축소.
    """
    spy_reason = "ok"

    # ── 필터 0: 주식/원자재 장시간 체크 ──
    if symbol_type == "stock":
        # 인스턴스 없이 정적으로 체크
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if now_kst.weekday() >= 5:
            return None, "market_closed:weekend"
        month = now_kst.month
        is_dst = 3 <= month <= 10 or (month == 11 and now_kst.day <= 7)
        open_h, open_m = (22, 30) if is_dst else (23, 30)
        close_h = 5 if is_dst else 6
        h, m = now_kst.hour, now_kst.minute
        after_open = h > open_h or (h == open_h and m >= open_m)
        before_close = h < close_h or (h == close_h and m == 0)
        is_open = after_open or before_close  # 자정을 넘어가므로 OR
        if not is_open:
            return None, f"market_closed:kst={now_kst.strftime('%H:%M')}"
    elif symbol_type == "commodity":
        # 원자재(금, 은, 원유 등)는 주중(월-금)만 거래 가능
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if now_kst.weekday() >= 5:
            return None, "commodity_market_closed:weekend"

    # ── 필터 1: SPY 매크로 필터 ──
    # bear regime는 하드 차단하지 않고 로그/사유만 남긴다.
    if spy_4h is not None and len(spy_4h) >= 50:
        spy_close = float(spy_4h["close"].iloc[-1])
        spy_ema50 = float(ema(spy_4h["close"], 50).iloc[-1])
        spy_atr = atr(spy_4h, 14)
        spy_floor = spy_ema50 - 2.0 * spy_atr
        if spy_close < spy_floor:
            spy_reason = "spy_bear_soft_pass:" + _fmt(close=spy_close, ema50=spy_ema50, floor=spy_floor)
            logger.info(
                "SPY bear regime soft-pass: %s (%s) close=%.4f ema50=%.4f floor=%.4f",
                symbol,
                symbol_type,
                spy_close,
                spy_ema50,
                spy_floor,
            )

    # ── 필터 2: 4H EMA50 정렬 ──
    close_4h = float(candles_4h["close"].iloc[-1])
    ema50_4h = float(ema(candles_4h["close"], config.EMA_SLOW).iloc[-1])
    sideband = max(0.0, config.SYMBOL_EMA50_SIDEBAND_PCT)
    floor_4h = ema50_4h * (1.0 - sideband / 100.0)
    if close_4h < floor_4h:
        return None, "4h_below_ema50:" + _fmt(close=close_4h, ema50=ema50_4h, floor=floor_4h)

    # ── 필터 3: 1H 추세 정렬 (close > EMA20 > EMA50) ──
    close_1h = float(candles_1h["close"].iloc[-1])
    ema20_1h = float(ema(candles_1h["close"], config.EMA_FAST).iloc[-1])
    ema50_1h = float(ema(candles_1h["close"], config.EMA_SLOW).iloc[-1])
    if not (close_1h > ema20_1h > ema50_1h):
        return None, "1h_alignment_fail:" + _fmt(close=close_1h, ema20=ema20_1h, ema50=ema50_1h)

    # ── 필터 4: RSI 범위 + 모멘텀 (TradFi 전용 범위 사용) ──
    rsi_min = config.TRADFI_RSI_ENTRY_MIN if symbol_type in ("stock", "commodity") else config.RSI_ENTRY_MIN
    rsi_max = config.TRADFI_RSI_ENTRY_MAX if symbol_type in ("stock", "commodity") else config.RSI_ENTRY_MAX
    _rsi_series = rsi_series(candles_1h["close"], 14)
    rsi_1h = float(_rsi_series.iloc[-1])
    if not (rsi_min <= rsi_1h <= rsi_max):
        return None, f"rsi_out_of_range:rsi={rsi_1h:.4f},min={rsi_min:.4f},max={rsi_max:.4f}"
    if len(_rsi_series) >= 2:
        rsi_prev = float(_rsi_series.iloc[-2])
        # bear_crash: RSI momentum drop filter 제거 (급락 후 반등 진입 허용)
        if regime == "bear_crash":
            pass
        elif rsi_prev - rsi_1h > config.RSI_MOMENTUM_MAX_DROP:
            return None, "rsi_momentum_declining:" + _fmt(rsi=rsi_1h, prev=rsi_prev)

    # ── 필터 4b: 4H RSI 모멘텀 ──
    _rsi_4h = rsi_series(candles_4h["close"], 14)
    rsi_4h_val = float(_rsi_4h.iloc[-1])
    if len(_rsi_4h) >= 2:
        rsi_4h_prev = float(_rsi_4h.iloc[-2])
        if rsi_4h_val < rsi_4h_prev - config.RSI_4H_MOMENTUM_SIDEBAND:
            return None, "rsi_4h_declining:" + _fmt(rsi=rsi_4h_val, prev=rsi_4h_prev)

    # ── 필터 5: 거래량 ──
    vol_min = config.TRADFI_VOLUME_RATIO_MIN if symbol_type in ("stock", "commodity") else config.VOLUME_RATIO_MIN
    vol = volume_ratio(candles_1h["volume"], 20)
    if vol < vol_min:
        return None, "volume_low:" + _fmt(volume_ratio=vol, min=vol_min)

    # ── 통과 ──
    atr_1h = atr(candles_1h, 14)

    # regime 기반 ATR 승수: bear_crash → 더 넓은 손절/느슨한 익절
    if symbol_type in ("stock", "commodity") and regime == "bear_crash":
        stop_multi = config.TRADFI_BEAR_ATR_STOP_MULTI
        tp_multi = config.TRADFI_BEAR_ATR_TP_MULTI
    else:
        stop_multi = config.ATR_STOP_MULTI
        tp_multi = config.ATR_TP_MULTI

    logger.info(
        "TRADFI ENTRY SIGNAL: %s (%s) @ %.4f rsi=%.1f vol=%.2f atr_stop=%.2f tp=%.2f regime=%s",
        symbol, symbol_type, close_1h, rsi_1h, vol,
        stop_multi, tp_multi, regime,
    )
    return TradFiEntrySignal(
        symbol=symbol,
        symbol_type=symbol_type,
        entry_price=close_1h,
        stop_loss=close_1h - stop_multi * atr_1h,
        take_profit=close_1h + tp_multi * atr_1h,
        atr=atr_1h,
    ), spy_reason
