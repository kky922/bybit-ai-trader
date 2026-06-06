from __future__ import annotations

import threading
import time
from typing import Any

import ccxt
import pandas as pd

import config


class BybitRateLimiter:
    def __init__(self, max_per_second: float = 5.0) -> None:
        self.min_interval = 1.0 / max(0.1, float(max_per_second))
        self.last_call_time = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self.last_call_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_call_time = time.time()


class BybitExchange:
    def __init__(self) -> None:
        self.rate_limiter = BybitRateLimiter()
        self.exchange = ccxt.bybit(
            {
                "apiKey": config.BYBIT_API_KEY,
                "secret": config.BYBIT_API_SECRET,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
                "timeout": 10000,
            }
        )
        self.exchange.load_markets()

    def _symbol(self, symbol: str) -> str:
        return symbol if "/" in symbol else f"{symbol[:-4]}/USDT"

    def _call(self, func, *args, **kwargs):
        self.rate_limiter.wait()
        return func(*args, **kwargs)

    def get_balance_usdt(self) -> float:
        """Return total equity in USDT (sum of all coin USD values).

        ⚠️ Changed from free USDT to total equity (2026-05-22).
        Before: returned only free USDT from the wallet (e.g., 30.87).
        This caused position_sizer to compute size=0 for expensive coins
        (ETH, BNB, SOL) when positions were open, because MAX_EQUITY_PCT
        was applied against the reduced free balance.

        Now: returns totalEquity from the API (e.g., 75.13) which includes
        the USD value of all held coins. This correctly sizes new positions
        against the full portfolio value.
        """
        if config.DRY_RUN and config.DRY_RUN_EQUITY_USDT > 0:
            return config.DRY_RUN_EQUITY_USDT
        bal = self._call(self.exchange.fetch_balance, {"type": "unified"})
        info = bal.get("info", {})
        result = info.get("result", {}) if isinstance(info, dict) else {}
        accounts = result.get("list", []) if isinstance(result, dict) else []
        if accounts:
            account = accounts[0]
            total_equity = account.get("totalEquity")
            if total_equity is not None:
                return float(total_equity)
            coins = account.get("coin", [])
            if coins:
                return sum(float(c.get("usdValue", 0)) for c in coins)
        # Last resort: free USDT (old behavior)
        return float((bal.get("free") or {}).get("USDT") or 0.0)

    def fetch_balance_spot(self, coin: str) -> float:
        bal = self._call(self.exchange.fetch_balance, {"type": "unified"})
        return float((bal.get("free") or {}).get(coin.upper()) or 0.0)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        rows = self._call(self.exchange.fetch_ohlcv, self._symbol(symbol), timeframe, None, limit)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        # Drop in-progress candle: ccxt fetch_ohlcv includes the currently forming bar
        # as the last row. Using it gives partial volume/close and silently breaks signals.
        if not df.empty:
            tf_ms = int(self.exchange.parse_timeframe(timeframe) * 1000)
            now_ms = int(self.exchange.milliseconds())
            last_close_ms = int(df["ts"].iloc[-1]) + tf_ms
            if now_ms < last_close_ms:
                df = df.iloc[:-1].reset_index(drop=True)
        return df

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self._call(self.exchange.fetch_ticker, self._symbol(symbol))

    def create_stop_loss_order(self, symbol: str, base_amount: float, stop_price: float) -> dict[str, Any]:
        """Place a stop-limit sell order on Bybit UTA for exchange-level protection.

        ⚠️ UTA spot does NOT support triggerDirection. Removing it fixes error 170130.
        The direction is implicit from side (sell + triggerPrice below market = stop-loss).
        """
        self.rate_limiter.wait()
        return self.exchange.create_order(
            self._symbol(symbol),
            "limit",
            "sell",
            base_amount,
            stop_price * 0.99,
            {
                "triggerPrice": stop_price,
                "triggerBy": "last",
                "category": "spot",
                "positionIdx": 0,
            },
        )

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self.rate_limiter.wait()
        return self.exchange.cancel_order(order_id, self._symbol(symbol))

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self.rate_limiter.wait()
        return self.exchange.fetch_open_orders(symbol if symbol else None)

    def create_market_buy(self, symbol: str, usdt_amount: float) -> dict[str, Any]:
        """UTA-compatible market buy using quoteCoin marketUnit."""
        self.rate_limiter.wait()
        return self.exchange.create_order(
            self._symbol(symbol),
            "market",
            "buy",
            usdt_amount,
            None,
            {"marketUnit": "quoteCoin", "category": "spot"},
        )

    def create_market_sell(self, symbol: str, base_amount: float) -> dict[str, Any]:
        return self._call(
            self.exchange.create_order,
            self._symbol(symbol),
            "market",
            "sell",
            base_amount,
            None,
            {"category": "spot"},
        )

    @staticmethod
    def _precision_to_step(precision_val: int | float | None, default: int = 6) -> float:
        """Convert ccxt precision value to lot/tick step size.

        ccxt returns precision differently per exchange:
        - Integer (e.g. 4): number of decimal places → step = 10^-4 = 0.0001
        - Float < 1 (e.g. 0.0001): actual step size directly → use as-is
        - None/missing: use default decimal places → step = 10^-default
        """
        if precision_val is None:
            return float(10 ** -default)
        prec = float(precision_val)
        if prec < 1.0:
            # Already a decimal step size (Bybit-style)
            return prec
        # Number of decimal places (older exchange pattern)
        return float(10 ** -int(prec))

    def symbol_meta(self, symbol: str) -> dict[str, float]:
        market = self.exchange.markets[self._symbol(symbol)]
        limits = market.get("limits", {})
        precision = market.get("precision", {})
        return {
            "min_amt": float((limits.get("amount") or {}).get("min") or 0.0),
            "min_notional": float((limits.get("cost") or {}).get("min") or config.MIN_NOTIONAL_USDT_DEFAULT),
            "lot_step": self._precision_to_step(precision.get("amount"), 6),
            "tick_size": self._precision_to_step(precision.get("price"), 4),
        }
