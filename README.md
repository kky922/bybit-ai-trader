# Coin Direction Bot

뉴스 + GPT 내러티브 + 기술적 타이밍(4H/1H) 기반의 Bybit Spot 방향성 자동매매 봇입니다.

## Quick Start

1. `cp .env.example .env`
2. `.env`에 API 키 입력
3. `pip install -r requirements.txt`
4. 드라이런 실행: `DRY_RUN=true python main.py`
5. 대시보드: `bash scripts/start_dashboard.sh`

## 구조

- `data/`: 뉴스/캔들/유니버스
- `analysis/`: GPT, 지표, 진입/청산 신호
- `trading/`: 거래소, 포지션 사이징, 리스크, 주문 실행
- `infra/`: 텔레그램, 이벤트로그, 상태저장
- `dashboard/`: Streamlit 모니터링
- `tests/`: 핵심 로직 유닛테스트

## 운영 원칙

- 기본값은 `DRY_RUN=true`
- 내부 시간 연산은 UTC 기준
- 수수료(0.1% taker)를 실현손익에 반영

---

## 최근 수정 내역 (Codex — 2026-04-26)

### 원인 분석

- 이 봇은 AI를 사용한다. `analysis/gpt_analyzer.py`가 Gemini(`GPT_MODEL=gemini-2.5-flash`)로 뉴스 기반 섹터와 후보 코인을 고른다.
- AI는 주문을 직접 실행하지 않는다. AI 후보가 나온 뒤 `analysis/entry_signal.py`의 BTC 추세, 4H 추세, 1H EMA, RSI, 거래량 필터가 최종 진입을 결정한다.
- 드라이런 중 거래가 보이지 않았던 주된 이유는 후보들이 진입 필터에서 계속 탈락했기 때문이다. 과거 로그의 `ema_cross_missing`는 구버전 진입 로직 또는 오래된 프로세스가 실행 중인 신호로 본다.
- 운영상 문제도 있었다. 앱 본체는 `logs/coin_bot.pid`를 쓰고 시작/워치독 스크립트는 `pids/coin_bot.pid`를 봐서 중복 프로세스가 생길 수 있었다.

### 적용한 수정

- PID 파일을 `logs/coin_bot.pid`로 통일했다.
- `scripts/start_coin_bot.sh`가 기존 PID 파일과 레거시 `pids/coin_bot.pid`를 모두 확인하고, 같은 작업 디렉터리(`$HOME/coin bot`)에서 실행 중인 중복 `main.py`도 정리하도록 변경했다.
- 시작 스크립트는 macOS `launchd` 작업(`com.coinbot.app`)으로 봇을 올린다. Codex/터미널 세션이 닫혀도 봇 프로세스가 같이 정리되지 않게 하기 위함이다.
- `scripts/watchdog_coin.sh`도 `logs/coin_bot.pid`만 보도록 변경했다.
- 진입 거절 사유에 실제 지표값을 포함했다. 예: `btc_trend_down:fast=...,slow=...`, `rsi_out_of_range:rsi=...,min=...,max=...`.
- `main.py`가 시작, 진입 스캔 시작/종료, GPT 갱신 시 `health`/`gpt_refresh` 이벤트를 남긴다. 후보 수, 포지션 수, 마지막 GPT/진입 스캔 시간이 함께 기록된다.
- Gemini가 실패하거나 빈 후보를 반환해도 후보가 0개로 덮이지 않게 했다. 이전 정상 후보가 있으면 유지하고, 없으면 fallback 후보를 사용한다.
- 최신 AI 섹터/후보는 `logs/latest_ai.json`에 저장되어 재시작 후에도 참고된다.
- 텔레그램 전송 실패는 이미 루프를 깨지 않도록 처리되어 있으며, 현재 구현은 실패 시 경고 로그만 남긴다.

### 운영 명령

