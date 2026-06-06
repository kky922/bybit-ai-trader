# 코인 방향성 자동매매봇 — 상세 설계 지시서 (Cursor 구현용)

## Context
그리드봇(횡보 특화)과 보완되는 **추세장 수익 커버용** 현물 방향성 봇을 신규로 만든다.
바이비트 서브계좌 2, 현물 전용, 무레버리지, 소액 시작. 뉴스→GPT(섹터/내러티브) → 기술적 타이밍(4H/1H 추세+RSI+거래량) → ATR 기반 리스크관리 파이프라인.

**작업 분담**: Cursor가 이 문서를 보고 구현, Claude Code는 디버깅/검증 담당.
**생성물 경로**: `$HOME/coin bot/` (현재 비어있음)

---

## 1. 디렉토리 / 파일 아키텍처

```
coin bot/
├── .env                       # API 키, 텔레그램 토큰, GPT 키
├── .env.example
├── requirements.txt
├── README.md
├── config.py                  # 전체 상수/임계값, .env 로딩
├── main.py                    # 메인 루프 (파이프라인 orchestration)
│
├── data/
│   ├── news_collector.py      # RSS/API 뉴스 수집
│   ├── market_data.py         # Bybit OHLCV 수집 (4H/1H/15m)
│   └── universe.py            # 거래 가능 심볼 화이트리스트/유동성 필터
│
├── analysis/
│   ├── gpt_analyzer.py        # GPT로 섹터/내러티브 탐지 + 코인 후보 추출
│   ├── technical.py           # EMA/RSI/ATR/Volume 지표
│   ├── entry_signal.py        # 4H 추세 + 1H 진입 + BTC 필터
│   └── exit_signal.py         # 손절/익절/추세이탈/내러티브소멸
│
├── trading/
│   ├── exchange.py            # Bybit ccxt 래퍼 (현물 Spot)
│   ├── position_sizer.py      # 포지션 사이징 (자금비율/동시포지션 제한)
│   ├── trader.py              # 진입/청산 실행
│   └── risk_manager.py        # 일일최대손실/연속손실/쿨다운
│
├── infra/
│   ├── telegram.py            # 알림 (진입/청산/리스크/일일요약)
│   ├── event_log.py           # 이벤트 JSON 로깅
│   └── state.py               # 포지션/잔고 영속화 (JSON)
│
├── dashboard/
│   └── coin_dashboard.py      # Streamlit (포지션/PnL/이벤트/설정)
│
├── scripts/
│   ├── start_coin_bot.sh
│   ├── watchdog_coin.sh
│   └── start_dashboard.sh
│
├── logs/
│   ├── coin_bot.log
│   ├── event_log.json
│   ├── positions.json
│   ├── pnl_history.json
│   └── news_snapshots.json    # GPT 분석 기록 (리플레이/디버깅용)
│
└── tests/
    ├── test_technical.py
    ├── test_entry_signal.py
    └── test_position_sizer.py
```

---

## 2. 재활용 참조 (반드시 먼저 읽기)

### stock_bot (뉴스→GPT→진입 패턴)
- `$HOME/stock_bot/news_scanner.py:20-57` `NewsScanner._fetch_rss/collect_news` — **news_collector.py 베이스**
- `$HOME/stock_bot/news_scanner.py:61-161` `_analyze_with_glm/_validate_ai_issues` — **gpt_analyzer.py 프롬프트/JSON 파싱 베이스**
- `$HOME/stock_bot/technical.py:18-318` `_ema/rsi/atr/volume_analysis` — **technical.py에 그대로 포팅** (pandas 기반)
- `$HOME/stock_bot/trader.py:47-154` `enter/exit` 구조 — **trader.py 골격**
- `$HOME/stock_bot/run_agents.py:312-508` `run_pipeline_once` — **main.py 루프 골격**
- `$HOME/stock_bot/config.py:1-209` STOP_LOSS_ATR_MULTI, MAX_POSITIONS 등 — **config.py 상수 참고**

