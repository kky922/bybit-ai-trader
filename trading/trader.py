from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import config
from analysis.entry_signal import EntrySignal
from infra.event_log import log_event
from infra.state import add_pnl_record, load_positions, save_positions
from trading.exchange import BybitExchange
from trading.position_sizer import compute_size_with_reason
from trading.risk_manager import RiskManager
from circuit_breaker import CircuitBreaker

logger = logging.getLogger("coin-bot.trader")


class Trader:
    def __init__(self, exchange: BybitExchange, risk_manager: RiskManager):
        self.exchange = exchange
        self.risk = risk_manager
        self.breaker = CircuitBreaker(log_func=log_event)

    def _check_entry_allowed(self, symbol: str, *, action: str, equity: float | None = None) -> dict[str, Any] | None:
        """Circuit-breaker gate for new exposure. Returns failure payload when blocked."""
        decision = self.breaker.check(symbol, action=action, equity=equity).to_dict()
        if decision.get("allowed"):
            return None
        log_event(
            "entry_blocked",
            f"{symbol} blocked by circuit breaker",
            f"action={action} reason={decision.get('reason')} details={decision.get('details')}",
        )
        return {"ok": False, "reason": f"circuit_breaker:{decision.get('reason')}", "circuit_breaker": decision}

    def _record_api_success(self) -> None:
        try:
            self.breaker.record_api_success()
        except Exception as exc:
            log_event("circuit_record_failed", "api_success", f"reason={exc}")

    def _record_api_error(self, exc: Exception | str) -> None:
        try:
            self.breaker.record_api_error(str(exc))
        except Exception as inner:
            log_event("circuit_record_failed", "api_error", f"reason={inner}")

    def _record_trade_result(self, symbol: str, pnl: float, *, equity: float | None = None) -> None:
        try:
            self.breaker.record_trade_result(symbol, pnl, equity=equity)
        except Exception as exc:
            log_event("circuit_record_failed", f"{symbol} trade_result", f"reason={exc}")

    def _build_position_record(
        self, symbol: str, signal: EntrySignal, size: float, usdt_amount: float,
        order: dict, sector: str, conviction: float
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
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
            "sl_order_id": None,
            # DCA/부분익절 상태
            "dca_triggered": [],     # ["dca1", "dca2", "dca3"]
            "dca_entries": [],        # [{"price": x, "size": y, "at": "ISO"}, ...]
            "tp_taken": [],           # ["tp1", "tp2", "tp3"]
            "remaining_pct": 1.0,     # 아직 보유 중인 비율 (1.0 = 100%)
        }

    def enter(self, symbol: str, signal: EntrySignal, sector: str, conviction: float) -> dict[str, Any]:
        equity = self.exchange.get_balance_usdt()
        blocked = self._check_entry_allowed(symbol, action="entry", equity=equity)
        if blocked:
            return blocked
        meta = self.exchange.symbol_meta(symbol)
        size, size_reason = compute_size_with_reason(
            equity_usdt=equity,
            entry=signal.entry_price,
            stop=signal.stop_loss,
            lot_step=meta["lot_step"],
            min_amount=meta["min_amt"],
            min_notional=meta["min_notional"],
        )
        if size <= 0:
            log_event(
                "entry_skip",
                f"{symbol} size rejected",
                f"reason=size_zero:{size_reason} min_amt={meta['min_amt']} min_notional={meta['min_notional']}",
            )
            return {"ok": False, "reason": "size=0"}

        usdt_amount = size * signal.entry_price
        if config.DRY_RUN:
            order = {"id": "dry-run-buy"}
            sl_order_id = None
        else:
            try:
                order = self.exchange.create_market_buy(symbol, usdt_amount)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise
            try:
                sl_order = self.exchange.create_stop_loss_order(symbol, size, signal.stop_loss)
                sl_order_id = sl_order.get("id")
            except Exception as e:
                log_event("sl_failed", f"{symbol} SL failed", f"reason={e}")
                sl_order_id = None

        positions = load_positions()
        if any(p["symbol"] == symbol for p in positions):
            return {"ok": False, "reason": "duplicate_symbol"}

        rec = self._build_position_record(symbol, signal, size, usdt_amount, order, sector, conviction)
        rec["sl_order_id"] = sl_order_id
        positions.append(rec)
        save_positions(positions)
        log_event("entry", f"{symbol} entered", f"entry={signal.entry_price:.4f} size={size:.6f} usdt={usdt_amount:.2f}")
        return {"ok": True, "order": order, "usdt_amount": usdt_amount, "size": size,
                "entry_price": signal.entry_price, "stop_loss": signal.stop_loss, "take_profit": signal.take_profit}

    def add_dca_position(self, symbol: str, dca_level: str) -> dict[str, Any]:
        """물타기: 기존 포지션에 추가 진입"""
        positions = load_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if not pos:
            return {"ok": False, "reason": "position_not_found"}

        blocked = self._check_entry_allowed(symbol, action=f"dca:{dca_level}")
        if blocked:
            return blocked

        # 현재 시세
        ticker = self.exchange.fetch_ticker(symbol)
        current_price = float(ticker.get("last") or 0)
        if current_price <= 0:
            return {"ok": False, "reason": "no_price"}

        entry = float(pos["entry_price"])
        atr_val = float(pos.get("atr", 0))

        # DCA 사이즈 결정
        dca_size_map = {"dca1": config.DCA_LEVEL_1_SIZE,
                        "dca2": config.DCA_LEVEL_2_SIZE,
                        "dca3": config.DCA_LEVEL_3_SIZE}
        size_pct = dca_size_map.get(dca_level, 0.5)
        dca_qty = pos["size"] * size_pct

        equity = self.exchange.get_balance_usdt()
        usdt_amount = dca_qty * current_price
        if usdt_amount > equity * 0.5:
            usdt_amount = equity * 0.5
            dca_qty = usdt_amount / current_price

        # 롯 사이즈 맞춤
        meta = self.exchange.symbol_meta(symbol)
        dca_qty = max(dca_qty, meta["min_amt"])
        if dca_qty * current_price < meta["min_notional"]:
            return {"ok": False, "reason": "dca_too_small"}

        if config.DRY_RUN:
            order = {"id": "dry-run-dca"}
        else:
            try:
                order = self.exchange.create_market_buy(symbol, usdt_amount)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise
            # SL 업데이트: 새 평단 기준으로 재설정
            self._update_stop_loss(pos, positions)

        # DCA 기록
        dca_entry = {"price": current_price, "size": dca_qty, "at": datetime.now(timezone.utc).isoformat(), "level": dca_level}
        pos.setdefault("dca_triggered", []).append(dca_level)
        pos.setdefault("dca_entries", []).append(dca_entry)
        pos["size"] += dca_qty  # 총 보유량 증가

        # 새 평단 계산
        total_cost = entry * float(pos.get("_initial_size", pos["size"]))  # approximate
        # 더 정확하게
        orig_size = pos["size"] - dca_qty
        orig_cost = float(pos.get("_entry_cost", entry * orig_size))
        new_cost = orig_cost + current_price * dca_qty
        new_avg_price = new_cost / pos["size"]
        pos["_entry_cost"] = new_cost
        # entry_price를 평단으로 업데이트하지 않고 보존 (기존 청산 로직 호환)

        pos["entry_price"] = new_avg_price
        # SL/TP를 새 평단 기준으로 재계산
        pos["stop_loss"] = new_avg_price - config.ATR_STOP_MULTI * atr_val
        pos["take_profit"] = new_avg_price + config.ATR_TP_MULTI * atr_val

        self.risk.record_dca(symbol)
        save_positions(positions)

        log_event("dca", f"{symbol} DCA {dca_level}", f"price={current_price:.4f} qty={dca_qty:.6f} avg={new_avg_price:.4f}")
        return {"ok": True, "dca_qty": dca_qty, "dca_price": current_price, "new_avg": new_avg_price}

    def _update_stop_loss(self, position: dict, all_positions: list) -> None:
        """Update exchange-level stop-loss to reflect new average entry."""
        symbol = position["symbol"]
        sl_order_id = position.get("sl_order_id")
        if sl_order_id:
            try:
                self.exchange.cancel_order(symbol, sl_order_id)
            except Exception:
                pass
        try:
            sl_order = self.exchange.create_stop_loss_order(symbol, position["size"], position["stop_loss"])
            position["sl_order_id"] = sl_order.get("id")
            log_event("sl_updated", f"{symbol} SL updated", f"stop={position['stop_loss']:.4f}")
        except Exception as e:
            log_event("sl_update_failed", f"{symbol} SL update failed", f"reason={e}")

    def cleanup_dust_positions(self) -> list[dict[str, Any]]:
        """시장가 매도가 가능한 dust 포지션을 정리한다."""
        cleaned: list[dict[str, Any]] = []
        positions = load_positions()
        remaining_positions = list(positions)

        for pos in positions:
            symbol = pos["symbol"]
            try:
                size = float(pos.get("size") or 0.0)
                if size <= 0:
                    continue

                ticker = self.exchange.fetch_ticker(symbol)
                exit_price = float(ticker.get("last") or pos.get("entry_price") or 0.0)
                value_usd = size * exit_price
                if exit_price <= 0 or value_usd >= config.DUST_THRESHOLD_USD:
                    continue

                sell_size = size
                if not config.DRY_RUN:
                    meta = self.exchange.symbol_meta(symbol)
                    min_amt = float(meta.get("min_amt") or 0.0)
                    min_notional = float(meta.get("min_notional") or config.MIN_NOTIONAL_USDT_DEFAULT)
                    lot_step = float(meta.get("lot_step") or 0.0)
                    spot_balance = self.exchange.fetch_balance_spot(symbol.replace("USDT", ""))
                    max_sell = round(spot_balance // lot_step * lot_step, 6) if lot_step > 0 else spot_balance
                    sell_size = min(size, max_sell)
                    if sell_size <= 0 or sell_size < min_amt or sell_size * exit_price < min_notional:
                        log_event(
                            "dust_cleanup_skip",
                            f"{symbol} dust sell skipped",
                            f"reason=min_qty_or_notional size={sell_size:.8f} value={sell_size * exit_price:.4f} "
                            f"min_amt={min_amt:.8f} min_notional={min_notional:.4f}",
                        )
                        continue
                    try:
                        order = self.exchange.create_market_sell(symbol, sell_size)
                        self._record_api_success()
                    except Exception as exc:
                        self._record_api_error(exc)
                        raise
                else:
                    order = {"id": "dry-run-dust-sell"}

                entry_price = float(pos.get("entry_price") or exit_price)
                gross_pnl = (exit_price - entry_price) * sell_size
                fee = (exit_price * sell_size + entry_price * sell_size) * 0.001
                net_pnl = gross_pnl - fee
                self.risk.record_exit(symbol, net_pnl)
                self._record_trade_result(symbol, net_pnl)
                add_pnl_record({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "size": sell_size,
                    "pnl": net_pnl,
                    "reason": "dust_cleanup",
                })
                remaining_positions = [p for p in remaining_positions if p["symbol"] != symbol]
                save_positions(remaining_positions)
                log_event(
                    "dust_cleanup",
                    f"{symbol} dust position cleaned",
                    f"value={value_usd:.4f} threshold={config.DUST_THRESHOLD_USD:.4f} pnl={net_pnl:.4f} order={order.get('id')}",
                )
                cleaned.append({"symbol": symbol, "value_usd": value_usd, "pnl": net_pnl, "exit_price": exit_price})
            except Exception as e:
                log_event("dust_cleanup_skip", f"{symbol} dust cleanup failed", f"reason={e}")
                logger.warning("dust cleanup skipped for %s: %s", symbol, e)

        return cleaned

    def partial_exit(self, position: dict[str, Any], tp_level: str) -> dict[str, Any]:
        """부분 익절: 지정된 TP 레벨 비율만큼 청산"""
        symbol = position["symbol"]
        tp_exit_map = {"tp1": config.TP_1_EXIT_PCT,
                       "tp2": config.TP_2_EXIT_PCT,
                       "tp3": config.TP_3_EXIT_PCT}
        exit_pct = tp_exit_map.get(tp_level, 0.5)

        current_positions = load_positions()
        pos = next((p for p in current_positions if p["symbol"] == symbol), None)
        if not pos:
            return {"ok": False, "reason": "already_exited"}

        total_size = float(pos["size"])
        exit_size = total_size * exit_pct

        ticker = self.exchange.fetch_ticker(symbol)
        exit_price = float(ticker.get("last") or 0)

        if config.DRY_RUN:
            order = {"id": "dry-run-partial"}
        else:
            try:
                order = self.exchange.create_market_sell(symbol, exit_size)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise

        # PnL 기록 (초기 진입가 기준)
        entry_price = float(pos["entry_price"])
        gross_pnl = (exit_price - entry_price) * exit_size
        fee = (exit_price * exit_size + entry_price * exit_size) * 0.001
        net_pnl = gross_pnl - fee

        # 남은 포지션 업데이트
        previous_remaining_pct = float(pos.get("remaining_pct", 1.0))
        pos["size"] = total_size - exit_size
        pos.setdefault("tp_taken", []).append(tp_level)
        pos["remaining_pct"] = max(0.0, previous_remaining_pct - exit_pct)

        if pos["size"] <= 0 or pos["remaining_pct"] <= 0:
            # 전량 청산
            self.risk.record_exit(symbol, net_pnl)
            self._record_trade_result(symbol, net_pnl)
            current_positions = [p for p in load_positions() if p["symbol"] != symbol]
            save_positions(current_positions)
            add_pnl_record({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size": exit_size,
                "pnl": net_pnl,
                "reason": f"partial_{tp_level}_final",
            })
            return {"ok": True, "partial": True, "final": True, "pnl": net_pnl, "exit_price": exit_price}
        else:
            # 일부만 청산
            # TP3 이후 남은 30% → Runner 모드 활성화 (하드TP 없음, 챈들리어 트레일)
            if tp_level == "tp3" and config.RUNNER_ENABLED:
                remaining_after = pos.get("remaining_pct", 1.0)
                remaining_size = float(pos.get("size") or 0.0)
                remaining_value_usd = remaining_size * exit_price
                if remaining_after <= config.TRAILING_REMAIN_PCT + 1e-9:
                    if remaining_value_usd < config.MIN_RUNNER_VALUE_USD:
                        if config.DRY_RUN:
                            final_order = {"id": "dry-run-runner-dust-sell"}
                            final_size = remaining_size
                        else:
                            spot_balance = self.exchange.fetch_balance_spot(symbol.replace("USDT", ""))
                            meta = self.exchange.symbol_meta(symbol)
                            lot_step = meta["lot_step"]
                            max_sell = round(spot_balance // lot_step * lot_step, 6) if lot_step > 0 else spot_balance
                            final_size = min(remaining_size, max_sell)
                            if final_size <= 0:
                                log_event(
                                    "runner_skipped_dust_failed",
                                    f"{symbol} runner dust full-exit failed",
                                    f"reason=zero_sellable_size remaining_value={remaining_value_usd:.4f}",
                                )
                                save_positions(current_positions)
                                return {"ok": False, "reason": "runner_dust_zero_sellable_size", "exit_price": exit_price}
                            try:
                                final_order = self.exchange.create_market_sell(symbol, final_size)
                                self._record_api_success()
                            except Exception as exc:
                                self._record_api_error(exc)
                                raise

                        final_gross_pnl = (exit_price - entry_price) * final_size
                        final_fee = (exit_price * final_size + entry_price * final_size) * 0.001
                        final_net_pnl = final_gross_pnl - final_fee
                        total_net_pnl = net_pnl + final_net_pnl

                        self.risk.record_exit(symbol, total_net_pnl)
                        self._record_trade_result(symbol, total_net_pnl)
                        current_positions = [p for p in load_positions() if p["symbol"] != symbol]
                        save_positions(current_positions)
                        add_pnl_record({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "symbol": symbol,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "size": exit_size + final_size,
                            "pnl": total_net_pnl,
                            "reason": "runner_skipped_dust",
                        })
                        log_event(
                            "runner_skipped_dust",
                            f"{symbol} runner skipped; full exit",
                            f"remaining_value={remaining_value_usd:.4f} threshold={config.MIN_RUNNER_VALUE_USD:.4f} order={final_order.get('id')}",
                        )
                        return {"ok": True, "partial": True, "final": True, "pnl": total_net_pnl, "exit_price": exit_price}

                    pos["runner_mode"] = True
                    log_event("runner_activated", f"{symbol} runner mode",
                              f"remaining={remaining_after:.2%} value={remaining_value_usd:.2f} trail_atr={config.RUNNER_TRAIL_ATR_MULTI}")
            save_positions(current_positions)
            self.risk.state["daily_realized_pnl"] += net_pnl
            self._record_trade_result(symbol, net_pnl)
            add_pnl_record({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size": exit_size,
                "pnl": net_pnl,
                "reason": f"partial_{tp_level}",
            })
            log_event("partial_exit", f"{symbol} {tp_level}", f"exit_pct={exit_pct:.2f} pnl={net_pnl:.4f} remaining={pos['size']:.6f}")
            return {"ok": True, "partial": True, "final": False, "pnl": net_pnl, "exit_price": exit_price}

    def exit(self, position: dict[str, Any], reason: str) -> dict[str, Any]:
        symbol = position["symbol"]
        current_positions = load_positions()
        if not any(p["symbol"] == symbol for p in current_positions):
            return {"ok": False, "reason": "already_exited"}
        size = float(position["size"])
        ticker = self.exchange.fetch_ticker(symbol)
        exit_price = float(ticker.get("last") or position["entry_price"])

        if config.DRY_RUN:
            order = {"id": "dry-run-sell"}
        else:
            sl_order_id = position.get("sl_order_id")
            if sl_order_id:
                try:
                    self.exchange.cancel_order(symbol, sl_order_id)
                except Exception:
                    pass
            spot_balance = self.exchange.fetch_balance_spot(symbol.replace("USDT", ""))
            if spot_balance < size * 0.1:
                log_event("exit_skip", f"{symbol} already exited by SL", f"balance={spot_balance:.6f}")
                positions = [p for p in load_positions() if p["symbol"] != symbol]
                save_positions(positions)
                return {"ok": True, "order": {"id": "sl-already-filled"}, "pnl": 0.0,
                        "exit_price": exit_price, "pnl_pct": 0.0, "already_exited": True}
            # 안전 매도: 저장된 size와 실제 지갑 잔액 중 작은 쪽 사용
            # float drift로 인한 170131 에러 방지 (SOL 0.1712 저장 vs 0.1707 실제)
            meta = self.exchange.symbol_meta(symbol)
            lot_step = meta['lot_step']
            max_sell = round(spot_balance // lot_step * lot_step, 6) if lot_step > 0 else spot_balance
            safe_size = min(size, max_sell)
            if safe_size <= 0:
                log_event("exit_skip", f"{symbol} zero balance", f"max_sell={max_sell:.6f} spot_balance={spot_balance:.6f}")
                positions = [p for p in load_positions() if p["symbol"] != symbol]
                save_positions(positions)
                return {"ok": True, "order": {"id": "zero-balance"}, "pnl": 0.0,
                        "exit_price": exit_price, "pnl_pct": 0.0, "already_exited": True}
            try:
                order = self.exchange.create_market_sell(symbol, safe_size)
                self._record_api_success()
            except Exception as exc:
                self._record_api_error(exc)
                raise

        entry_price = float(position["entry_price"])
        gross_pnl = (exit_price - entry_price) * size
        fee = (exit_price * size + entry_price * size) * 0.001
        net_pnl = gross_pnl - fee
        self.risk.record_exit(symbol, net_pnl)
        self._record_trade_result(symbol, net_pnl)

        positions = [p for p in load_positions() if p["symbol"] != symbol]
        save_positions(positions)
        pnl_pct = ((exit_price / entry_price) - 1.0) * 100.0 if entry_price > 0 else 0.0
        add_pnl_record({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "pnl": net_pnl,
            "reason": reason,
        })
        log_event("exit", f"{symbol} exited", f"reason={reason} pnl={net_pnl:.4f}")
        return {"ok": True, "order": order, "pnl": net_pnl, "exit_price": exit_price, "pnl_pct": pnl_pct}
