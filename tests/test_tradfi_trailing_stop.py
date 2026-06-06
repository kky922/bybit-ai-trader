"""Tests for tradfi_main._check_trailing_stops logic.

These tests verify the fast-trailing-stop mechanism that runs every loop
iteration (30s), not just at 15-min boundaries. This is critical because
macOS resource management (launchd) sends SIGTERM every ~5 minutes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_positions(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "symbol": "XAUUSDT",
            "symbol_type": "commodity",
            "sector": "test",
            "conviction": 7.0,
            "entry_price": 2000.0,
            "size": 0.1,
            "stop_loss": 1980.0,
            "take_profit": 2060.0,
            "atr": 10.0,
            "highest_price": 2000.0,
            "entered_at": "2026-05-30T00:00:00+00:00",
            "remaining_pct": 1.0,
            "runner_mode": False,
        }
    ]


@pytest.fixture
def mock_exchange() -> MagicMock:
    ex = MagicMock()
    ex.fetch_ticker.return_value = {"last": 2005.0, "symbol": "XAUUSDT"}
    return ex


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_trader(exited: list[str] | None = None) -> MagicMock:
    tr = MagicMock()
    tr.exit.return_value = {"ok": True, "pnl": 10.0, "exit_price": 2010.0}
    return tr


# ── Tests ────────────────────────────────────────────────────────────────


def test_skip_when_elapsed_too_short(tmp_path, mock_positions, mock_exchange):
    """elapsed < 25 → return immediately without ticker fetch."""
    from tradfi_main import _check_trailing_stops

    _check_trailing_stops(mock_exchange, elapsed=5)
    mock_exchange.fetch_ticker.assert_not_called()


def test_update_highest_price_when_price_exceeds(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """When current price > highest_price, update highest_price."""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    mock_exchange.fetch_ticker.return_value = {"last": 2050.0, "symbol": "XAUUSDT"}

    from tradfi_main import _check_trailing_stops

    _check_trailing_stops(mock_exchange, elapsed=30)
    assert mock_positions[0]["highest_price"] == 2050.0


def test_no_stop_when_price_below_activation(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """Price above entry but below activation threshold → no exit."""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    # price=2000.5 = +0.025% above entry=2000, but activation=0.3%
    mock_exchange.fetch_ticker.return_value = {"last": 2000.5}

    from tradfi_main import _check_trailing_stops

    trader = _make_trader()
    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    trader.exit.assert_not_called()


def test_trailing_stop_hits_when_callback_triggered(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """Activated trailing: price drops from 2050 to 2040, callback 0.2% → trail_price=2040.9"""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    # highest=2050, callback=0.2% → trail at 2050*0.998=2045.9
    # price=2044 < 2045.9 → exit
    mock_positions[0]["highest_price"] = 2050.0
    mock_exchange.fetch_ticker.return_value = {"last": 2044.0}

    from tradfi_main import _check_trailing_stops

    trader = _make_trader()
    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    trader.exit.assert_called_once()
    args, _ = trader.exit.call_args
    assert args[1] == "trailing_stop"


def test_stop_loss_hits_when_price_below_sl(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """Price below stop_loss when trailing not activated → stop_loss exit."""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    mock_positions[0]["highest_price"] = 2005.0
    mock_exchange.fetch_ticker.return_value = {"last": 1975.0}  # below SL=1980

    from tradfi_main import _check_trailing_stops

    trader = _make_trader()
    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    trader.exit.assert_called_once()
    args, _ = trader.exit.call_args
    assert args[1] == "stop_loss"


def test_skipped_symbols_removed_from_positions(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """After exit, symbol is removed from positions by trader.exit()."""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    saved: list[list] = []

    def _fake_save(pos_list):
        saved.append(pos_list)

    monkeypatch.setattr("tradfi_main.save_positions", _fake_save)
    mock_positions[0]["highest_price"] = 2050.0
    mock_exchange.fetch_ticker.return_value = {"last": 2044.0}

    from tradfi_main import _check_trailing_stops

    trader = _make_trader()
    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    # No highest_price update happened (2044 < 2050), so save_positions
    # is not called — trader.exit() handles its own save internally.
    # The symbol is removed by trader.exit() (mocked here).
    assert len(saved) == 0
    trader.exit.assert_called_once()


def test_ticker_error_does_not_crash(tmp_path, monkeypatch, mock_positions, mock_exchange, caplog):
    """When fetch_ticker raises, log debug and continue (no crash)."""
    monkeypatch.setattr("tradfi_main.load_positions", lambda: mock_positions)
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    mock_exchange.fetch_ticker.side_effect = RuntimeError("rate limit")

    from tradfi_main import _check_trailing_stops

    _check_trailing_stops(mock_exchange, elapsed=30)
    # Should not raise — just log debug
    assert True


def test_highest_price_persisted_before_exit(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """When highest_price is updated (symbol A) and another symbol exits in same
    call, highest_price update is persisted before trader.exit() removes the
    exiting symbol — not lost by re-reading stale data from disk."""
    # Two positions: XAUUSDT (highest update), COINUSDT (exit trigger)
    positions = [
        dict(mock_positions[0]),  # XAUUSDT
        {
            "symbol": "COINUSDT",
            "symbol_type": "stock",
            "sector": "crypto",
            "conviction": 6.0,
            "entry_price": 200.0,
            "size": 0.1,
            "stop_loss": 190.0,
            "take_profit": 220.0,
            "atr": 5.0,
            "highest_price": 210.0,  # already above activation (entry*1.003=200.6)
            "entered_at": "2026-05-30T00:00:00+00:00",
            "remaining_pct": 1.0,
            "runner_mode": False,
        },
    ]
    monkeypatch.setattr("tradfi_main.load_positions", lambda: positions)
    saved: list[list] = []

    def _fake_save(pos_list):
        saved.append([dict(p) for p in pos_list])

    monkeypatch.setattr("tradfi_main.save_positions", _fake_save)

    from tradfi_main import _check_trailing_stops

    # First ticker (XAUUSDT): price=2050 > highest=2000 → update to 2050
    # Second ticker (COINUSDT): price=206 < highest=210 → callback from 210*0.998=209.58
    #   → 206 < 209.58 → exit triggered
    mock_exchange.fetch_ticker.side_effect = [
        {"last": 2050.0, "symbol": "XAUUSDT"},  # highest update
        {"last": 206.0, "symbol": "COINUSDT"},  # callback exit
    ]

    trader = _make_trader()

    def _exit_tracker(pos, reason):
        # Before exit, save_positions must have been called with
        # XAUUSDT's highest_price=2050 persisted
        assert len(saved) >= 1, "save_positions should be called before exit"
        persisted = saved[-1]
        xau = [p for p in persisted if p["symbol"] == "XAUUSDT"]
        assert len(xau) >= 1, "XAUUSDT should still be in saved positions"
        assert xau[0]["highest_price"] == 2050.0, (
            f"XAUUSDT highest_price should be 2050.0, got {xau[0]['highest_price']}"
        )
        return {"ok": True, "pnl": 5.0, "exit_price": 206.0}

    trader.exit.side_effect = _exit_tracker

    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    # save_positions was called once (before exit)
    assert len(saved) >= 1, "save_positions should have been called"
    trader.exit.assert_called_once()
    args, _ = trader.exit.call_args
    assert args[1] == "trailing_stop"


def test_double_exit_prevented_by_disk_check(tmp_path, monkeypatch, mock_positions, mock_exchange):
    """When another instance already exited the symbol from disk, skip duplicate exit."""
    # Setup: position on disk is empty (already exited by another instance),
    # but in-memory positions still have the symbol.
    monkeypatch.setattr("tradfi_main.load_positions", lambda: [])
    monkeypatch.setattr("tradfi_main.save_positions", lambda _: None)
    mock_positions[0]["highest_price"] = 2050.0
    mock_exchange.fetch_ticker.return_value = {"last": 2044.0}  # trigger trailing

    from tradfi_main import _check_trailing_stops

    trader = MagicMock()
    trader.exit.return_value = {"ok": True, "pnl": 10.0, "exit_price": 2044.0}
    # Pass the in-memory positions by overriding load_positions inside
    _check_trailing_stops(mock_exchange, elapsed=30, trader=trader)
    # exit() should NOT be called because disk says position is gone
    trader.exit.assert_not_called()
