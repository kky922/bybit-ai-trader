from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_health_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "coin_bot_autopilot_health.py"
    spec = importlib.util.spec_from_file_location("coin_bot_autopilot_health", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_trades_reports_recent_performance_and_repeat_losers():
    health = load_health_module()
    trades = [
        {"ts": "2026-05-01T00:00:00+00:00", "symbol": "AAAUSDT", "pnl": -1.0, "reason": "narrative_faded"},
        {"ts": "2026-05-01T01:00:00+00:00", "symbol": "AAAUSDT", "pnl": -2.0, "reason": "narrative_faded"},
        {"ts": "2026-05-01T02:00:00+00:00", "symbol": "BBBUSDT", "pnl": 1.0, "reason": "take_profit"},
    ]

    summary = health._summarize_trades(trades, recent_count=3)

    assert summary["trade_count"] == 3
    assert summary["total_pnl"] == -2.0
    assert summary["win_rate_pct"] == 33.3
    assert summary["recent_win_rate_pct"] == 33.3
    assert summary["exit_reasons"] == {"narrative_faded": 2, "take_profit": 1}
    assert summary["repeat_losers"][0]["symbol"] == "AAAUSDT"
    assert summary["repeat_losers"][0]["losses"] == 2


def test_should_emit_only_when_signature_changes_or_reset_window_passes():
    health = load_health_module()
    state = {"last_signature": {"a": 1}, "last_sent_at": 1000}

    assert health._should_emit({"a": 2}, state=state, now_ts=1010, silent_reset_seconds=3600)
    assert not health._should_emit({"a": 1}, state=state, now_ts=1200, silent_reset_seconds=3600)
    assert health._should_emit({"a": 1}, state=state, now_ts=5000, silent_reset_seconds=3600)


def test_append_history_keeps_time_series_for_live_readiness(tmp_path, monkeypatch):
    health = load_health_module()
    history_file = tmp_path / "autopilot_history.jsonl"
    monkeypatch.setattr(health, "HISTORY_FILE", history_file)

    health._append_history({"generated_at": "2026-05-01T00:00:00+00:00", "health": "ok"})
    health._append_history({"generated_at": "2026-05-01T00:10:00+00:00", "health": "warn"})

    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["health"] == "ok"
    assert json.loads(lines[1])["health"] == "warn"


def test_build_report_flags_stale_pid_and_negative_recent_performance(tmp_path, monkeypatch):
    health = load_health_module()
    root = tmp_path
    logs = root / "logs"
    data_agents = root / "data" / "agents"
    logs.mkdir(parents=True)
    data_agents.mkdir(parents=True)

    (logs / "coin_bot.pid").write_text("999999", encoding="utf-8")
    (logs / "coin_bot.log").write_text("2026-05-01 00:00:00 [ERROR] Gemini call failed\n", encoding="utf-8")
    (logs / "event_log.json").write_text(json.dumps([
        {"date": "2026-05-01", "time": "00:00:00", "type": "risk_block", "detail": "global cooldown active"},
        {"date": "2026-05-01", "time": "01:00:00", "type": "entry_skip", "msg": "AAA rejected", "detail": "reason=volume_ratio_low"},
    ]), encoding="utf-8")
    (logs / "pnl_history.json").write_text(json.dumps([
        {"ts": "2026-05-01T00:00:00+00:00", "symbol": "AAAUSDT", "pnl": -1.0, "reason": "narrative_faded"},
        {"ts": "2026-05-01T01:00:00+00:00", "symbol": "AAAUSDT", "pnl": -2.0, "reason": "narrative_faded"},
    ]), encoding="utf-8")
    (logs / "risk_state.json").write_text(json.dumps({"consecutive_losses": 2}), encoding="utf-8")
    (logs / "positions.json").write_text("[]", encoding="utf-8")
    (logs / "latest_ai.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")

    monkeypatch.setattr(health, "ROOT", root)
    monkeypatch.setattr(health, "LOGS", logs)
    monkeypatch.setattr(health, "DATA", data_agents)
    monkeypatch.setattr(health, "OUTFILE", data_agents / "autopilot_latest.json")
    monkeypatch.setattr(health, "STATE_FILE", data_agents / "autopilot_state.json")
    monkeypatch.setattr(health, "_launchctl_pid", lambda: (False, None))

    report = health._build_report()

    assert report["health"] == "critical"
    assert report["signals"]["coin_alive"] is False
    assert any("코인봇 프로세스" in issue for issue in report["issues"])
    assert any("최근" in issue and "승률" in issue for issue in report["issues"])
    assert any("후보" in issue for issue in report["issues"])


def test_count_patterns_does_not_flag_normal_lock_acquisition_logs():
    health = load_health_module()

    lines = [
        "2026-05-14 13:36:50 [INFO] coin-bot: single-instance lock acquired: <project-root>/logs/coin_bot.lock (pid=52772)",
        "2026-05-14 13:36:52 [INFO] coin-bot: PID file created: <project-root>/logs/coin_bot.pid (pid=52790)",
    ]

    counts = health._count_patterns(lines)

    assert counts["lock_stale"] == 0
