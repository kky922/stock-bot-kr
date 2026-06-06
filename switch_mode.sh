#!/bin/bash
# ============================================================
# Stock Bot Pro - 모의투자/실전 전환 스크립트
# 사용법:
#   ./switch_mode.sh          # 현재 모드 확인
#   ./switch_mode.sh virtual  # 모의투자로 전환
#   ./switch_mode.sh real     # 실전투자로 전환
# ============================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# 색상
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

get_current_mode() {
    grep "^KIS_MODE=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2 | tr -d ' "' || echo "virtual"
}

show_status() {
    local mode=$(get_current_mode)
    if [ "$mode" = "virtual" ]; then
        echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${BLUE}  현재 모드: 🔵 모의투자 (VIRTUAL)${NC}"
        echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    else
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${RED}  현재 모드: 🔴 실전투자 (REAL)${NC}"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    fi
}

switch_to_virtual() {
    echo -e "${BLUE}🔵 모의투자 모드로 전환 중...${NC}"
    
    # .env 백업
    cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    
    # 모드 변경
    if grep -q "^KIS_MODE=" "$ENV_FILE"; then
        sed -i '' 's/^KIS_MODE=.*/KIS_MODE=virtual/' "$ENV_FILE"
    else
        echo "KIS_MODE=virtual" >> "$ENV_FILE"
    fi
    
    # __pycache__ 삭제 (config 리로드 위해)
    rm -rf "$SCRIPT_DIR/__pycache__"
    
    echo -e "${GREEN}✅ 모의투자 모드 전환 완료!${NC}"
    echo ""
    echo "  엔드포인트: https://openapivts.koreainvestment.com:29443"
    echo "  실제 돈이 사용되지 않습니다."
    echo ""
    echo "  실행: cd ~/stock_bot && python3 main.py"
    show_status
}

switch_to_real() {
    echo -e "${RED}⚠️  실전투자 모드로 전환합니다!${NC}"
    echo -e "${RED}⚠️  실제 돈이 거래됩니다!${NC}"
    echo ""
    read -p "정말 실전으로 전환하시겠습니까? (yes/no): " confirm
    
    if [ "$confirm" != "yes" ]; then
        echo "취소되었습니다."
        exit 0
    fi
    
    echo -e "${YELLOW}🔴 실전투자 모드로 전환 중...${NC}"
    
    # .env 백업
    cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    
    # 모드 변경
    if grep -q "^KIS_MODE=" "$ENV_FILE"; then
        sed -i '' 's/^KIS_MODE=.*/KIS_MODE=real/' "$ENV_FILE"
    else
        echo "KIS_MODE=real" >> "$ENV_FILE"
    fi
    
    # __pycache__ 삭제
    rm -rf "$SCRIPT_DIR/__pycache__"
    
    # 리스크 설정 강화 (실전은 더 보수적으로)
    echo ""
    echo -e "${YELLOW}📋 실전 권장 설정:${NC}"
    echo "  STOP_LOSS_PERCENT=3.0 (손절 3%)"
    echo "  TAKE_PROFIT_PERCENT=5.0 (익절 5%)"
    echo "  MAX_DAILY_LOSS_PERCENT=3.0 (일일최대손실 3%)"
    echo "  MAX_CONSECUTIVE_LOSSES=2 (연속손실 2회)"
    echo ""
    echo "  → .env 파일에서 위 설정을 권장합니다."
    
    echo -e "${GREEN}✅ 실전투자 모드 전환 완료!${NC}"
    echo ""
    echo "  엔드포인트: https://openapi.koreainvestment.com:9443"
    echo ""
    echo "  실행: cd ~/stock_bot && python3 main.py"
    show_status
}

# ── 메인 ──

if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}❌ .env 파일을 찾을 수 없습니다: $ENV_FILE${NC}"
    exit 1
fi

case "${1:-}" in
    virtual|v|모의|모의투자)
        switch_to_virtual
        ;;
    real|r|실전|실전투자)
        switch_to_real
        ;;
    status|show|"")
        show_status
        echo ""
        echo "사용법:"
        echo "  ./switch_mode.sh virtual  # 모의투자"
        echo "  ./switch_mode.sh real     # 실전투자"
        ;;
    *)
        echo "알 수 없는 명령: $1"
        echo "사용법: ./switch_mode.sh [virtual|real|status]"
        exit 1
        ;;
esac