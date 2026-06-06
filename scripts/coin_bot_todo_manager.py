#!/usr/bin/env python3
"""Deterministic TODO manager for Coin Bot Phase A.

Reads the latest autopilot health snapshot and emits a concise TODO list only
when the derived TODO signature changes. Empty stdout = no change, so the cron
stays silent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "agents"
LATEST = DATA / "autopilot_latest.json"
STATE = DATA / "coin_bot_todo_state.json"
TODOS_FILE = DATA / "coin_bot_todos.json"


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _slug(text: str) -> str:
    return sha1(text.encode("utf-8")).hexdigest()[:10]


def _load_latest() -> dict[str, Any]:
    data = _read_json(LATEST, {})
    return data if isinstance(data, dict) else {}


def _derive_todos(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = report.get("issues", []) if isinstance(report.get("issues", []), list) else []
    suggestions = report.get("suggestions", []) if isinstance(report.get("suggestions", []), list) else []
    trade_summary = report.get("metrics", {}) if isinstance(report.get("metrics", {}), dict) else {}
    evidence = report.get("evidence", {}) if isinstance(report.get("evidence", {}), dict) else {}

    todos: list[dict[str, Any]] = []

    def add(priority: str, title: str, detail: str, source: str) -> None:
        todos.append(
            {
                "id": _slug(f"{priority}|{title}|{detail}"),
                "priority": priority,
                "title": title,
                "detail": detail,
                "source": source,
                "status": "open",
            }
        )

    for issue in issues:
        text = str(issue)
        if "프로세스가 내려가" in text or "log가" in text or "갱신되지 않음" in text:
            add("P0", "운영 상태 복구 확인", text, "issues")
        elif "예외" in text or "오류" in text or "Bybit/ccxt" in text:
            add("P0", "실거래 오류 원인 확인", text, "issues")
        elif "승률 저하" in text or "실현손익 음수" in text:
            add("P1", "전략 수익성 점검", text, "issues")
        elif "반복 손실 심볼" in text:
            add("P1", "반복 손실 심볼 쿨다운 검토", text, "issues")
        elif "연속 손실" in text:
            add("P1", "연속 손실 차단 정책 검토", text, "issues")
        elif "후보가 비어" in text:
            add("P1", "진입 후보 품질/필터 점검", text, "issues")

    for suggestion in suggestions:
        text = str(suggestion)
        if "narrative_faded" in text or "손절/익절 비율" in text:
            add("P1", "청산 로직 재평가", text, "suggestions")
        elif "진입 실패 사유" in text or "후보 품질" in text:
            add("P1", "후보 필터 재점검", text, "suggestions")
        elif "trailing" in text or "신규 진입보다 기존 포지션" in text:
            add("P2", "기존 포지션 관리 우선", text, "suggestions")
        elif "쿨다운" in text:
            add("P2", "쿨다운 정책 재검토", text, "suggestions")

    # Live snapshot-based TODOs for the current Phase A setup.
    if report.get("health") in {"warn", "critical"} and trade_summary.get("recent_pnl", 0) < 0:
        add("P1", "최근 손익 음수 구간 추적", f"recent_pnl={trade_summary.get('recent_pnl')}", "metrics")
    if trade_summary.get("repeat_losers"):
        top = trade_summary["repeat_losers"][0]
        add("P1", "반복 손실 심볼 관찰", f"{top.get('symbol')} losses={top.get('losses')} pnl={top.get('pnl')}", "evidence")
    if report.get("open_positions", 0) == 0 and report.get("candidate_count", 0) > 0:
        add("P2", "진입 대기 후보 상태 확인", f"candidates={report.get('candidate_count')} open_positions=0", "metrics")

    # De-duplicate by stable id while preserving order.
    seen = set()
    deduped = []
    for item in todos:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        deduped.append(item)
    return deduped


def _signature(report: dict[str, Any], todos: list[dict[str, Any]]) -> str:
    payload = {
        "health": report.get("health"),
        "issues": report.get("issues", []),
        "todos": [(t["priority"], t["title"], t["detail"]) for t in todos],
    }
    return _slug(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def main() -> int:
    report = _load_latest()
    if not report:
        return 0

    todos = _derive_todos(report)
    if not todos:
        return 0

    sig = _signature(report, todos)
    state = _read_json(STATE, {})
    if isinstance(state, dict) and state.get("last_signature") == sig:
        return 0

    payload = {
        "generated_at": _now_iso(),
        "signature": sig,
        "source_report_generated_at": report.get("generated_at"),
        "health": report.get("health"),
        "todos": todos,
    }
    _write_json(TODOS_FILE, payload)
    _write_json(STATE, {"last_signature": sig, "updated_at": _now_iso()})

    lines = ["## Coin Bot TODO"]
    for item in todos[:6]:
        lines.append(f"- [{item['priority']}] {item['title']}: {item['detail']}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
