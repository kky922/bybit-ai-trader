from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
LOGS_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- Hot reloadable params.json (strategy/risk only; secrets stay in .env) ---
def _load_params() -> dict[str, Any]:
    params_path = ROOT_DIR / "params.json"
    try:
        if params_path.exists():
            with params_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            params: dict[str, Any] = {}
            params.update(raw.get("strategy", {}) or {})
            params.update(raw.get("risk", {}) or {})
            return params
    except Exception:
        pass
    return {}

_PARAMS = _load_params()


def _param(key: str, env_key: str, default: Any, cast):
    if key in _PARAMS:
        try:
            return cast(_PARAMS[key])
        except (TypeError, ValueError):
            return cast(default)
    env_val = os.getenv(env_key)
    if env_val is not None:
        try:
            return cast(env_val)
        except (TypeError, ValueError):
            return cast(default)
    return cast(default)


def _param_bool(key: str, env_key: str, default: bool) -> bool:
    if key in _PARAMS:
        raw = _PARAMS[key]
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}
    return _env_bool(env_key, default)


# --- API ---
BYBIT_API_KEY = _env("BYBIT_API_KEY_SUB2", "")
BYBIT_API_SECRET = _env("BYBIT_API_SECRET_SUB2", "")
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
GEMINI_API_KEY = _env("GEMINI_API_KEY", "")
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
GPT_MODEL = _env("GPT_MODEL", "deepseek-chat")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN_COIN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID_COIN", "")

# --- 전략 ---
ATR_STOP_MULTI = _param("ATR_STOP_MULTI", "ATR_STOP_MULTI", 1.5, float)
ATR_TP_MULTI = _env_float("ATR_TP_MULTI", 3.0)

# 5/24 추가: 심볼별 ATR_STOP_MULTI 차등 (클로드 피드백 반영)
# 변동성 높은 심볼(BTC/SOL)은 SL 여유 확대, 낮은 심볼은 타이트
SYMBOL_ATR_STOP_MAP: dict[str, float] = {
    "BTCUSDT": 3.0,
    "SOLUSDT": 3.0,
    "AVAXUSDT": 2.5,
    "XRPUSDT": 2.5,
    "PEPEUSDT": 2.5,
    "WLDUSDT": 2.5,
    "INJUSDT": 2.5,
    "LINKUSDT": 2.0,
    "DOTUSDT": 2.0,
    "NEARUSDT": 2.0,
    "ETHUSDT": 1.5,
    "STETHUSDT": 1.5,
}
def get_symbol_stop_mult(symbol: str) -> float:
    """심볼별 ATR stop multiplier 반환. 없으면 기본값."""
    return SYMBOL_ATR_STOP_MAP.get(symbol, ATR_STOP_MULTI)
RSI_ENTRY_MIN = _param("RSI_ENTRY_MIN", "RSI_ENTRY_MIN", 40.0, float)
RSI_ENTRY_MAX = _param("RSI_ENTRY_MAX", "RSI_ENTRY_MAX", 70.0, float)
RSI_MOMENTUM_MAX_DROP = _env_float("RSI_MOMENTUM_MAX_DROP", 0.0)
RSI_4H_MOMENTUM_SIDEBAND = _env_float("RSI_4H_MOMENTUM_SIDEBAND", 3.0)
VOLUME_RATIO_MIN = _env_float("VOLUME_RATIO_MIN", 1.0)
EMA_FAST = _param("EMA_FAST", "EMA_FAST", 20, int)
EMA_SLOW = _param("EMA_SLOW", "EMA_SLOW", 50, int)

# --- 마켓 컨디션 ---
ADX_PERIOD = _env_int("ADX_PERIOD", 14)
ADX_TRENDING_THRESHOLD = _env_float("ADX_TRENDING_THRESHOLD", 25.0)
ADX_STRONG_THRESHOLD = _env_float("ADX_STRONG_THRESHOLD", 50.0)
BTC_BAND_ATR_MULTI = _env_float("BTC_BAND_ATR_MULTI", 2.5)