```bash
cd "$HOME/coin bot"

# 시작 또는 재시작: 기존 코인봇 중복 프로세스를 정리한 뒤 1개만 실행
bash scripts/start_coin_bot.sh

# 워치독 1회 실행
bash scripts/watchdog_coin.sh

# PID 확인
cat logs/coin_bot.pid

# launchd 작업 상태 확인
launchctl print gui/$(id -u)/com.coinbot.app

# 실행 중인 코인봇 프로세스 확인
ps -ef | rg 'coin bot|main.py|coin_bot'

# 로그 확인
tail -n 120 logs/coin_bot.log
tail -n 120 logs/event_log.json
```

중복 프로세스를 수동 확인할 때는 `ps` 결과에 `$HOME/coin bot` 작업 디렉터리의 `main.py`가 1개만 남아야 한다. `event_log.json`에 `ema_cross_missing`가 계속 찍히면 오래된 프로세스가 남아 있거나 최신 코드로 재시작되지 않은 상태를 의심한다.

### 드라이런 확인법

- `DRY_RUN=true`에서는 실제 Bybit 주문을 내지 않는다.
- 진입 조건을 통과하면 `logs/event_log.json`에 `dry_entry`와 `entry`가 기록되고, `logs/positions.json`에 가상 포지션이 추가된다.
- 진입 조건을 통과하지 못하면 `entry_skip`이 기록된다. 새 진단 형식에서는 탈락 사유와 실제 지표값이 함께 남는다.
- GPT/Gemini 후보 상태는 `logs/news_snapshots.json`과 `logs/latest_ai.json`에서 확인한다.
- `risk_block`이 있으면 리스크 매니저가 신규 진입을 막은 것이다. 현재 드라이런 점검 기준에서는 `entry_skip`의 세부 사유가 가장 중요하다.

### 검증 결과

```bash
python3 -m pytest tests/ -q
# 15 passed, 6 warnings

bash -n scripts/start_coin_bot.sh
# OK

bash -n scripts/watchdog_coin.sh
# OK

bash scripts/start_coin_bot.sh
# launchd state=running, pid=59346

PYTHONPYCACHEPREFIX=/tmp/coin-bot-pycache python3 -m compileall -q main.py analysis trading infra data
# OK
```

주의: 기본 `compileall`은 macOS 사용자 캐시(`$HOME/Library/Caches/com.apple.python/...`)에 쓰려다 권한 오류가 날 수 있어, 위처럼 `PYTHONPYCACHEPREFIX`를 지정해 검증한다.

---

## 코드 품질 평가 (Claude Code 검토 — 2026-04-22)

### 전체 등급: B+ (현물 방향성 봇 수준에서 실사용 가능)

---

### 수정된 버그 (Critical)

| # | 파일 | 버그 | 영향 |
| - | ---- | ---- | ---- |
| 1 | `analysis/__init__.py` 외 4개 | `__init__.py` 누락 | 모든 import 실패 → 봇 기동 불가 |
| 2 | `analysis/exit_signal.py:38` | narrative_faded 조건에 데드 EMA 체크 → 해당 청산이 절대 발동 안됨 | 내러티브 소멸 청산 0% 작동 |
| 3 | `main.py` | `highest_price`가 진입 시 단 1회만 설정, 이후 가격 상승 미반영 | trailing stop 영구 미작동 |
| 4 | `main.py` | 주기 체크에 중복 발화 방지 없음 | 루프 지연 시 같은 시간대 진입/GPT 분석 중복 실행 |
| 5 | `trading/position_sizer.py:30` | `size * entry < min_notional` → 경계값 통과 허용 | 최소 주문 금액 필터 미작동 (테스트 실패로 발견) |
| 6 | `data/news_collector.py` | `requests` 유지용 죽은 코드 (cp_token noop) | 불필요한 의존성 오염 |

모두 수정 완료. `python -m pytest tests/ -v` → **7/7 PASSED**

---

### 잘 된 부분

#### 구조 설계

- 모듈 분리가 명확: data/analysis/trading/infra 레이어가 단방향 의존성 유지
- config.py가 모든 상수를 중앙 관리하고 env override 지원 — 재배포 없이 파라미터 변경 가능

#### 진입 로직 (entry_signal.py)

