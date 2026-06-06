#!/bin/bash
# ============================================================
# Stock Bot Pro - Watchdog (프로세스 자동 복구)
# crontab으로 5분마다 실행 권장:
#   */5 * * * * $HOME/stock_bot/watchdog_stock.sh >> $HOME/stock_bot/logs/watchdog.log 2>&1
# ============================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/pids"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$PID_DIR" "$LOG_DIR"

WATCHDOG_LOG="$LOG_DIR/watchdog.log"
RESTART_COUNT_FILE="$PID_DIR/watchdog_restarts"

# 최대 일일 재시작 횟수 (이 이상이면 알림만)
MAX_DAILY_RESTARTS=10

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

is_process_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

bot_pids() {
    ps -axo pid=,command= | awk '/[Pp]ython.*run_agents\.py/ {print $1}'
}

reconcile_bot_processes() {
    local pids
    pids=$(bot_pids)
    if [ -z "$pids" ]; then
        rm -f "$PID_DIR/stock_bot.pid"
        return 1
    fi

    local pid_count
    pid_count=$(echo "$pids" | wc -l | tr -d ' ')

    # 딱 1개만 실행 중이면 PID 파일만 갱신 (죽이지 않음)
    # 과거 버그: PID 파일이 낡았을 때 유일한 프로세스를 '중복'으로 오인해 kill
    # → 봇 사망 → lock 파일 잔존 → 재시작 실패 → 10회 재시도 후 watchdog 포기
    # Fix (2026-05-18): 유일 프로세스는 항상 보존
    if [ "$pid_count" -eq 1 ]; then
        local pid=$(echo "$pids" | tr -d ' ')
        echo "$pid" > "$PID_DIR/stock_bot.pid"
        return 0
    fi

    # 여러 개 실행 중일 때만 중복 정리
    local keep=""
    if [ -f "$PID_DIR/stock_bot.pid" ]; then
        local pid_file_pid=$(cat "$PID_DIR/stock_bot.pid" 2>/dev/null || true)
        for pid in $pids; do
            if [ "$pid" = "$pid_file_pid" ]; then
                keep="$pid"
                break
            fi
        done
    fi
    if [ -z "$keep" ]; then
        keep=$(echo "$pids" | head -n 1)
    fi

    for pid in $pids; do
        if [ "$pid" != "$keep" ]; then
            log "⚠️ 중복 run_agents.py 종료: PID $pid"
            kill "$pid" 2>/dev/null || true
        fi
    done
    echo "$keep" > "$PID_DIR/stock_bot.pid"
    return 0
}

get_restart_count() {
    local today=$(date +%Y%m%d)
    local stored_date=$(cut -d: -f1 "$RESTART_COUNT_FILE" 2>/dev/null || echo "0")
    local stored_count=$(cut -d: -f2 "$RESTART_COUNT_FILE" 2>/dev/null || echo "0")
    
    if [ "$stored_date" = "$today" ]; then
        echo "$stored_count"
    else
        echo "0"
    fi
}

increment_restart_count() {
    local today=$(date +%Y%m%d)
    local count=$(get_restart_count)
    count=$((count + 1))
    echo "${today}:${count}" > "$RESTART_COUNT_FILE"
}

restart_bot() {
    log "🔄 주식봇 재시작 중..."
    # 락 파일 정리 (중요): 비정상 종료 시 run_agents.lock이 남아 새 인스턴스 차단
    # failure mode #27: 락 파일 잔존 → 재시작 실패 → 10회 재시도 후 포기
    rm -f "$SCRIPT_DIR/data/agents/run_agents.lock"

    cd "$SCRIPT_DIR"
    
    # 기존 프로세스 정리
    for pid in $(bot_pids); do
        kill -9 "$pid" 2>/dev/null || true
    done
    if [ -f "$PID_DIR/stock_bot.pid" ]; then
        local pid=$(cat "$PID_DIR/stock_bot.pid" 2>/dev/null)
        if [ -n "$pid" ]; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/stock_bot.pid"
    fi
    
    # 재시작
    nohup python3 run_agents.py >> "$LOG_DIR/stock_bot.log" 2>&1 &  # [Claude Fix] main.py → run_agents.py (v2 엔트리포인트)
    echo $! > "$PID_DIR/stock_bot.pid"
    
    sleep 3
    if is_process_running "$PID_DIR/stock_bot.pid"; then
        log "✅ 주식봇 재시작 성공 (PID: $(cat $PID_DIR/stock_bot.pid))"
        increment_restart_count
        return 0
    else
        log "❌ 주식봇 재시작 실패!"
        return 1
    fi
}

restart_dashboard() {
    log "🔄 대시보드 재시작 중..."
    cd "$SCRIPT_DIR"
    
    # 기존 프로세스 정리
    if [ -f "$PID_DIR/dashboard.pid" ]; then
        local pid=$(cat "$PID_DIR/dashboard.pid" 2>/dev/null)
        if [ -n "$pid" ]; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/dashboard.pid"
    fi
    
    # 8501 포트가 사용 중이면 정리
    local port_pid=$(lsof -ti :8501 2>/dev/null || true)
    if [ -n "$port_pid" ]; then
        kill -9 $port_pid 2>/dev/null || true
    fi
    
    nohup python3 dashboard.py >> "$LOG_DIR/dashboard.log" 2>&1 &
    echo $! > "$PID_DIR/dashboard.pid"
    
    sleep 2
    if is_process_running "$PID_DIR/dashboard.pid"; then
        log "✅ 대시보드 재시작 성공 (PID: $(cat $PID_DIR/dashboard.pid))"
        return 0
    else
        log "❌ 대시보드 재시작 실패!"
        return 1
    fi
}

# ── 메인 체크 ──

log "--- Watchdog 체크 시작 ---"

restarts=$(get_restart_count)

# 1. 주식봇 체크
if reconcile_bot_processes; then
    log "🤖 주식봇: 정상 실행 중"
else
    log "⚠️ 주식봇 다운 감지!"
    if [ "$restarts" -lt "$MAX_DAILY_RESTARTS" ]; then
        restart_bot
    else
        log "🚨 일일 재시작 한계 초과 ($restarts/$MAX_DAILY_RESTARTS) — 수동 확인 필요"
    fi
fi

# 2. 대시보드 체크
if is_process_running "$PID_DIR/dashboard.pid"; then
    log "📊 대시보드: 정상 실행 중"
else
    log "⚠️ 대시보드 다운 감지!"
    if [ "$restarts" -lt "$MAX_DAILY_RESTARTS" ]; then
        restart_dashboard
    else
        log "🚨 일일 재시작 한계 초과 — 수동 확인 필요"
    fi
fi

log "--- Watchdog 체크 완료 ---"
