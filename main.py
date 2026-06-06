from __future__ import annotations

import atexit
import fcntl
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

import config
from hot_reload import HotReloader
from analysis.entry_signal import check_entry_diagnostic
from analysis.exit_signal import check_exit, check_dca_trigger, check_partial_tp, detect_parabolic
from analysis.gpt_analyzer import GPTAnalyzer
from data.market_data import MarketData, fetch_fear_greed_index, detect_market_regime, get_cached_regime_sizing
from data.news_collector import NewsCollector
from data.universe import get_tradable_symbols
from infra.context_from import check_tier2_trigger, load_context
from infra.event_log import log_event
from infra.runtime_policy import RuntimePolicy, load_runtime_policy
from infra.state import load_positions, save_positions
from infra.telegram import CoinTelegram
from trading.exchange import BybitExchange
from trading.position_sizer import set_regime_sizing
from trading.risk_manager import RiskManager
from trading.trader import Trader

# --- 개선 2: 로그 로테이션 (10MB × 5개 = 최대 50MB) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            config.LOGS_DIR / "coin_bot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("coin-bot")

# --- 개선 1: PID 파일 관리 ---
PID_FILE = config.LOGS_DIR / "coin_bot.pid"
LOCK_FILE = config.LOGS_DIR / "coin_bot.lock"
_LOCK_HANDLE = None


def _acquire_single_instance() -> None:
    """Prevent multiple coin bot processes from running at the same time.

    PID files are useful for humans and scripts, but they are not atomic: two
    processes can start together before either one writes the PID file. The
    advisory flock below is acquired before expensive startup work, so direct
    `python3 main.py`, launchd, watchdog, and manual starts all share the same
    single-instance gate.
    """
    global _LOCK_HANDLE
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.seek(0)
        existing_pid = lock_handle.read().strip() or "unknown"
        logger.error(
            "coin bot is already running; refusing duplicate startup "
            "(lock=%s owner_pid=%s current_pid=%d)",
            LOCK_FILE,
            existing_pid,
            os.getpid(),
        )
        raise SystemExit(0)

    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    os.fsync(lock_handle.fileno())
    _LOCK_HANDLE = lock_handle
    logger.info("single-instance lock acquired: %s (pid=%d)", LOCK_FILE, os.getpid())


def _write_pid() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    logger.info("PID file created: %s (pid=%d)", PID_FILE, os.getpid())


def _remove_pid() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _release_single_instance() -> None:
    global _LOCK_HANDLE
    if _LOCK_HANDLE is None:
        return
    try:
        fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        _LOCK_HANDLE.close()
    except Exception:
        pass
    finally:
        _LOCK_HANDLE = None


atexit.register(_remove_pid)
atexit.register(_release_single_instance)


def at_hour_close(now: datetime) -> bool:
    return now.minute == 0 and now.second >= 30


def at_15min(now: datetime) -> bool:
    return now.minute % 15 == 0 and now.second >= 30


def at_4h_close(now: datetime) -> bool:
    return now.hour % 4 == 0 and at_hour_close(now)


def every_5min(now: datetime) -> bool:
    return now.minute % 5 == 0 and now.second >= 30


def _policy_reason(policy: RuntimePolicy) -> str:
    return ";".join(policy.reasons) if policy.reasons else "runtime_policy"


def runtime_entry_skip_reason(symbol: str, policy: RuntimePolicy) -> str | None:
    sym = str(symbol).upper().replace("/", "")
    reason = _policy_reason(policy)
    if policy.block_new_entries:
        return f"runtime_policy_block:{reason}"
    if policy.conservative_mode and sym in policy.excluded_symbols:
        return f"runtime_policy_symbol_excluded:{sym}:{reason}"
    return None


def runtime_dca_skip_reason(policy: RuntimePolicy) -> str | None:
    if policy.block_dca:
        return f"runtime_policy_dca_block:{_policy_reason(policy)}"
    return None


