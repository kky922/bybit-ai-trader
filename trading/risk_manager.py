from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import config
from infra.state import load_risk_state, save_risk_state


class RiskManager:
    def __init__(self, account: str = "spot") -> None:
        self.account = account
        self.state = load_risk_state(account)
        self._roll_day()

    def _save(self) -> None:
        save_risk_state(self.state, self.account)

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["daily_realized_pnl"] = 0.0
            self.state["daily_profit_hit"] = None
            # Don't reset global_cooldown_until, cooldown_ended_at, or
            # post_cooldown_scans — these manage cross-day risk controls
            # (e.g. a 6h cooldown triggered at 23:15 UTC must persist
            # past midnight UTC until it naturally expires).
            #
            # Do reset consecutive_losses so the daily loss-trigger chain
            # starts fresh. The global cooldown itself is *independent*.
            self.state["consecutive_losses"] = 0
            self._save()
        self._clean_expired_cooldowns()

    def can_trade(self, equity_usdt: float, btc_4h_change_pct: float = 0.0) -> tuple[bool, str]:
        self._roll_day()

        # 글로벌 쿨다운
        if self.state.get("global_cooldown_until"):
            until = datetime.fromisoformat(self.state["global_cooldown_until"])
            if datetime.now(timezone.utc) < until:
                return False, "global cooldown active"

        # 일일 손실 한도 (기존)
        if equity_usdt > 0 and self.state.get("daily_realized_pnl", 0.0) <= -(
            equity_usdt * config.DAILY_MAX_LOSS_PCT
        ):
            return False, "daily loss limit reached"

        # 일일 수익 목표 도달 → 신규 진입 중단
        if config.DAILY_PROFIT_STOP_NEW_ENTRIES and equity_usdt > 0:
            daily_pnl = self.state.get("daily_realized_pnl", 0.0)
            if daily_pnl >= equity_usdt * config.DAILY_PROFIT_TARGET_PCT:
                if self.state.get("daily_profit_hit") is None:
                    self.state["daily_profit_hit"] = datetime.now(timezone.utc).isoformat()
                    self._save()
                # DAILY_PROFIT_COOLDOWN_HOURS > 0이면 지정 시간만큼 대기, 0이면 익일 재개
                profit_hit_at = self.state.get("daily_profit_hit")
                if profit_hit_at and config.DAILY_PROFIT_COOLDOWN_HOURS > 0:
                    hit_dt = datetime.fromisoformat(profit_hit_at)
                    if datetime.now(timezone.utc) < hit_dt + timedelta(hours=config.DAILY_PROFIT_COOLDOWN_HOURS):
                        return False, "daily profit target reached (cooldown)"
                elif config.DAILY_PROFIT_COOLDOWN_HOURS == 0:
                    # 0이면 _roll_day에서 리셋되므로 여기 도달하면 하루 종일 중단
                    return False, "daily profit target reached (resumes tomorrow)"

        # BTC 급락 필터 (기존)
        if btc_4h_change_pct <= config.BTC_CRASH_FILTER_PCT_4H:
            return False, "btc crash filter"

        return True, "ok"

    def in_symbol_cooldown(self, symbol: str) -> bool:
        cooldowns = self.state.get("symbol_cooldowns", {})
        val = cooldowns.get(symbol)
        if not val:
            return False
        return datetime.now(timezone.utc) < datetime.fromisoformat(val)

    def in_dca_cooldown(self, symbol: str) -> bool:
        """DCA between levels: minimum DCA_COOLDOWN_MINUTES gap."""
        dca_state = self.state.get("dca_state", {})
        entry = dca_state.get(symbol)
        if not entry:
            return False
        last_dca_at = entry.get("dca_at")
        if not last_dca_at:
            return False
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_dca_at)).total_seconds()
        return elapsed < config.DCA_COOLDOWN_MINUTES * 60

    def get_dca_level(self, symbol: str) -> int:
        """Return current DCA level for this symbol (0 = none, 1-3 = levels triggered)."""
        dca_state = self.state.get("dca_state", {})
        entry = dca_state.get(symbol, {})
        return entry.get("level", 0)

    def record_dca(self, symbol: str) -> None:
        """Record that a DCA level was triggered."""
        dca_state = self.state.setdefault("dca_state", {})
        current = dca_state.get(symbol, {"level": 0})
        new_level = current["level"] + 1
        dca_state[symbol] = {
            "level": new_level,
            "dca_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def has_same_sector_position(self, symbol: str, sector: str, positions: list[dict]) -> bool:
        """Check if any existing position is in the same sector.
        Prevents over-concentration in one narrative."""
        if not sector:
            return False
        sector_lower = sector.lower()
        for p in positions:
            if p["symbol"] == symbol:
                continue
            ps = p.get("sector", "").lower()
            if ps and (ps == sector_lower or ps in sector_lower or sector_lower in ps):
                return True
        return False

    def record_exit(self, symbol: str, realized_pnl: float) -> None:
        self._roll_day()
        self.state["daily_realized_pnl"] = self.state.get("daily_realized_pnl", 0.0) + realized_pnl

        # DCA 상태 정리: 해당 심볼 DCA 상태 삭제
        dca_state = self.state.get("dca_state", {})
        if symbol in dca_state:
            del dca_state[symbol]
            self._save()

        # --- 심볼별 연속 손실 추적 ---
        sym_losses: dict[str, int] = self.state.setdefault("symbol_consecutive_losses", {})

        if realized_pnl < 0:
            self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
            if self.state["consecutive_losses"] >= config.CONSECUTIVE_LOSS_STOP:
                cooldown_until = datetime.now(timezone.utc) + timedelta(hours=config.GLOBAL_COOLDOWN_HOURS)
                self.state["global_cooldown_until"] = cooldown_until.isoformat()
                self.state["cooldown_ended_at"] = cooldown_until.isoformat()  # post-cooldown grace tracking
                self.state["post_cooldown_scans"] = 0
            # Increment per-symbol loss counter
            sym_losses[symbol] = sym_losses.get(symbol, 0) + 1
        else:
            self.state["consecutive_losses"] = 0
            self.state["global_cooldown_until"] = None
            self.state["cooldown_ended_at"] = None
            self.state["post_cooldown_scans"] = 0
            # Reset per-symbol loss counter on win
            sym_losses[symbol] = 0

        # --- 반복 손실 심볼: 연장 쿨다운 ---
        symbol_loss_count = sym_losses.get(symbol, 0)
        if symbol_loss_count >= config.REPEAT_LOSER_THRESHOLD:
            cooldown_hours = config.REPEAT_LOSER_COOLDOWN_HOURS
        else:
            cooldown_hours = config.PER_SYMBOL_COOLDOWN_HOURS

        cooldown_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
        self.state.setdefault("symbol_cooldowns", {})[symbol] = cooldown_until.isoformat()
        self._save()

    def in_post_cooldown_grace(self) -> tuple[bool, int]:
        """Check if bot is in post-cooldown grace period.

        After a global cooldown ends, the next N scans (POST_COOLDOWN_GRACE_SCANS)
        have reduced entry aggressiveness. Returns (in_grace, scans_remaining).

        Tracks scans since cooldown lifted via state['cooldown_ended_at'].
        """
        ended = self.state.get("cooldown_ended_at")
        if not ended:
            return False, 0
        # Check that cooldown actually ended
        now = datetime.now(timezone.utc)
        cooldown_until = self.state.get("global_cooldown_until")
        if cooldown_until and now < datetime.fromisoformat(cooldown_until):
            return False, 0  # cooldown still active, not in grace period yet
        scans_since = self.state.get("post_cooldown_scans", 0)
        remaining = max(0, config.POST_COOLDOWN_GRACE_SCANS - scans_since)
        return remaining > 0, remaining

    def record_post_cooldown_scan(self) -> None:
        """Called after each entry scan during post-cooldown grace period."""
        ended = self.state.get("cooldown_ended_at")
        if not ended:
            return
        self.state["post_cooldown_scans"] = self.state.get("post_cooldown_scans", 0) + 1
        self._save()

    def _clean_expired_cooldowns(self) -> None:
        cooldowns = self.state.get("symbol_cooldowns", {})
        if not cooldowns:
            return
        now = datetime.now(timezone.utc)
        before = len(cooldowns)
        self.state["symbol_cooldowns"] = {
            sym: iso for sym, iso in cooldowns.items()
            if now < datetime.fromisoformat(iso)
        }
        after = len(self.state["symbol_cooldowns"])
        if before != after:
            self._save()
