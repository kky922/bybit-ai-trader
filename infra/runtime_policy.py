from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import config

logger = logging.getLogger("runtime_policy")


@dataclass(frozen=True)
class RuntimePolicy:
    """Runtime guardrails derived from deterministic agent TODOs.

    Fail-open by design: malformed/missing TODO files should not stop trading.
    Only open P0/P1 TODOs affect live behavior.
    """

    block_new_entries: bool = False
    block_dca: bool = False
    conservative_mode: bool = False
    excluded_symbols: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()


_OPEN_STATUSES = {"open", "pending", "in_progress", "todo", "doing"}
_SYMBOL_RE = re.compile(r"\b[A-Z0-9]{2,15}USDT\b")


def _todos_path() -> Path:
    return config.DATA_DIR / "agents" / "coin_bot_todos.json"


def _read_todos(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("runtime policy TODO read failed; failing open: %s", exc)
        return []

    raw = data.get("todos", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _is_open(item: dict[str, Any]) -> bool:
    status = str(item.get("status", "open")).strip().lower()
    return status in _OPEN_STATUSES


def _extract_symbols(text: str, tradable_symbols: set[str] | None) -> set[str]:
    found = {m.group(0).upper() for m in _SYMBOL_RE.finditer(text.upper())}
    if tradable_symbols is not None:
        found &= tradable_symbols
    return found


def load_runtime_policy(tradable_symbols: Iterable[str] | None = None) -> RuntimePolicy:
    """Build runtime policy from data/agents/coin_bot_todos.json.

    Mapping:
    - open P0 TODO: block new entries and DCA until the issue is resolved.
    - open P1 TODO: conservative mode; any USDT symbols mentioned in TODO text are
      excluded from new entries while the TODO remains open.
    - P2 and closed TODOs: observability only; no runtime block.
    """

    tradable = {str(s).upper().replace("/", "") for s in tradable_symbols} if tradable_symbols else None
    block_new_entries = False
    block_dca = False
    conservative_mode = False
    excluded: set[str] = set()
    reasons: list[str] = []

    for item in _read_todos(_todos_path()):
        if not _is_open(item):
            continue
        priority = str(item.get("priority", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        detail = str(item.get("detail", "")).strip()
        reason = f"{priority}:{title}" if title else priority

        if priority == "P0":
            block_new_entries = True
            block_dca = True
            if reason:
                reasons.append(reason)
        elif priority == "P1":
            conservative_mode = True
            if reason:
                reasons.append(reason)
            excluded |= _extract_symbols(f"{title} {detail}", tradable)

    return RuntimePolicy(
        block_new_entries=block_new_entries,
        block_dca=block_dca,
        conservative_mode=conservative_mode,
        excluded_symbols=frozenset(sorted(excluded)),
        reasons=tuple(reasons),
    )
