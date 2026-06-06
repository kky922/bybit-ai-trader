import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from analysis.tradfi_entry_signal import check_tradfi_entry


WEEKDAY_KST = datetime(2026, 6, 8, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def _ohlcv(closes, volumes=None):
    if volumes is None:
        volumes = [1000 + i * 50 for i, _ in enumerate(closes)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


def test_tradfi_allows_spy_bear_regime_when_filters_pass(monkeypatch):
    monkeypatch.setattr("config.TRADFI_RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.TRADFI_VOLUME_RATIO_MIN", 0.8)
    closes = [100 + i for i in range(80)]
    spy_bear = [180 - i for i in range(80)]

    sig, reason = check_tradfi_entry(
        "TESTCMD",
        "commodity",
        _ohlcv(closes),
        _ohlcv(closes),
        _ohlcv(spy_bear),
        now_kst=WEEKDAY_KST,
    )

    assert sig is not None
    assert reason.startswith("spy_bear_soft_pass:") or reason == "ok"
    assert sig.symbol == "TESTCMD"


def test_tradfi_rejects_low_volume(monkeypatch):
    monkeypatch.setattr("config.TRADFI_RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.TRADFI_VOLUME_RATIO_MIN", 2.0)
    closes = [100 + i for i in range(80)]

    sig, reason = check_tradfi_entry(
        "TESTCMD",
        "commodity",
        _ohlcv(closes),
        _ohlcv(closes),
        _ohlcv(closes),
        now_kst=WEEKDAY_KST,
    )

    assert sig is None
    assert reason.startswith("volume_low:")
