from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import LOGS_DIR

EVENT_FILE = LOGS_DIR / "event_log.json"
MAX_EVENTS = 200


def _load() -> list[dict[str, Any]]:
    try:
        if EVENT_FILE.exists():
            return json.loads(EVENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save(items: list[dict[str, Any]]) -> None:
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENT_FILE.write_text(
        json.dumps(items[-MAX_EVENTS:], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def log_event(event_type: str, message: str, detail: str = "") -> None:
    events = _load()
    events.append(
        {
            "ts": int(time.time()),
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "type": event_type,
            "msg": message,
            "detail": detail[:500],
        }
    )
    _save(events)


def get_recent_events(count: int = 50) -> list[dict[str, Any]]:
    return _load()[-count:]