def build_hourly_status(exchange: BybitExchange) -> str:
    positions = load_positions()
    balance = exchange.get_balance_usdt()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone().strftime("%m-%d %H:%M")
    now_utc_text = now_utc.strftime("%H:%M UTC")
    if not positions:
        return (
            "━━━━━━━━━━━━━━\n"
            "📊 <b>코인봇 1시간 리포트</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🕒 시각: {now_local} ({now_utc_text})\n"
            f"🧪 모드: {'드라이런' if config.DRY_RUN else '실거래'}\n"
            f"💵 USDT 잔고: <b>{balance:.2f}</b>\n"
            "📦 보유 포지션: <b>0개</b>\n"
            "━━━━━━━━━━━━━━"
        )

    lines: list[str] = []
    total_unrealized = 0.0
    for p in positions:
        symbol = p.get("symbol", "?")
        entry = float(p.get("entry_price", 0.0) or 0.0)
        size = float(p.get("size", 0.0) or 0.0)
        try:
            ticker = exchange.fetch_ticker(symbol)
            last = float(ticker.get("last") or entry)
        except Exception:
            last = entry
        pnl = (last - entry) * size
        pnl_pct = ((last / entry - 1.0) * 100.0) if entry > 0 else 0.0
        total_unrealized += pnl
        lines.append(f"• {symbol}: {pnl:+.2f} USDT ({pnl_pct:+.2f}%) | 현재가 {last:.4f}")

    held_symbols = [p.get("symbol", "?") for p in positions[:8]]
    held_symbols_text = ", ".join(held_symbols)
    if len(positions) > 8:
        held_symbols_text += f" ...(+{len(positions) - 8})"
    return (
        "━━━━━━━━━━━━━━\n"
        "📊 <b>코인봇 1시간 리포트</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🕒 시각: {now_local} ({now_utc_text})\n"
        f"🧪 모드: {'드라이런' if config.DRY_RUN else '실거래'}\n"
        f"💵 USDT 잔고: <b>{balance:.2f}</b>\n"
        f"📦 보유 포지션: <b>{len(positions)}개</b>\n"
        f"📈 미실현 손익: <b>{total_unrealized:+.2f} USDT</b>\n"
        f"🪙 보유 심볼: {held_symbols_text}\n"
        "━━━━━━━━━━━━━━\n"
        "📋 포지션별 손익\n"
        + "\n".join(lines)
        + "\n━━━━━━━━━━━━━━"
    )


def _live_guard() -> None:
    """DRY_RUN → LIVE 전환 시 환경변수 확인으로 실수 방지.

    LIVE 모드로 시작하려면 .env에 CONFIRM_LIVE=yes 를 반드시 명시해야 한다.
    이 가드가 없으면 DRY_RUN=false 만으로 즉시 실거래가 시작되어 의도치 않은
    주문이 발생할 수 있다.
    """
    if config.DRY_RUN:
        return
    confirm = os.getenv("CONFIRM_LIVE", "").strip().lower()
    if confirm != "yes":
        raise SystemExit(
            "\n⛔ LIVE 모드 실행 차단됨.\n"
            "   LIVE 거래를 시작하려면 .env 또는 환경변수에 CONFIRM_LIVE=yes 를 설정하세요.\n"
            "   현재값: CONFIRM_LIVE=" + repr(os.getenv("CONFIRM_LIVE", "(미설정)"))
        )
    logger.warning("🔴 LIVE MODE 확인됨 (CONFIRM_LIVE=yes) — 실거래 시작")


