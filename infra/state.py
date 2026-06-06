from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import LOGS_DIR

POSITIONS_FILE = LOGS_DIR / "positions.json"
PNL_FILE = LOGS_DIR / "pnl_history.json"
RISK_STATE_FILE = LOGS_DIR / "risk_state.json"
SNAPSHOT_FILE = LOGS_DIR / "news_snapshots.json"
LATEST_AI_FILE = LOGS_DIR / "latest_ai.json"

# TradFi 전용 상태 파일 (코인봇과 분리)
POSITIONS_FILE_TRADFI = LOGS_DIR / "positions_tradfi.json"
PNL_FILE_TRADFI = LOGS_DIR / "pnl_history_tradfi.json"
RISK_STATE_FILE_TRADFI = LOGS_DIR / "risk_state_tradfi.json"
LATEST_AI_FILE_TRADFI = LOGS_DIR / "latest_ai_tradfi.json"


def _positions_file(account: str = "spot") -> Path:
    return POSITIONS_FILE_TRADFI if account == "tradfi" else POSITIONS_FILE


def _pnl_file(account: str = "spot") -> Path:
    return PNL_FILE_TRADFI if account == "tradfi" else PNL_FILE


def _risk_state_file(account: str = "spot") -> Path:
    return RISK_STATE_FILE_TRADFI if account == "tradfi" else RISK_STATE_FILE


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    """원자적(atomic) write: .tmp에 쓴 후 rename으로 교체."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)


def load_positions() -> list[dict[str, Any]]:
    return _read_json(POSITIONS_FILE, [])


def save_positions(positions: list[dict[str, Any]]) -> None:
    """원자적 atomic write (via _write_json)."""
    _write_json(POSITIONS_FILE, positions)


def add_pnl_record(record: dict[str, Any]) -> None:
    data = _read_json(PNL_FILE, [])
    key_fields = {"symbol", "entry_price", "exit_price", "pnl", "reason"}
    rec_key = {k: record.get(k) for k in key_fields}
    for existing in data:
        existing_key = {k: existing.get(k) for k in key_fields}
        if rec_key == existing_key:
            return
    data.append(record)
    _write_json(PNL_FILE, data[-5000:])


def _default_risk_state() -> dict[str, Any]:
    return {
        "day": datetime.now(timezone.utc).date().isoformat(),
        "daily_realized_pnl": 0.0,
        "consecutive_losses": 0,
        "global_cooldown_until": None,
        "symbol_cooldowns": {},
        # DCA 상태: { "symbol": {"level": 1, "dca_at": "ISO"} }
        "dca_state": {},
        # 일일 수익 보호: profit_hit_at 기록
        "daily_profit_hit": None,
        # 상관관계 캐시: { "symbol": correlation_value } (24h)
        "btc_correlations": {},
    }


def load_risk_state(account: str = "spot") -> dict[str, Any]:
    path = _risk_state_file(account)
    state = _read_json(path, None)
    if state is None:
        state = _default_risk_state()
        _write_json(path, state)  # persist immediately so restart doesn't lose it
    return state


def save_risk_state(state: dict[str, Any], account: str = "spot") -> None:
    _write_json(_risk_state_file(account), state)


def append_news_snapshot(snapshot: dict[str, Any]) -> None:
    data = _read_json(SNAPSHOT_FILE, [])
    data.append(snapshot)
    _write_json(SNAPSHOT_FILE, data[-1000:])


def load_latest_ai() -> dict[str, Any]:
    return _read_json(LATEST_AI_FILE, {"sectors": {"sectors": []}, "candidates": []})


def save_latest_ai(data: dict[str, Any]) -> None:
    _write_json(LATEST_AI_FILE, data)