### polymarket_bot/grid_bot (인프라 재활용)
- `$HOME/polymarket_bot/grid_bot/exchange.py:62-100` `BybitExchange` (ccxt, UNIFIED/spot, 레이트리미터) — **그대로 포팅**
- `$HOME/polymarket_bot/grid_bot/grid_telegram.py:34-80` `GridTelegram.send_message` (aiohttp 비동기, 쿨다운) — **infra/telegram.py 베이스**
- `$HOME/polymarket_bot/grid_bot/event_log.py:15-81` `log_event/get_recent_events` (JSON, MAX_EVENTS=200) — **infra/event_log.py 그대로**
- `$HOME/polymarket_bot/watchdog_grid.sh` — **watchdog_coin.sh 베이스** (PID 체크 + 재시작)
- `$HOME/polymarket_bot/grid_bot/grid_dashboard_v5.py` — **coin_dashboard.py 레이아웃 참고** (30초 auto-refresh, 탭 구조, 모바일)

---

## 3. 뉴스 수집 (data/news_collector.py)

### 소스 (코인 특화)
1. **CoinDesk RSS**: `https://www.coindesk.com/arc/outboundfeeds/rss/`
2. **Cointelegraph RSS**: `https://cointelegraph.com/rss`
3. **The Block RSS**: `https://www.theblock.co/rss.xml`
4. **Decrypt RSS**: `https://decrypt.co/feed`
5. **Bitcoin Magazine**: `https://bitcoinmagazine.com/feed`
6. **Google News 코인 쿼리**: `https://news.google.com/rss/search?q=crypto+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en`
7. **CryptoPanic API** (선택, 무료 tier): `https://cryptopanic.com/api/v1/posts/?auth_token=XXX&kind=news`

### 인터페이스
```python
class NewsCollector:
    def collect(self, lookback_hours: int = 6) -> list[NewsItem]:
        """NewsItem: {title, body, source, url, published_at, coins_mentioned}"""
```
- 중복 제거: URL 해시 기반
- 시간 필터: lookback_hours 이내만
- `logs/news_snapshots.json`에 수집 결과 저장 (리플레이/디버깅)

---

## 4. GPT 분석 (analysis/gpt_analyzer.py)

### 2단계 호출 구조

**1단계 — 섹터 탐지** (뉴스 10-30개 → 핫 섹터 Top 3)
```
SYSTEM: 당신은 크립토 마켓 내러티브 분석가다.
USER: 아래 지난 6시간 뉴스 기반으로 지금 가장 핫한 섹터 3개를 JSON으로 뽑아라.
섹터 카테고리: [AI, L2, L1, DeFi, RWA, Meme, DePIN, GameFi, BTC-ETF, Stablecoin, Restaking, 기타]

[뉴스 N건: title + 1줄 summary]

OUTPUT JSON:
{
  "sectors": [
    {
      "name": "AI",
      "heat_score": 1-10,
      "persistence": "short|medium|long",  # 단기(1-3일)/중기(1주)/장기(1개월+)
      "narrative": "한 문장 요약",
      "key_catalysts": ["뉴스 제목1", ...]
    }
  ]
}
```

**2단계 — 코인 후보 추출** (섹터 Top 3 → 심볼별 점수)
```
SYSTEM: 당신은 섹터별 대표 코인 선정 전문가다.
USER: 섹터 {AI, L2, DeFi}에 대해 Bybit 현물 USDT 페어 중 대표/수혜 코인을 각 섹터 최대 5개씩 뽑아라.
거래량/시총/내러티브 적합성 기준.

OUTPUT JSON:
{
  "candidates": [
    {
      "symbol": "FETUSDT",
      "sector": "AI",
      "conviction": 1-10,
      "reason": "한 문장"
    }
  ]
}
```

