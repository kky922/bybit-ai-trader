import pandas as pd

from analysis.technical import atr, ema, rsi, volume_ratio


def test_ema_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ema(s, 3)
    assert len(out) == 5
    assert out.iloc[-1] > out.iloc[0]


def test_rsi_range():
    s = pd.Series([1, 2, 1.5, 2.2, 2.1, 2.8, 2.7, 3.0, 3.2, 3.1, 3.3, 3.6, 3.8, 3.7, 3.9])
    value = rsi(s)
    assert 0 <= value <= 100


def test_atr_positive():
    df = pd.DataFrame(
        {
            "high": [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25],
            "low": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
            "close": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
        }
    )
    assert atr(df) > 0


def test_volume_ratio():
    v = pd.Series([100] * 19 + [200], dtype=float)
    assert volume_ratio(v, 20) > 1.0