# --- 물타기 (DCA) ---
DCA_ENABLED = _param_bool("DCA_ENABLED", "DCA_ENABLED", True)
DCA_LEVEL_1_PCT = _env_float("DCA_LEVEL_1_PCT", -4.0)     # -4% → 50% 추가
DCA_LEVEL_2_PCT = _env_float("DCA_LEVEL_2_PCT", -8.0)     # -8% → 75% 추가
DCA_LEVEL_3_PCT = _env_float("DCA_LEVEL_3_PCT", -12.0)    # -12% → 100% 추가
DCA_LEVEL_1_SIZE = _env_float("DCA_LEVEL_1_SIZE", 0.25)   # 초기 포지션의 25% (자본증액으로 DCA 리스크 축소)
DCA_LEVEL_2_SIZE = _env_float("DCA_LEVEL_2_SIZE", 0.50)   # 초기 포지션의 50% (자본증액으로 DCA 리스크 축소)
DCA_LEVEL_3_SIZE = _env_float("DCA_LEVEL_3_SIZE", 0.75)   # 초기 포지션의 75% (자본증액으로 DCA 리스크 축소)
DCA_COOLDOWN_MINUTES = _env_int("DCA_COOLDOWN_MINUTES", 60)  # 물타기 간격 최소 1시간
DCA_MAX_LEVELS = _env_int("DCA_MAX_LEVELS", 3)

# --- 불타기 / 부분익절 ---
PYRAMID_ENABLED = _env_bool("PYRAMID_ENABLED", True)
TP_1_MULTI = _param("TP_1_MULTI", "TP_1_MULTI", 3.0, float)       # TP1: ATR×2.0 → 40% 익절
TP_2_MULTI = _param("TP_2_MULTI", "TP_2_MULTI", 3.5, float)       # TP2: ATR×3.5 → 20% 익절
TP_3_MULTI = _param("TP_3_MULTI", "TP_3_MULTI", 5.0, float)       # TP3: ATR×5.0 → 10% 익절 (클로드 리뷰: Runner Tranche 30% 남김)
TP_1_EXIT_PCT = _env_float("TP_1_EXIT_PCT", 0.40)
TP_2_EXIT_PCT = _env_float("TP_2_EXIT_PCT", 0.20)
TP_3_EXIT_PCT = _env_float("TP_3_EXIT_PCT", 0.10)
TRAILING_REMAIN_PCT = _env_float("TRAILING_REMAIN_PCT", 0.30)  # Runner 30% — 하드TP 없이 순수 트레일링

# --- Runner Tranche (대세상승 캐치용) ---
# 클로드 Opus 4.7 리뷰 반영: "Runner tranche 하나면 80% 해결"
RUNNER_ENABLED = _param_bool("RUNNER_ENABLED", "RUNNER_ENABLED", True)                         # Runner 활성화
MIN_RUNNER_VALUE_USD = _param("MIN_RUNNER_VALUE_USD", "MIN_RUNNER_VALUE_USD", 10.0, float)           # TP3 이후 남은 Runner 최소 평가액
RUNNER_TRAIL_ATR_MULTI = _env_float("RUNNER_TRAIL_ATR_MULTI", 3.0)        # 챈들리어 트레일 (highest - ATR×3)
RUNNER_HARD_TRAIL_PCT = _env_float("RUNNER_HARD_TRAIL_PCT", 12.0)         # 하드 바닥 (%)
RUNNER_SKIP_NARRATIVE_FADE = _env_bool("RUNNER_SKIP_NARRATIVE_FADE", True) # Runner는 내러티브 페이드 무시
RUNNER_SKIP_TIMEOUT = _env_bool("RUNNER_SKIP_TIMEOUT", True)              # Runner는 시간제한 무시

# --- Parabolic "Skyhook" Detector (클로드 리뷰 반영) ---
# 포지션이 급등 중이면 Runner 모드로 조기 전환
PARABOLIC_ENABLED = _param_bool("PARABOLIC_ENABLED", "PARABOLIC_ENABLED", True)
PARABOLIC_GAIN_7D_PCT = _env_float("PARABOLIC_GAIN_7D_PCT", 40.0)          # 7일 +40% 이상
PARABOLIC_GAIN_3D_PCT = _env_float("PARABOLIC_GAIN_3D_PCT", 25.0)          # 3일 +25% 이상
PARABOLIC_VOLUME_MIN = _env_float("PARABOLIC_VOLUME_MIN", 1.5)             # 거래량 1.5배 이상 (20일 평균 대비)

# --- 켈리 기준 ---
KELLY_ENABLED = _env_bool("KELLY_ENABLED", True)
KELLY_FRACTION = _param("KELLY_FRACTION", "KELLY_FRACTION", 0.25, float)   # 1/4 켈리