### 출력 검증
- `_validate_ai_response()` — JSON 파싱 실패 시 재시도 1회 후 빈 결과 반환 (stock_bot 패턴)
- heat_score, conviction 범위 [1,10] 클램프
- symbol은 화이트리스트 교차검증 (data/universe.py)

### 실행 주기
- **4시간마다 1회** (= 4H 캔들 종가 시점 동기화)
- GPT 모델: `gpt-4o-mini` 또는 `claude-haiku-4-5` (비용 최적화)
- `news_snapshots.json`에 요청/응답 기록

---

## 5. 유니버스 (data/universe.py)

```python
def get_tradable_symbols() -> list[str]:
    """Bybit 현물 USDT 페어 중:
    - 24h 거래량 >= $20M
    - 상장 7일 이상
    - 스테이블/랩드 토큰 제외
    """
```
- 캐싱: 1시간 단위 `logs/universe_cache.json`

---

## 6. 기술적 분석 (analysis/technical.py)

stock_bot/technical.py 함수를 그대로 포팅 (pandas DataFrame 입력):
- `ema(series, period) -> pd.Series`
- `rsi(closes, period=14) -> float`
- `atr(ohlc_df, period=14) -> float`
- `volume_ratio(volumes, period=20) -> float`  # 최근 vs 20봉 평균

캔들 데이터는 `data/market_data.py`가 ccxt `fetch_ohlcv`로 공급 (4H, 1H, 15m).

---

## 7. 진입 신호 (analysis/entry_signal.py)

```python
def check_entry(symbol: str, candles_4h, candles_1h, btc_4h) -> EntrySignal | None:
    # 필터 1: BTC 시장 필터
    if ema(btc_4h.close, 20).iloc[-1] < ema(btc_4h.close, 50).iloc[-1]:
        return None  # BTC 하락 추세 시 진입 억제

    # 필터 2: 4H 추세
    if ema(candles_4h.close, 20).iloc[-1] <= ema(candles_4h.close, 50).iloc[-1]:
        return None

    # 트리거 3: 1H 진입 타이밍
    ema20_1h = ema(candles_1h.close, 20)
    ema50_1h = ema(candles_1h.close, 50)
    cross_up = ema20_1h.iloc[-2] <= ema50_1h.iloc[-2] and ema20_1h.iloc[-1] > ema50_1h.iloc[-1]
    rsi_1h = rsi(candles_1h.close, 14)
    vol_ratio = volume_ratio(candles_1h.volume, 20)

    if not (cross_up and 45 <= rsi_1h <= 65 and vol_ratio >= 1.2):
        return None

    atr_1h = atr(candles_1h, 14)
    entry_price = candles_1h.close.iloc[-1]
    return EntrySignal(
        symbol=symbol,
        entry_price=entry_price,
        stop_loss=entry_price - ATR_STOP_MULTI * atr_1h,   # 기본 2.0
        take_profit=entry_price + ATR_TP_MULTI * atr_1h,   # 기본 3.5
        atr=atr_1h,
    )
```

**주의**: 크로스업은 "직전 봉 → 현재 봉" 확정된 캔들 기준. 리페인팅 방지.

---

## 8. 청산 신호 (analysis/exit_signal.py)

포지션 보유 중 1H 캔들 종가마다 체크:
1. **손절**: price <= stop_loss
2. **익절**: price >= take_profit
3. **추세 이탈**: 1H EMA20 < EMA50 (데드크로스)
4. **4H 추세 이탈**: 4H EMA20 < EMA50 (상위 추세 깨짐)
5. **내러티브 소멸**: 해당 섹터가 최신 GPT 분석에서 Top3에서 이탈 + heat_score 5 미만 → 소프트 청산 (즉시 아님, 1H 추세 이탈과 AND 조건)
6. **트레일링 익절** (선택): 고점 대비 -1.5*ATR 하락 시

우선순위: 1,2 > 3,4 > 5,6

---

## 9. 포지션 사이징 (trading/position_sizer.py)

