from __future__ import annotations

import atexit
import fcntl
import logging
import math
import os
import signal
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

import config
from hot_reload import HotReloader
from analysis.exit_signal import check_dca_trigger, check_exit, check_partial_tp, detect_parabolic
from analysis.tradfi_entry_signal import TradFiEntrySignal, check_tradfi_entry
from data.news_collector import NewsCollector
from data.tradfi_universe import get_tradfi_symbols
from infra.context_from import check_tier2_trigger
from infra.event_log import log_event
from infra.state import (
    _pnl_file,
    _positions_file,
    _risk_state_file,
    _read_json,
    _write_json,
    load_risk_state,
    save_latest_ai,
    append_news_snapshot,
)
from infra.telegram import CoinTelegram
from circuit_breaker import CircuitBreaker
from trading.position_sizer import round_to_lot
from trading.risk_manager import RiskManager
from trading.tradfi_exchange import TradFiExchange

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            config.LOGS_DIR / "tradfi_bot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("tradfi-bot")

PID_FILE = config.LOGS_DIR / "tradfi_bot.pid"
LOCK_FILE = config.LOGS_DIR / "tradfi_bot.lock"
HEARTBEAT_FILE = config.LOGS_DIR / "tradfi_bot.heartbeat"
RESTART_COUNT_FILE = config.LOGS_DIR / "tradfi_bot.restart_count"
SIGTERM_CLEAN_FILE = config.LOGS_DIR / "tradfi_bot.sigterm_clean"
_LOCK_HANDLE = None

ACCOUNT = "tradfi"


def _load_restart_count() -> int:
    """Load and increment crash restart counter. Resets after 1 stable hour."""
    try:
        if RESTART_COUNT_FILE.exists():
            val = int(RESTART_COUNT_FILE.read_text().strip())
            return val + 1
    except (ValueError, OSError):
        pass
    return 1


def _save_restart_count(count: int) -> None:
    try:
        RESTART_COUNT_FILE.write_text(str(count))
    except OSError:
        pass


def _check_crash_loop() -> None:
    """Block startup if crash-looping excessively (10+ restarts in the last hour).

    Prevents runaway API cost from crash-restart->GPT-call cycles.
    The lock from the previous instance is released on crash (launchd killed it),
    so fcntl won't save us — we need this escalation blocker.

    If the previous instance exited via SIGTERM (clean shutdown), the restart
    is not counted as a crash — launchd may be cycling the process for resource
    management, not because the bot crashed.
    """
    # If the previous exit was a clean SIGTERM shutdown, reset the crash counter
    # so we don't falsely trigger crash-loop protection
    sigterm_ts = None
    try:
        if SIGTERM_CLEAN_FILE.exists():
            sigterm_ts = float(SIGTERM_CLEAN_FILE.read_text().strip())
            SIGTERM_CLEAN_FILE.unlink()
    except (ValueError, OSError):
        SIGTERM_CLEAN_FILE.unlink(missing_ok=True)

    count = _load_restart_count()
    ts_file = config.LOGS_DIR / "tradfi_bot.first_restart"
    now = time.time()

    # If the last exit was a clean SIGTERM, this is not a crash — reset the counter
    # so launchd cycling doesn't trigger false crash-loop protection
    if sigterm_ts is not None and count > 1:
        logger.info(
            "Previous exit was clean SIGTERM (%.1fs ago). Resetting restart "
            "counter to avoid false crash-loop detection.",
            now - sigterm_ts,
        )
        _save_restart_count(0)
        ts_file.unlink(missing_ok=True)
        count = 0

    if count >= 10:
        # Get first restart timestamp
        try:
            first_ts = float(ts_file.read_text().strip())
        except (ValueError, OSError):
            first_ts = now

        elapsed = now - first_ts
        if elapsed < 3600:  # 10+ restarts in 1 hour = crash loop
            logger.critical(
                "CRASH LOOP DETECTED: %d restarts in %.0fs. "
                "Blocking startup permanently until next launchd cycle.",
                count, elapsed,
            )
            # Block startup — launchd will retry after ThrottleInterval (60s)
            # This prevents runaway API cost from crash-restart->GPT-call cycles
            time.sleep(120)
            _save_restart_count(count - 5)
            raise SystemExit(1)
        # Elapsed > 1h — this is a new cycle, reset
        _save_restart_count(0)
        ts_file.unlink(missing_ok=True)
        logger.info("Crash loop expired (%.0fs since first restart). Counter reset.", elapsed)
        return

    # Set first_restart if this is the first increment in a new cycle
    if count == 1 and not ts_file.exists():
        ts_file.write_text(str(now))

    # Under threshold — carry on
    _save_restart_count(count)


def _write_heartbeat() -> None:
    """Write current timestamp as heartbeat. Read by health checks."""
    try:
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def _acquire_single_instance() -> None:
    global _LOCK_HANDLE
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("TradFi bot is already running")
        raise SystemExit(0)
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    os.fsync(lock_handle.fileno())
    _LOCK_HANDLE = lock_handle


def _release_single_instance() -> None:
    global _LOCK_HANDLE
    if _LOCK_HANDLE:
        try:
            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
            _LOCK_HANDLE.close()
        except Exception:
            pass
        _LOCK_HANDLE = None


atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
atexit.register(_release_single_instance)


# ── 상태 관리 (tradfi 전용 파일) ───────────────────────────────────────────────

def load_positions() -> list[dict]:
    return _read_json(_positions_file(ACCOUNT), [])


def save_positions(positions: list[dict]) -> None:
    _write_json(_positions_file(ACCOUNT), positions)


