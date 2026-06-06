from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import config

logger = logging.getLogger(__name__)


_SECTOR_STOP_WORDS = frozenset(
    {
        "&",
        "and",
        "the",
        "for",
        "of",
        "to",
        "in",
        "or",
        "a",
        "an",
        "by",
        "on",
        "at",
        "is",
        "it",
        "as",
        "be",
        "with",
        "its",
        "no",
        "not",
        "but",
        "so",
        "if",
    }
)

_NORM_CHARS = str.maketrans("", "", "()/")


def _sector_matches(stored_sector: str, ai_sector_name: str) -> bool:
    s = stored_sector.lower().strip()
    a = ai_sector_name.lower().strip()
    if s == a or s in a or a in s:
        return True
    _norm = lambda t: t.translate(_NORM_CHARS).split()
    s_words = {w for w in _norm(s) if w not in _SECTOR_STOP_WORDS and len(w) >= 2}
    a_words = {w for w in _norm(a) if w not in _SECTOR_STOP_WORDS and len(w) >= 2}
    if s_words & a_words:
        return True
    s_prefixes = set()
    for w in s_words:
        s_prefixes.update(w[:i] for i in range(4, len(w) + 1))
    for w in a_words:
        for i in range(4, len(w) + 1):
            if w[:i] in s_prefixes:
                return True
    return False


def check_partial_tp(position: dict[str, Any], candles_1h: pd.DataFrame) -> str | None:
    """Check if partial take-profit level is hit.
    Returns: 'tp1', 'tp2', 'tp3' or None.
    Skips levels that have already been taken (tracked in position['tp_taken']).
    Runner tranche (30%) has NO hard TP — only trailing stop."""
    price = float(candles_1h["close"].iloc[-1])
    entry = float(position["entry_price"])
    atr_val = float(position.get("atr", 0))
    tp_taken = set(position.get("tp_taken", []))
    remaining_pct = float(position.get("remaining_pct", 1.0))

    if atr_val <= 0:
        return None

    # Runner mode: after TP3, remaining 30% has no TPs
    if position.get("runner_mode") or remaining_pct <= config.TRAILING_REMAIN_PCT:
        return None

    # TP1
    if "tp1" not in tp_taken and price >= entry + config.TP_1_MULTI * atr_val:
        return "tp1"
    # TP2
    if "tp2" not in tp_taken and price >= entry + config.TP_2_MULTI * atr_val:
        return "tp2"
    # TP3
    if "tp3" not in tp_taken and price >= entry + config.TP_3_MULTI * atr_val:
        return "tp3"
    return None


def check_dca_trigger(position: dict[str, Any], candles_1h: pd.DataFrame) -> str | None:
    """Check if DCA (averaging down) level is hit.
    Returns: 'dca1', 'dca2', 'dca3' or None.
    Skips levels that have already been triggered (tracked in position['dca_triggered'])."""
    if not config.DCA_ENABLED:
        return None

    price = float(candles_1h["close"].iloc[-1])
    entry = float(position["entry_price"])
    dca_triggered = set(position.get("dca_triggered", []))

    pnl_pct = ((price / entry) - 1.0) * 100.0 if entry > 0 else 0.0

    # DCA는 손실 상태에서만 (가격이 진입가 아래)
    if pnl_pct >= 0:
        return None

    max_levels = max(0, int(config.DCA_MAX_LEVELS))

    if max_levels >= 1 and "dca1" not in dca_triggered and pnl_pct <= config.DCA_LEVEL_1_PCT:
        return "dca1"
    if max_levels >= 2 and "dca2" not in dca_triggered and pnl_pct <= config.DCA_LEVEL_2_PCT:
        return "dca2"
    if max_levels >= 3 and "dca3" not in dca_triggered and pnl_pct <= config.DCA_LEVEL_3_PCT:
        return "dca3"
    return None


