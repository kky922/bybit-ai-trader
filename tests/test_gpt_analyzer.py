from pathlib import Path

from analysis import gpt_analyzer
from analysis.gpt_analyzer import GPTAnalyzer


def _disable_external_state(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    monkeypatch.setattr("config.DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(gpt_analyzer.config, "DATA_DIR", Path("/tmp/coin-bot-test-data"))
    monkeypatch.setattr(gpt_analyzer, "load_latest_ai", lambda: {"sectors": {"sectors": []}, "candidates": []})
    monkeypatch.setattr(gpt_analyzer, "save_latest_ai", lambda data: None)
    monkeypatch.setattr(gpt_analyzer, "append_news_snapshot", lambda snapshot: None)


def test_pick_coins_uses_fallback_when_gemini_empty(monkeypatch):
    _disable_external_state(monkeypatch)
    monkeypatch.setattr("config.DRY_RUN", True)
    monkeypatch.setattr("config.DRY_RUN_EQUITY_USDT", 1000.0)
    analyzer = GPTAnalyzer(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    candidates = analyzer.pick_coins({"sectors": [{"name": "Bitcoin"}]})

    # BTCUSDT excluded when dry-run with small equity (<2000)
    symbols = [c["symbol"] for c in candidates]
    assert "ETHUSDT" in symbols
    assert "SOLUSDT" in symbols
    assert "BTCUSDT" not in symbols
    assert all(c["reason"].startswith("fallback candidates") for c in candidates)


def test_pick_coins_keeps_previous_candidates_when_gemini_empty(monkeypatch):
    _disable_external_state(monkeypatch)
    analyzer = GPTAnalyzer(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    analyzer.last_candidates = [
        {"symbol": "SOLUSDT", "sector": "Major Altcoins", "conviction": 2, "reason": "previous"}
    ]

    candidates = analyzer.pick_coins({"sectors": [{"name": "Bitcoin"}]})

    assert candidates == analyzer.last_candidates
    assert candidates[0]["symbol"] == "SOLUSDT"


def test_pick_coins_fallback_prefers_historical_winners(monkeypatch, tmp_path):
    _disable_external_state(monkeypatch)
    monkeypatch.setattr(gpt_analyzer.config, "ROOT_DIR", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "pnl_history.json").write_text(
        "["
        '{"symbol": "TONUSDT", "pnl": -3.0},'
        '{"symbol": "XRPUSDT", "pnl": 1.5},'
        '{"symbol": "SUIUSDT", "pnl": 0.8},'
        '{"symbol": "SOLUSDT", "pnl": -0.5}'
        "]",
        encoding="utf-8",
    )
    analyzer = GPTAnalyzer(["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "DOGEUSDT"])

    candidates = analyzer.pick_coins({"sectors": [{"name": "Bitcoin"}]})

    symbols = [c["symbol"] for c in candidates]
    assert symbols[:2] == ["XRPUSDT", "SUIUSDT"]
    assert "TONUSDT" not in symbols


def test_closed_loop_profile_ignores_expired_cooldowns(monkeypatch, tmp_path):
    _disable_external_state(monkeypatch)
    monkeypatch.setattr(gpt_analyzer.config, "ROOT_DIR", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "pnl_history.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        gpt_analyzer,
        "load_risk_state",
        lambda account="spot": {"symbol_cooldowns": {"SOLUSDT": "2024-01-01T00:00:00+00:00"}},
    )
    analyzer = GPTAnalyzer(["SOLUSDT"])

    profile = analyzer._closed_loop_profile()

    assert profile["cooldowns"] == set()