```python
MAX_CONCURRENT_POSITIONS = 3          # 동시 보유 상한
MAX_EQUITY_PCT_PER_POSITION = 0.30    # 1 포지션당 총자본의 30%
MAX_RISK_PCT_PER_TRADE = 0.01         # 손절까지 총자본의 1% 리스크

def compute_size(equity_usdt, entry, stop) -> float:
    risk_per_unit = entry - stop
    size_by_risk = (equity_usdt * MAX_RISK_PCT_PER_TRADE) / risk_per_unit
    size_by_equity = (equity_usdt * MAX_EQUITY_PCT_PER_POSITION) / entry
    size = min(size_by_risk, size_by_equity)
    return round_to_lot(size, symbol_meta)
```
- Bybit 현물 최소주문금액(보통 $5 또는 심볼별) 이상 강제

---

## 10. 거래소 래퍼 (trading/exchange.py)

grid_bot/exchange.py `BybitExchange`를 포팅. 필수 메서드:
- `get_balance_usdt() -> float`
- `fetch_ohlcv(symbol, timeframe, limit) -> pd.DataFrame`
- `fetch_ticker(symbol) -> dict`
- `create_market_buy(symbol, usdt_amount) -> order`   # quoteOrderQty 방식
- `create_market_sell(symbol, base_amount) -> order`
- `fetch_balance_spot(coin) -> float`
- `symbol_meta(symbol) -> {min_amt, lot_step, tick_size}`

ccxt 옵션: `accountType="UNIFIED"`, `defaultType="spot"`, `enableRateLimit=True`. BybitRateLimiter 포팅.

**서브계좌 2**: .env에 `BYBIT_API_KEY_SUB2`, `BYBIT_API_SECRET_SUB2`. config.py에서 명시적으로 이 키만 사용.

---

## 11. 리스크 관리 (trading/risk_manager.py)

- `DAILY_MAX_LOSS_PCT = 0.03` — 일일 실현손실 -3% 도달 시 당일 신규진입 중단
- `CONSECUTIVE_LOSS_STOP = 3` — 연속 3패 시 24시간 쿨다운
- `PER_SYMBOL_COOLDOWN_HOURS = 8` — 동일 심볼 청산 후 재진입 금지
- BTC 급락 (-5% 4H) 시 전체 신규진입 중단
- 최대 레버리지 = 1 (현물이므로 자명, 방어적 assert)

상태 저장: `logs/risk_state.json` (daily_pnl, consecutive_losses, cooldowns).

---

## 12. 메인 루프 (main.py)

```
while True:
    now = utc_now()

    # 1. 1H 캔들 종가 근처 (매시 정각 + 30초 지연) — 진입/청산 체크
    if at_hour_close(now):
        positions = load_positions()
        for pos in positions:
            exit_sig = check_exit(pos, fetch_ohlcv_1h(pos.symbol), fetch_ohlcv_4h(pos.symbol), latest_sector_analysis)
            if exit_sig:
                trader.exit(pos, exit_sig)

        if risk_manager.can_trade():
            candidates = latest_gpt_candidates   # 메모리 캐시
            btc_4h = fetch_ohlcv_4h("BTCUSDT")
            for sym in candidates:
                if already_holding(sym): continue
                if in_cooldown(sym): continue
                entry_sig = check_entry(sym, fetch_ohlcv_4h(sym), fetch_ohlcv_1h(sym), btc_4h)
                if entry_sig and can_open_new_position():
                    trader.enter(sym, entry_sig)

    # 2. 4H 정각 — GPT 분석 갱신
    if at_4h_close(now):
        news = news_collector.collect(lookback_hours=6)
        sectors = gpt_analyzer.detect_sectors(news)
        latest_gpt_candidates = gpt_analyzer.pick_coins(sectors)
        save_snapshot(...)

    # 3. 매 5분 — 하드 손절/익절만 체크 (빠른 반응)
    if every_5min(now):
        for pos in load_positions():
            price = fetch_ticker(pos.symbol).last
            if price <= pos.stop_loss or price >= pos.take_profit:
                trader.exit(pos, "hard_sl_tp")

    sleep(30)
```

