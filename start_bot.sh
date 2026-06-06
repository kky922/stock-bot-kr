#!/bin/bash
# ============================================================
# Stock Bot Pro - 통합 시작 스크립트
# 주식봇 + 대시보드 동시 실행
# 사용법: ./start_bot.sh [bot|dashboard|all]
# ============================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/pids"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

mkdir -p "$PID_DIR" "$LOG_DIR"

# 색상
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 모드 확인
MODE=$(grep "^KIS_MODE=" "$SCRIPT_DIR/.env" 2>/dev/null | cut -d'=' -f2 | tr -d ' "' || echo "virtual")
MODE_TEXT="🔵 모의투자" && MODE_COLOR="$BLUE"
if [ "$MODE" = "real" ]; then
    MODE_TEXT="🔴 실전투자" && MODE_COLOR="$RED"
fi

is_running() {
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
    ps -axo pid=,command= | awk '$2 ~ /[Pp]ython/ && $3 ~ /(^|\/)run_agents\.py$/ {print $1}'
}

dashboard_pids() {
    ps -axo pid=,command= | awk '$2 ~ /[Pp]ython/ && $3 ~ /(^|\/)dashboard\.py$/ {print $1}'
}

reconcile_bot_processes() {
    local pids
    pids=$(bot_pids)
    if [ -z "$pids" ]; then
        rm -f "$PID_DIR/stock_bot.pid"
        return 1
    fi

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
            echo -e "${YELLOW}⚠️ 중복 run_agents.py 종료: PID $pid${NC}"
            kill "$pid" 2>/dev/null || true
        fi
    done
    echo "$keep" > "$PID_DIR/stock_bot.pid"
    return 0
}

reconcile_dashboard_processes() {
    local pids
    pids=$(dashboard_pids)
    if [ -z "$pids" ]; then
        rm -f "$PID_DIR/dashboard.pid"
        return 1
    fi

    local keep=""
    if [ -f "$PID_DIR/dashboard.pid" ]; then
        local pid_file_pid=$(cat "$PID_DIR/dashboard.pid" 2>/dev/null || true)
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
            echo -e "${YELLOW}⚠️ 중복 dashboard.py 종료: PID $pid${NC}"
            kill "$pid" 2>/dev/null || true
        fi
    done
    echo "$keep" > "$PID_DIR/dashboard.pid"
    return 0
}

start_bot() {
    echo -e "${GREEN}🚀 주식봇 시작 중... (${MODE_TEXT})${NC}"
    
    if reconcile_bot_processes; then
        echo -e "${YELLOW}⚠️ 이미 실행 중 (PID: $(cat $PID_DIR/stock_bot.pid))${NC}"
        return 0
    fi
    
    cd "$SCRIPT_DIR"
    # 락 파일 정리: 비정상 종료 후 잔존 시 새 인스턴스 차단 방지
    rm -f "$SCRIPT_DIR/data/agents/run_agents.lock"
    nohup "$PYTHON_BIN" run_agents.py >> "$LOG_DIR/stock_bot.log" 2>&1 &
    echo $! > "$PID_DIR/stock_bot.pid"
    
    sleep 2
    if kill -0 $(cat "$PID_DIR/stock_bot.pid") 2>/dev/null; then
        echo -e "${GREEN}✅ 주식봇 시작됨 (PID: $(cat $PID_DIR/stock_bot.pid))${NC}"
    else
        echo -e "${RED}❌ 주식봇 시작 실패! 로그 확인: $LOG_DIR/stock_bot.log${NC}"
        rm -f "$PID_DIR/stock_bot.pid"
        return 1
    fi
}

