from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("coin-bot.tradfi")

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"


class TradFiExchange:
    def __init__(self, api_key: str, private_key_path: str | Path) -> None:
        self.api_key = api_key
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = 0.2  # 5 req/s

        key_path = Path(private_key_path)
        if not key_path.is_absolute():
            from config import ROOT_DIR
            key_path = ROOT_DIR / key_path
        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    # ── 인증 ──────────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, payload: str) -> str:
        sign_str = f"{timestamp}{self.api_key}{RECV_WINDOW}{payload}"
        sig = self._private_key.sign(sign_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN-TYPE": "3",
            "Content-Type": "application/json",
        }

    def _rate_wait(self) -> None:
        with self._lock:
            elapsed = time.time() - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.time()

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        self._rate_wait()
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, query)
        req = urllib.request.Request(url, headers=self._headers(ts, sig))
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error [{path}]: {data.get('retMsg')} | {data}")
        return data

    def _post(self, path: str, body: dict[str, Any]) -> dict:
        self._rate_wait()
        body_str = json.dumps(body)
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, body_str)
        req = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=body_str.encode(),
            headers=self._headers(ts, sig),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error [{path}]: {data.get('retMsg')} | {data}")
        return data

    # ── 잔고 ───────────────────────────────────────────────────────────────────

    def get_balance_usdt(self) -> float:
        data = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        for acct in data["result"]["list"]:
            for coin in acct.get("coin", []):
                if coin["coin"] == "USDT":
                    return float(coin.get("walletBalance") or 0.0)
        return 0.0

    # ── 시세 ───────────────────────────────────────────────────────────────────

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        data = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = data["result"]["list"]
        if not items:
            raise RuntimeError(f"No ticker for {symbol}")
        t = items[0]
        return {
            "symbol": symbol,
            "last": float(t.get("lastPrice") or t.get("last") or 0),
            "bid": float(t.get("bid1Price") or 0),
            "ask": float(t.get("ask1Price") or 0),
            "volume": float(t.get("volume24h") or 0),
        }

    def fetch_ohlcv(self, symbol: str, interval: str = "60", limit: int = 200) -> pd.DataFrame:
        """interval: '1','5','15','60','240','D'"""
        data = self._get("/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        rows = data["result"]["list"]
        if not rows:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df = df.astype({"ts": float, "open": float, "high": float, "low": float, "close": float, "volume": float, "turnover": float})
        df["ts"] = df["ts"] / 1000
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df = df.iloc[:-1]  # 진행 중인 캔들 제거
        return df

    # ── 심볼 메타 ──────────────────────────────────────────────────────────────

    def symbol_meta(self, symbol: str) -> dict[str, float]:
        data = self._get("/v5/market/instruments-info", {
            "category": "linear",
            "symbol": symbol,
        })
        items = data["result"]["list"]
        if not items:
            return {"min_qty": 0.0, "lot_step": 0.01, "min_notional": 1.0, "tick_size": 0.01}
        info = items[0]
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        return {
            "min_qty": float(lot.get("minOrderQty") or 0.0),
            "lot_step": float(lot.get("qtyStep") or 0.01),
            "min_notional": float(lot.get("minNotionalValue") or lot.get("minOrderAmt") or 1.0),
            "tick_size": float(price.get("tickSize") or 0.01),
        }

    # ── 주문 ───────────────────────────────────────────────────────────────────

    def create_market_buy(self, symbol: str, usdt_amount: float, meta: dict | None = None) -> dict[str, Any]:
        if meta is None:
            meta = self.symbol_meta(symbol)
        ticker = self.fetch_ticker(symbol)
        price = ticker["last"]
        if price <= 0:
            raise RuntimeError(f"Invalid price for {symbol}: {price}")
        qty = usdt_amount / price
        qty = max(meta["min_qty"], round(qty / meta["lot_step"]) * meta["lot_step"])
        qty = round(qty, 8)
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": str(qty),
        }
        logger.info("BUY %s qty=%.6f usdt=%.2f", symbol, qty, usdt_amount)
        return self._post("/v5/order/create", body)

    def create_market_sell(self, symbol: str, qty: float) -> dict[str, Any]:
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(round(qty, 8)),
        }
        logger.info("SELL %s qty=%.6f", symbol, qty)
        return self._post("/v5/order/create", body)

    # ── 시장 시간 ──────────────────────────────────────────────────────────────

    def is_market_open(self, symbol: str, symbol_type: str = "commodity") -> bool:
        """원자재: 월-금 항상 오픈. 주식: NYSE 장시간(KST) 확인."""
        if symbol_type == "commodity":
            now = datetime.now(timezone.utc)
            return now.weekday() < 5  # 월-금

        # 주식: NYSE KST 기준 23:30~06:00 (서머타임 자동 감지)
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore

        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if now_kst.weekday() >= 5:
            return False

        # 서머타임 여부 감지: 3월 둘째 일요일 ~ 11월 첫째 일요일 (미국 기준)
        month = now_kst.month
        is_dst = 3 <= month <= 10 or (month == 11 and now_kst.day <= 7)
        open_h, open_m = (22, 30) if is_dst else (23, 30)
        close_h = 5 if is_dst else 6

        h, m = now_kst.hour, now_kst.minute
        after_open = h > open_h or (h == open_h and m >= open_m)
        before_close = h < close_h or (h == close_h and m == 0)
        crosses_midnight = open_h > close_h

        if crosses_midnight:
            return after_open or before_close
        return after_open and before_close
