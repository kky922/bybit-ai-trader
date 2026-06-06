# Bybit AI Trader

뉴스 내러티브와 기술 지표를 결합한 Bybit 현물 자동매매 연구 프로젝트입니다.
기본값은 `DRY_RUN=true`이며 AI가 주문을 직접 실행하지 않고 별도 진입·리스크 검증을 거칩니다.

## 기능

- 뉴스 수집과 AI 기반 섹터·후보 분석
- BTC 시장 국면, 4시간·1시간 추세, RSI, 거래량 진입 필터
- ATR 손절·익절, 부분 청산, 트레일링 스톱
- 주문 한도, 회로 차단기, 포지션 상태 저장
- Telegram 알림과 Streamlit 대시보드
- Bybit TradFi RSA 인증 경로의 독립 dry-run 지원

## 설치

```bash
git clone https://github.com/kky922/bybit-ai-trader.git
cd bybit-ai-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 설정

dry-run에는 거래소 키가 필요하지 않습니다. AI 분석도 키가 없으면 비활성화됩니다.

```env
DRY_RUN=true
BYBIT_API_KEY_SUB2=
BYBIT_API_SECRET_SUB2=
DEEPSEEK_API_KEY=
TELEGRAM_BOT_TOKEN_COIN=
TELEGRAM_CHAT_ID_COIN=
DASHBOARD_PASSWORD_COIN=change-me
```

## 실행

```bash
# 주문 없는 기본 실행
DRY_RUN=true python main.py

# 대시보드
bash scripts/start_dashboard.sh

# 상태 점검
python scripts/coin_bot_autopilot_health.py
```

## 안전장치

- `DRY_RUN=true`가 저장소 기본값입니다.
- 최소 주문 금액, 동시 포지션 수, 일일 손실과 연속 손실을 제한합니다.
- 상태 파일과 로그는 Git에서 제외됩니다.
- 대시보드는 `DASHBOARD_PASSWORD_COIN`이 없으면 접근을 거부합니다.

## 테스트

```bash
pytest -q
```

## 주의사항

이 프로젝트는 투자 자문이 아닙니다. 거래소 장애, 모델 오류, 슬리피지와 원금 손실이
발생할 수 있습니다. 실거래 전 충분한 dry-run과 소액 검증이 필요합니다.

구조는 [docs/architecture.md](docs/architecture.md), 주요 변경 배경은
[docs/history.md](docs/history.md)에 정리되어 있습니다.

## 라이선스

MIT
