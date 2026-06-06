from datetime import datetime, timedelta, timezone

import pandas as pd

from analysis.exit_signal import _sector_matches, check_exit


def _ohlcv(closes):
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000 + i * 10 for i, _ in enumerate(closes)],
        }
    )


def test_exit_ignores_narrative_fade_for_fresh_position():
    position = {
        "symbol": "TESTUSDT",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 130.0,
        "atr": 2.0,
        "highest_price": 105.0,
        "sector": "AI Tokens",
        "entered_at": datetime.now(timezone.utc).isoformat(),
    }
    candles_1h = _ohlcv([100 + i * 0.2 for i in range(80)])
    candles_4h = _ohlcv([100 + i * 0.3 for i in range(80)])
    latest_sector_analysis = {"sectors": [{"name": "AI", "heat_score": 1}]}

    reason = check_exit(position, candles_1h, candles_4h, latest_sector_analysis)

    assert reason is None


def test_exit_allows_narrative_fade_after_grace_period(monkeypatch):
    monkeypatch.setattr("config.NARRATIVE_FADE_GRACE_HOURS", 6)
    monkeypatch.setattr("config.SECTOR_HEAT_EXIT_THRESHOLD", 2)
    monkeypatch.setattr("config.MAX_POSITION_HOURS", 24.0)
    position = {
        "symbol": "TESTUSDT",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 130.0,
        "atr": 2.0,
        "highest_price": 105.0,
        "sector": "AI",
        "entered_at": (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
    }
    candles_1h = _ohlcv([100 + i * 0.2 for i in range(80)])
    candles_4h = _ohlcv([100 + i * 0.3 for i in range(80)])
    # AI sector is in analysis but heat_score (1) < threshold (2) → should fade
    latest_sector_analysis = {"sectors": [{"name": "AI", "heat_score": 1}]}

    reason = check_exit(position, candles_1h, candles_4h, latest_sector_analysis)

    assert reason == "narrative_faded"


def test_exit_skips_narrative_fade_for_unassessed_sector(monkeypatch):
    """Positions whose sector is not in the latest analysis should not be faded."""
    monkeypatch.setattr("config.NARRATIVE_FADE_GRACE_HOURS", 6)
    monkeypatch.setattr("config.MAX_POSITION_HOURS", 24.0)
    position = {
        "symbol": "TESTUSDT",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 130.0,
        "atr": 2.0,
        "highest_price": 105.0,
        "sector": "UnknownAltcoinSector",
        "entered_at": (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
    }
    candles_1h = _ohlcv([100 + i * 0.2 for i in range(80)])
    candles_4h = _ohlcv([100 + i * 0.3 for i in range(80)])
    # Only "AI" is in the analysis; "UnknownAltcoinSector" is not → position survives
    latest_sector_analysis = {"sectors": [{"name": "AI", "heat_score": 1}]}

    reason = check_exit(position, candles_1h, candles_4h, latest_sector_analysis)

    assert reason is None


def test_sector_matches():
    """_sector_matches handles Gemini's inconsistent naming."""
    # Exact match
    assert _sector_matches("AI", "AI")
    # Substring: stored is part of AI name
    assert _sector_matches("Regulation", "Stablecoins & Regulation")
    assert _sector_matches("Defi", "DeFi & L2 Scaling")
    # Substring: AI name is part of stored (shouldn't happen often but handles it)
    assert _sector_matches("Bitcoin & ETFs & Mining", "Bitcoin & ETFs")
    # Mismatch
    assert not _sector_matches("AI", "Bitcoin & ETFs")
    assert not _sector_matches("Meme", "AI")
    # Case insensitive
    assert _sector_matches("regulation", "Stablecoins & Regulation")
    assert _sector_matches("REGULATION", "STABLECOINS & REGULATION")
    # Word-level overlap: different names but share key words
    assert _sector_matches(
        "Institutional Adoption & Regulation", "Regulation & Policy"
    )
    assert _sector_matches("Stablecoins & Regulation", "Regulation & Policy")
    assert not _sector_matches("Meme", "Artificial Intelligence (AI)")
