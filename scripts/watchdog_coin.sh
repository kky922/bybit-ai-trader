#!/usr/bin/env bash
set -euo pipefail

ROOT="${COIN_BOT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
mkdir -p logs pids

PID_FILE="logs/coin_bot.pid"
LEGACY_PID_FILE="pids/coin_bot.pid"
WATCHDOG_LOG="logs/watchdog_coin.log"
RESTART_COUNT_FILE="pids/watchdog_coin_restarts"
MAX_DAILY_RESTARTS=10

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$WATCHDOG_LOG"
}

is_pid_alive() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

bot_pids() {
  # pgrep -f "main\\.py" matches tradfi_main.py because "main.py" is a substring.
  # Use a word-boundary-like anchor: match "main.py" only when preceded by
  # non-alphanum (slash, space, etc.) or start-of-command.
  # Additionally, explicitly skip any process whose command-line contains "tradfi".
  for pid in $(pgrep -f "[Pp]ython.*main\\.py" 2>/dev/null || true); do
    local cmdline
    cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    # Skip tradfi_main.py explicitly
    if echo "$cmdline" | grep -q "tradfi_main"; then
      continue
    fi
    if lsof -a -p "$pid" -d cwd 2>/dev/null | grep -F "$ROOT" >/dev/null; then
      echo "$pid"
    fi
  done
}

reconcile_bot_processes() {
  local pids
  pids="$(bot_pids || true)"
  if [[ -z "$pids" ]]; then
    rm -f "$PID_FILE" "$LEGACY_PID_FILE"
    return 1
  fi

  local keep=""
  if [[ -f "$PID_FILE" ]]; then
    local recorded
    recorded="$(cat "$PID_FILE" 2>/dev/null || true)"
    for pid in $pids; do
      if [[ "$pid" = "$recorded" ]]; then
        keep="$pid"
        break
      fi
    done
  fi
  if [[ -z "$keep" ]]; then
    keep="$(echo "$pids" | head -n 1)"
  fi

  for pid in $pids; do
    if [[ "$pid" != "$keep" ]]; then
      log "⚠️ 중복 main.py 종료: PID $pid"
      kill "$pid" 2>/dev/null || true
    fi
  done
  echo "$keep" > "$PID_FILE"
  rm -f "$LEGACY_PID_FILE"
  return 0
}

get_restart_count() {
  local today stored_date stored_count
  today="$(date +%Y%m%d)"
  stored_date="$(cut -d: -f1 "$RESTART_COUNT_FILE" 2>/dev/null || echo 0)"
  stored_count="$(cut -d: -f2 "$RESTART_COUNT_FILE" 2>/dev/null || echo 0)"
  if [[ "$stored_date" = "$today" ]]; then
    echo "$stored_count"
  else
    echo 0
  fi
}

increment_restart_count() {
  local today count
  today="$(date +%Y%m%d)"
  count="$(get_restart_count)"
  count=$((count + 1))
  echo "${today}:${count}" > "$RESTART_COUNT_FILE"
}

restart_bot() {
  log "🔄 코인봇 재시작 중..."
  bash scripts/start_coin_bot.sh >> "$WATCHDOG_LOG" 2>&1 || true
  sleep 3
  if reconcile_bot_processes || is_pid_alive "$PID_FILE"; then
    log "✅ 코인봇 재시작 성공 (PID: $(cat "$PID_FILE" 2>/dev/null || echo '?'))"
    increment_restart_count
    return 0
  fi
  log "❌ 코인봇 재시작 실패"
  return 1
}

log "--- Watchdog 체크 시작 ---"
if reconcile_bot_processes; then
  log "🤖 코인봇: 정상 실행 중 (PID: $(cat "$PID_FILE" 2>/dev/null || echo '?'))"
else
  log "⚠️ 코인봇 다운 감지"
  restarts="$(get_restart_count)"
  if [[ "$restarts" -lt "$MAX_DAILY_RESTARTS" ]]; then
    restart_bot || true
  else
    log "🚨 일일 재시작 한계 초과 ($restarts/$MAX_DAILY_RESTARTS) — 수동 확인 필요"
  fi
fi
log "--- Watchdog 체크 완료 ---"