- BTC 필터 → 4H 추세 → 1H 크로스업+RSI+거래량 3단계 게이팅 정확 구현
- "직전 봉 → 현재 봉" 기준으로 크로스업 판정 → 리페인팅 방지 구현 정확
- EntrySignal dataclass로 SL/TP/ATR을 묶어 전달 → 진입~청산 간 데이터 일관성 보장

#### 리스크 관리 (risk_manager.py)

- 일일 손실 한도, 연속 손실 쿨다운, 심볼별 재진입 금지, BTC 급락 필터 4중 방어
- 상태를 `risk_state.json`에 영속화 → 봇 재기동 후에도 쿨다운 유지

#### 드라이런 격리

- `DRY_RUN=true` 시 주문 API 미호출, 포지션만 JSON 기록 → API 키 없이도 전 파이프라인 검증 가능

#### 수수료 반영

- `trader.exit()`에서 매수·매도 양면 0.1% taker 수수료를 PnL에서 차감 → 실제 수익과 괴리 최소화

---

### 개선 여지 (운영 전 권장)

#### 1. Bybit quoteOrderQty 호환성 (`trading/exchange.py:73`)

- `quoteOrderQty`는 Binance 전용 파라미터. Bybit ccxt에서 동작 여부 미확인.
- 권장: `exchange.create_market_buy_order_with_cost(symbol, usdt_amount)` 또는 실주문 1회 테스트로 확인.
- 드라이런 중에는 미호출이라 무관하지만, **라이브 전환 전 소액 실주문 1회 필수 검증**.

#### 2. MarketData 캐싱 없음 (`data/market_data.py`)

- 진입 체크 시 후보 코인마다 `fetch_ohlcv` 호출 → 후보 10개면 최대 20회 API 콜
- 권장: 동일 심볼·타임프레임을 같은 루프 내에서 캐싱. 레이트리밋 여유가 충분하면 무관.

#### 3. GPT 프롬프트에 유니버스 미포함 (`analysis/gpt_analyzer.py:62`)

- `pick_coins` 프롬프트에 Bybit 상장 심볼 목록이 없어 GPT가 없는 심볼 추천 가능.
- `filtered` 단계 교차 검증으로 기능상 안전하나 GPT 호출 비용 낭비 발생.
- 권장: 프롬프트에 `"Bybit 현물 상장 예시: {tradable[:30]}"` 추가.

#### 4. 텔레그램 미설정 시 silent fail

- `TELEGRAM_BOT_TOKEN_COIN`이 비어있으면 조용히 비활성화.
- 기동 로그에 "텔레그램 미설정 — 알림 비활성화" 경고 출력 권장.

---

### 한계 / 설계상 결정 (버그 아님)

| 항목 | 현황 | 비고 |
| ---- | ---- | ---- |
| 포지션 중 가격 하락 추적 | 1H 종가 기준 (5분 하드체크 병행) | 급락 구간 최대 1H 노출 |
| ATR 계산 방식 | SMA-ATR (rolling mean) | Wilder ATR(EWM)보다 단순하나 큰 차이 없음 |
| CryptoPanic 연동 | 미구현 (config에만 정의) | 선택 사항, 추후 추가 가능 |
| 백테스트 | 없음 | 드라이런으로 대체. 추후 추가 권장 |

---

### 라이브 전환 전 필수 체크리스트

- [ ] `DRY_RUN=true python main.py` 1 cycle 로그 정상 확인 (뉴스→GPT→캔들→시그널→이벤트로그)
- [ ] Bybit 서브계좌 API로 `fetch_balance`, `fetch_ohlcv("BTCUSDT","4h",100)` 응답 정상
- [ ] **create_market_buy 실주문 소액 1회 테스트 → quoteOrderQty 호환 확인**
- [ ] 텔레그램 알림 1건 수신 확인
- [ ] 대시보드 포트 8502 정상 접속
- [ ] 워치독: 수동 kill 후 5분 내 재기동 확인
- [ ] 드라이런 최소 14일, 시그널 ≥ 20건, 승률 ≥ 45%, 평균 RR ≥ 1.3 달성 후 라이브 전환
