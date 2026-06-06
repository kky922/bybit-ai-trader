#!/usr/bin/env python3
"""Coin bot health collector for autonomous improvement loop.

No-agent cron behavior:
- Always writes a full snapshot to data/agents/autopilot_latest.json.
- Prints JSON only when an issue/opportunity signature changed, or after a reset window.
- Stays silent on repeated identical issues so Telegram does not get spammed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
DATA = ROOT / "data" / "agents"
OUTFILE = DATA / "autopilot_latest.json"
STATE_FILE = DATA / "autopilot_state.json"
HISTORY_FILE = DATA / "autopilot_history.jsonl"

RECENT_LOG_LINES = 300
RECENT_TRADE_COUNT = 20
RECENT_EVENT_COUNT = 80
SILENT_RESET_SECONDS = 6 * 60 * 60

PATTERNS = {
    "gemini_error": re.compile(r"Gemini|google.*api|503|429|rate limit|quota", re.I),
    "bybit_error": re.compile(r"Bybit|ccxt|ExchangeError|NetworkError|DDoSProtection|RequestTimeout", re.I),
    "order_fail": re.compile(r"order_fail|create_.*order|주문.*실패|failed.*order", re.I),
    "exception": re.compile(r"Traceback|Unhandled exception|\bCRITICAL\b|\bException\b", re.I),
    # Only count genuine stale-lock / stale-PID conditions, not normal "lock acquired" startup logs.
    "lock_stale": re.compile(r"(?:stale\s+(?:lock|pid)|(?:lock|pid)\s+file\s+stale|lock\s+stale)", re.I),
    "duplicate": re.compile(r"duplicate_symbol|duplicate rejected", re.I),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _append_history(report: dict[str, Any]) -> None:
    """Persist every health snapshot as JSONL for live-readiness analysis.

    `autopilot_latest.json` is intentionally overwritten for quick status checks,
    but live-trading readiness needs a time series: uptime, stale-log intervals,
    candidate availability, trade outcomes, and recurring issues over days/weeks.
    """
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")


def _read_log_delta(path: Path, previous_bytes: int | None, limit: int = RECENT_LOG_LINES) -> tuple[list[str], int]:
    if not path.exists():
        return [], 0
    try:
        size = path.stat().st_size
        start = int(previous_bytes or 0)
        if start < 0 or start > size:
            start = 0
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read()
        lines = chunk.decode("utf-8", errors="ignore").splitlines()
        return lines[-limit:], size
    except Exception:
        return [], 0


def _file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_proc_alive(pid_file: Path) -> tuple[bool, int | None]:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, pid


def _launchctl_pid(label: str = "com.coinbot.app") -> tuple[bool, int | None]:
    """Return launchd PID fallback when PID file is missing/stale."""
    try:
        proc = subprocess.run(
            ["launchctl", "list", label],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return False, None
        match = re.search(r'"PID"\s*=\s*(\d+);', proc.stdout)
        if not match:
            return False, None
        pid = int(match.group(1))
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, None


def _count_patterns(lines: list[str]) -> dict[str, int]:
    text = "\n".join(lines)
    return {name: len(rx.findall(text)) for name, rx in PATTERNS.items()}


def _event_counter(events: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(e.get("type", "unknown")) for e in events[-RECENT_EVENT_COUNT:]))


def _summarize_trades(trades: list[dict[str, Any]], recent_count: int = RECENT_TRADE_COUNT) -> dict[str, Any]:
    normalized = [t for t in trades if isinstance(t, dict)]
    recent = normalized[-recent_count:]

    def pnl(row: dict[str, Any]) -> float:
        try:
            return float(row.get("pnl", 0) or 0)
        except Exception:
            return 0.0

    def win_rate(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        return sum(1 for x in rows if pnl(x) > 0) / len(rows) * 100.0

    repeat: dict[str, dict[str, Any]] = defaultdict(lambda: {"losses": 0, "trades": 0, "pnl": 0.0})
    for t in recent:
        symbol = str(t.get("symbol", ""))
        if not symbol:
            continue
        repeat[symbol]["trades"] += 1
        repeat[symbol]["pnl"] += pnl(t)
        if pnl(t) < 0:
            repeat[symbol]["losses"] += 1
    repeat_losers = [
        {"symbol": symbol, **stats}
        for symbol, stats in repeat.items()
        if stats["losses"] >= 2
    ]
    repeat_losers.sort(key=lambda x: (x["losses"], -x["pnl"]), reverse=True)

    exit_reasons = Counter(str(t.get("reason", "unknown")) for t in recent)
    latest_times = [_parse_dt(t.get("ts") or t.get("timestamp")) for t in normalized]
    latest_times = [t for t in latest_times if t]

    return {
        "trade_count": len(normalized),
        "recent_trade_count": len(recent),
        "total_pnl": round(sum(pnl(t) for t in normalized), 4),
        "recent_pnl": round(sum(pnl(t) for t in recent), 4),
        "win_rate_pct": round(win_rate(normalized), 1),
        "recent_win_rate_pct": round(win_rate(recent), 1),
        "avg_recent_pnl": round(sum(pnl(t) for t in recent) / len(recent), 4) if recent else 0.0,
        "exit_reasons": dict(exit_reasons),
        "repeat_losers": repeat_losers[:10],
        "latest_trade_time": max(latest_times).isoformat() if latest_times else None,
    }


def _candidate_count(latest_ai: Any) -> int:
    if not isinstance(latest_ai, dict):
        return 0
    candidates = latest_ai.get("candidates", [])
    return len(candidates) if isinstance(candidates, list) else 0


def _sector_count(latest_ai: Any) -> int:
    if not isinstance(latest_ai, dict):
        return 0
    sectors = latest_ai.get("sectors", {})
    if isinstance(sectors, dict):
        inner = sectors.get("sectors", [])
        return len(inner) if isinstance(inner, list) else 0
    if isinstance(sectors, list):
        return len(sectors)
    return 0


def _bucket(value: float | int | None, step: int) -> int:
    if value is None:
        return -1
    try:
        return int(float(value) // step)
    except Exception:
        return -1


def _load_state() -> dict[str, Any]:
    state = _read_json(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def _should_emit(
    signature: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    now_ts: float,
    silent_reset_seconds: int = SILENT_RESET_SECONDS,
) -> bool:
    state = _load_state() if state is None else state
    if signature != state.get("last_signature"):
        return True
    try:
        last_sent_at = float(state.get("last_sent_at", 0) or 0)
    except Exception:
        last_sent_at = 0.0
    return now_ts - last_sent_at >= silent_reset_seconds


def _persist_state(signature: dict[str, Any], emitted: bool, report: dict[str, Any]) -> None:
    prior = _load_state()
    state = {
        "last_signature": signature,
        "last_sent_at": datetime.now(timezone.utc).timestamp() if emitted else prior.get("last_sent_at", 0),
        "last_report": report,
        "log_offsets": report.get("log_offsets", {}),
        "updated_at": _now_iso(),
    }
    _write_json(STATE_FILE, state)


def _build_report() -> dict[str, Any]:
    prior_state = _load_state()
    log_offsets = prior_state.get("log_offsets", {}) if isinstance(prior_state, dict) else {}

    coin_alive, coin_pid = _is_proc_alive(LOGS / "coin_bot.pid")
    legacy_alive, legacy_pid = _is_proc_alive(ROOT / "pids" / "coin_bot.pid")
    launch_alive, launch_pid = _launchctl_pid()
    coin_alive = coin_alive or legacy_alive or launch_alive
    coin_pid = coin_pid or legacy_pid or launch_pid

    log_tail, log_bytes = _read_log_delta(LOGS / "coin_bot.log", log_offsets.get("coin_bytes", 0))
    out_tail, out_bytes = _read_log_delta(LOGS / "coin_bot.out", log_offsets.get("out_bytes", 0))
    bot_tail, bot_bytes = _read_log_delta(LOGS / "bot_output.log", log_offsets.get("bot_bytes", 0))
    notable_errors = dict(Counter(_count_patterns(log_tail)) + Counter(_count_patterns(out_tail)) + Counter(_count_patterns(bot_tail)))

    positions = _read_json(LOGS / "positions.json", [])
    trades = _read_json(LOGS / "pnl_history.json", [])
    risk_state = _read_json(LOGS / "risk_state.json", {})
    events = _read_json(LOGS / "event_log.json", [])
    latest_ai = _read_json(LOGS / "latest_ai.json", {"sectors": {"sectors": []}, "candidates": []})
    news_snapshots = _read_json(LOGS / "news_snapshots.json", [])

    positions = positions if isinstance(positions, list) else []
    trades = trades if isinstance(trades, list) else []
    events = events if isinstance(events, list) else []
    trade_summary = _summarize_trades(trades)
    event_counts = _event_counter(events)

    coin_log_age = _file_age_minutes(LOGS / "coin_bot.log")
    latest_ai_age = _file_age_minutes(LOGS / "latest_ai.json")
    event_log_age = _file_age_minutes(LOGS / "event_log.json")
    candidate_count = _candidate_count(latest_ai)
    sector_count = _sector_count(latest_ai)
    consecutive_losses = int((risk_state or {}).get("consecutive_losses", 0) or 0) if isinstance(risk_state, dict) else 0
    daily_pnl = float((risk_state or {}).get("daily_realized_pnl", 0) or 0) if isinstance(risk_state, dict) else 0.0
    global_cooldown_until = (risk_state or {}).get("global_cooldown_until") if isinstance(risk_state, dict) else None
    symbol_cooldowns = (risk_state or {}).get("symbol_cooldowns", {}) if isinstance(risk_state, dict) else {}

    issues: list[str] = []
    notes: list[str] = []
    suggestions: list[str] = []

    if not coin_alive:
        issues.append("코인봇 프로세스가 내려가 있음")
        suggestions.append("watchdog_coin.sh로 즉시 재기동하고 PID 파일/launchctl 상태 확인")
    # 20분 threshold는 heartbeat 간격(1h)보다 짧아 항상 false-positive. 70분으로 완화.
    LOG_STALE_THRESHOLD_MIN = 70
    if coin_log_age is not None and coin_log_age > LOG_STALE_THRESHOLD_MIN:
        issues.append(f"coin_bot.log가 {coin_log_age:.0f}분 이상 갱신되지 않음")
        suggestions.append("메인 루프 lock/예외/거래소 API 대기 상태 확인")
    if latest_ai_age is not None and latest_ai_age > 360:
        issues.append(f"AI 후보 파일이 {latest_ai_age/60:.1f}시간 이상 갱신되지 않음")
        suggestions.append("뉴스 수집/Gemini refresh/GPT_REFRESH_HOURS 경로 점검")
    if candidate_count == 0:
        issues.append("최근 AI 매수 후보가 비어 있음")
        suggestions.append("뉴스 소스·유니버스 필터·Gemini 응답 실패 여부 점검")
    if notable_errors.get("gemini_error", 0) >= 2:
        issues.append(f"Gemini/API 오류 감지 {notable_errors['gemini_error']}건")
        suggestions.append("Gemini 재시도/백오프와 후보 캐시 fallback 유지 여부 점검")
    if notable_errors.get("bybit_error", 0) >= 1:
        issues.append(f"Bybit/ccxt 오류 감지 {notable_errors['bybit_error']}건")
        suggestions.append("ccxt timeout/retry 및 API 키/서브계좌 권한 확인")
    if notable_errors.get("order_fail", 0) >= 1:
        issues.append(f"주문 실패 로그 감지 {notable_errors['order_fail']}건")
        suggestions.append("실거래 전 최소주문금액·수량 step·잔고 계산을 회귀 테스트로 확인")
    if notable_errors.get("exception", 0) >= 1:
        issues.append(f"예외/Traceback 감지 {notable_errors['exception']}건")
        suggestions.append("Traceback 원인 파일 기준으로 테스트 추가 후 수정")
    if consecutive_losses >= 2:
        issues.append(f"연속 손실 {consecutive_losses}회")
        suggestions.append("동일 심볼/섹터 쿨다운과 진입 필터를 일시 강화")
    if trade_summary["recent_trade_count"] >= 2 and trade_summary["recent_win_rate_pct"] < 45:
        issues.append(f"최근 {trade_summary['recent_trade_count']}건 승률 저하 ({trade_summary['recent_win_rate_pct']}%)")
        suggestions.append("RSI/거래량/ATR 진입조건을 최근 손실 케이스 기준으로 보수화")
    if trade_summary["recent_trade_count"] >= 2 and trade_summary["recent_pnl"] < 0:
        issues.append(f"최근 실현손익 음수 ({trade_summary['recent_pnl']} USDT)")
        suggestions.append("손절/익절 비율과 narrative_faded 청산 기준 재평가")
    if trade_summary["repeat_losers"]:
        top = trade_summary["repeat_losers"][0]
        issues.append(f"반복 손실 심볼 감지: {top['symbol']} 손실 {top['losses']}회")
        suggestions.append("반복 손실 심볼은 PER_SYMBOL_COOLDOWN_HOURS보다 긴 쿨다운 후보")
    if event_counts.get("risk_block", 0) >= 3:
        notes.append(f"risk_block 이벤트 {event_counts['risk_block']}건")
        suggestions.append("리스크 차단이 과도한지/필요한 보호인지 risk_state와 함께 판단")
    if event_counts.get("entry_skip", 0) >= 10:
        notes.append(f"entry_skip 이벤트 {event_counts['entry_skip']}건")
        suggestions.append("진입 실패 사유 상위 항목을 집계해 후보 품질 또는 필터 임계값 조정")
    if len(positions) >= 3:
        notes.append(f"포지션 슬롯 {len(positions)}개 사용 중")
        suggestions.append("신규 진입보다 기존 포지션 trailing/청산 관리 우선")

    signature = {
        "coin_alive": coin_alive,
        "coin_log_age_bucket": _bucket(coin_log_age, 15),
        "latest_ai_age_bucket": _bucket(latest_ai_age, 60),
        "candidate_count": candidate_count,
        "consecutive_losses": consecutive_losses,
        "recent_win_rate_bucket": _bucket(trade_summary["recent_win_rate_pct"], 10),
        "recent_pnl_bucket": _bucket(trade_summary["recent_pnl"], 5),
        "open_positions": len(positions),
        "repeat_loser_symbols": [x["symbol"] for x in trade_summary["repeat_losers"][:3]],
        "gemini_error_bucket": _bucket(notable_errors.get("gemini_error", 0), 2),
        "bybit_error_bucket": _bucket(notable_errors.get("bybit_error", 0), 1),
        "exception_bucket": _bucket(notable_errors.get("exception", 0), 1),
        "latest_trade_time": trade_summary["latest_trade_time"],
    }

    report = {
        "generated_at": _now_iso(),
        "health": "ok" if not issues else ("warn" if coin_alive else "critical"),
        "signals": {
            "coin_alive": coin_alive,
            "coin_pid": coin_pid,
            "coin_log_age_min": round(coin_log_age, 1) if coin_log_age is not None else None,
            "latest_ai_age_min": round(latest_ai_age, 1) if latest_ai_age is not None else None,
            "event_log_age_min": round(event_log_age, 1) if event_log_age is not None else None,
            "notable_errors": notable_errors,
            "event_counts": event_counts,
        },
        "metrics": {
            **trade_summary,
            "open_positions": len(positions),
            "candidate_count": candidate_count,
            "sector_count": sector_count,
            "daily_realized_pnl": round(daily_pnl, 4),
            "consecutive_losses": consecutive_losses,
            "active_symbol_cooldowns": len(symbol_cooldowns) if isinstance(symbol_cooldowns, dict) else 0,
        },
        "issues": issues,
        "notes": notes,
        "suggestions": list(dict.fromkeys(suggestions)),
        "evidence": {
            "global_cooldown_until": global_cooldown_until,
            "recent_loss_symbols": [
                {"symbol": t.get("symbol"), "pnl": round(float(t.get("pnl", 0) or 0), 4), "reason": t.get("reason")}
                for t in trades[-10:]
                if isinstance(t, dict) and float(t.get("pnl", 0) or 0) < 0
            ][-5:],
            "recent_events": events[-10:],
            "news_snapshot_count": len(news_snapshots) if isinstance(news_snapshots, list) else None,
        },
        "signature": signature,
        "log_offsets": {"coin_bytes": log_bytes, "out_bytes": out_bytes, "bot_bytes": bot_bytes},
    }
    return report


def main() -> int:
    report = _build_report()
    now_ts = datetime.now(timezone.utc).timestamp()
    emit = bool(report["issues"]) and _should_emit(report["signature"], now_ts=now_ts)
    _write_json(OUTFILE, report)
    _append_history(report)
    _persist_state(report["signature"], emit, report)
    if emit:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