**타임존**: 내부 연산은 전부 UTC. 로그 출력만 KST 병기.

---

## 13. 알림 (infra/telegram.py)

grid_telegram.py 포팅. 발송 이벤트:
- **진입**: 심볼/섹터/진입가/손절/익절/수량/근거(1H 신호+GPT conviction)
- **청산**: 심볼/청산가/PnL $, %/사유
- **리스크**: 일일손실한도/연속손실/BTC급락 트리거
- **일일 요약** (UTC 00:00): 당일 진입수/청산수/실현PnL/현재 보유포지션
- **에러**: 예외 발생 시 (쿨다운 5분)

환경변수: `TELEGRAM_BOT_TOKEN_COIN`, `TELEGRAM_CHAT_ID_COIN` (그리드봇과 채팅 분리 권장).

---

## 14. 대시보드 (dashboard/coin_dashboard.py)

Streamlit, grid_dashboard_v5 스타일 참고. 탭:
1. **현재 포지션**: 심볼/진입가/현재가/미실현PnL/손절/익절/보유시간
2. **수익**: 일별/누적 PnL 그래프, 승률, 평균 RR
3. **섹터/내러티브**: 최신 GPT 분석 결과 (heat_score, candidates)
4. **이벤트 로그**: 최근 50건
5. **설정**: 주요 상수 읽기전용 표시

데이터 소스: JSON 읽기 전용(`positions.json`, `pnl_history.json`, `event_log.json`, `news_snapshots.json`). 30초 auto-refresh.

인증: grid_bot/auth.py `check_password()` 재사용, `DASHBOARD_PASSWORD_COIN` 환경변수.

---

## 15. 설정 (config.py + .env.example)

```python
# --- API ---
BYBIT_API_KEY = env("BYBIT_API_KEY_SUB2")
BYBIT_API_SECRET = env("BYBIT_API_SECRET_SUB2")
OPENAI_API_KEY = env("OPENAI_API_KEY")
GPT_MODEL = env("GPT_MODEL", "gpt-4o-mini")
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN_COIN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID_COIN")

# --- 전략 ---
ATR_STOP_MULTI = 2.0
ATR_TP_MULTI = 3.5
RSI_ENTRY_MIN = 45
RSI_ENTRY_MAX = 65
VOLUME_RATIO_MIN = 1.2
EMA_FAST = 20
EMA_SLOW = 50

# --- 리스크 ---
MAX_CONCURRENT_POSITIONS = 3
MAX_EQUITY_PCT_PER_POSITION = 0.30
MAX_RISK_PCT_PER_TRADE = 0.01
DAILY_MAX_LOSS_PCT = 0.03
CONSECUTIVE_LOSS_STOP = 3
PER_SYMBOL_COOLDOWN_HOURS = 8

# --- 운영 ---
DRY_RUN = env_bool("DRY_RUN", True)          # 기본 드라이런
MIN_24H_VOLUME_USD = 20_000_000
NEWS_LOOKBACK_HOURS = 6
GPT_REFRESH_HOURS = 4
```

**드라이런 원칙**: 초기 2주 이상 DRY_RUN=True로 시그널/체결 시뮬 → 승률·PnL 검증 후 라이브 전환.

---

## 16. Bybit 현물 API 연동 포인트

