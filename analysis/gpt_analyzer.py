from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

import config
from data.news_collector import NewsItem
from infra.state import append_news_snapshot, load_latest_ai, load_risk_state, save_latest_ai

logger = logging.getLogger("gpt_analyzer")


class GPTAnalyzer:
    def __init__(self, tradable_symbols: list[str]):
        self.tradable_symbols = set(tradable_symbols)
        cached = load_latest_ai()
        self.last_sectors: dict[str, Any] = cached.get("sectors", {"sectors": []})
        self.last_candidates: list[dict[str, Any]] = cached.get("candidates", [])
        self.client = None
        api_key = config.DEEPSEEK_API_KEY or config.GEMINI_API_KEY
        base_url = getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key, base_url=base_url)
                logger.info("DeepSeek client initialized (model=%s)", config.GPT_MODEL)
            except Exception as exc:
                logger.warning("DeepSeek client init failed: %s", exc)
        else:
            logger.warning("LLM client not initialized (no API key)")

    def _parse_json(self, text: str) -> dict[str, Any]:
        cleaned = re.sub(r"```json|```", "", text).strip()
        return json.loads(cleaned)

    def _parse_iso_dt(self, value: Any) -> datetime | None:
        try:
            if not value:
                return None
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _chat_json(self, system: str, user: str) -> dict[str, Any]:
        if not self.client:
            logger.warning("LLM client not initialized (no API key)")
            return {}
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=config.GPT_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                return self._parse_json(resp.choices[0].message.content or "{}")
            except Exception as exc:
                logger.warning("DeepSeek call failed (attempt %d/%d): %s", attempt, max_retries, exc)
                if attempt < max_retries:
                    backoff = 5 * attempt  # 5s, 10s, 15s
                    logger.info("retrying in %ds...", backoff)
                    time.sleep(backoff)
                continue
        logger.error("DeepSeek call failed after %d attempts, returning empty", max_retries)
        return {}

    def _recent_repeat_loser_symbols(self) -> set[str]:
        path = config.DATA_DIR / "agents" / "autopilot_latest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return set()

        symbols: set[str] = set()
        evidence = data.get("evidence", {}) if isinstance(data, dict) else {}
        repeat_loser_symbols = evidence.get("repeat_loser_symbols", []) if isinstance(evidence, dict) else []
        if isinstance(repeat_loser_symbols, list):
            symbols.update(str(sym).upper().strip() for sym in repeat_loser_symbols if str(sym).strip())

        metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
        repeat_losers = metrics.get("repeat_losers", []) if isinstance(metrics, dict) else []
        if isinstance(repeat_losers, list):
            for row in repeat_losers:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol", "")).upper().strip()
                if sym:
                    symbols.add(sym)

        return symbols

    def _historical_winner_symbols(self) -> list[str]:
        """Rank tradable symbols by realized PnL in the bot's own history."""
        path = config.ROOT_DIR / "logs" / "pnl_history.json"
        try:
            trades = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            return []

        excluded = self._recent_repeat_loser_symbols()
        stats: dict[str, dict[str, float | int]] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            sym = str(trade.get("symbol", "")).upper().strip()
            if not sym or sym not in self.tradable_symbols or sym in excluded:
                continue
            try:
                pnl = float(trade.get("pnl", 0) or 0)
            except Exception:
                pnl = 0.0
            row = stats[sym]
            row["count"] = int(row["count"]) + 1
            row["pnl"] = float(row["pnl"]) + pnl
            if pnl > 0:
                row["wins"] = int(row["wins"]) + 1

        ranked = sorted(
            stats.items(),
            key=lambda kv: (float(kv[1]["pnl"]), int(kv[1]["wins"]), int(kv[1]["count"])),
            reverse=True,
        )
        return [sym for sym, row in ranked if float(row["pnl"]) > 0][:8]

    def _closed_loop_profile(self) -> dict[str, Any]:
        path = config.ROOT_DIR / "logs" / "pnl_history.json"
        try:
            trades = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            trades = []

        stats: dict[str, dict[str, float | int]] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            sym = str(trade.get("symbol", "")).upper().strip()
            if not sym or sym not in self.tradable_symbols:
                continue
            try:
                pnl = float(trade.get("pnl", 0) or 0)
            except Exception:
                pnl = 0.0
            row = stats[sym]
            row["count"] = int(row["count"]) + 1
            row["pnl"] = float(row["pnl"]) + pnl
            if pnl > 0:
                row["wins"] = int(row["wins"]) + 1
            elif pnl < 0:
                row["losses"] = int(row["losses"]) + 1

        repeat_losers: set[str] = {
            sym for sym, row in stats.items()
            if int(row["losses"]) >= 2 and float(row["pnl"]) <= 0
        }
        winners = [
            sym for sym, row in sorted(
                stats.items(),
                key=lambda kv: (float(kv[1]["pnl"]), int(kv[1]["wins"]), int(kv[1]["count"])),
                reverse=True,
            )
            if float(row["pnl"]) > 0
        ][:8]

        risk_state = load_risk_state("spot")
        now = datetime.now(timezone.utc)
        cooldowns = {
            str(sym).upper().strip()
            for sym, iso in (risk_state.get("symbol_cooldowns", {}) or {}).items()
            if str(sym).strip() and iso
            and (dt := self._parse_iso_dt(iso)) is not None
            and now < dt
        }

        return {
            "stats": stats,
            "repeat_losers": repeat_losers,
            "cooldowns": cooldowns,
            "winners": winners,
        }

    # Symbols permanently excluded from fallback candidates (repeat losers).
    PERMANENT_EXCLUDED: set[str] = {"TONUSDT", "DOGEUSDT", "FFUSDT", "ADAUSDT", "XAUTUSDT"}

    # Symbols excluded when dry-run equity is too small to trade them.
    # BTCUSDT at $81K+ requires >0.001 BTC (~$81+) per position; with $1000 equity
    # and 30% max per position, only $300 is available, and lot_step rounding can
    # produce size=0 for high-price symbols.
    _DRY_RUN_SMALL_EQUITY_EXCLUDED: set[str] = {"BTCUSDT", "BNBUSDT"}

    def _fallback_candidates(self, sectors: dict[str, Any]) -> list[dict[str, Any]]:
        sector_names = [str(s.get("name", "")).strip() for s in sectors.get("sectors", [])]
        historical = self._historical_winner_symbols()
        preferred = [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "XRPUSDT",
            "DOGEUSDT",
            "LINKUSDT",
            "ADAUSDT",
            "AVAXUSDT",
            "SUIUSDT",
            "INJUSDT",
            "FETUSDT",
            "RNDRUSDT",
            "NEARUSDT",
            "TIAUSDT",
            "ARBUSDT",
            "OPUSDT",
        ]
        excluded = self._recent_repeat_loser_symbols() | self.PERMANENT_EXCLUDED
        if config.DRY_RUN and config.DRY_RUN_EQUITY_USDT < 2000:
            excluded |= self._DRY_RUN_SMALL_EQUITY_EXCLUDED
        symbols: list[str] = []
        for sym in historical + preferred:
            if sym in self.tradable_symbols and sym not in excluded and sym not in symbols:
                symbols.append(sym)
        if len(symbols) < 10:
            for sym in sorted(self.tradable_symbols):
                if sym not in symbols and sym not in excluded:
                    symbols.append(sym)
                if len(symbols) >= 15:
                    break

        if not symbols:
            symbols = [sym for sym in preferred if sym in self.tradable_symbols]
            if len(symbols) < 10:
                for sym in sorted(self.tradable_symbols):
                    if sym not in symbols:
                        symbols.append(sym)
                    if len(symbols) >= 15:
                        break

        primary_sector = sector_names[0] if sector_names else "fallback"
        reason = "fallback candidates (gemini unavailable or empty)"
        if excluded:
            reason += f"; excluded_repeat_losers={len(excluded)}"
        return [
            {
                "symbol": sym,
                "sector": primary_sector,
                "conviction": 1,
                "reason": reason,
            }
            for sym in symbols[:15]
        ]

    def detect_sectors(self, news: list[NewsItem]) -> dict[str, Any]:
        lines = "\n".join(f"- {x.title}: {x.body[:120]}" for x in news[:30])
        system = "당신은 크립토 마켓 내러티브 분석가다."
        user = (
            "아래 지난 6시간 뉴스 기반으로 지금 가장 핫한 섹터 3개를 JSON으로 뽑아라.\n"
            '반드시 {"sectors":[{"name":"AI","heat_score":1,"persistence":"short","narrative":"","key_catalysts":[]}]}\n'
            f"{lines}"
        )
        data = self._chat_json(system, user)
        sectors = data.get("sectors", []) if isinstance(data, dict) else []
        for x in sectors:
            x["heat_score"] = max(1, min(10, int(x.get("heat_score", 1))))
        result = {"sectors": sectors[:3]}
        if result["sectors"]:
            self.last_sectors = result
        else:
            logger.warning("LLM returned no sectors, keeping last sectors")
            result = self.last_sectors
        append_news_snapshot({"ts": datetime.now(timezone.utc).isoformat(), "type": "sectors", "data": result})
        save_latest_ai({"sectors": result, "candidates": self.last_candidates})
        return result

    def pick_coins(self, sectors: dict[str, Any]) -> list[dict[str, Any]]:
        names = [s.get("name", "") for s in sectors.get("sectors", [])][:3]
        tradable_list = sorted(self.tradable_symbols)
        symbol_list = "\n".join(f"  - {s}" for s in tradable_list)
        system = "당신은 Bybit 현물 시장 분석가다. 아래 거래 가능한 USDT 페어 목록에서만 코인을 골라야 한다."
        user = (
            f"현재 핫한 섹터: {names}\n\n"
            f"아래 Bybit USDT 페어 중에서 위 섹터에 해당하는 코인을 골라라.\n"
            f"반드시 아래 목록에 있는 심볼만 사용하고, 목록에 없는 심볼은 절대 출력하지 마라.\n\n"
            f"=== 거래 가능 USDT 페어 목록 ===\n{symbol_list}\n\n"
            f"규칙:\n"
            f"- 섹터당 최소 3개씩, 총 8~12개를 반드시 채워라\n"
            f"- 모르는 섹터면 목록에서 비슷한 성격의 코인을 골라서라도 채워라\n"
            f"- {names} 외 섹터도 시장 상황에 따라 1개까지 추가 가능\n"
            f'- 반드시 아래 JSON 형식만 출력하라. 절대 다른 텍스트를 출력하지 마라.\n'
            f'{{"candidates":[{{"symbol":"FETUSDT","sector":"AI","conviction":1,"reason":"AI 에이전트 생태계 선두"}}]}}\n'
            f"conviction: 1~10 정수, reason: 간단한 한국어 한 문장"
        )
        data = self._chat_json(system, user)
        cands = data.get("candidates", []) if isinstance(data, dict) else []
        filtered = []
        # In dry-run mode with small equity, exclude symbols that will always
        # fail at trader.enter() with size=0 (e.g. high-price symbols like
        # BTCUSDT, BNBUSDT). These can waste hourly entry scan slots with
        # false-positive entry signals that always get rejected at sizing.
        dry_run_excluded: set[str] = set()
        if config.DRY_RUN and config.DRY_RUN_EQUITY_USDT < 2000:
            dry_run_excluded = self._DRY_RUN_SMALL_EQUITY_EXCLUDED
        # LLM 경로에서도 런타임 반복 손실 심볼을 필터링 (PERMANENT_EXCLUDED 외 추가 보호)
        runtime_excluded = self._recent_repeat_loser_symbols()
        for c in cands:
            symbol = str(c.get("symbol", "")).upper().replace("/", "")
            if symbol not in self.tradable_symbols:
                continue
            if symbol in self.PERMANENT_EXCLUDED:
                continue
            if symbol in dry_run_excluded:
                continue
            if symbol in runtime_excluded:
                continue
            c["symbol"] = symbol
            c["conviction"] = max(1, min(10, int(c.get("conviction", 1))))
            filtered.append(c)
        # LLM가 비거나 실패했을 때는 generic fallback보다 직전 검증 후보가 더 낫다.
        # 단, 현재 거래 가능 유니버스에 아직 존재하는 후보만 유지한다.
        if not filtered and self.last_candidates:
            runtime_excluded_retained = self._recent_repeat_loser_symbols()
            retained = [
                {**c, "symbol": str(c.get("symbol", "")).upper().replace("/", "")}
                for c in self.last_candidates
                if str(c.get("symbol", "")).upper().replace("/", "") in self.tradable_symbols
                and str(c.get("symbol", "")).upper().replace("/", "") not in self.PERMANENT_EXCLUDED
                and str(c.get("symbol", "")).upper().replace("/", "") not in runtime_excluded_retained
            ]
            if retained:
                logger.warning("DeepSeek returned no usable candidates, keeping %d previous candidates", len(retained))
                filtered = retained

        # LLM 반환 결과가 5개 미만이면 fallback으로 보충 (503 장기화 / 소수 반환 대비)
        if len(filtered) < 5:
            fallback = self._fallback_candidates(sectors)
            existing_syms = {c["symbol"] for c in filtered}
            for fb in fallback:
                if fb["symbol"] not in existing_syms:
                    filtered.append(fb)
                if len(filtered) >= 10:
                    break
            if len(filtered) >= 5:
                logger.info("candidates boosted with fallback: total=%d", len(filtered))
            elif not filtered:
                logger.warning("Gemini returned no usable candidates, using %d fallback candidates", len(fallback))
                filtered = fallback

        profile = self._closed_loop_profile()
        stats = profile["stats"]
        repeat_losers = profile["repeat_losers"]
        cooldowns = profile["cooldowns"]
        for c in filtered:
            symbol = str(c.get("symbol", "")).upper().replace("/", "")
            row = stats.get(symbol, {})
            raw_count = int(row.get("count", 0) or 0)
            count = max(1, raw_count)
            pnl = float(row.get("pnl", 0.0) or 0.0)
            wins = int(row.get("wins", 0) or 0)
            losses = int(row.get("losses", 0) or 0)
            conviction = int(c.get("conviction", 0) or 0)
            history_boost = max(-4.0, min(4.0, (pnl / count) * 2.0))
            win_boost = min(2.0, wins * 0.5)
            penalty = 0.0
            if symbol in repeat_losers:
                penalty += 5.0
            if symbol in cooldowns:
                penalty += 3.0
            if losses >= 3:
                penalty += 1.5
            c["closed_loop_score"] = round(conviction * 10.0 + history_boost + win_boost - penalty, 3)
            c["closed_loop_bias"] = {
                "history_pnl": round(pnl, 4),
                "history_count": raw_count,
                "wins": wins,
                "losses": losses,
                "repeat_loser": symbol in repeat_losers,
                "cooldown": symbol in cooldowns,
            }

        filtered.sort(
            key=lambda c: (
                float(c.get("closed_loop_score", 0) or 0),
                int(c.get("conviction", 0) or 0),
                str(c.get("symbol", "")),
            ),
            reverse=True,
        )
        append_news_snapshot(
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "candidates", "data": filtered}
        )
        result = filtered[:15]
        self.last_candidates = result
        save_latest_ai(
            {
                "sectors": self.last_sectors,
                "candidates": result,
                "feedback": {
                    "repeat_loser_symbols": sorted(profile["repeat_losers"]),
                    "cooldown_symbols": sorted(profile["cooldowns"]),
                    "winner_symbols": profile["winners"],
                },
            }
        )
        return result

