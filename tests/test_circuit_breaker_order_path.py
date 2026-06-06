from __future__ import annotations

import sys
import types
from typing import Any, cast

# The order-path tests do not instantiate BybitExchange, but importing
# trading.trader imports trading.exchange, whose optional runtime dependency
# ccxt is not installed in the lightweight test environment.
ccxt_stub = types.ModuleType("ccxt")
ccxt_stub.bybit = lambda *args, **kwargs: None  # type: ignore[attr-defined]
sys.modules.setdefault("ccxt", ccxt_stub)

from analysis.entry_signal import EntrySignal
from analysis.tradfi_entry_signal import TradFiEntrySignal
from trading.risk_manager import RiskManager
from trading.trader import Trader
from tradfi_main import TradFiTrader

import circuit_breaker
import config


class DummyCoinExchange:
    def __init__(self) -> None:
        self.market_buy_calls = 0
        self.market_sell_calls = 0

    def get_balance_usdt(self) -> float:
        return 1_000.0

    def symbol_meta(self, symbol: str) -> dict:
        return {"lot_step": 0.001, "min_amt": 0.001, "min_notional": 5.0}

    def create_market_buy(self, symbol: str, usdt_amount: float) -> dict:
        self.market_buy_calls += 1
        return {"id": "buy"}

    def create_stop_loss_order(self, symbol: str, size: float, stop_loss: float) -> dict:
        return {"id": "sl"}

    def create_market_sell(self, symbol: str, size: float) -> dict:
        self.market_sell_calls += 1
        return {"id": "sell"}


class DummyTradFiExchange:
    def __init__(self) -> None:
        self.market_buy_calls = 0
        self.market_sell_calls = 0

    def get_balance_usdt(self) -> float:
        return 1_000.0

    def symbol_meta(self, symbol: str) -> dict:
        return {"lot_step": 0.01, "min_qty": 0.01, "min_notional": 5.0}

    def create_market_buy(self, symbol: str, usdt_amount: float, meta: dict | None = None) -> dict:
        self.market_buy_calls += 1
        return {"id": "tradfi-buy"}

    def create_market_sell(self, symbol: str, size: float) -> dict:
        self.market_sell_calls += 1
        return {"id": "tradfi-sell"}

    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": 100.0}


def _isolate_circuit_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(circuit_breaker.config, "ROOT_DIR", tmp_path)


def test_coin_entry_is_blocked_before_market_buy_when_breaker_tripped(monkeypatch, tmp_path):
    _isolate_circuit_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "DRY_RUN", False)

    exchange = DummyCoinExchange()
    trader = Trader(cast(Any, exchange), RiskManager(account="coin"))
    trader.breaker.trip("test_manual_halt")

    result = trader.enter(
        "BTCUSDT",
        EntrySignal(symbol="BTCUSDT", entry_price=100.0, stop_loss=90.0, take_profit=120.0, atr=5.0),
        sector="test",
        conviction=7.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "circuit_breaker:test_manual_halt"
    assert exchange.market_buy_calls == 0


def test_tradfi_entry_is_blocked_before_market_buy_when_breaker_tripped(monkeypatch, tmp_path):
    _isolate_circuit_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "TRADFI_DRY_RUN", False)

    exchange = DummyTradFiExchange()
    trader = TradFiTrader(cast(Any, exchange))
    trader.breaker.trip("test_manual_halt")

    result = trader.enter(
        TradFiEntrySignal(
            symbol="XAUUSD",
            symbol_type="commodity",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=112.0,
            atr=2.0,
        ),
        sector="commodity",
        conviction=7.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "circuit_breaker:test_manual_halt"
    assert exchange.market_buy_calls == 0


def test_circuit_breaker_counts_api_errors_and_blocks_entries(tmp_path):
    breaker = circuit_breaker.CircuitBreaker(state_path=tmp_path / "circuit_state.json", api_failure_stop=2)

    assert breaker.check("BTCUSDT").allowed is True
    breaker.record_api_error("first")
    assert breaker.check("BTCUSDT").allowed is True
    breaker.record_api_error("second")

    decision = breaker.check("BTCUSDT").to_dict()
    assert decision["allowed"] is False
    assert decision["reason"] == "api_failure_stop"
    assert decision["details"]["count"] == 2