- **인증**: `ccxt.bybit({apiKey, secret, options:{defaultType:'spot', accountType:'UNIFIED'}})`
- **주문**: 시장가 매수는 `create_order(symbol, 'market', 'buy', None, None, {'quoteOrderQty': usdt_amt})` (quoteOrderQty로 USDT 금액 지정)
- **잔고**: `fetch_balance({'accountType':'UNIFIED'})` → `['free']['USDT']`
- **심볼 메타**: `markets[symbol]['limits']['amount']['min']`, `precision`
- **레이트리밋**: enableRateLimit=True + BybitRateLimiter (gridbot 포팅)
- **수수료**: 현물 기본 taker 0.1% (VIP/BB 보유로 할인 가능) — PnL 계산 시 반드시 반영

---

## 17. 테스트 (tests/)

- `test_technical.py`: 알려진 입력의 EMA/RSI/ATR 계산값 검증 (stock_bot 테스트케이스 재활용 가능)
- `test_entry_signal.py`: 합성 캔들 fixture로 크로스업/RSI/볼륨 조건 시나리오 10개
- `test_position_sizer.py`: 리스크 1% 상한, 자본 30% 상한, 최소주문금액 엣지
- GPT 호출은 mock (응답 JSON fixture 재생)

---

## 18. 스크립트 & 워치독

- `scripts/start_coin_bot.sh`: nohup python main.py, PID 저장
- `scripts/watchdog_coin.sh`: grid_bot watchdog_grid.sh 포팅, `kill -0 $PID` 체크 → 재시작
- `scripts/start_dashboard.sh`: `streamlit run dashboard/coin_dashboard.py --server.port 8502`
- crontab: `*/5 * * * * $HOME/coin\ bot/scripts/watchdog_coin.sh`

---

## 19. 라이브 전환 체크리스트 (메모리 feedback_live_strategy 준수)

- [ ] 드라이런 최소 14일
- [ ] 시그널 ≥ 20건 발생 & 승률 ≥ 45% & 평균 RR ≥ 1.3
- [ ] 텔레그램 알림/워치독/대시보드 모두 검증
- [ ] DRY_RUN=False 전환 전 `MAX_EQUITY_PCT_PER_POSITION`을 0.10으로 낮춰 최소 1주 소액 실거래
- [ ] 주간 리뷰 후 단계적 상향

---

## 20. Verification (Claude Code 담당)

구현 후 Claude Code가 아래를 검증:
1. `python -m pytest tests/ -v` 전체 통과
2. `DRY_RUN=True python main.py` 실행 → 1 cycle 로그에 뉴스수집→GPT→캔들조회→시그널체크→이벤트로그 순 기록 확인
3. Bybit 테스트: 실API키로 `fetch_balance`, `fetch_ohlcv("BTCUSDT","4h",100)` 정상 응답
4. 텔레그램 테스트 메시지 1건 발송 확인
5. 대시보드 기동 후 포트 8502 접속 + 빈 상태 렌더링 확인
6. Watchdog 수동 kill 후 5분 내 재기동 확인

---

## 21. 실행 순서 (Cursor에게)

1. `config.py`, `.env.example`, `requirements.txt` 먼저
2. `infra/event_log.py`, `infra/telegram.py`, `infra/state.py` (재활용 포팅)
3. `trading/exchange.py` (grid_bot 포팅) + 간단한 REPL로 fetch_ticker, fetch_ohlcv, fetch_balance 검증
4. `analysis/technical.py` + `tests/test_technical.py`
5. `data/news_collector.py`, `data/market_data.py`, `data/universe.py`
6. `analysis/gpt_analyzer.py` + 샘플 뉴스로 수동 1회 호출
7. `analysis/entry_signal.py`, `analysis/exit_signal.py` + `tests/test_entry_signal.py`
8. `trading/position_sizer.py`, `trading/risk_manager.py`, `trading/trader.py`
9. `main.py` (드라이런 모드)
10. `dashboard/coin_dashboard.py`
11. `scripts/*.sh`
12. README.md (실행 방법, .env 설명, 드라이런→라이브 전환 절차)

---

## 최종 산출물 위치
이 설계 지시서는 승인 후 **`$HOME/coin bot/SPEC.md`** 로 기록되어 Cursor가 참조한다.