def run() -> None:
    config.ensure_dirs()
    _live_guard()
    _acquire_single_instance()
    _write_pid()

    reloader = HotReloader(
        params_path=str(config.ROOT_DIR / "params.json"),
        config_module=config,
        history_dir=str(config.ROOT_DIR / "params_history"),
        check_interval=5.0,
        log_func=log_event,
    )
    reloader.start()
    atexit.register(reloader.stop)

    exchange = BybitExchange()
    market_data = MarketData(exchange)
    risk_manager = RiskManager()
    trader = Trader(exchange, risk_manager)
    telegram = CoinTelegram()
    news_collector = NewsCollector()

    tradable = get_tradable_symbols(exchange)
    logger.info("tradable symbols at startup: %d", len(tradable))
    if not tradable:
        logger.warning("⚠️ No tradable symbols loaded! Check exchange connection.")

    analyzer = GPTAnalyzer(tradable_symbols=tradable)
    latest_sector_analysis: dict[str, Any] = analyzer.last_sectors or {"sectors": []}
    latest_candidates: list[dict[str, Any]] = analyzer.last_candidates or []
    last_gpt_refresh_at = "cached" if latest_candidates else "never"
    last_entry_scan_at = "never"

    # 중복 발화 방지: 마지막 실행 시각 추적
    last_15min_run: int = -1
    last_4h_run: int = -1
    last_5min_run: int = -1

    # Two-Tier: context_from 체인 — 이전 브리지 해시 (Tier1 상시 체크용)
    last_bridge_hash: str | None = None

    logger.info("coin bot started | dry_run=%s", config.DRY_RUN)
    log_event(
        "health",
        "coin bot started",
        f"pid={os.getpid()} dry_run={config.DRY_RUN} tradable={len(tradable)} "
        f"cached_candidates={len(latest_candidates)}",
    )
    telegram.send_sync(
        "🚀 <b>Coin Bot Started</b>\n"
        f"모드: {'🧪 DRY_RUN' if config.DRY_RUN else '💰 LIVE'}\n"
        f"잔고: {exchange.get_balance_usdt():.2f} USDT\n"
        f"설정: 최대 {config.MAX_CONCURRENT_POSITIONS}개 포지션, "
        f"1회 {config.MAX_ENTRIES_PER_SCAN}개 진입, "
        f"포지션당 {config.MAX_EQUITY_PCT_PER_POSITION*100:.0f}%\n"
        f"일손절: {config.DAILY_MAX_LOSS_PCT*100:.0f}% | "
        f"BTC필터: {config.BTC_CRASH_FILTER_PCT_4H:.0f}%"
    )

    # 기존 포지션에 거래소 SL 복원 (재시작 대비)
    if not config.DRY_RUN:
        existing = load_positions()
        restored = 0
        for p in existing:
            if not p.get("sl_order_id"):
                sym = p["symbol"]
                try:
                    sl_order = exchange.create_stop_loss_order(sym, float(p["size"]), float(p["stop_loss"]))
                    p["sl_order_id"] = sl_order.get("id")
                    restored += 1
                    log_event("sl_restored", f"{sym} SL restored", f"order_id={p['sl_order_id']}")
                except Exception as e:
                    log_event("sl_restore_failed", f"{sym} SL restore failed", f"reason={e}")
        if restored:
            save_positions(existing)
            log_event("health", f"SL orders restored for {restored} positions", "")

    # 개선 5: 시작 시 첫 GPT 분석 즉시 실행 (4시간 공백 제거)
    try:
        logger.info("Running initial GPT analysis...")
        news = news_collector.collect(config.NEWS_LOOKBACK_HOURS)
        latest_sector_analysis = analyzer.detect_sectors(news)
        latest_candidates = analyzer.pick_coins(latest_sector_analysis)
        last_gpt_refresh_at = datetime.now(timezone.utc).isoformat()
        log_event("gpt_init", "initial analysis done", f"candidates={len(latest_candidates)}")
        logger.info("Initial analysis complete: %d candidates", len(latest_candidates))
        telegram.send_sync(f"🧠 Initial analysis done\nCandidates: {len(latest_candidates)}")
    except Exception as e:
        logger.warning("Initial GPT analysis failed (will retry at 4h mark): %s", e)
        latest_sector_analysis = analyzer.last_sectors or latest_sector_analysis
        latest_candidates = analyzer.last_candidates or latest_candidates
        log_event("gpt_init_failed", "initial analysis failed", f"reason={e} cached_candidates={len(latest_candidates)}")

    while True:
        now = datetime.now(timezone.utc)
        try:
            current_15min = now.hour * 4 + (now.minute // 15)
            current_4h = now.hour // 4
            current_5min = now.hour * 60 + (now.minute // 5)

            trader.cleanup_dust_positions()

            if at_15min(now) and current_15min != last_15min_run:
                last_15min_run = current_15min
                last_entry_scan_at = now.isoformat()
                log_event(
                    "health",
                    "entry scan started",
                    f"candidates={len(latest_candidates)} positions={len(load_positions())} "
                    f"last_gpt={last_gpt_refresh_at}",
                )
                runtime_policy = load_runtime_policy(analyzer.tradable_symbols)
                if runtime_policy != RuntimePolicy():
                    log_event(
                        "runtime_policy",
                        "policy loaded",
                        f"block_new_entries={runtime_policy.block_new_entries} "
                        f"block_dca={runtime_policy.block_dca} "
                        f"conservative={runtime_policy.conservative_mode} "
                        f"excluded={','.join(sorted(runtime_policy.excluded_symbols)) or '-'} "
                        f"reasons={';'.join(runtime_policy.reasons) or '-'}",
                    )
                positions = load_positions()
                for pos in positions:
                    c1 = market_data.get(pos["symbol"], "1h")
                    c4 = market_data.get(pos["symbol"], "4h")

                    # ── DCA 트리거 체크 (물타기) ──
                    if config.DCA_ENABLED:
                        dca_policy_reason = runtime_dca_skip_reason(runtime_policy)
                        if dca_policy_reason:
                            log_event(
                                "dca_skip",
                                f"{pos['symbol']} skipped",
                                f"reason={dca_policy_reason}",
                            )
                        else:
                            dca_sig = check_dca_trigger(pos, c1)
                            if dca_sig and not risk_manager.in_dca_cooldown(pos["symbol"]):
                                dca_level = risk_manager.get_dca_level(pos["symbol"])
                                dca_map = {"dca1": 1, "dca2": 2, "dca3": 3}
                                if dca_map.get(dca_sig, 0) > dca_level:
                                    dca_result = trader.add_dca_position(pos["symbol"], dca_sig)
                                    if dca_result.get("ok"):
                                        mode = "🧪" if config.DRY_RUN else "💰"
                                        telegram.send_sync(
                                            f"{mode} <b>물타기</b> {pos['symbol']}\n"
                                            f"레벨: {dca_sig}\n"
                                            f"추가: {dca_result.get('dca_qty', 0):.6f} @ {dca_result.get('dca_price', 0):.4f}\n"
                                            f"평단: {dca_result.get('new_avg', 0):.4f}"
                                        )

                    # ── 부분 TP 체크 (불타기) ──
                    if config.PYRAMID_ENABLED:
                        tp_sig = check_partial_tp(pos, c1)
                        if tp_sig:
                            part_result = trader.partial_exit(pos, tp_sig)
                            if part_result.get("ok"):
                                mode = "🧪" if config.DRY_RUN else "💰"
                                pnl = part_result.get("pnl", 0)
                                is_final = part_result.get("final", False)
                                tag = "🏁 최종" if is_final else "📗 부분익절"
                                telegram.send_sync(
                                    f"{mode} {tag} {pos['symbol']}\n"
                                    f"레벨: {tp_sig}\n"
                                    f"PnL: {pnl:+.2f} USDT\n"
                                    f"청산가: {part_result.get('exit_price', 0):.4f}"
                                )

                    # ── 일반 청산 체크 (SL/TP/타임아웃 등) ──
                    reason = check_exit(pos, c1, c4, latest_sector_analysis)
                    if reason:
                        result = trader.exit(pos, reason)
                        mode = "🧪" if config.DRY_RUN else "💰"
                        pnl = result.get("pnl", 0)
                        pnl_pct = result.get("pnl_pct", 0)
                        exit_px = result.get("exit_price", 0)

                        if result.get("already_exited"):
                            telegram.send_sync(
                                f"⚠️ <b>청산 감지</b> {pos['symbol']}\n"
                                f"거래소 SL 자동청산됨 (봇 재시작 후 정리)\n"
                                f"청산가: {exit_px:.4f}"
                            )
                        else:
                            balance = exchange.get_balance_usdt() if not config.DRY_RUN else 0
                            msg_parts = [
                                f"{mode} <b>청산</b> {pos['symbol']}",
                                f"사유: {reason}",
                                f"📈 PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)",
                            ]
                            if exit_px:
                                msg_parts.append(f"청산가: {exit_px:.4f}")
                            if balance:
                                msg_parts.append(f"💵 잔고: {balance:.2f} USDT")
                            telegram.send_sync("\n".join(msg_parts))

                # highest_price 업데이트 (trailing stop 작동을 위해)
                updated = []
                for pos in load_positions():
                    ticker = exchange.fetch_ticker(pos["symbol"])
                    price = float(ticker.get("last") or 0.0)
                    if price and price > float(pos.get("highest_price", 0)):
                        pos["highest_price"] = price
                    updated.append(pos)
                save_positions(updated)

                # ── Parabolic Detector: 급등 포지션 → Runner 모드 조기 전환 ──
                # 클로드 Opus 4.7 리뷰 반영: "Skyhook detector is 80% of the value"
                if config.PARABOLIC_ENABLED:
                    for pos in load_positions():
                        if pos.get("runner_mode"):
                            continue  # 이미 Runner 모드
                        try:
                            c1 = market_data.get(pos["symbol"], "1h")
                            c4 = market_data.get(pos["symbol"], "4h")
                            if detect_parabolic(pos, c1, c4):
                                pos["runner_mode"] = True
                                save_positions(load_positions())  # flush
                                log_event("parabolic", f"{pos['symbol']} parabolic detected",
                                          f"mode=runner_transition gain_pct estimate")
                                mode = "🧪" if config.DRY_RUN else "💰"
                                telegram.send_sync(
                                    f"{mode} 🚀 <b>Parabolic 감지!</b> {pos['symbol']}\n"
                                    f"Skyhook 모드 전환 — 모든 TP 해제, 챈들리어 트레일 활성화\n"
                                    f"하늘끝까지 탑승 준비 완료 🛸"
                                )
                        except Exception as e:
                            logger.debug("Parabolic check failed for %s: %s", pos["symbol"], e)

                btc_4h = market_data.get("BTCUSDT", "4h")
                btc_change = ((btc_4h["close"].iloc[-1] / btc_4h["close"].iloc[-2]) - 1.0) * 100
                can_trade, block_reason = risk_manager.can_trade(exchange.get_balance_usdt(), btc_change)

                # ── Regime-aware Sizing (클로드 리뷰 반영) ──
                regime_sizing = get_cached_regime_sizing(btc_4h)
                regime_max_pos = regime_sizing["max_positions"]
                set_regime_sizing(regime_sizing)  # position_sizer에 전달
                logger.info("Regime sizing: max_pos=%d equity_pct=%.1f%% kelly=%.2f",
                            regime_max_pos, regime_sizing["equity_pct"]*100, regime_sizing["kelly_fraction"])

                if can_trade:
                    # ── Post-cooldown grace: after global cooldown lifts, run conservative ──
                    in_grace, grace_scans_remaining = risk_manager.in_post_cooldown_grace()
                    max_entries = config.POST_COOLDOWN_MAX_ENTRIES if in_grace else config.MAX_ENTRIES_PER_SCAN
                    if in_grace:
                        logger.info(
                            "Post-cooldown grace: %d scans remaining, max %d entry/scan, "
                            "conviction >= %.1f",
                            grace_scans_remaining, max_entries, config.POST_COOLDOWN_CONVICTION_MIN,
                        )
                    held = {p["symbol"] for p in load_positions()}
                    entries_this_scan = 0
                    for c in latest_candidates:
                        sym = c["symbol"]
                        policy_skip = runtime_entry_skip_reason(sym, runtime_policy)
                        if policy_skip:
                            log_event("entry_skip", f"{sym} skipped", f"reason={policy_skip}")
                            continue
                        if sym in held:
                            log_event("entry_skip", f"{sym} skipped", "reason=already_held")
                            continue
                        if risk_manager.in_symbol_cooldown(sym):
                            log_event("entry_skip", f"{sym} skipped", "reason=symbol_cooldown")
                            continue
                        if len(held) >= regime_max_pos:
                            log_event(
                                "entry_skip",
                                f"{sym} skipped",
                                f"reason=max_positions_reached:{regime_max_pos}",
                            )
                            break
                        # 동일 섹터 중복 방지 (예: AI 코인 2개 동시 보유 금지)
                        sector = c.get("sector", "")
                        if sector and risk_manager.has_same_sector_position(sym, sector, load_positions()):
                            log_event("entry_skip", f"{sym} skipped", "reason=same_sector_already_held")
                            continue
                        # Small-account 집중모드: LLM conviction 낮은 후보는 기술조건 검사 전 차단
                        conv = float(c.get("conviction", 0) or 0)
                        if conv < config.MIN_CANDIDATE_CONVICTION:
                            log_event(
                                "entry_skip", f"{sym} skipped",
                                f"reason=low_candidate_conviction:{conv:.1f}<min:{config.MIN_CANDIDATE_CONVICTION:.1f}",
                            )
                            continue
                        # Post-cooldown grace: require higher conviction
                        if in_grace:
                            if conv < config.POST_COOLDOWN_CONVICTION_MIN:
                                log_event(
                                    "entry_skip", f"{sym} skipped",
                                    f"reason=post_cooldown_low_conviction:{conv:.1f}",
                                )
                                continue
                        s4 = market_data.get(sym, "4h")
                        s1 = market_data.get(sym, "1h")
                        sig, sig_reason = check_entry_diagnostic(sym, s4, s1, btc_4h)
                        if sig:
                            if sig_reason.startswith("btc_soft_pass:"):
                                log_event("health", "BTC soft-pass", sig_reason)
                            result = trader.enter(
                                sym, sig, c.get("sector", "unknown"), float(c.get("conviction", 1))
                            )
                            if result.get("ok"):
                                mode = "🧪" if config.DRY_RUN else "💰"
                                usdt_amount = result.get("usdt_amount", 0)
                                entry_px = sig.entry_price
                                sl_px = sig.stop_loss
                                tp_px = sig.take_profit
                                risk_pct = ((entry_px - sl_px) / entry_px) * 100 if entry_px > 0 else 0
                                rpct = risk_pct * config.MAX_EQUITY_PCT_PER_POSITION
                                msg_parts = [
                                    f"{mode} <b>진입</b> {sym}",
                                    f"섹터: {c.get('sector', '?')} (conviction {c.get('conviction', '?')})",
                                    f"💰 {usdt_amount:.2f} USDT @ {entry_px:.4f}",
                                    f"🎯 SL {sl_px:.4f} ({risk_pct:.1f}%) | TP {tp_px:.4f}",
                                ]
                                telegram.send_sync("\n".join(msg_parts))
                                held.add(sym)
                                entries_this_scan += 1
                                if entries_this_scan >= max_entries:
                                    log_event(
                                        "entry_skip",
                                        "remaining candidates skipped",
                                        f"reason=max_entries_per_scan:{max_entries} "
                                        f"(entered {sym}, {len(load_positions())} positions after entry)",
                                    )
                                    break
                            else:
                                log_event(
                                    "entry_skip",
                                    f"{sym} enter failed",
                                    f"reason={result.get('reason', 'unknown')}",
                                )
                        else:
                            log_event("entry_skip", f"{sym} signal rejected", f"reason={sig_reason}")
                    if in_grace:
                        risk_manager.record_post_cooldown_scan()
                else:
                    log_event("risk_block", "entry blocked", block_reason)
                    logger.warning("Entry blocked: %s", block_reason)

                logger.info(
                    "Hourly scan done: candidates=%d positions=%d last_gpt=%s",
                    len(latest_candidates),
                    len(load_positions()),
                    last_gpt_refresh_at,
                )
                log_event(
                    "health",
                    "entry scan finished",
                    f"candidates={len(latest_candidates)} positions={len(load_positions())} "
                    f"last_entry_scan={last_entry_scan_at}",
                )

                # 1시간마다 운영 현황을 텔레그램으로 발송
                try:
                    telegram.send_sync(build_hourly_status(exchange))
                except Exception as e:
                    logger.warning("hourly status send failed: %s", e)

            if at_4h_close(now) and current_4h != last_4h_run:
                last_4h_run = current_4h

                # ── Tier 1: context_from 해시 체크 (항상 실행) ──
                run_tier2, bridge_ctx = check_tier2_trigger("coin-bot")
                if bridge_ctx.hash != last_bridge_hash:
                    last_bridge_hash = bridge_ctx.hash
                    log_event(
                        "context_from",
                        "bridge state updated",
                        f"hash={bridge_ctx.hash} regime={bridge_ctx.regime} "
                        f"bias={bridge_ctx.action_bias} targets={bridge_ctx.targets}",
                    )

                # ── Tier 2: regime 변경 또는 신규 신호 시에만 GPT 갱신 ──
                # no_change(동일 해시) 상황에서는 아래 유니버스/GPT 갱신은 스킵되지 않음
                # (4h 주기 갱신은 시장 유니버스 최신화 목적이므로 항상 수행)
                # Tier2 트리거 시: 브리지 컨텍스트를 GPT 분석에 주입
                if run_tier2:
                    log_event(
                        "context_from",
                        "Tier2 triggered",
                        f"regime={bridge_ctx.regime} bias={bridge_ctx.action_bias} "
                        f"confidence={bridge_ctx.confidence} urgency={bridge_ctx.urgency}",
                    )
                    telegram.send_sync(
                        f"📡 <b>브리지 신호 수신</b>\n"
                        f"Regime: {bridge_ctx.regime} | Bias: {bridge_ctx.action_bias}\n"
                        f"신뢰도: {bridge_ctx.confidence} | 긴급도: {bridge_ctx.urgency}\n"
                        f"리스크: {bridge_ctx.risk_flags[:80]}\n"
                        f"근거: {bridge_ctx.evidence[:120]}"
                    )

                # 4시간마다 유니버스 갱신 (새 상장/상폐 반영)
                try:
                    new_tradable = get_tradable_symbols(exchange)
                    if new_tradable:
                        analyzer.tradable_symbols = set(new_tradable)
                        logger.info("universe refreshed: %d symbols", len(new_tradable))
                    else:
                        logger.warning("universe refresh returned 0 symbols, keeping previous")
                except Exception as e:
                    logger.warning("universe refresh failed: %s", e)

                news = news_collector.collect(config.NEWS_LOOKBACK_HOURS)
                latest_sector_analysis = analyzer.detect_sectors(news)
                latest_candidates = analyzer.pick_coins(latest_sector_analysis)
                last_gpt_refresh_at = datetime.now(timezone.utc).isoformat()
                log_event(
                    "gpt_refresh",
                    "sector/candidate refreshed",
                    f"sectors={len(latest_sector_analysis.get('sectors', []))} "
                    f"candidates={len(latest_candidates)} last_entry_scan={last_entry_scan_at}",
                )
                logger.info(
                    "GPT refresh done: sectors=%d candidates=%d bridge_hash=%s",
                    len(latest_sector_analysis.get("sectors", [])),
                    len(latest_candidates),
                    last_bridge_hash or "none",
                )

            # 5-min hard SL/TP check REMOVED (2026-05-23): This check was bypassing the
            # DCA (물타기) evaluation in the 15-min scan. When price dropped quickly, the
            # 5-min check would fire SL before the 15-min DCA check could trigger at -4%.
            # Result: 0 DCA entries in 89 trades. Exchange-level SL orders (placed on Bybit
            # via create_stop_loss_order) provide server-side protection between scans.

            time.sleep(config.MAIN_LOOP_SLEEP_SECONDS)
        except Exception as exc:
            logger.exception("loop error: %s", exc)
            telegram.notify_error(str(exc))
            time.sleep(5)


if __name__ == "__main__":
    run()
