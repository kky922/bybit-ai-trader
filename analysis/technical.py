from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    value = 100 - (100 / (1 + rs.iloc[-1]))
    return float(value if pd.notna(value) else 100.0)


def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Return the full RSI series (not just last value)."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    series = 100 - (100 / (1 + rs))
    # Replace NaN/NA values: RSI=100 when no losses (perfect uptrend), RSI=0 when no gains
    return series.where(series.notna(), 100.0)


def atr(ohlc_df: pd.DataFrame, period: int = 14) -> float:
    high_low = ohlc_df["high"] - ohlc_df["low"]
    high_close = (ohlc_df["high"] - ohlc_df["close"].shift()).abs()
    low_close = (ohlc_df["low"] - ohlc_df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr_series = true_range.rolling(period).mean()
    return float(atr_series.iloc[-1])


def adx(ohlc_df: pd.DataFrame, period: int = 14) -> float:
    """Return ADX (Average Directional Index) for trend strength.
    ADX < 25 = weak/choppy, 25-50 = trending, > 50 = strong trend."""
    high = ohlc_df["high"]
    low = ohlc_df["low"]
    close = ohlc_df["close"]

    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = minus_dm.abs()

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr_series = tr.rolling(period).mean()
    # Use .clip(lower=eps) instead of .replace(0, pd.NA) to keep dtype
    # numeric.  pd.NA in a float series causes object dtype and breaks
    # rolling().mean() downstream (pandas bug-compat issue).
    eps = 1e-10
    atr_safe = atr_series.clip(lower=eps)  # type: ignore[call-overload]
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_safe)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_safe)
    dsum_safe = (plus_di + minus_di).clip(lower=eps)  # type: ignore[call-overload]
    dx = 100 * ((plus_di - minus_di).abs() / dsum_safe)
    adx_series = dx.rolling(period).mean()
    return float(adx_series.iloc[-1] if pd.notna(adx_series.iloc[-1]) else 0.0)


def volume_ratio(volumes: pd.Series, period: int = 20) -> float:
    base = volumes.tail(period).mean()
    if base <= 0:
        return 0.0
    return float(volumes.iloc[-1] / base)