# --- 일일 한도 ---
DAILY_PROFIT_TARGET_PCT = _env_float("DAILY_PROFIT_TARGET_PCT", 0.03)    # +3% 도달 시 진입 중단
DAILY_PROFIT_STOP_NEW_ENTRIES = _env_bool("DAILY_PROFIT_STOP_NEW_ENTRIES", True)
DAILY_LOSS_STOP_TRADING = _env_bool("DAILY_LOSS_STOP_TRADING", True)
DAILY_PROFIT_COOLDOWN_HOURS = _env_int("DAILY_PROFIT_COOLDOWN_HOURS", 0)  # 0 = 익일 자동 재개

# --- 리스크 ---
MAX_CONCURRENT_POSITIONS = _param("MAX_CONCURRENT_POSITIONS", "MAX_CONCURRENT_POSITIONS", 4, int)
MAX_EQUITY_PCT_PER_POSITION = _env_float("MAX_EQUITY_PCT_PER_POSITION", 0.20)
MAX_RISK_PCT_PER_TRADE = _param("MAX_RISK_PCT_PER_TRADE", "MAX_RISK_PCT_PER_TRADE", 0.01, float)
DAILY_MAX_LOSS_PCT = _param("DAILY_MAX_LOSS_PCT", "DAILY_MAX_LOSS_PCT", 0.05, float)
CONSECUTIVE_LOSS_STOP = _param("CONSECUTIVE_LOSS_STOP", "CONSECUTIVE_LOSS_STOP", 3, int)
PER_SYMBOL_COOLDOWN_HOURS = _param("PER_SYMBOL_COOLDOWN_HOURS", "PER_SYMBOL_COOLDOWN_HOURS", 8, int)
REPEAT_LOSER_THRESHOLD = _env_int("REPEAT_LOSER_THRESHOLD", 2)           # 심볼이 이 횟수 이상 연속 손실 시 extended cooldown
REPEAT_LOSER_COOLDOWN_HOURS = _param("REPEAT_LOSER_COOLDOWN_HOURS", "REPEAT_LOSER_COOLDOWN_HOURS", 72, int) # 반복 손실 심볼의 추가 쿨다운 시간

# --- 운영 ---
DRY_RUN = _env_bool("DRY_RUN", True)
DRY_RUN_EQUITY_USDT = _env_float("DRY_RUN_EQUITY_USDT", 1000.0)  # 드라이런 가상 잔고
DUST_THRESHOLD_USD = _param("DUST_THRESHOLD_USD", "DUST_THRESHOLD_USD", 5.0, float)       # 이 평가액 미만 포지션 자동 정리 시도
MIN_24H_VOLUME_USD = _env_float("MIN_24H_VOLUME_USD", 5_000_000)
NEWS_LOOKBACK_HOURS = _env_int("NEWS_LOOKBACK_HOURS", 6)
GPT_REFRESH_HOURS = _env_int("GPT_REFRESH_HOURS", 4)
MAIN_LOOP_SLEEP_SECONDS = _env_int("MAIN_LOOP_SLEEP_SECONDS", 30)
MIN_NOTIONAL_USDT_DEFAULT = _env_float("MIN_NOTIONAL_USDT_DEFAULT", 5.0)
BTC_CRASH_FILTER_PCT_4H = _env_float("BTC_CRASH_FILTER_PCT_4H", -3.0)
BTC_TREND_SIDEBAND_PCT = _env_float("BTC_TREND_SIDEBAND_PCT", 3.0)  # BTC EMA50 아래 얕은 눌림 허용 폭(%)
SYMBOL_EMA50_SIDEBAND_PCT = _env_float("SYMBOL_EMA50_SIDEBAND_PCT", 3.0)  # 개별 코인 EMA50 아래 얕은 눌림 허용 폭(%)
SOFT_PASS_1H_SIDEBAND_PCT = _env_float("SOFT_PASS_1H_SIDEBAND_PCT", 1.5)  # BTC soft-pass 중 1H 정렬 close > EMA20 완화 폭(%)
TREND_BREAK_SIDEBAND_PCT = _env_float("TREND_BREAK_SIDEBAND_PCT", 0.0)  # 1H EMA50 이탈 청산 허용 폭(%), 0=현행유지
TRAILING_ATR_MULTI = _env_float("TRAILING_ATR_MULTI", 1.5)
NARRATIVE_FADE_GRACE_HOURS = _env_int("NARRATIVE_FADE_GRACE_HOURS", 4)
SECTOR_HEAT_EXIT_THRESHOLD = _env_int("SECTOR_HEAT_EXIT_THRESHOLD", 2)
GLOBAL_COOLDOWN_HOURS = _env_int("GLOBAL_COOLDOWN_HOURS", 6)
POST_COOLDOWN_GRACE_SCANS = _env_int("POST_COOLDOWN_GRACE_SCANS", 2)  # 쿨다운 종료 후 첫 N회 스캔을 그레이스 기간으로
POST_COOLDOWN_CONVICTION_MIN = _env_float("POST_COOLDOWN_CONVICTION_MIN", 7.0)  # 그레이스 기간 중 최소 확신도
POST_COOLDOWN_MAX_ENTRIES = _env_int("POST_COOLDOWN_MAX_ENTRIES", 1)  # 그레이스 기간 중 최대 진입 수
MIN_CANDIDATE_CONVICTION = _env_float("MIN_CANDIDATE_CONVICTION", 1.0)  # 일반 진입 후보 최소 확신도
MAX_POSITION_HOURS = _env_float("MAX_POSITION_HOURS", 48.0)
MAX_ENTRIES_PER_SCAN = _env_int("MAX_ENTRIES_PER_SCAN", 2)

