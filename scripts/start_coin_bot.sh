#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs pids
PID_FILE="logs/coin_bot.pid"
LEGACY_PID_FILE="pids/coin_bot.pid"
LAUNCH_LABEL="com.coinbot.app"

stop_pid() {
    local pid="$1"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "stopping existing coin bot (pid=$pid)..."
        kill "$pid" 2>/dev/null || true
        sleep 2
    fi
}

# 기존 PID 파일과 예전 PID 파일을 모두 확인한다.
for pid_file in "$PID_FILE" "$LEGACY_PID_FILE"; do
    if [ -f "$pid_file" ]; then
        stop_pid "$(cat "$pid_file" 2>/dev/null || true)"
    fi
done

# launchd로 띄운 기존 작업도 제거한다.
launchctl remove "$LAUNCH_LABEL" 2>/dev/null || true

# PID 파일이 어긋난 중복 main.py도 작업 디렉터리 기준으로 정리한다.
for pid in $(pgrep -f "main.py" 2>/dev/null || true); do
    if lsof -a -p "$pid" -d cwd 2>/dev/null | grep -F "$ROOT" >/dev/null; then
        stop_pid "$pid"
    fi
done

# 시작 전 유니버스 캐시 삭제 (최신 마켓 데이터 강제 로드)
rm -f logs/universe_cache.json
rm -f "$PID_FILE"

launchctl submit -l "$LAUNCH_LABEL" -- /bin/bash -lc "cd \"$ROOT\" && exec python3 main.py"

for _ in {1..20}; do
    if [ -s "$PID_FILE" ]; then
        BOT_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
            break
        fi
    fi
    sleep 0.5
done

rm -f "$LEGACY_PID_FILE"
echo "coin bot started pid=$(cat "$PID_FILE")"