def check_exit(
    position: dict[str, Any],
    candles_1h: pd.DataFrame,
    candles_4h: pd.DataFrame,
    latest_sector_analysis: dict[str, Any] | None,
) -> str | None:
    price = float(candles_1h["close"].iloc[-1])

    # 진입가 + 물타기 포함 평단 계산
    entry = float(position["entry_price"])
    size = float(position.get("size", 0))
    dca_entries = position.get("dca_entries", [])
    if dca_entries:
        total_cost = entry * size + sum(de["price"] * de["size"] for de in dca_entries)
        total_size = size + sum(de["size"] for de in dca_entries)
        if total_size > 0:
            entry = total_cost / total_size

    entered_at_raw = position.get("entered_at")
    entry_dt = None
    if entered_at_raw:
        try:
            entry_dt = datetime.fromisoformat(str(entered_at_raw).replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        except Exception:
            entry_dt = None
    # 안전장치: entry_dt 파싱 실패 시 보수적 기본값 사용 (그레이스 기간 보호)
    entry_dt_fallback = False
    if entry_dt is None and entered_at_raw is not None:
        entry_dt = datetime.now(timezone.utc)
        entry_dt_fallback = True

    runner_mode = position.get("runner_mode", False)
    highest = float(position.get("highest_price", 0))
    atr_val = float(position.get("atr", 0))

    # 하드 손절: 물타기 평단 기준 ATR×1.5 (Runner도 동일 적용)
    stop_loss = float(position.get("stop_loss", 0))
    if price <= stop_loss:
        return "stop_loss"

    # Runner 전용 챈들리어 트레일 (하드TP 없음)
    if runner_mode and config.RUNNER_ENABLED:
        # ATR 챈들리어 트레일
        if atr_val > 0 and highest > 0:
            chandelier = highest - config.RUNNER_TRAIL_ATR_MULTI * atr_val
            if price <= chandelier:
                return "runner_trail_atr"
        # 하드 퍼센트 트레일 (ATR 계산 실패시 백업)
        if highest > 0 and config.RUNNER_HARD_TRAIL_PCT > 0:
            hard_trail = highest * (1.0 - config.RUNNER_HARD_TRAIL_PCT / 100.0)
            if price <= hard_trail:
                return "runner_trail_hard"
        # Runner는 여기서 종료 (narrative fade, timeout 무시)
        return None

    # 하드 익절: 물타기 평단 기준 (일반 모드만)
    if not runner_mode:
        take_profit = float(position.get("take_profit", 0))
        if price >= take_profit:
            return "take_profit"

    # 포지션 시간 제한 (Runner는 무시)
    if entry_dt is not None and not (runner_mode and config.RUNNER_SKIP_TIMEOUT):
        age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600.0
        if age_hours > config.MAX_POSITION_HOURS:
            return "position_timeout"

    # 내러티브 소멸 (Runner는 무시)
    if latest_sector_analysis and not (runner_mode and config.RUNNER_SKIP_NARRATIVE_FADE):
        sector = position.get("sector")
        if sector:
            sectors = latest_sector_analysis.get("sectors", [])
            # Only narrative_fade if the sector was explicitly analyzed and found below threshold.
            # If the sector isn't in the analysis at all, treat it as unassessed — don't kill the position.
            exists_in_analysis = [x for x in sectors if _sector_matches(sector, x.get("name", ""))]
            if exists_in_analysis:
                exists_above_threshold = [x for x in exists_in_analysis if x.get("heat_score", 0) >= config.SECTOR_HEAT_EXIT_THRESHOLD]
                if not exists_above_threshold:
                    if entry_dt is not None:
                        age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600.0
                        if age_hours < config.NARRATIVE_FADE_GRACE_HOURS:
                            logger.debug(
                                "narrative_fade_grace: %s sector=%s age=%.2fh grace=%.1fh (protected)",
                                position.get("symbol", "?"), sector, age_hours, config.NARRATIVE_FADE_GRACE_HOURS,
                            )
                            return None
                    else:
                        age_hours = 0.0
                    logger.info(
                        "narrative_faded: %s sector=%s (analyzed, heat below %d) age=%.2fh grace=%.1fh",
                        position.get("symbol", "?"), sector, config.SECTOR_HEAT_EXIT_THRESHOLD,
                        age_hours if entry_dt is not None else 0,
                        config.NARRATIVE_FADE_GRACE_HOURS,
                    )
                    return "narrative_faded"
            else:
                logger.info(
                    "narrative_skip_unassessed: %s sector=%s (not in latest sector analysis, keeping position alive)",
                    position.get("symbol", "?"), sector,
                )

    # 트레일링 스탑 (남은 포지션용 — 일반 모드)
    if not runner_mode and position.get("highest_price") and position.get("atr"):
        trailing_stop = float(position["highest_price"]) - config.TRAILING_ATR_MULTI * float(position["atr"])
        if price <= trailing_stop:
            return "trailing_stop"

    return None


def detect_parabolic(position: dict[str, Any], candles_1h: pd.DataFrame, candles_4h: pd.DataFrame) -> bool:
    """Check if a position is in parabolic uptrend.
    Returns True if skyhook conditions met → switch to runner mode early.
    클로드 Opus 4.7: Use >40% in 7d OR >25% in 3d with volume expansion."""
    if not config.PARABOLIC_ENABLED:
        return False

    entry = float(position["entry_price"])
    if entry <= 0:
        return False

    current = float(candles_1h["close"].iloc[-1])
    pnl_pct = ((current / entry) - 1.0) * 100.0

    if pnl_pct <= 0:
        return False

    # Check 7d gain: position age
    entered_at_raw = position.get("entered_at")
    if entered_at_raw:
        try:
            entry_dt = datetime.fromisoformat(str(entered_at_raw).replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600.0
            # 7d threshold: gain > 40%
            if age_hours >= 72 and pnl_pct >= config.PARABOLIC_GAIN_7D_PCT:
                return True
            # 3d threshold: gain > 25%
            if age_hours >= 48 and pnl_pct >= config.PARABOLIC_GAIN_3D_PCT:
                return True
        except Exception:
            pass

    # Volume expansion check
    if len(candles_1h) >= 20:
        try:
            recent_vol = float(candles_1h["volume"].iloc[-24:].mean())
            avg_vol = float(candles_1h["volume"].iloc[-48:-24].mean())
            if avg_vol > 0 and (recent_vol / avg_vol) >= config.PARABOLIC_VOLUME_MIN:
                if pnl_pct >= config.PARABOLIC_GAIN_3D_PCT:
                    return True
        except Exception:
            pass

    return False
