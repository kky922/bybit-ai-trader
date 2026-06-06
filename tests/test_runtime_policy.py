from __future__ import annotations

import json
from pathlib import Path


def _write_todos(data_dir: Path, todos: list[dict]) -> Path:
    agents = data_dir / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / "coin_bot_todos.json"
    path.write_text(json.dumps({"todos": todos}, ensure_ascii=False), encoding="utf-8")
    return path


def test_p0_todo_blocks_new_entries_and_dca(monkeypatch, tmp_path):
    from infra import runtime_policy

    monkeypatch.setattr(runtime_policy.config, "DATA_DIR", tmp_path)
    _write_todos(
        tmp_path,
        [
            {
                "priority": "P0",
                "title": "운영 상태 복구 확인",
                "detail": "프로세스가 내려가 있음",
                "status": "open",
            }
        ],
    )

    policy = runtime_policy.load_runtime_policy()

    assert policy.block_new_entries is True
    assert policy.block_dca is True
    assert policy.conservative_mode is False
    assert "P0:운영 상태 복구 확인" in policy.reasons


def test_p1_todo_enables_conservative_mode_and_extracts_symbol(monkeypatch, tmp_path):
    from infra import runtime_policy

    monkeypatch.setattr(runtime_policy.config, "DATA_DIR", tmp_path)
    _write_todos(
        tmp_path,
        [
            {
                "priority": "P1",
                "title": "반복 손실 심볼 관찰",
                "detail": "SOLUSDT losses=3 pnl=-1.2",
                "status": "open",
            },
            {
                "priority": "P1",
                "title": "후보 필터 재점검",
                "detail": "후보 품질 낮음",
                "status": "open",
            },
        ],
    )

    policy = runtime_policy.load_runtime_policy(tradable_symbols={"SOLUSDT", "ETHUSDT"})

    assert policy.block_new_entries is False
    assert policy.block_dca is False
    assert policy.conservative_mode is True
    assert policy.excluded_symbols == frozenset({"SOLUSDT"})


def test_closed_or_malformed_todos_fail_open(monkeypatch, tmp_path):
    from infra import runtime_policy

    monkeypatch.setattr(runtime_policy.config, "DATA_DIR", tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "coin_bot_todos.json").write_text("{not-json", encoding="utf-8")

    malformed = runtime_policy.load_runtime_policy()
    assert malformed == runtime_policy.RuntimePolicy()

    _write_todos(
        tmp_path,
        [
            {
                "priority": "P0",
                "title": "실거래 오류 원인 확인",
                "detail": "Bybit 오류",
                "status": "done",
            }
        ],
    )
    closed = runtime_policy.load_runtime_policy()
    assert closed == runtime_policy.RuntimePolicy()


def test_main_entry_policy_skips_global_and_symbol_blocks():
    import main
    from infra.runtime_policy import RuntimePolicy

    global_block = RuntimePolicy(block_new_entries=True, reasons=("P0:bot down",))
    assert main.runtime_entry_skip_reason("ETHUSDT", global_block) == "runtime_policy_block:P0:bot down"

    symbol_block = RuntimePolicy(conservative_mode=True, excluded_symbols=frozenset({"SOLUSDT"}), reasons=("P1:loss",))
    assert main.runtime_entry_skip_reason("SOLUSDT", symbol_block) == "runtime_policy_symbol_excluded:SOLUSDT:P1:loss"
    assert main.runtime_entry_skip_reason("ETHUSDT", symbol_block) is None


def test_main_dca_policy_blocks_only_when_requested():
    import main
    from infra.runtime_policy import RuntimePolicy

    assert main.runtime_dca_skip_reason(RuntimePolicy(block_dca=True, reasons=("P0:bot down",))) == "runtime_policy_dca_block:P0:bot down"
    assert main.runtime_dca_skip_reason(RuntimePolicy(block_dca=False)) is None