def add_pnl_record(record: dict) -> None:
    pnl_file = _pnl_file(ACCOUNT)
    data = _read_json(pnl_file, [])
    key_fields = {"symbol", "entry_price", "exit_price", "pnl", "reason"}
    rec_key = {k: record.get(k) for k in key_fields}
    for existing in data:
        if {k: existing.get(k) for k in key_fields} == rec_key:
            return
    # Secondary dedup: same symbol + entry_price + reason within 60s →
    # likely a race-condition duplicate even if exit_price/pnl differ slightly
    rec_symbol = str(record.get("symbol", "")).upper().strip()
    rec_entry = record.get("entry_price")
    rec_reason = str(record.get("reason", ""))
    ts = record.get("ts")
    # Only dedup if both records have ts
    if ts:
        try:
            rec_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            rec_ts = None
        if rec_ts is not None and rec_symbol and rec_entry and rec_reason:
            for existing in data:
                ext = existing.get("ts")
                if not ext:
                    continue
                try:
                    ext_ts = datetime.fromisoformat(ext.replace("Z", "+00:00"))
                except (ValueError, TypeError, AttributeError):
                    continue
                diff = abs((rec_ts - ext_ts).total_seconds())
                if (diff < 60
                        and str(existing.get("symbol", "")).upper().strip() == rec_symbol
                        and existing.get("entry_price") == rec_entry
                        and str(existing.get("reason", "")) == rec_reason):
                    logger.debug(
                        "Dedup: skipped duplicate PnL record for %s (%.0fs apart, same reason=%s)",
                        rec_symbol, diff, rec_reason,
                    )
                    return
    data.append(record)
    _write_json(pnl_file, data[-5000:])


# ── GPT 분석 (TradFi 전용 프롬프트) ────────────────────────────────────────────

