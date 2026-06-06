"""Test that TradFi bot persists risk state across launchd restarts.

The tradfi bot is restarted by macOS launchd every ~5 minutes via SIGTERM.
Risk state (cooldowns, daily PnL, DCA state) must survive restarts or
risk controls are reset on every cycle.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
from infra import state as st
from trading.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def _backup_risk_file():
    """Backup and restore the tradfi risk state file to avoid side effects."""
    orig = st.RISK_STATE_FILE_TRADFI
    backup = None
    if orig.exists():
        backup = orig.read_text(encoding="utf-8")
        orig.unlink()
    yield
    orig.unlink(missing_ok=True)
    if backup is not None:
        orig.write_text(backup, encoding="utf-8")


def _risk_file() -> Path:
    return st.RISK_STATE_FILE_TRADFI


def test_load_risk_state_creates_file_if_missing():
    """load_risk_state should create the file on first load for tradfi account."""
    assert not _risk_file().exists(), "Precondition: file should not exist"
    state = st.load_risk_state("tradfi")
    assert _risk_file().exists(), "load_risk_state should create the risk state file"
    assert isinstance(state, dict)
    assert "day" in state
    assert "daily_realized_pnl" in state


def test_risk_state_persists_across_risk_manager_reinit():
    """Simulate a restart: RiskManager re-init should load the same state."""
    # First init — set a cooldown
    rm1 = RiskManager(account="tradfi")
    rm1.state["symbol_cooldowns"]["TESTUSDT"] = "2099-01-01T00:00:00+00:00"
    rm1._save()

    # Second init (= bot restart)
    rm2 = RiskManager(account="tradfi")
    assert rm2.in_symbol_cooldown("TESTUSDT"), (
        "Symbol cooldown must survive RiskManager re-init (simulated restart)"
    )


def test_risk_state_survives_clean_risk_state_reload():
    """Direct state reload after saving should reflect the persisted state."""
    rm = RiskManager(account="tradfi")
    rm.state["daily_realized_pnl"] = 12.34
    rm.state["consecutive_losses"] = 2
    rm._save()

    loaded = st.load_risk_state("tradfi")
    assert loaded["daily_realized_pnl"] == 12.34
    assert loaded["consecutive_losses"] == 2


def test_global_cooldown_persists():
    """Global cooldown set by RiskManager must survive reload."""
    rm = RiskManager(account="tradfi")
    from datetime import timedelta
    rm.state["global_cooldown_until"] = (
        datetime.now(timezone.utc) + timedelta(hours=6)
    ).isoformat()
    rm._save()

    rm2 = RiskManager(account="tradfi")
    ok, reason = rm2.can_trade(100.0)
    assert not ok
    assert "global cooldown" in reason


def test_roll_day_does_not_reset_risk_file():
    """_roll_day should not delete the file or reset cooldowns on same day."""
    rm = RiskManager(account="tradfi")
    rm.state["symbol_cooldowns"]["XAUUSDT"] = "2099-06-01T00:00:00+00:00"
    rm._save()

    rm2 = RiskManager(account="tradfi")  # triggers _roll_day
    assert "XAUUSDT" in rm2.state.get("symbol_cooldowns", {})


def test_default_values_sensible():
    """Check default risk state has reasonable initial values."""
    state = st.load_risk_state("tradfi")
    assert state["daily_realized_pnl"] == 0.0
    assert state["consecutive_losses"] == 0
    assert state["global_cooldown_until"] is None
    assert state["symbol_cooldowns"] == {}