# --- TradFi (Bybit AI Subaccount) ---
TRADFI_API_KEY = _env("TRADFI_API_KEY", "")
TRADFI_PRIVATE_KEY_PATH = _env("TRADFI_PRIVATE_KEY_PATH", "bybit_tradfi_private.pem")
TRADFI_DRY_RUN = _env_bool("TRADFI_DRY_RUN", True)
TRADFI_EQUITY_USDT = _env_float("TRADFI_EQUITY_USDT", 30.0)
TRADFI_MAX_CONCURRENT_POSITIONS = _env_int("TRADFI_MAX_CONCURRENT_POSITIONS", 2)
TRADFI_MAX_EQUITY_PCT_PER_POSITION = _env_float("TRADFI_MAX_EQUITY_PCT_PER_POSITION", 0.30)
# TradFi 전용 진입 필터 (주식/원자재는 변동성 낮아 더 타이트한 RSI/volume 범위 사용)
TRADFI_RSI_ENTRY_MIN = _env_float("TRADFI_RSI_ENTRY_MIN", 35.0)
TRADFI_RSI_ENTRY_MAX = _env_float("TRADFI_RSI_ENTRY_MAX", 75.0)
TRADFI_VOLUME_RATIO_MIN = _env_float("TRADFI_VOLUME_RATIO_MIN", 0.8)
# TradFi 베어 시장 진입 필터 완화 — bear_crash regime에서 ATR stop을 넓게
TRADFI_BEAR_ATR_STOP_MULTI = _env_float("TRADFI_BEAR_ATR_STOP_MULTI", 2.5)
TRADFI_BEAR_ATR_TP_MULTI = _env_float("TRADFI_BEAR_ATR_TP_MULTI", 2.0)  # 베어장은 TP를 좁게

# --- Trailing Stop Params (referenced by tradfi_main._check_trailing_stops) ---
TRAILING_STOP_ACTIVATION_PCT = _env_float("TRAILING_STOP_ACTIVATION_PCT", 0.003)  # 0.3% above entry -> activate trailing
TRAILING_STOP_CALLBACK_PCT = _env_float("TRAILING_STOP_CALLBACK_PCT", 0.002)     # 0.2% callback from highest -> trigger exit

# --- 대시보드 ---
DASHBOARD_PASSWORD = _env("DASHBOARD_PASSWORD_COIN", "")

NEWS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    "https://news.google.com/rss/search?q=crypto+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en",
]
CRYPTOPANIC_URL_TEMPLATE = (
    "https://cryptopanic.com/api/v1/posts/?auth_token={token}&kind=news"
)


def ensure_dirs() -> None:
    for directory in [
        LOGS_DIR,
        ROOT_DIR / "dashboard",
        ROOT_DIR / "data",
        ROOT_DIR / "analysis",
        ROOT_DIR / "trading",
        ROOT_DIR / "infra",
        ROOT_DIR / "scripts",
        ROOT_DIR / "tests",
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def config_summary() -> dict[str, Any]:
    return {
        "dry_run": DRY_RUN,
        "gpt_model": GPT_MODEL,
        "max_positions": MAX_CONCURRENT_POSITIONS,
        "risk_pct_per_trade": MAX_RISK_PCT_PER_TRADE,
    }
