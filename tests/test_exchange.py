from __future__ import annotations

from trading.exchange import BybitExchange


class _FakeBybit:
    def __init__(self) -> None:
        self.load_markets_called = False

    def load_markets(self) -> None:
        self.load_markets_called = True


def test_bybit_exchange_sets_explicit_network_timeout(monkeypatch):
    captured = {}
    fake = _FakeBybit()

    def factory(config):
        captured.update(config)
        return fake

    monkeypatch.setattr("trading.exchange.ccxt.bybit", factory)

    exchange = BybitExchange()

    assert exchange.exchange is fake
    assert fake.load_markets_called is True
    assert captured["enableRateLimit"] is True
    assert captured["timeout"] == 10000
    assert captured["options"] == {"defaultType": "spot"}


def test_precision_to_step() -> None:
    # Integer decimal places (older pattern)
    assert BybitExchange._precision_to_step(4, 6) == 0.0001
    assert BybitExchange._precision_to_step(2, 6) == 0.01
    assert BybitExchange._precision_to_step(8, 6) == 1e-08

    # Float step size directly (Bybit pattern)
    assert BybitExchange._precision_to_step(0.0001, 6) == 0.0001
    assert BybitExchange._precision_to_step(1e-06, 6) == 1e-06
    assert BybitExchange._precision_to_step(0.01, 6) == 0.01
    assert BybitExchange._precision_to_step(0.001, 6) == 0.001

    # None / missing
    assert BybitExchange._precision_to_step(None, 6) == 1e-06
    assert BybitExchange._precision_to_step(None, 4) == 0.0001

    # Integer but passed as float
    assert BybitExchange._precision_to_step(4.0, 6) == 0.0001