class TradFiGPTAnalyzer:
    def __init__(self, symbols: dict[str, dict]) -> None:
        self.symbols = symbols
        self.client = None
        api_key = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
        if api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    api_key=api_key,
                    base_url=getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                )
            except Exception as e:
                logger.warning("LLM client init failed: %s", e)

        # TradFi 전용 GPT 캐시: launchd 재시작 사이클(5분)에서 중복 API 호출 방지
        self.last_analysis: list[dict] = []
        self._cache_file = config.LOGS_DIR / "latest_ai_tradfi.json"
        self._cache_ttl = config.GPT_REFRESH_HOURS * 3600  # 기본 4h (int로 변환)

    def _chat_json(self, system: str, user: str) -> Any:
        import json, re
        if not self.client:
            return {}
        for _ in range(3):
            try:
                resp = self.client.chat.completions.create(
                    model=config.GPT_MODEL,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content or "{}"
                return json.loads(re.sub(r"```json|```", "", text).strip())
            except Exception as e:
                logger.warning("LLM call failed: %s", e)
                time.sleep(5)
        return {}

    def _closed_loop_profile(self) -> dict[str, Any]:
        pnl_path = config.ROOT_DIR / "logs" / "pnl_history_tradfi.json"
        trades = _read_json(pnl_path, [])
        stats: dict[str, dict[str, float | int]] = {}
        for trade in trades if isinstance(trades, list) else []:
            if not isinstance(trade, dict):
                continue
            symbol = str(trade.get("symbol", "")).upper().strip()
            if not symbol:
                continue
            try:
                pnl = float(trade.get("pnl", 0) or 0)
            except Exception:
                pnl = 0.0
            row = stats.setdefault(symbol, {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
            row["count"] = int(row["count"]) + 1
            row["pnl"] = float(row["pnl"]) + pnl
            if pnl > 0:
                row["wins"] = int(row["wins"]) + 1
            elif pnl < 0:
                row["losses"] = int(row["losses"]) + 1

        repeat_losers = {
            symbol for symbol, row in stats.items()
            if int(row["losses"]) >= 2 and float(row["pnl"]) <= 0
        }
        risk_state = load_risk_state(ACCOUNT)
        cooldowns = {
            str(symbol).upper().strip()
            for symbol, value in (risk_state.get("symbol_cooldowns", {}) or {}).items()
            if str(symbol).strip() and value
        }
        winners = [
            symbol for symbol, row in sorted(
                stats.items(),
                key=lambda kv: (float(kv[1]["pnl"]), int(kv[1]["wins"]), int(kv[1]["count"])),
                reverse=True,
            )
            if float(row["pnl"]) > 0
        ][:8]
        return {
            "stats": stats,
            "repeat_losers": repeat_losers,
            "cooldowns": cooldowns,
            "winners": winners,
        }

    def _rank_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        profile = self._closed_loop_profile()
        stats = profile["stats"]
        repeat_losers = profile["repeat_losers"]
        cooldowns = profile["cooldowns"]
        ranked: list[dict[str, Any]] = []
        for candidate in candidates:
            symbol = str(candidate.get("symbol", "")).upper().strip()
            if not symbol:
                continue
            row = stats.get(symbol, {})
            count = max(1, int(row.get("count", 0) or 0))
            pnl = float(row.get("pnl", 0.0) or 0.0)
            wins = int(row.get("wins", 0) or 0)
            losses = int(row.get("losses", 0) or 0)
            conviction = float(candidate.get("conviction", 0) or 0)
            history_boost = max(-4.0, min(4.0, (pnl / count) * 2.0))
            win_boost = min(2.0, wins * 0.5)
            penalty = 0.0
            if symbol in repeat_losers:
                penalty += 5.0
            if symbol in cooldowns:
                penalty += 3.0
            if losses >= 3:
                penalty += 1.5
            candidate = dict(candidate)
            candidate["closed_loop_score"] = round(conviction * 10.0 + history_boost + win_boost - penalty, 3)
            candidate["closed_loop_bias"] = {
                "history_pnl": round(pnl, 4),
                "history_count": count,
                "wins": wins,
                "losses": losses,
                "repeat_loser": symbol in repeat_losers,
                "cooldown": symbol in cooldowns,
            }
            ranked.append(candidate)
        ranked.sort(
            key=lambda c: (
                float(c.get("closed_loop_score", 0) or 0),
                float(c.get("conviction", 0) or 0),
                str(c.get("symbol", "")),
            ),
            reverse=True,
        )
        return ranked

    def _feedback_snapshot(self) -> dict[str, Any]:
        profile = self._closed_loop_profile()
        return {
            "repeat_loser_symbols": sorted(profile["repeat_losers"]),
            "cooldown_symbols": sorted(profile["cooldowns"]),
            "winner_symbols": profile["winners"],
        }

    def _load_cache(self) -> list[dict] | None:
        """Load cached GPT analysis if still fresh (< GPT_REFRESH_HOURS old)."""
        import json, time
        try:
            if self._cache_file.exists():
                payload = json.loads(self._cache_file.read_text(encoding="utf-8"))
                ts = float(payload.get("ts", 0))
                age = time.time() - ts
                if age < self._cache_ttl:
                    candidates = payload.get("candidates", [])
                    if candidates:
                        logger.info(
                            "TradFi GPT cache hit: %d candidates (%.0fs old, TTL=%.0fs)",
                            len(candidates), age, self._cache_ttl,
                        )
                        return candidates
                    logger.info("TradFi GPT cache empty, will re-analyze.")
                else:
                    logger.info(
                        "TradFi GPT cache expired: %.0fs old > %.0fs TTL",
                        age, self._cache_ttl,
                    )
        except Exception as e:
            logger.warning("TradFi GPT cache read error: %s", e)
        return None

    def _save_cache(self, candidates: list[dict]) -> None:
        """Persist GPT analysis with timestamp for cache TTL."""
        import json, time
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps({"ts": time.time(), "candidates": candidates}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("TradFi GPT cache write error: %s", e)

    def pick_candidates(self, news: list) -> list[dict]:
        """뉴스 기반으로 TradFi 거래 후보 선정.

        Uses TTL cache to avoid redundant API calls on launchd restart cycles.
        """
        # Try cache first
        cached = self._load_cache()
        if cached is not None:
            self.last_analysis = cached
            return cached
        commodity_syms = [s for s, v in self.symbols.items() if v["type"] == "commodity"]
        stock_syms = [s for s, v in self.symbols.items() if v["type"] == "stock"]

        news_text = "\n".join(
            f"- {n.title} ({n.source})" for n in news[:30]
        ) if news else "No recent news."

        system = (
            "You are a professional TradFi trader. Analyze news and select 2-4 instruments "
            "likely to move in the next 4-8 hours. Return JSON only."
        )
        user = (
            f"Available commodities: {commodity_syms[:10]}\n"
            f"Available stocks: {stock_syms[:20]}\n\n"
            f"Recent news:\n{news_text}\n\n"
            "Return JSON: {\"candidates\": [{\"symbol\": \"XAUUSD\", \"type\": \"commodity\", "
            "\"reason\": \"...\", \"conviction\": 7}]}"
        )
        result = self._chat_json(system, user)
        candidates = result.get("candidates", [])
        ranked = self._rank_candidates(candidates)
        logger.info("TradFi GPT candidates: %d", len(ranked))
        self.last_analysis = ranked
        # Cache so launchd restart cycles don't trigger redundant API calls
        if ranked:
            self._save_cache(ranked)
        return ranked


# ── 트레이더 ───────────────────────────────────────────────────────────────────

class TradFiTrader:
    def __init__(self, exchange: TradFiExchange) -> None:
        self.exchange = exchange
        self.breaker = CircuitBreaker(log_func=log_event)

    def _check_entry_allowed(self, symbol: str, *, action: str, equity: float | None = None) -> dict[str, Any] | None:
        """Circuit-breaker gate for new TradFi exposure."""
        decision = self.breaker.check(symbol, action=action, equity=equity).to_dict()
        if decision.get("allowed"):
            return None
        log_event(
            "entry_blocked",
            f"[TRADFI] {symbol} blocked by circuit breaker",
            f"action={action} reason={decision.get('reason')} details={decision.get('details')}",
        )
        return {"ok": False, "reason": f"circuit_breaker:{decision.get('reason')}", "circuit_breaker": decision}

    def _record_api_success(self) -> None:
        try:
            self.breaker.record_api_success()
        except Exception as exc:
            log_event("circuit_record_failed", "[TRADFI] api_success", f"reason={exc}")

    def _record_api_error(self, exc: Exception | str) -> None:
        try:
            self.breaker.record_api_error(str(exc))
        except Exception as inner:
            log_event("circuit_record_failed", "[TRADFI] api_error", f"reason={inner}")

    def _record_trade_result(self, symbol: str, pnl: float, *, equity: float | None = None) -> None:
        try:
            self.breaker.record_trade_result(symbol, pnl, equity=equity)
        except Exception as exc:
            log_event("circuit_record_failed", f"[TRADFI] {symbol} trade_result", f"reason={exc}")

    def check_budget(self, symbol: str, price: float, risk_per_unit: float) -> tuple[bool, str]:
        """사전 진입 가능성 체크 — trader.enter()의 실제 포지션 크기 로직과 일치.
        
        trader.enter()는 TRADFI_MAX_EQUITY_PCT_PER_POSITION(X) equity_pct와
        MAX_RISK_PCT_PER_TRADE(Y) risk_pct 중 작은 쪽으로 결정한다.
        이 함수는 두 제약 모두를 고려해 최소 예상 USDT 금액이 min_notional 이상인지 확인한다.
        Returns (passes_budget, reason).
        """
        meta = self.exchange.symbol_meta(symbol)
        equity = config.TRADFI_EQUITY_USDT if config.TRADFI_DRY_RUN else self.exchange.get_balance_usdt()

        if risk_per_unit <= 0:
            return False, "invalid_sl_for_budget"

        # trader.enter() 로직 정확히 재현
        equity_pct = config.TRADFI_MAX_EQUITY_PCT_PER_POSITION
        size_by_equity = (equity * equity_pct) / price
        size_by_risk = (equity * config.MAX_RISK_PCT_PER_TRADE) / risk_per_unit
        size = min(size_by_equity, size_by_risk)
        size = round_to_lot(size, meta["lot_step"])

        if size < meta["min_qty"]:
            return False, f"size_too_small:qty={size:.6f}<min={meta['min_qty']}"
        usdt_amount = size * price
        if usdt_amount < meta["min_notional"]:
            return False, f"below_notional:{usdt_amount:.2f}<{meta['min_notional']}"

        return True, f"ok:est_size={size:.6f}_usdt={usdt_amount:.2f}"

    def enter(self, signal: TradFiEntrySignal, sector: str, conviction: float) -> dict:
        equity = (
            config.TRADFI_EQUITY_USDT
            if config.TRADFI_DRY_RUN
            else self.exchange.get_balance_usdt()
        )
        blocked = self._check_entry_allowed(signal.symbol, action="entry", equity=equity)
        if blocked:
            return blocked
        meta = self.exchange.symbol_meta(signal.symbol)

        risk_per_unit = signal.entry_price - signal.stop_loss
        if risk_per_unit <= 0:
            return {"ok": False, "reason": "invalid_sl"}

        equity_pct = config.TRADFI_MAX_EQUITY_PCT_PER_POSITION
        size_by_equity = (equity * equity_pct) / signal.entry_price
        size_by_risk = (equity * config.MAX_RISK_PCT_PER_TRADE) / risk_per_unit
        size = min(size_by_equity, size_by_risk)
        size = round_to_lot(size, meta["lot_step"])
        if size < meta["min_qty"] or size * signal.entry_price < meta["min_notional"]:
            return {"ok": False, "reason": f"size_too_small:{size:.6f}"}

        usdt_amount = size * signal.entry_price

        positions = load_positions()
        if any(p["symbol"] == signal.symbol for p in positions):
            return {"ok": False, "reason": "already_held"}

        if config.TRADFI_DRY_RUN:
            order = {"id": "tradfi-dry-run"}
        else:
            try:
                order = self.exchange.create_market_buy(signal.symbol, usdt_amount, meta)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise

        record = {
            "symbol": signal.symbol,
            "symbol_type": signal.symbol_type,
            "sector": sector,
            "conviction": conviction,
            "entry_price": signal.entry_price,
            "size": size,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "atr": signal.atr,
            "highest_price": signal.entry_price,
            "entered_at": datetime.now(timezone.utc).isoformat(),
            "order_id": order.get("id"),
            "dca_triggered": [],
            "dca_entries": [],
            "tp_taken": [],
            "remaining_pct": 1.0,
            "runner_mode": False,
        }
        positions.append(record)
        save_positions(positions)
        log_event("entry", f"[TRADFI] {signal.symbol}", f"price={signal.entry_price} size={size} usdt={usdt_amount:.2f}")
        return {"ok": True, "usdt_amount": usdt_amount, "size": size, "entry_price": signal.entry_price}

    def exit(self, pos: dict, reason: str, risk_manager: RiskManager | None = None) -> dict:
        symbol = pos["symbol"]
        size = float(pos.get("size", 0))
        entry = float(pos.get("entry_price", 0))

        if config.TRADFI_DRY_RUN:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                exit_price = float(ticker.get("last") or entry)
            except Exception:
                exit_price = entry
        else:
            ticker = self.exchange.fetch_ticker(symbol)
            exit_price = float(ticker.get("last") or entry)
            try:
                self.exchange.create_market_sell(symbol, size)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise

        pnl = (exit_price - entry) * size * float(pos.get("remaining_pct", 1.0))
        pnl -= abs(pnl) * 0.001 * 2  # 수수료 추정

        # Persist realized PnL to risk manager so daily loss/profit limits,
        # consecutive loss stops, and symbol cooldowns are enforced across
        # launchd restart cycles. This was missing — risk state was never
        # written on TradFi exits.
        if risk_manager is not None:
            risk_manager.record_exit(symbol, pnl)
        equity_for_breaker = config.TRADFI_EQUITY_USDT if config.TRADFI_DRY_RUN else None
        self._record_trade_result(symbol, pnl, equity=equity_for_breaker)

        add_pnl_record({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "entry_price": entry,
            "exit_price": exit_price,
            "size": size,
            "pnl": pnl,
            "reason": reason,
            "account": ACCOUNT,
        })

        positions = [p for p in load_positions() if p["symbol"] != symbol]
        save_positions(positions)
        log_event("exit", f"[TRADFI] {symbol}", f"reason={reason} pnl={pnl:.2f} exit={exit_price}")
        return {"ok": True, "pnl": pnl, "exit_price": exit_price}

    def partial_exit(self, pos: dict, tp_level: str, risk_manager: RiskManager | None = None) -> dict:
        symbol = pos["symbol"]
        tp_map = {"tp1": config.TP_1_EXIT_PCT, "tp2": config.TP_2_EXIT_PCT, "tp3": config.TP_3_EXIT_PCT}
        exit_pct = tp_map.get(tp_level, 0.0)
        if not exit_pct:
            return {"ok": False}

        size = float(pos.get("size", 0))
        remaining = float(pos.get("remaining_pct", 1.0))
        exit_size = size * remaining * exit_pct
        entry = float(pos.get("entry_price", 0))

        if config.TRADFI_DRY_RUN:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                exit_price = float(ticker.get("last") or entry)
            except Exception:
                exit_price = entry
        else:
            ticker = self.exchange.fetch_ticker(symbol)
            exit_price = float(ticker.get("last") or entry)
            try:
                self.exchange.create_market_sell(symbol, exit_size)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise

        pnl = (exit_price - entry) * exit_size
        new_remaining = remaining * (1.0 - exit_pct)

        positions = load_positions()
        for p in positions:
            if p["symbol"] == symbol:
                p["remaining_pct"] = new_remaining
                p["tp_taken"] = p.get("tp_taken", []) + [tp_level]
                if tp_level == "tp3" and new_remaining <= config.TRAILING_REMAIN_PCT:
                    p["runner_mode"] = True
        save_positions(positions)

        add_pnl_record({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "entry_price": entry,
            "exit_price": exit_price,
            "size": exit_size,
            "pnl": pnl,
            "reason": f"partial_{tp_level}",
            "account": ACCOUNT,
        })

        # Persist partial exit PnL to risk manager
        if risk_manager is not None:
            risk_manager.record_exit(symbol, pnl)
        equity_for_breaker = config.TRADFI_EQUITY_USDT if config.TRADFI_DRY_RUN else None
        self._record_trade_result(symbol, pnl, equity=equity_for_breaker)

        return {"ok": True, "pnl": pnl, "exit_price": exit_price, "final": new_remaining <= 0.05}


# ── 타이밍 헬퍼 ────────────────────────────────────────────────────────────────

def at_hour_close(now: datetime) -> bool:
    return now.minute == 0 and now.second >= 30

def at_15min(now: datetime) -> bool:
    return now.minute % 15 == 0 and now.second >= 30

def at_4h_close(now: datetime) -> bool:
    return now.hour % 4 == 0 and at_hour_close(now)


# ── 빠른 trailing stop 체크 (매 루프) ───────────────────────────────────────────

def _check_trailing_stops(exchange: TradFiExchange, elapsed: int, trader: TradFiTrader | None = None, risk_manager: RiskManager | None = None) -> None:
    """Check and update trailing stops on every loop iteration, not just 15-min cycles.
    
    SIGTERM arrives every ~5 minutes (macOS resource management), which means the
    traditional 15-min boundary check often never fires during a session. This
    runs every 30s loop iteration to keep trailing stops alive.
    
    Rate-limited: only acts if >=30s since last check (elapsed param serves as
    a safety check — the caller in run() already enforces this).
    """
    if elapsed < 25:
        return  # too soon since last check
    try:
        positions = load_positions()
        if not positions:
            return
        changed = False
        exited_symbols: set[str] = set()
        for pos in positions:
            sym = pos["symbol"]
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker.get("last") or 0)
                if price <= 0:
                    continue

                # Update highest price for trailing stop
                highest = float(pos.get("highest_price", pos["entry_price"]))
                entry = float(pos["entry_price"])
                if price > highest:
                    pos["highest_price"] = price
                    changed = True
                    logger.debug("Trailing high updated %s: %.4f (entry %.4f)", sym, price, entry)

                # Check trailing stop activation
                activation_pct = config.TRAILING_STOP_ACTIVATION_PCT  # 0.003 = 0.3%
                callback_pct = config.TRAILING_STOP_CALLBACK_PCT      # 0.002 = 0.2%
                trail_price = highest * (1.0 - callback_pct)
                exit_reason = None
                if price >= entry * (1.0 + activation_pct):
                    # Activated: use trailing stop
                    if price <= trail_price:
                        exit_reason = "trailing_stop"
                        logger.info("Trailing stop hit %s: %.4f <= %.4f (trail from %.4f)",
                                    sym, price, trail_price, highest)
                elif price <= pos.get("stop_loss", 0):
                    exit_reason = "stop_loss"
                    logger.info("Fixed stop-loss hit %s: %.4f", sym, price)

                if exit_reason and trader and sym not in exited_symbols:
                    # Double-check: reload positions from disk and verify the symbol
                    # still exists. This prevents cross-instance race conditions where
                    # two bot instances (overlapping launchd cycles) both see the same
                    # position and both try to exit it.
                    still_held = any(
                        p["symbol"] == sym for p in load_positions()
                    )
                    if not still_held:
                        logger.info(
                            "%s already exited by another instance, skipping "
                            "duplicate exit (reason=%s)", sym, exit_reason,
                        )
                        continue
                    # Save updated positions (including highest_price updates) before exit,
                    # so trader.exit() can cleanly remove the exited symbol from disk
                    if changed:
                        save_positions(positions)
                    result = trader.exit(pos, exit_reason, risk_manager=risk_manager)
                    exited_symbols.add(sym)
                    changed = True
                    if result.get("ok"):
                        logger.info("%s exit via fast trail: pnl=%.2f", sym, result.get("pnl", 0))
            except Exception as e:
                logger.debug("trailing check %s skipped: %s", sym, e)
        if changed:
            if not exited_symbols:
                # Only price updates — save the in-memory positions with new highest_price
                save_positions(positions)
    except Exception as e:
        logger.warning("_check_trailing_stops error: %s", e)


# ── 메인 루프 ──────────────────────────────────────────────────────────────────

def _tradfi_live_guard() -> None:
    """TRADFI_DRY_RUN=false 전환 시 CONFIRM_TRADFI_LIVE=yes 환경변수 확인.

    TradFi 봇은 실거래 설정이 코인봇과 분리되어 있으므로 별도 가드가 필요하다.
    의도치 않은 LIVE 전환 방지용.
    """
    if config.TRADFI_DRY_RUN:
        return
    confirm = os.getenv("CONFIRM_TRADFI_LIVE", "").strip().lower()
    if confirm != "yes":
        raise SystemExit(
            "\n⛔ TradFi LIVE 모드 실행 차단됨.\n"
            "   실거래를 시작하려면 .env에 CONFIRM_TRADFI_LIVE=yes 를 설정하세요.\n"
            "   현재값: CONFIRM_TRADFI_LIVE=" + repr(os.getenv("CONFIRM_TRADFI_LIVE", "(미설정)"))
        )
    logger.warning("🔴 TRADFI LIVE MODE 확인됨 (CONFIRM_TRADFI_LIVE=yes) — 실거래 시작")


def run() -> None:
    config.ensure_dirs()
    _tradfi_live_guard()
    _acquire_single_instance()
    PID_FILE.write_text(str(os.getpid()))

    # Signal handlers for graceful shutdown logging
    # NOTE: parameter name is 'sig' not 'signal' to avoid shadowing the module
    def _sig_handler(sig, frame):
        logger.info("Received signal %d. Shutting down gracefully.", sig)
        _write_heartbeat()
        # Mark clean SIGTERM exit so _check_crash_loop doesn't count this restart as a crash
        try:
            SIGTERM_CLEAN_FILE.write_text(str(time.time()))
        except OSError:
            pass
        raise SystemExit(0)

    # Check crash loop BEFORE registering signal handlers (and before anything that may crash)
    _check_crash_loop()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    logger.info("Signal handlers registered (SIGTERM/SIGINT)")

    exchange = TradFiExchange(config.TRADFI_API_KEY, config.TRADFI_PRIVATE_KEY_PATH)
    trader = TradFiTrader(exchange)
    telegram = CoinTelegram()
    news_collector = NewsCollector()
    risk_manager = RiskManager(account=ACCOUNT)

    symbols = get_tradfi_symbols()
    logger.info("TradFi symbols loaded: %d", len(symbols))

    analyzer = TradFiGPTAnalyzer(symbols)
    latest_candidates: list[dict] = []

    last_15min_run: int = -1
    last_4h_run: int = -1
    # Two-Tier: context_from 브리지 해시 추적
    last_bridge_hash: str | None = None

    balance = exchange.get_balance_usdt()
    mode_tag = "🧪 DRY_RUN" if config.TRADFI_DRY_RUN else "💰 LIVE"
    logger.info("TradFi bot started | dry_run=%s balance=%.2f", config.TRADFI_DRY_RUN, balance)
    telegram.send_sync(
        f"🚀 <b>TradFi Bot Started</b>\n"
        f"모드: {mode_tag}\n"
        f"잔고: {balance:.2f} USDT\n"
        f"심볼: {len(symbols)}개 (원자재 {sum(1 for v in symbols.values() if v['type']=='commodity')}개, "
        f"주식 {sum(1 for v in symbols.values() if v['type']=='stock')}개)"
    )

    # 시작 시 GPT 분석 (skip if crash-loop recovery)
    try:
        news = news_collector.collect(config.NEWS_LOOKBACK_HOURS)
        latest_candidates = sorted(
            analyzer.pick_candidates(news),
            key=lambda c: (
                float(c.get("closed_loop_score", c.get("conviction", 0)) or 0),
                float(c.get("conviction", 0) or 0),
                str(c.get("symbol", "")),
            ),
            reverse=True,
        )[:12]
        logger.info("Initial TradFi analysis: %d candidates", len(latest_candidates))
    except Exception as e:
        logger.warning("Initial analysis failed: %s", e)

    # SPY 캔들 캐시 (4H마다 갱신)
    spy_4h = None
    try:
        spy_4h = exchange.fetch_ohlcv("SPYUSDT", "240", 100)
    except Exception as e:
        logger.info("SPY macro filter disabled: %s: %s", type(e).__name__, e)

    # ── 시작 시 기존 포지션 즉시 점검 (재시작으로 놓친 15분 사이클 보완) ──
    try:
        startup_positions = load_positions()
        if startup_positions:
            logger.info("Checking %d existing positions at startup", len(startup_positions))
            for i, pos in enumerate(startup_positions):
                sym = pos["symbol"]
                try:
                    # Stagger API calls to avoid rate limits
                    if i > 0:
                        time.sleep(0.5)
                    c1 = exchange.fetch_ohlcv(sym, "60", 200)
                    c4 = exchange.fetch_ohlcv(sym, "240", 200)
                except Exception as e:
                    logger.warning("startup candle fetch failed for %s: %s", sym, e)
                    continue
                if config.PARABOLIC_ENABLED and not pos.get("runner_mode"):
                    if detect_parabolic(pos, c1, c4):
                        pos["runner_mode"] = True
                reason = check_exit(pos, c1, c4, {})
                if reason:
                    logger.info("Startup exit signal for %s: %s", sym, reason)
                    result = trader.exit(pos, reason, risk_manager=risk_manager)
                    mode = "🧪" if config.TRADFI_DRY_RUN else "💰"
                    pnl = result.get("pnl", 0)
                    telegram.send_sync(
                        f"{mode} <b>[TradFi] 시작 청산</b> {sym}\n"
                        f"사유: {reason} | PnL: {pnl:+.2f} USDT\n"
                        f"청산가: {result.get('exit_price', 0):.4f}"
                    )
                else:
                    # highest_price 업데이트
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        price = float(ticker.get("last") or 0)
                        if price > float(pos.get("highest_price", 0)):
                            pos["highest_price"] = price
                    except Exception:
                        pass
            save_positions(startup_positions)

        # ── 시작 시 진입 스캔 (재시작으로 놓친 15분 사이클 보완) ──
        # launchd가 5분마다 SIGTERM을 보내므로 15분 경계 체크(at_15min)가
        # 절대 실행되지 않음. 따라서 시작 시 진입 스캔을 수행한다.
        held_on_startup = {p["symbol"] for p in load_positions()}
        if len(held_on_startup) < config.TRADFI_MAX_CONCURRENT_POSITIONS:
            for i, candidate in enumerate(latest_candidates):
                sym = candidate.get("symbol", "")
                sym_type = candidate.get("type", symbols.get(sym, {}).get("type", "stock"))
                if sym in held_on_startup or sym not in symbols:
                    continue
                if risk_manager.in_symbol_cooldown(sym):
                    continue
                try:
                    if i > 0:
                        time.sleep(0.5)
                    c1 = exchange.fetch_ohlcv(sym, "60", 200)
                    c4 = exchange.fetch_ohlcv(sym, "240", 200)
                except Exception as e:
                    logger.warning("startup entry candle fetch failed %s: %s", sym, e)
                    continue
                # 자본 예산 사전체크 (Trader.check_budget — enter()의 실제 로직 재현)
                meta = symbols.get(sym, {})
                if meta.get("min_qty") and meta.get("min_notional"):
                    try:
                        close_price = float(c1["close"].iloc[-1])
                        # 보수적 SL 가정: ATR의 2배 (진입 전이므로 추정)
                        est_risk = close_price * 0.02  # 진입가의 2% 가상 SL
                        ok, reason = trader.check_budget(sym, close_price, est_risk)
                        if not ok:
                            logger.debug("startup budget skip %s: %s (price=%.4f)", sym, reason, close_price)
                            continue
                    except Exception:
                        pass
                entry_signal, sig_reason = check_tradfi_entry(sym, sym_type, c4, c1, spy_4h)
                if entry_signal:
                    result = trader.enter(entry_signal, candidate.get("reason", "gpt"), float(candidate.get("conviction", 5)))
                    if result.get("ok"):
                        mode = "🧪" if config.TRADFI_DRY_RUN else "💰"
                        telegram.send_sync(
                            f"{mode} <b>[TradFi] 시작 진입</b> {sym} ({sym_type})\n"
                            f"사유: {candidate.get('reason', '?')}\n"
                            f"💰 {result['usdt_amount']:.2f} USDT @ {entry_signal.entry_price:.4f}\n"
                            f"🎯 SL {entry_signal.stop_loss:.4f} | TP {entry_signal.take_profit:.4f}"
                        )
                        held_on_startup.add(sym)
                        if len(held_on_startup) >= config.TRADFI_MAX_CONCURRENT_POSITIONS:
                            break
                    else:
                        logger.info(
                            "startup entry failed %s: %s (price=%.2f)",
                            sym, result.get("reason", "unknown"), entry_signal.entry_price,
                        )
                else:
                    logger.debug("startup entry %s rejected: %s", sym, sig_reason)

            held_count = len(held_on_startup)
            if held_count == 0 and len(latest_candidates) > 0:
                # 진입 실패 원인 디버그 로그 (자본 대비 최소 진입비용)
                for c in latest_candidates:
                    s = c.get("symbol", "")
                    m = symbols.get(s, {})
                    if m:
                        logger.info(
                            "startup scan: %s min_qty=%.4f lot_step=%.4f min_notional=%.2f available_budget=%.2f",
                            s, m.get("min_qty", 0), m.get("lot_step", 0), m.get("min_notional", 0),
                            config.TRADFI_EQUITY_USDT * config.TRADFI_MAX_EQUITY_PCT_PER_POSITION,
                        )
            logger.info("Startup entry scan: %d positions held after scan (%d candidates)", held_count, len(latest_candidates))
    except Exception as e:
        logger.warning("Startup position check failed: %s", e)

    # ── 메인 루프 진입 전 마지막 heartbeat 기록 ──
    _write_heartbeat()
    reloader = HotReloader(
        params_path=str(config.ROOT_DIR / "params.json"),
        config_module=config,
        history_dir=str(config.ROOT_DIR / "params_history"),
        check_interval=5.0,
        log_func=log_event,
    )
    reloader.start()
    atexit.register(reloader.stop)
    last_trailing_check: float = time.time()

    while True:
        _write_heartbeat()
        now = datetime.now(timezone.utc)
        try:
            current_15min = now.hour * 4 + (now.minute // 15)
            current_4h = now.hour // 4

            # ── 매 루프: trailing stop 업데이트 (15분 간격은 깨지는 경우를 대비한 fallback) ──
            # GitHub Issue #tradfi-fast-trailing: SIGTERM이 5분마다 발생하므로 15분 주기까지
            # 기다리면 trailing stop이 전혀 업데이트되지 않음. 매 30초 루프에서 시도하되
            # rate-limit 보호를 위해 최소 30초 간격 유지.
            _check_trailing_stops(exchange, int(time.time() - last_trailing_check), trader, risk_manager=risk_manager)
            last_trailing_check = time.time()

            # ── 15분마다: 포지션 관리 + 진입 스캔 ──
            if at_15min(now) and current_15min != last_15min_run:
                last_15min_run = current_15min

                positions = load_positions()

                # 포지션 관리 (청산/DCA/부분익절)
                for i, pos in enumerate(positions):
                    sym = pos["symbol"]
                    sym_type = pos.get("symbol_type", "commodity")
                    try:
                        # Stagger API calls across positions to avoid rate limits
                        if i > 0:
                            time.sleep(0.3)
                        c1 = exchange.fetch_ohlcv(sym, "60", 200)
                        c4 = exchange.fetch_ohlcv(sym, "240", 200)
                    except Exception as e:
                        logger.warning("candle fetch failed for %s: %s", sym, e)
                        continue

                    if config.DCA_ENABLED:
                        dca_sig = check_dca_trigger(pos, c1)
                        if dca_sig and not risk_manager.in_dca_cooldown(sym):
                            logger.info("DCA trigger %s: %s", sym, dca_sig)

                    if config.PYRAMID_ENABLED:
                        tp_sig = check_partial_tp(pos, c1)
                        if tp_sig:
                            result = trader.partial_exit(pos, tp_sig, risk_manager=risk_manager)
                            if result.get("ok"):
                                mode = "🧪" if config.TRADFI_DRY_RUN else "💰"
                                telegram.send_sync(
                                    f"{mode} 📗 <b>[TradFi] 부분익절</b> {sym}\n"
                                    f"레벨: {tp_sig} | PnL: {result.get('pnl', 0):+.2f} USDT"
                                )

                    reason = check_exit(pos, c1, c4, {})
                    if reason:
                        result = trader.exit(pos, reason, risk_manager=risk_manager)
                        mode = "🧪" if config.TRADFI_DRY_RUN else "💰"
                        pnl = result.get("pnl", 0)
                        telegram.send_sync(
                            f"{mode} <b>[TradFi] 청산</b> {sym}\n"
                            f"사유: {reason} | PnL: {pnl:+.2f} USDT\n"
                            f"청산가: {result.get('exit_price', 0):.4f}"
                        )

                # highest_price 업데이트
                updated = []
                for pos in load_positions():
                    try:
                        ticker = exchange.fetch_ticker(pos["symbol"])
                        price = float(ticker.get("last") or 0)
                        if price > float(pos.get("highest_price", 0)):
                            pos["highest_price"] = price
                    except Exception:
                        pass
                    updated.append(pos)
                save_positions(updated)

                # Parabolic 감지
                if config.PARABOLIC_ENABLED:
                    for pos in load_positions():
                        if pos.get("runner_mode"):
                            continue
                        try:
                            c1 = exchange.fetch_ohlcv(pos["symbol"], "60", 200)
                            c4 = exchange.fetch_ohlcv(pos["symbol"], "240", 200)
                            if detect_parabolic(pos, c1, c4):
                                pos["runner_mode"] = True
                                save_positions(load_positions())
                                telegram.send_sync(
                                    f"🚀 <b>[TradFi] Parabolic!</b> {pos['symbol']}\nRunner 모드 전환"
                                )
                        except Exception:
                            pass

                # 진입 스캔
                held = {p["symbol"] for p in load_positions()}
                if len(held) < config.TRADFI_MAX_CONCURRENT_POSITIONS:
                    for i, candidate in enumerate(latest_candidates):
                        sym = candidate.get("symbol", "")
                        sym_type = candidate.get("type", symbols.get(sym, {}).get("type", "stock"))
                        if sym in held or sym not in symbols:
                            continue
                        if risk_manager.in_symbol_cooldown(sym):
                            continue

                        try:
                            if i > 0:
                                time.sleep(0.5)
                            c1 = exchange.fetch_ohlcv(sym, "60", 200)
                            c4 = exchange.fetch_ohlcv(sym, "240", 200)
                        except Exception as e:
                            logger.warning("candle fetch failed %s: %s", sym, e)
                            continue

                        # ── 15분 스캔 자본예산 사전체크 (Trader.check_budget) ──
                        meta = symbols.get(sym, {})
                        if meta.get("min_qty") and meta.get("min_notional"):
                            try:
                                close_price = float(c1["close"].iloc[-1])
                                est_risk = close_price * 0.02
                                ok, reason = trader.check_budget(sym, close_price, est_risk)
                                if not ok:
                                    logger.debug("15m budget skip %s: %s (price=%.4f)", sym, reason, close_price)
                                    continue
                            except Exception:
                                pass
                        entry_signal, sig_reason = check_tradfi_entry(sym, sym_type, c4, c1, spy_4h)
                        if entry_signal:
                            result = trader.enter(entry_signal, candidate.get("reason", "gpt"), float(candidate.get("conviction", 5)))
                            if result.get("ok"):
                                mode = "🧪" if config.TRADFI_DRY_RUN else "💰"
                                telegram.send_sync(
                                    f"{mode} <b>[TradFi] 진입</b> {sym} ({sym_type})\n"
                                    f"사유: {candidate.get('reason', '?')}\n"
                                    f"💰 {result['usdt_amount']:.2f} USDT @ {entry_signal.entry_price:.4f}\n"
                                    f"🎯 SL {entry_signal.stop_loss:.4f} | TP {entry_signal.take_profit:.4f}"
                                )
                                held.add(sym)
                                if len(held) >= config.TRADFI_MAX_CONCURRENT_POSITIONS:
                                    break
                            else:
                                logger.info(
                                    "Entry failed for %s: %s (price=%.2f)",
                                    sym, result.get("reason", "unknown"), entry_signal.entry_price,
                                )
                        else:
                            logger.debug("%s signal rejected: %s", sym, sig_reason)

            # ── 4시간마다: 뉴스 + GPT 분석 갱신 ──
            if at_4h_close(now) and current_4h != last_4h_run:
                last_4h_run = current_4h

                # ── Tier 1: context_from 해시 체크 ──
                run_tier2, bridge_ctx = check_tier2_trigger("tradfi-bot")
                if bridge_ctx.hash != last_bridge_hash:
                    last_bridge_hash = bridge_ctx.hash
                    log_event(
                        "context_from",
                        "[TradFi] bridge state updated",
                        f"hash={bridge_ctx.hash} regime={bridge_ctx.regime} "
                        f"bias={bridge_ctx.action_bias} targets={bridge_ctx.targets}",
                    )

                # ── Tier 2: 브리지 신호 수신 시 텔레그램 알림 ──
                if run_tier2:
                    log_event(
                        "context_from",
                        "[TradFi] Tier2 triggered",
                        f"regime={bridge_ctx.regime} bias={bridge_ctx.action_bias} "
                        f"confidence={bridge_ctx.confidence}",
                    )
                    telegram.send_sync(
                        f"📡 <b>[TradFi] 브리지 신호 수신</b>\n"
                        f"Regime: {bridge_ctx.regime} | Bias: {bridge_ctx.action_bias}\n"
                        f"신뢰도: {bridge_ctx.confidence} | 긴급도: {bridge_ctx.urgency}\n"
                        f"리스크: {bridge_ctx.risk_flags[:80]}"
                    )

                symbols = get_tradfi_symbols(force_refresh=True)
                analyzer.symbols = symbols
                try:
                    news = news_collector.collect(config.NEWS_LOOKBACK_HOURS)
                    latest_candidates = sorted(
                        analyzer.pick_candidates(news),
                        key=lambda c: (int(c.get("conviction", 0) or 0), str(c.get("symbol", ""))),
                        reverse=True,
                    )[:12]
                    logger.info(
                        "TradFi GPT refresh: %d candidates bridge_hash=%s",
                        len(latest_candidates), last_bridge_hash or "none",
                    )
                except Exception as e:
                    logger.warning("GPT refresh failed: %s", e)

                # SPY 갱신
                try:
                    spy_4h = exchange.fetch_ohlcv("SPYUSDT", "240", 100)
                except Exception as e:
                    logger.warning("SPY 4H refresh failed: %s: %s", type(e).__name__, e)

            time.sleep(config.MAIN_LOOP_SLEEP_SECONDS)

        except Exception as exc:
            logger.exception("tradfi loop error: %s", exc)
            telegram.notify_error(f"[TradFi] {exc}")
            time.sleep(5)


if __name__ == "__main__":
    run()