start_dashboard() {
    echo -e "${GREEN}📊 대시보드 시작 중...${NC}"
    
    if reconcile_dashboard_processes; then
        echo -e "${YELLOW}⚠️ 대시보드 이미 실행 중 (PID: $(cat $PID_DIR/dashboard.pid))${NC}"
        return 0
    fi
    
    cd "$SCRIPT_DIR"
    # 대시보드 로그 로테이션 (100MB 초과 시 이전 로그 삭제)
    if [ -f "$LOG_DIR/dashboard.log" ] && [ $(stat -f%z "$LOG_DIR/dashboard.log" 2>/dev/null || stat -c%s "$LOG_DIR/dashboard.log" 2>/dev/null || echo 0) -gt 104857600 ]; then
        mv "$LOG_DIR/dashboard.log" "$LOG_DIR/dashboard.log.old"
        echo "[$(date)] 대시보드 로그 로테이션" >> "$LOG_DIR/dashboard.log"
    fi
    nohup "$PYTHON_BIN" dashboard.py >> "$LOG_DIR/dashboard.log" 2>&1 &
    echo $! > "$PID_DIR/dashboard.pid"
    
    sleep 2
    if kill -0 $(cat "$PID_DIR/dashboard.pid") 2>/dev/null; then
        echo -e "${GREEN}✅ 대시보드 시작됨 (PID: $(cat $PID_DIR/dashboard.pid))${NC}"
        echo -e "  로컬: ${BLUE}http://localhost:8501${NC}"
        echo -e "  외부: ${BLUE}https://poly.dashmybot-home.com${NC}"
    else
        echo -e "${RED}❌ 대시보드 시작 실패!${NC}"
        rm -f "$PID_DIR/dashboard.pid"
        return 1
    fi
}

stop_bot() {
    echo -e "${YELLOW}🛑 주식봇 정지 중...${NC}"
    for pid in $(bot_pids); do
        kill "$pid" 2>/dev/null || true
    done
    if [ -f "$PID_DIR/stock_bot.pid" ]; then
        local pid=$(cat "$PID_DIR/stock_bot.pid")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 3
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/stock_bot.pid"
        echo -e "${GREEN}✅ 주식봇 정지${NC}"
    else
        echo "실행 중인 주식봇 없음"
    fi
}

stop_dashboard() {
    echo -e "${YELLOW}🛑 대시보드 정지 중...${NC}"
    for pid in $(dashboard_pids); do
        kill "$pid" 2>/dev/null || true
    done
    if [ -f "$PID_DIR/dashboard.pid" ]; then
        local pid=$(cat "$PID_DIR/dashboard.pid")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 2
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/dashboard.pid"
        echo -e "${GREEN}✅ 대시보드 정지${NC}"
    else
        echo "실행 중인 대시보드 없음"
    fi
}

stop_all() {
    stop_bot
    stop_dashboard
}

show_status() {
    echo ""
    echo -e "${MODE_COLOR}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${MODE_COLOR}  Stock Bot Pro - ${MODE_TEXT}${NC}"
    echo -e "${MODE_COLOR}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    
    # 주식봇
    if reconcile_bot_processes; then
        echo -e "  🤖 주식봇: ${GREEN}실행 중${NC} (PID: $(cat $PID_DIR/stock_bot.pid))"
    else
        echo -e "  🤖 주식봇: ${RED}정지${NC}"
    fi
    
    # 대시보드
    if reconcile_dashboard_processes; then
        echo -e "  📊 대시보드: ${GREEN}실행 중${NC} (PID: $(cat $PID_DIR/dashboard.pid))"
    else
        echo -e "  📊 대시보드: ${RED}정지${NC}"
    fi
    
    echo ""
}

# ── 메인 ──

echo ""
case "${1:-all}" in
    bot|주식봇)
        start_bot
        ;;
    dashboard|대시보드|dash)
        start_dashboard
        ;;
    all|전체|"")
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${GREEN}  Stock Bot Pro 전체 시작${NC}"
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        start_dashboard
        start_bot
        show_status
        ;;
    stop|정지)
        stop_all
        ;;
    status|상태)
        show_status
        ;;
    restart|재시작)
        stop_all
        sleep 2
        start_dashboard
        start_bot
        show_status
        ;;
    *)
        echo "사용법: $0 [bot|dashboard|all|stop|status|restart]"
        ;;
esac
