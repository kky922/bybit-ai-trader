"""Context-from bridge reader for coin_bot and tradfi_bot.

Reads ~/.hermes/cron/bridge_state.json produced by info_strategy_closed_loop_bridge.py
and provides structured access to regime, risk flags, and signal metadata.

Two-Tier usage pattern
----------------------
Tier 1 (always): load snapshot, compare hash, update heartbeat
Tier 2 (on change): trigger GPT refresh / regime-dependent logic

    ctx = load_context()
    if ctx.is_stale():
        return  # bridge not updated — skip Tier 2
    if ctx.hash_changed(last_hash):
        last_hash = ctx.hash
        # run expensive Tier-2 work
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BRIDGE_STATE_FILE = Path.home() / ".hermes" / "cron" / "bridge_state.json"
CONTEXT_HASH_FILE = Path(__file__).resolve().parent.parent / "logs" / "context_from_hash.json"

logger = logging.getLogger(__name__)


@dataclass
class ContextSnapshot:
    hash: str
    ts: str
    report_file: str
    status: str                      # "proposed" | "blocked"
    targets: list[str]               # e.g. ["coin-bot"], ["tradfi-bot"], ["coin-bot", "stock-bot"]
    regime: str                      # "bull" | "bear" | "neutral" | "unknown"
    confidence: str                  # "1" | "3" | "5" | "unknown"
    action_bias: str                 # "aggressive" | "defensive" | "neutral" | "unknown"
    urgency: str                     # "high" | "medium" | "low" | "unknown"
    risk_flags: str
    suggested_size: str              # "increase" | "reduce" | "hold" | "unknown"
    expiry: str
    evidence: str
    loaded_at: float = field(default_factory=time.time)

    # ── helpers ──────────────────────────────────────────────────────────────

    def targets_coin(self) -> bool:
        return "coin-bot" in self.targets

    def targets_tradfi(self) -> bool:
        return "tradfi-bot" in self.targets

    def targets_stock(self) -> bool:
        return "stock-bot" in self.targets

    def is_actionable(self) -> bool:
        return self.status == "proposed"

    def is_bear(self) -> bool:
        return "bear" in self.regime.lower()

    def is_bull(self) -> bool:
        return "bull" in self.regime.lower()

    def is_defensive(self) -> bool:
        return "defensive" in self.action_bias.lower()

    def confidence_score(self) -> int:
        """Return numeric confidence: 1 (low), 3 (medium), 5 (high)."""
        try:
            return int(self.confidence)
        except (ValueError, TypeError):
            return 0

    def is_stale(self, max_age_hours: float = 6.0) -> bool:
        """Return True if the bridge state is older than max_age_hours."""
        try:
            ts_dt = datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 3600.0
            return age > max_age_hours
        except Exception:
            return True

    def hash_changed(self, previous_hash: Optional[str]) -> bool:
        return self.hash != previous_hash

    def __repr__(self) -> str:
        return (
            f"ContextSnapshot(hash={self.hash}, regime={self.regime}, "
            f"targets={self.targets}, bias={self.action_bias}, "
            f"confidence={self.confidence}, status={self.status})"
        )


_NULL_SNAPSHOT = ContextSnapshot(
    hash="none",
    ts="",
    report_file="unavailable",
    status="blocked",
    targets=["none"],
    regime="unknown",
    confidence="unknown",
    action_bias="unknown",
    urgency="unknown",
    risk_flags="unavailable",
    suggested_size="unknown",
    expiry="unknown",
    evidence="unavailable",
)


def load_context() -> ContextSnapshot:
    """Load the latest bridge state. Returns a null snapshot on any error."""
    try:
        if not BRIDGE_STATE_FILE.exists():
            logger.debug("bridge_state.json not found: %s", BRIDGE_STATE_FILE)
            return _NULL_SNAPSHOT
        raw = json.loads(BRIDGE_STATE_FILE.read_text(encoding="utf-8"))
        return ContextSnapshot(
            hash=str(raw.get("hash", "none")),
            ts=str(raw.get("ts", "")),
            report_file=str(raw.get("report_file", "")),
            status=str(raw.get("status", "blocked")),
            targets=list(raw.get("targets", ["none"])),
            regime=str(raw.get("regime", "unknown")),
            confidence=str(raw.get("confidence", "unknown")),
            action_bias=str(raw.get("action_bias", "unknown")),
            urgency=str(raw.get("urgency", "unknown")),
            risk_flags=str(raw.get("risk_flags", "unknown")),
            suggested_size=str(raw.get("suggested_size", "unknown")),
            expiry=str(raw.get("expiry", "unknown")),
            evidence=str(raw.get("evidence", "")),
        )
    except Exception as exc:
        logger.warning("context_from: failed to load bridge state: %s", exc)
        return _NULL_SNAPSHOT


def load_last_hash() -> Optional[str]:
    """Load the hash from the previous context_from check."""
    try:
        if CONTEXT_HASH_FILE.exists():
            data = json.loads(CONTEXT_HASH_FILE.read_text(encoding="utf-8"))
            return data.get("hash")
    except Exception:
        pass
    return None


def save_last_hash(hash_value: str) -> None:
    """Persist the current hash so next invocation can detect changes."""
    try:
        CONTEXT_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_HASH_FILE.write_text(
            json.dumps({"hash": hash_value, "ts": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("context_from: failed to save hash: %s", exc)


def check_tier2_trigger(bot_name: str) -> tuple[bool, ContextSnapshot]:
    """Tier-1 check: load context, compare hash, return (should_run_tier2, snapshot).

    bot_name: "coin-bot" | "tradfi-bot" | "stock-bot"
    Returns (True, ctx) when Tier 2 should execute (regime shift or new signal).
    Returns (False, ctx) when nothing changed — callers should skip expensive work.
    """
    ctx = load_context()
    last_hash = load_last_hash()

    if ctx.is_stale():
        logger.info("context_from[%s]: bridge state stale — Tier2 skipped", bot_name)
        return False, ctx

    if not ctx.hash_changed(last_hash):
        logger.info("context_from[%s]: no change (hash=%s) — Tier2 skipped", bot_name, ctx.hash)
        return False, ctx

    if not ctx.is_actionable():
        logger.info("context_from[%s]: status=blocked — Tier2 skipped", bot_name)
        save_last_hash(ctx.hash)
        return False, ctx

    target_check = {
        "coin-bot": ctx.targets_coin,
        "tradfi-bot": ctx.targets_tradfi,
        "stock-bot": ctx.targets_stock,
    }.get(bot_name, lambda: False)

    if not target_check():
        logger.info(
            "context_from[%s]: not targeted (targets=%s) — Tier2 skipped",
            bot_name, ctx.targets,
        )
        save_last_hash(ctx.hash)
        return False, ctx

    logger.info(
        "context_from[%s]: Tier2 triggered (hash=%s → %s, regime=%s, bias=%s)",
        bot_name, last_hash, ctx.hash, ctx.regime, ctx.action_bias,
    )
    save_last_hash(ctx.hash)
    return True, ctx
