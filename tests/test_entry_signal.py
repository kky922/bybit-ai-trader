import pandas as pd

from analysis.entry_signal import check_entry, check_entry_diagnostic


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


def test_entry_passes_on_sustained_uptrend(monkeypatch):
    # RSI는 단조 우상향에선 100에 가까우니 상한을 풀고, 거래량은 마지막봉이 평균 이상이도록 충분히
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    closes = [100 + i for i in range(80)]
    c4 = _ohlcv(closes)
    c1 = _ohlcv(closes)
    btc = _ohlcv(closes)

    sig, reason = check_entry_diagnostic("TESTUSDT", c4, c1, btc)

    assert reason == "ok"
    assert sig is not None
    assert sig.symbol == "TESTUSDT"


def test_entry_rejects_btc_below_ema50(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    monkeypatch.setattr("config.BTC_TREND_SIDEBAND_PCT", 3.0)
    # Mock market regime to avoid bear_regime affecting the BTC EMA50 check
    monkeypatch.setattr("analysis.entry_signal.detect_market_regime", lambda btc: "neutral")
    up = [100 + i for i in range(80)]
    down = [180 - i for i in range(80)]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(up), _ohlcv(up), _ohlcv(down))

    assert sig is None
    assert reason.startswith("btc_below_ema50:")


def test_entry_rejects_symbol_4h_below_ema50(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    up = [100 + i for i in range(80)]
    down = [180 - i for i in range(80)]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(down), _ohlcv(up), _ohlcv(up))

    assert sig is None
    assert reason.startswith("symbol_4h_below_ema50:")


def test_entry_rejects_1h_alignment_fail(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    up = [100 + i for i in range(80)]
    # 1H가 우상향 후 막판에 EMA20 아래로 떨어진 시나리오
    sideways_then_drop = [150 + i * 0.1 for i in range(70)] + [150 - i for i in range(10)]

    sig, reason = check_entry_diagnostic(
        "TESTUSDT", _ohlcv(up), _ohlcv(sideways_then_drop), _ohlcv(up)
    )

    assert sig is None
    assert reason.startswith("1h_alignment_fail:")


def test_entry_rejects_rsi_out_of_range(monkeypatch):
    # 단조 우상향이면 RSI는 매우 높음. 상한을 60으로 제한해 거부 확인.
    monkeypatch.setattr("config.RSI_ENTRY_MIN", 45.0)
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 60.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    closes = [100 + i for i in range(80)]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(closes), _ohlcv(closes), _ohlcv(closes))

    assert sig is None
    assert reason.startswith("rsi_out_of_range:")


def test_entry_allows_small_rsi_pullback_with_momentum_tolerance(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    monkeypatch.setattr("config.RSI_MOMENTUM_MAX_DROP", 2.0, raising=False)
    up = [100 + i for i in range(80)]
    # 마지막 봉에서 아주 작은 되돌림만 있는 시나리오.
    # 현재 구현은 RSI가 조금이라도 내려가면 거부하지만,
    # 작은 하락폭은 허용해야 데이터가 너무 마르지 않는다.
    gentle_pullback = [100 + i * 0.5 for i in range(76)] + [138.0, 138.2, 138.15, 138.14]

    sig, reason = check_entry_diagnostic(
        "TESTUSDT", _ohlcv(up), _ohlcv(gentle_pullback), _ohlcv(up)
    )

    assert reason == "ok"
    assert sig is not None


def test_entry_allows_small_btc_pullback_within_sideband(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    monkeypatch.setattr("config.BTC_TREND_SIDEBAND_PCT", 3.0)
    up = [100 + i for i in range(80)]
    # BTC가 EMA50 아래로 살짝 눌렸지만 sideband 안인 시나리오
    mild_pullback = [100.0 for _ in range(70)] + [99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2, 98.0, 97.8]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(up), _ohlcv(up), _ohlcv(mild_pullback))

    assert reason.startswith("btc_soft_pass:")
    assert sig is not None


def test_symbol_sideband_allows_2_5pct_pullback(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    monkeypatch.setattr("config.SYMBOL_EMA50_SIDEBAND_PCT", 3.0)
    up = [100 + i for i in range(80)]
    # 심볼 4H가 EMA50 아래로 -2.5% 눌렸지만 3.0% sideband 이내 → 통과
    mild_symbol_pullback = [100.0 for _ in range(70)] + [99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2, 98.0]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(mild_symbol_pullback), _ohlcv(up), _ohlcv(up))

    assert reason.startswith("ok")
    assert sig is not None


def test_entry_rejects_low_volume(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 2.0)
    closes = [100 + i for i in range(80)]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(closes), _ohlcv(closes), _ohlcv(closes))

    assert sig is None
    assert reason.startswith("volume_ratio_low:")


def test_check_entry_returns_signal_on_uptrend(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    closes = [100 + i for i in range(80)]
    sig = check_entry("TESTUSDT", _ohlcv(closes), _ohlcv(closes), _ohlcv(closes))
    assert sig is not None
    assert sig.symbol == "TESTUSDT"
    assert sig.stop_loss < sig.entry_price < sig.take_profit


def test_entry_allows_bear_regime_when_filters_pass(monkeypatch):
    monkeypatch.setattr("config.RSI_ENTRY_MAX", 100.0)
    monkeypatch.setattr("config.VOLUME_RATIO_MIN", 0.8)
    monkeypatch.setattr("analysis.entry_signal.detect_market_regime", lambda btc: "bear")
    closes = [100 + i for i in range(80)]

    sig, reason = check_entry_diagnostic("TESTUSDT", _ohlcv(closes), _ohlcv(closes), _ohlcv(closes))

    assert reason == "ok"
    assert sig is not None
    assert sig.symbol == "TESTUSDT"
