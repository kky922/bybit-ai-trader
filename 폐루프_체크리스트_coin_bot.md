# coin_bot 폐루프 체크리스트

> 최종 업데이트: 2026-06-02

## Phase 0 — 공통 기반

| 항목 | 상태 | 비고 |
|------|------|------|
| Bridge target 분류 수정 (`classify_targets` → list 반환) | ✅ 완료 | `info_strategy_closed_loop_bridge.py` |
| `bridge_state.json` 파일 출력 | ✅ 완료 | `~/.hermes/cron/bridge_state.json` |
| `infra/context_from.py` — ContextSnapshot + check_tier2_trigger | ✅ 완료 | coin_bot & tradfi_bot 공유 |
| Two-Tier 파이프라인 — Tier1 (해시 체크 상시) | ✅ 완료 | `main.py` 4h 블록 |
| Two-Tier 파이프라인 — Tier2 (regime 변경 시 GPT 알림) | ✅ 완료 | `main.py` 4h 블록 |
| context_from 해시 영속 저장 (`logs/context_from_hash.json`) | ✅ 완료 | `context_from.save_last_hash()` |

## Phase 1 — coin_bot 핵심 구현

| 항목 | 상태 | 비고 |
|------|------|------|
| DRY_RUN → LIVE 전환 가드 (`CONFIRM_LIVE=yes`) | ✅ 완료 | `main.py:_live_guard()` |
| `get_balance_usdt` 기반 동적 사이징 (Regime 30/20/15%) | ✅ 기존 | `trading/position_sizer.py` + `get_cached_regime_sizing` |
| Runner Tranche 모드 (`runner_mode` 플래그) | ✅ 기존 | `main.py` Parabolic 감지 |
| Parabolic Skyhook detector | ✅ 기존 | `analysis/exit_signal.detect_parabolic` |
| watchdog + PID 단일 인스턴스 lock | ✅ 기존 | `fcntl.flock` + PID_FILE |
| Exchange-level SL 자동 복원 (재시작 후) | ✅ 기존 | `main.py:run()` startup block |
| Post-cooldown grace 스캔 | ✅ 기존 | `risk_manager.in_post_cooldown_grace()` |

## Phase 2 — 피드백 루프

| 항목 | 상태 | 비고 |
|------|------|------|
| PnL 집계 스크립트 | ✅ 완료 | `~/.hermes/scripts/closed_loop_feedback.py` |
| `closed_loop_metrics.json` 출력 | ✅ 완료 | 실행 확인됨 |
| regime 적중률 계산 | ✅ 완료 | `_compute_regime_hit_rate()` |

## 독립 실행 테스트

```bash
# Tier1/Tier2 context_from 단독 테스트
cd ~/coin\ bot && python3 -c "
from infra.context_from import check_tier2_trigger, load_context
ctx = load_context()
print('Snapshot:', ctx)
run, ctx2 = check_tier2_trigger('coin-bot')
print('Tier2 trigger:', run, '|', ctx2.regime, ctx2.action_bias)
"

# Bridge 실행 + state 확인
python3 ~/.hermes/scripts/info_strategy_closed_loop_bridge.py
cat ~/.hermes/cron/bridge_state.json

# 피드백 집계
python3 ~/.hermes/scripts/closed_loop_feedback.py
cat ~/.hermes/cron/closed_loop_metrics.json

# DRY_RUN guard 테스트 (CONFIRM_LIVE 미설정 → 차단 확인)
DRY_RUN=false python3 -c "import config; config.DRY_RUN=False; exec(open('main.py').read())" 2>&1 | head -5
```

## DRY_RUN → LIVE 전환 절차

1. `.env`에서 `DRY_RUN=true` → `DRY_RUN=false` 변경
2. `.env`에 `CONFIRM_LIVE=yes` 추가 (라인 추가 필수)
3. `python3 main.py` 실행 — "🔴 LIVE MODE 확인됨" 로그 확인
4. Telegram 알림에서 "💰 LIVE" 모드 확인
5. 잔고·포지션 초기 상태 점검 후 정상 운영

## context_from chain end-to-end 테스트 (최소 3회)

```bash
# 1회차: bridge 실행 → 해시 기록
python3 ~/.hermes/scripts/info_strategy_closed_loop_bridge.py
# 2회차: 동일 리포트 → Tier2 스킵 확인 (no_change)
python3 -c "from infra.context_from import check_tier2_trigger; print(check_tier2_trigger('coin-bot'))"
# 3회차: bridge_state.json 해시 수동 변경 후 → Tier2 트리거 확인
```
