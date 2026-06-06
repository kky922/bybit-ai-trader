"""
Tests for infra/state.py — PnL history dedup and state persistence.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import infra.state
import pytest


class TestAddPnlRecordDedup:
    """Tests for add_pnl_record duplicate prevention."""

    @pytest.fixture
    def state_with_temp(self):
        """Set up add_pnl_record with isolated temp paths."""
        tmp = Path(tempfile.mkdtemp())
        pnl_file = tmp / "pnl_history.json"

        patcher1 = patch.object(infra.state, "PNL_FILE", pnl_file)
        patcher2 = patch.object(infra.state, "LOGS_DIR", tmp)
        patcher1.start()
        patcher2.start()

        yield infra.state.add_pnl_record, pnl_file

        patcher1.stop()
        patcher2.stop()

        # Cleanup temp
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_identical_record_not_duplicated(self, state_with_temp):
        """동일한 레코드가 두 번 추가되면 중복이 발생하지 않아야 함."""
        add_pnl_record, pnl_file = state_with_temp

        record = {
            "symbol": "BTCUSDT",
            "entry_price": 50000.0,
            "exit_price": 51000.0,
            "size": 0.1,
            "pnl": 100.0,
            "reason": "take_profit",
        }

        add_pnl_record(record.copy())
        assert len(json.loads(pnl_file.read_text())) == 1

        add_pnl_record(record.copy())
        assert len(json.loads(pnl_file.read_text())) == 1, "Identical record should not create duplicate"

    def test_different_records_both_stored(self, state_with_temp):
        """서로 다른 레코드는 모두 저장되어야 함."""
        add_pnl_record, pnl_file = state_with_temp

        r1 = {"symbol": "BTCUSDT", "entry_price": 50000.0, "exit_price": 51000.0,
              "size": 0.1, "pnl": 100.0, "reason": "take_profit"}
        r2 = {"symbol": "ETHUSDT", "entry_price": 3000.0, "exit_price": 3100.0,
              "size": 1.0, "pnl": 100.0, "reason": "take_profit"}

        add_pnl_record(r1.copy())
        add_pnl_record(r2.copy())

        assert len(json.loads(pnl_file.read_text())) == 2

    def test_same_symbol_different_details_not_deduped(self, state_with_temp):
        """같은 심볼이지만 진입가/청산가가 다르면 별도 레코드."""
        add_pnl_record, pnl_file = state_with_temp

        r1 = {"symbol": "BTCUSDT", "entry_price": 50000.0, "exit_price": 51000.0,
              "size": 0.1, "pnl": 100.0, "reason": "take_profit"}
        r2 = {"symbol": "BTCUSDT", "entry_price": 51000.0, "exit_price": 52000.0,
              "size": 0.1, "pnl": 100.0, "reason": "take_profit"}

        add_pnl_record(r1.copy())
        add_pnl_record(r2.copy())

        assert len(json.loads(pnl_file.read_text())) == 2

    def test_different_reason_same_price_not_deduped(self, state_with_temp):
        """같은 가격/PnL이지만 사유가 다르면 별도 레코드."""
        add_pnl_record, pnl_file = state_with_temp

        r1 = {"symbol": "BTCUSDT", "entry_price": 50000.0, "exit_price": 51000.0,
              "size": 0.1, "pnl": 100.0, "reason": "take_profit"}
        r2 = {"symbol": "BTCUSDT", "entry_price": 50000.0, "exit_price": 51000.0,
              "size": 0.1, "pnl": 100.0, "reason": "narrative_faded"}

        add_pnl_record(r1.copy())
        add_pnl_record(r2.copy())

        assert len(json.loads(pnl_file.read_text())) == 2

    def test_empty_history_first_record(self, state_with_temp):
        """빈 PnL 파일에서 첫 레코드 저장."""
        add_pnl_record, pnl_file = state_with_temp

        assert not pnl_file.exists()

        record = {"symbol": "BTCUSDT", "entry_price": 50000.0, "exit_price": 51000.0,
                  "size": 0.1, "pnl": 100.0, "reason": "take_profit"}
        add_pnl_record(record.copy())

        assert pnl_file.exists()
        assert len(json.loads(pnl_file.read_text())) == 1

    def test_dedup_by_key_fields_not_size(self, state_with_temp):
        """중복 판단: symbol + entry_price + exit_price + reason + pnl 일치 = 중복 (size 무시)"""
        add_pnl_record, pnl_file = state_with_temp

        base = {"symbol": "SOLUSDT", "entry_price": 150.0, "exit_price": 155.0,
                "size": 0.5, "pnl": 2.5, "reason": "take_profit"}

        add_pnl_record(base.copy())
        add_pnl_record(base.copy())
        assert len(json.loads(pnl_file.read_text())) == 1, "Exact duplicate should be deduped"

        # Size differs but key fields match — should still dedup
        diff_size = base.copy()
        diff_size["size"] = 1.0
        add_pnl_record(diff_size)
        data = json.loads(pnl_file.read_text())
        assert len(data) == 1, "Same key fields but different size should dedup"
