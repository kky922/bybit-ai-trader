from __future__ import annotations

import math
from typing import Any

import config

# Regime-aware sizing (main.py에서 업데이트, position_sizer가 읽음)
# 클로드 Opus 4.7: "Regime-aware sizing is more impactful than adjusting exits"
_CURRENT_REGIME_SIZING: dict = {}


def set_regime_sizing(sizing: dict) -> None:
    global _CURRENT_REGIME_SIZING
    _CURRENT_REGIME_SIZING = sizing


def get_effective_equity_pct() -> float:
    return _CURRENT_REGIME_SIZING.get("equity_pct", config.MAX_EQUITY_PCT_PER_POSITION)


def get_effective_kelly_frac() -> float:
    return _CURRENT_REGIME_SIZING.get("kelly_fraction", config.KELLY_FRACTION)


def round_to_lot(size: float, lot_step: float) -> float:
    if lot_step <= 0:
        return size
    return math.floor(size / lot_step) * lot_step


def _kelly_fraction() -> float:
    """Calculate optimal bet size using Kelly Criterion.
    Uses stored win_rate and avg_rr from PnL history.
    Returns fraction of equity to risk (0.0-1.0).
    Falls back to MAX_RISK_PCT_PER_TRADE if insufficient data."""
    from infra.state import _read_json
    from config import LOGS_DIR

    if not config.KELLY_ENABLED:
        return config.MAX_RISK_PCT_PER_TRADE * (1.0 / config.KELLY_FRACTION)

    try:
        pnl_file = LOGS_DIR / "pnl_history.json"
        records = _read_json(pnl_file, [])
        if len(records) < 5:
            return config.MAX_RISK_PCT_PER_TRADE * (1.0 / config.KELLY_FRACTION)

        wins = [r for r in records if r.get("pnl", 0) > 0]
        losses = [r for r in records if r.get("pnl", 0) <= 0]
        win_rate = len(wins) / len(records) if records else 0.5

        # 평균 RR (Risk/Reward): 각 거래의 수익/손실 비율
        avg_win = abs(sum(r["pnl"] for r in wins) / len(wins)) if wins else 0
        avg_loss = abs(sum(r["pnl"] for r in losses) / len(losses)) if losses else 0
        avg_rr = (avg_win / avg_loss) if avg_loss > 0 else 1.0

        # 켈리 공식: f* = (p * (b + 1) - 1) / b
        # p = win_rate, b = avg_rr (odds ratio)
        kelly = (win_rate * (avg_rr + 1) - 1) / avg_rr if avg_rr > 0 else 0
        kelly = max(0.0, min(kelly, 1.0))  # clamp [0, 1]

        # 1/4 켈리 (보수적) — regime-aware
        effective_kelly_frac = get_effective_kelly_frac()
        kelly *= effective_kelly_frac

        # MAX_RISK_PCT_PER_TRADE와 결합: 켈리가 더 보수적이면 켈리 우선
        return max(kelly * 0.5, config.MAX_RISK_PCT_PER_TRADE * 0.5)
    except Exception:
        return config.MAX_RISK_PCT_PER_TRADE * (1.0 / config.KELLY_FRACTION)


def compute_size(
    equity_usdt: float,
    entry: float,
    stop: float,
    lot_step: float,
    min_amount: float,
    min_notional: float,
) -> float:
    size, _ = compute_size_with_reason(
        equity_usdt=equity_usdt,
        entry=entry,
        stop=stop,
        lot_step=lot_step,
        min_amount=min_amount,
        min_notional=min_notional,
    )
    return size


def compute_size_with_reason(
    equity_usdt: float,
    entry: float,
    stop: float,
    lot_step: float,
    min_amount: float,
    min_notional: float,
) -> tuple[float, str]:
    risk_per_unit = max(entry - stop, 1e-12)

    # Kelly 기반 리스크 비율
    risk_pct = _kelly_fraction()

    size_by_risk = (equity_usdt * risk_pct) / risk_per_unit
    size_by_equity = (equity_usdt * get_effective_equity_pct()) / entry
    size = min(size_by_risk, size_by_equity)
    size = round_to_lot(size, lot_step)

    if size < min_amount:
        return 0.0, "min_amount"
    if size * entry <= min_notional:
        return 0.0, "min_notional"
    return float(size), "ok"
