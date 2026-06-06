# tradfi_bot 폐루프 체크리스트

> 최종 업데이트: 2026-06-02

## Phase 0 — 공통 기반

| 항목 | 상태 | 비고 |
|------|------|------|
| Bridge target 분류 — tradfi-bot 독립 타겟 지원 | ✅ 완료 | `classify_targets` list 반환 |
| `infra/context_from.py` — Tier1/Tier2 check | ✅ 완료 | coin_bot과 공유 모듈 |
| Two-Tier 파이프라인 (tradfi_main.py 4h 블록) | ✅ 완료 | `tradfi_main.py` |
| `last_bridge_hash` 추적 변수 | ✅ 완료 | `run()` 함수 내 |

## Phase 1 — tradfi_bot 핵심 구현

| 항목 | 상태 | 비고 |
|------|------|------|
| `risk_state_tradfi.json` 분리 관리 | ✅ 기존 | `RiskManager(account="tradfi")` |
| `TRADFI_DRY_RUN → LIVE` 전환 가드 (`CONFIRM_TRADFI_LIVE=yes`) | ✅ 완료 | `tradfi_main.py:_tradfi_live_guard()` |
| crash-loop 감지 + SIGTERM clean exit 처리 | ✅ 기존 | `_check_crash_loop()` + `SIGTERM_CLEAN_FILE` |
| heartbeat 파일 (`tradfi_bot.heartbeat`) | ✅ 기존 | `_write_heartbeat()` |
| fast trailing stop (30초 루프) | ✅ 기존 | `_check_trailing_stops()` |
| TradFiGPTAnalyzer closed-loop 점수 (`closed_loop_score`) | ✅ 기존 | `_rank_candidates()` |
| GPT 캐시 (TTL = GPT_REFRESH_HOURS) | ✅ 기존 | `_load_cache()` / `_save_cache()` |
| PnL → risk_manager.record_exit() 연동 | ✅ 기존 | `trader.exit()` / `partial_exit()` |
| Bybit 포지션 헬스 체크 (시작 시 즉시 청산 체크) | ✅ 기존 | `run()` startup block |

## Phase 2 — 피드백 루프

| 항목 | 상태 | 비고 |
|------|------|------|
| `pnl_history_tradfi.json` 기록 | ✅ 기존 | `add_pnl_record()` |
| `closed_loop_feedback.py`에서 tradfi PnL 집계 | ✅ 완료 | `TRADFI_PNL_FILE` |
| closed_loop_score 피드백 → 다음 GPT 사이클 반영 | ✅ 기존 | `_closed_loop_profile()` |

## 독립 실행 테스트

```bash
# context_from Tier1/Tier2 테스트
cd ~/coin\ bot && python3 -c "
from infra.context_from import check_tier2_trigger
run, ctx = check_tier2_trigger('tradfi-bot')
print('Tier2:', run, '| regime:', ctx.regime, '| targets:', ctx.targets)
"

# DRY_RUN guard 테스트 (CONFIRM_TRADFI_LIVE 미설정 → 차단)
TRADFI_DRY_RUN=false python3 -c "
import os; os.environ['TRADFI_DRY_RUN']='false'
from tradfi_main import _tradfi_live_guard
_tradfi_live_guard()  # SystemExit 발생 확인
" 2>&1

# tradfi_bot 시작 (DRY_RUN 모드)
python3 tradfi_main.py
```

## DRY_RUN → LIVE 전환 절차

1. `.env`에서 `TRADFI_DRY_RUN=true` → `TRADFI_DRY_RUN=false` 변경
2. `.env`에 `CONFIRM_TRADFI_LIVE=yes` 추가
3. `python3 tradfi_main.py` 실행 — "🔴 TRADFI LIVE MODE 확인됨" 로그 확인
4. Telegram에서 "💰 LIVE" 모드 + 잔고 정상 확인
5. 자본 예산 재확인 (`TRADFI_EQUITY_USDT`, `TRADFI_MAX_EQUITY_PCT_PER_POSITION`)

## risk_state 분리 검증

```bash
# coin_bot vs tradfi_bot 리스크 상태 분리 확인
cat ~/coin\ bot/logs/risk_state.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('coin:', d.get('day'), 'losses:', d.get('consecutive_losses'))"
cat ~/coin\ bot/logs/risk_state_tradfi.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('tradfi:', d.get('day'), 'losses:', d.get('consecutive_losses'))"
```
