from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

import config
from analysis.technical import adx, atr, ema, rsi, rsi_series, volume_ratio
from data.market_data import detect_market_regime, fetch_fear_greed_index

logger = logging.getLogger("coin-bot.entry_signal")


@dataclass
class EntrySignal:
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float


def _fmt_metrics(**items: float | str) -> str:
    parts = []
    for key, value in items.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:.4f}")
        else:
            parts.append(f"{key}={value}")
    return ",".join(parts)


def _check_entry_with_reason(
    symbol: str, candles_4h: pd.DataFrame, candles_1h: pd.DataFrame, btc_4h: pd.DataFrame
) -> tuple[EntrySignal | None, str]:
    # ── 필터 0a: 마켓 레짐 체크 ──
    regime = detect_market_regime(btc_4h)
    if regime == "bear":
        logger.info(
            "Market regime: %s (bear detected; entry filters remain active)",
            regime,
        )
    else:
        logger.info("Market regime: %s", regime)

    # ── 필터 0b: ADX 추세 강도 체크 ──
    btc_adx_val = adx(btc_4h, config.ADX_PERIOD)
    logger.info("BTC ADX(14)=%.1f", btc_adx_val)

    # ── 필터 0c: 공포탐욕지수 ──
    fng = fetch_fear_greed_index()
    if fng is not None:
        logger.info("Fear & Greed: %.0f", fng)

    # ── 필터 1: BTC 4H 종가 > EMA50 ──
    # BTC 급락 차단은 risk_manager.can_trade()에서 별도 처리 (BTC_CRASH_FILTER_PCT_4H).
    # 여기선 시장 체질만 체크하되, EMA50 바로 아래의 얕은 눌림은 sideband로 허용한다.
    btc_close = float(btc_4h["close"].iloc[-1])
    btc_ema50 = float(ema(btc_4h["close"], config.EMA_SLOW).iloc[-1])
    btc_sideband_pct = max(0.0, float(config.BTC_TREND_SIDEBAND_PCT))
    btc_soft_floor = btc_ema50 * (1.0 - btc_sideband_pct / 100.0)
    if btc_close <= btc_soft_floor:
        return None, "btc_below_ema50:" + _fmt_metrics(
            close=btc_close, ema50=btc_ema50, band_pct=btc_sideband_pct, floor=btc_soft_floor
        )
    if btc_close <= btc_ema50:
        logger.info(
            "BTC soft-pass: close=%.4f ema50=%.4f sideband=%.2f%%",
            btc_close,
            btc_ema50,
            btc_sideband_pct,
        )
        btc_reason = "btc_soft_pass:" + _fmt_metrics(
            close=btc_close, ema50=btc_ema50, band_pct=btc_sideband_pct, floor=btc_soft_floor
        )
    else:
        btc_reason = "ok"

    # ── 필터 2: 심볼 4H 종가 > EMA50 (상위 추세 우상향) ──
    # BTC sideband와 동일한 원리: EMA50 바로 아래 얕은 눌림은 sideband로 허용
    sym4h_close = float(candles_4h["close"].iloc[-1])
    sym4h_ema50 = float(ema(candles_4h["close"], config.EMA_SLOW).iloc[-1])
    sym_sideband_pct = max(0.0, float(config.SYMBOL_EMA50_SIDEBAND_PCT))
    sym_soft_floor = sym4h_ema50 * (1.0 - sym_sideband_pct / 100.0)
    # BTC soft-pass(얕은 눌림장) 활성화 시: 개별 심볼 4H EMA50 필터를 건너뛴다.
    #   - 4H EMA50이 얕은 눌림장에서 72%의 진입을 차단 (data-driven 발견)
    #   - BTC 매크로 필터로 시장 체질 보호, 1H 정렬+RSI+거래량으로 품질 관리
    #   - 실험 목적: 건조한 눌림장에서 진입 기회를 늘려 데이터 축적 가속
    if not btc_reason.startswith("btc_soft_pass"):
        if sym4h_close <= sym_soft_floor:
            return None, "symbol_4h_below_ema50:" + _fmt_metrics(
                close=sym4h_close, ema50=sym4h_ema50, band_pct=sym_sideband_pct, floor=sym_soft_floor
            )
        if sym4h_close <= sym4h_ema50:
            logger.info(
                "심볼 %s soft-pass: close=%.4f ema50=%.4f sideband=%.2f%%",
                symbol, sym4h_close, sym4h_ema50, sym_sideband_pct,
            )
    else:
        logger.info(
            "BTC soft-pass → 개별 %s 4H EMA50 생략: close=%.4f ema50=%.4f",
            symbol, sym4h_close, sym4h_ema50,
        )

    # ── 필터 3: 1H 추세 정렬 close > EMA20 ──
    #   - 일반 모드: 엄격한 close > EMA20 > EMA50
    #   - BTC soft-pass 활성화 시: close > EMA20 * (1 - sideband) (EMA20 > EMA50 생략)
    #     (2026-05-16 실험: 얕은 BTC 눌림장에서 1H EMA20 < EMA50인 심볼이
    #      100% 차단되어 모든 진입이 막힘. BTC soft-pass는 이미 BTC가 EMA50 근처임을
    #      보증하므로, 개별 심볼의 1H EMA20/EMA50 크로스오버는 불필요한 과잉 필터.
    #      시장이 횡보/약한 하락일 때 보편적으로 발생. RSI(필터4)+거래량(필터5)로 품질 관리)
    close_1h = float(candles_1h["close"].iloc[-1])
    ema20_1h = float(ema(candles_1h["close"], config.EMA_FAST).iloc[-1])
    ema50_1h = float(ema(candles_1h["close"], config.EMA_SLOW).iloc[-1])
    if btc_reason.startswith("btc_soft_pass"):
        soft_floor_1h = ema20_1h * (1.0 - config.SOFT_PASS_1H_SIDEBAND_PCT / 100.0)
        # 실험 Z (2026-05-20): Exp X(1H EMA50 조건) 제거 — BTC soft-pass 중 과잉 차단 해소
        #   Exp X(2026-05-18)가 BTC soft-pass 중 1H EMA50 아래 심볼을 차단했으나,
        #   BTC 2.5%↓ EMA50 약세장에서 7시간 연속 진입 0건 발생. 모든 진입이 EMA50에 막힘.
        #   근거: BTC soft-pass(EMA50 5% 이내)는 이미 거시적 하락장이 아님을 보증.
        #   1H 사이드밴드(EMA20×0.95) + RSI(20-75) + RSI 모멘텀(1H+4H, 5.0) + 거래량(0.3)
        #   로 충분한 품질 관리. EMA20>EMA50 조건도 여전히 미복원(Exp C).
        #   BTC가 5% 바닥 아래로 떨어지면 soft-pass 해제 → 일반 모드(close>EMA20>EMA50)로 자동 전환.
        if not (close_1h > soft_floor_1h):
            return None, "1h_alignment_fail:" + _fmt_metrics(
                close=close_1h, ema20=ema20_1h, ema50=ema50_1h,
                soft_pass_sideband=config.SOFT_PASS_1H_SIDEBAND_PCT,
                soft_floor=soft_floor_1h,
            )
        if close_1h <= ema20_1h:
            logger.info(
                "1H soft-pass (experiment Z): %s close=%.4f ema20=%.4f ema50=%.4f sideband=%.2f%% floor=%.4f",
                symbol, close_1h, ema20_1h, ema50_1h, config.SOFT_PASS_1H_SIDEBAND_PCT, soft_floor_1h,
            )
    else:
        if not (close_1h > ema20_1h > ema50_1h):
            return None, "1h_alignment_fail:" + _fmt_metrics(
                close=close_1h, ema20=ema20_1h, ema50=ema50_1h
            )

    # ── 필터 4: RSI 모멘텀 범위 ──
    # RSI 시리즈를 먼저 계산해 추세 확인에 재활용
    _rsi_series = rsi_series(candles_1h["close"], 14)
    rsi_1h = float(_rsi_series.iloc[-1])
    if not (config.RSI_ENTRY_MIN <= rsi_1h <= config.RSI_ENTRY_MAX):
        return None, "rsi_out_of_range:" + _fmt_metrics(
            rsi=rsi_1h, min=config.RSI_ENTRY_MIN, max=config.RSI_ENTRY_MAX
        )
    # ── 필터 4b: RSI 추세 확인 (모멘텀 하락 중인 심볼 제외) ──
    #   RSI가 범위 내여도 하락 중이면 진입하지 않음.
    #   (실험 K — 2026-05-17: 추세 반전의 초기 신호 없이 눌림목만 따른 베팅 차단)
    if len(_rsi_series) >= 2:
        rsi_prev = float(_rsi_series.iloc[-2])
        rsi_drop = rsi_prev - rsi_1h
        if rsi_drop > config.RSI_MOMENTUM_MAX_DROP:
            return None, "rsi_momentum_declining:" + _fmt_metrics(
                rsi=rsi_1h, prev=rsi_prev
            )
        if rsi_drop > 0:
            logger.info(
                "1H RSI momentum soft-pass: %s rsi=%.4f prev=%.4f max_drop=%.2f",
                symbol,
                rsi_1h,
                rsi_prev,
                config.RSI_MOMENTUM_MAX_DROP,
            )

    # ── 필터 4c: 4H RSI 모멘텀 (중기 추세 확인) ──
    #   1H RSI(2h 창)는 단기 바운스에 반응하지만 4H RSI(8h 창)가 하락 중이면
    #   중기 하락 추세에 진입하는 것. CCUSDT(진입하자마자 하락, 18h+ 회복 못함)와
    #   SUIUSDT(진입 후 9h+ 회복 못함) 같은 케이스를 차단.
    #   (실험 Q — 2026-05-18: 중기 추세 상승 확인 후 진입)
    _rsi_4h = rsi_series(candles_4h["close"], 14)
    rsi_4h_val = float(_rsi_4h.iloc[-1])
    if len(_rsi_4h) >= 2:
        rsi_4h_prev = float(_rsi_4h.iloc[-2])
        rsi_4h_floor = rsi_4h_prev - config.RSI_4H_MOMENTUM_SIDEBAND
        if rsi_4h_val < rsi_4h_floor:
            return None, "rsi_4h_momentum_declining:" + _fmt_metrics(
                rsi=rsi_4h_val, prev=rsi_4h_prev, floor=round(rsi_4h_floor, 2)
            )

    # ── 필터 5: 거래량 비율 ──
    vol_ratio = volume_ratio(candles_1h["volume"], 20)
    if vol_ratio < config.VOLUME_RATIO_MIN:
        return None, "volume_ratio_low:" + _fmt_metrics(
            volume_ratio=vol_ratio, min=config.VOLUME_RATIO_MIN
        )

    # ── 통과: 진입 시그널 생성 ──
    atr_1h = atr(candles_1h, 14)
    entry_price = close_1h
    logger.info(
        "ENTRY SIGNAL: %s @ %.4f | btc_close=%.2f sym4h_close=%.4f 1h_close=%.4f rsi=%.1f vol=%.2f",
        symbol, entry_price, btc_close, sym4h_close, close_1h, rsi_1h, vol_ratio,
    )
    return (
        EntrySignal(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=entry_price - config.get_symbol_stop_mult(symbol) * atr_1h,
            take_profit=entry_price + config.ATR_TP_MULTI * atr_1h,
            atr=atr_1h,
        ),
        btc_reason,
    )


def check_entry(
    symbol: str, candles_4h: pd.DataFrame, candles_1h: pd.DataFrame, btc_4h: pd.DataFrame
) -> EntrySignal | None:
    signal, _ = _check_entry_with_reason(symbol, candles_4h, candles_1h, btc_4h)
    return signal


def check_entry_diagnostic(
    symbol: str, candles_4h: pd.DataFrame, candles_1h: pd.DataFrame, btc_4h: pd.DataFrame
) -> tuple[EntrySignal | None, str]:
    return _check_entry_with_reason(symbol, candles_4h, candles_1h, btc_4h)
