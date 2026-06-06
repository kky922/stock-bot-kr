"""
주식봇 시작 전 사전 체크 스크립트.
월요일 모의투자 시작 전 모든 시스템이 정상인지 확인합니다.
"""

import sys
import json
import logging
from pathlib import Path

# 로깅
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("preflight")

def check_env():
    """1. .env 파일 및 필수 설정 확인"""
    print("\n" + "=" * 50)
    print("📋 [1/6] 환경 설정 (.env) 체크")
    print("=" * 50)
    
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        print("  ❌ .env 파일 없음!")
        return False
    
    from dotenv import load_dotenv
    import os
    load_dotenv(env_file)
    
    checks = {
        "KIS_APP_KEY": os.getenv("KIS_APP_KEY", ""),
        "KIS_APP_SECRET": os.getenv("KIS_APP_SECRET", ""),
        "KIS_ACCOUNT_NO": os.getenv("KIS_ACCOUNT_NO", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    }
    
    all_ok = True
    for key, val in checks.items():
        status = "✅" if val else "❌"
        if not val:
            all_ok = False
        print(f"  {status} {key}: {'설정됨' if val else '누락!'}")
    
    mode = os.getenv("KIS_MODE", "virtual")
    mode_text = "🔵 모의투자" if mode == "virtual" else "🔴 실전투자"
    print(f"  ℹ️  KIS_MODE: {mode_text}")
    
    return all_ok


def check_api_connection():
    """2. KIS API 연결 테스트"""
    print("\n" + "=" * 50)
    print("📋 [2/6] KIS API 연결 테스트")
    print("=" * 50)
    
    try:
        import config
        from kis_api import KISAPI
        kis = KISAPI()
        
        print(f"  엔드포인트: {config.KIS_BASE_URL}")
        print(f"  모드: {'모의투자' if config.KIS_MODE == 'virtual' else '실전'}")
        
        if kis.test_connection():
            print("  ✅ API 연결 성공!")
            return True
        else:
            print("  ❌ API 연결 실패!")
            return False
    except Exception as e:
        print(f"  ❌ 오류: {e}")
        return False


def check_balance():
    """3. 계좌 잔고 확인"""
    print("\n" + "=" * 50)
    print("📋 [3/6] 계좌 잔고 확인")
    print("=" * 50)
    
    try:
        from kis_api import KISAPI
        kis = KISAPI()
        
        # 국내 잔고
        balance = kis.get_balance()
        print(f"  🇰🇷 국내 예수금: {balance.get('total_deposit', 0):,.0f}원")
        print(f"  🇰🇷 총평가액: {balance.get('total_eval', 0):,.0f}원")
        print(f"  🇰🇷 총손익: {balance.get('total_pnl', 0):+,.0f}원")
        
        stocks = balance.get("stocks", [])
        if stocks:
            print(f"  📊 보유종목: {len(stocks)}개")
            for s in stocks:
                pnl_emoji = "🟢" if s["pnl"] >= 0 else "🔴"
                print(f"     {pnl_emoji} {s['name']} ({s['code']}) {s['quantity']}주 {s['pnl']:+,.0f}원 ({s['pnl_rate']:+.1f}%)")
        else:
            print("  📊 보유종목: 없음 (청산 상태)")
        
        # 미국 잔고
        if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
            us_balance = kis.get_us_balance()
            print(f"  🇺🇸 미국 총자산: ${us_balance.get('total_usd', 0):,.2f}")
            print(f"  🇺🇸 미국 가용: ${us_balance.get('available_usd', 0):,.2f}")
            
            us_stocks = us_balance.get("stocks", [])
            if us_stocks:
                for s in us_stocks:
                    pnl_emoji = "🟢" if s["pnl"] >= 0 else "🔴"
                    print(f"     {pnl_emoji} {s['name']} ({s['symbol']}) {s['quantity']}주 ${s['pnl']:+,.2f}")
        
        return True
    except Exception as e:
        print(f"  ⚠️ 잔고 조회 오류: {e}")
        return False


def check_risk_state():
    """4. 리스크 상태 확인"""
    print("\n" + "=" * 50)
    print("📋 [4/6] 리스크 관리 상태")
    print("=" * 50)
    
    try:
        from risk_manager import RiskManager
        risk = RiskManager()
        
        can, msg = risk.can_trade()
        print(f"  거래 가능: {'✅' if can else '❌'} {msg}")
        print(f"  킬스위치: {'🔴 ON' if risk.kill_switch else '🟢 OFF'}")
        print(f"  일일 PnL: {risk.daily_pnl:+,.0f}원")
        print(f"  연속손실: {risk.consecutive_losses}회")
        
        import config
        print(f"  손절 설정: -{config.STOP_LOSS_PERCENT}%")
        print(f"  익절 설정: +{config.TAKE_PROFIT_PERCENT}%")
        print(f"  일일최대손실: -{config.MAX_DAILY_LOSS_PERCENT}%")
        print(f"  연속손실한계: {config.MAX_CONSECUTIVE_LOSSES}회")
        
        return can
    except Exception as e:
        print(f"  ⚠️ 오류: {e}")
        return False


def check_order_capability():
    """5. 주문 API 가용성 확인 (EGW00356 서버 차단 감지)"""
    print("\n" + "=" * 50)
    print("📋 [5/7] 주문 API 가용성 테스트")
    print("=" * 50)
    
    try:
        from kis_api import KISAPI
        kis = KISAPI()
        
        result = kis.test_order_capability()
        
        print(f"  모드: {result['mode']} ({result['base_url']})")
        print(f"  연결: {'✅' if result['can_connect'] else '❌'}")
        print(f"  국내 주문: {'✅' if result['can_order_domestic'] else '❌'}")
        print(f"  해외 주문: {'✅' if result['can_order_overseas'] else '❌'}")
        
        if result.get('errors'):
            print("  ⚠️ 에러:")
            for err in result['errors']:
                print(f"     - {err}")
        
        if not result.get('can_trade', False):
            print()
            print("  🚨⚠️ 주문 API가 차단되어 있습니다!")
            print("  해결 방법:")
            print("    1. KIS 모의투자 고객센터 문의 (1588-6611)")
            print("    2. 실전 모드로 전환: ./switch_mode.sh real")
            print("       (실전 APP_KEY/APP_SECRET 필요)")
            print()
            return False
        
        print("  ✅ 주문 API 정상 — 거래 가능")
        return True
    except Exception as e:
        print(f"  ❌ 오류: {e}")
        return False


def check_news_scanner():
    """6. 뉴스 스캐너 동작 확인"""
    print("\n" + "=" * 50)
    print("📋 [6/7] 뉴스 스캐너 테스트")
    print("=" * 50)
    
    try:
        from news_scanner import NewsScanner
        scanner = NewsScanner()
        
        issues = scanner.scan()
        if issues:
            print(f"  ✅ 이슈 {len(issues)}개 탐지됨")
            for i, issue in enumerate(issues[:3], 1):
                print(f"     {i}. [{issue.get('score', 0)}/10] {issue.get('title', 'N/A')[:50]}")
        else:
            print("  ℹ️ 현재 탐지된 이슈 없음 (정상)")
        
        return True
    except Exception as e:
        print(f"  ⚠️ 오류: {e}")
        return False


def check_directories():
    """7. 필수 디렉토리/파일 확인"""
    print("\n" + "=" * 50)
    print("📋 [7/7] 데이터 디렉토리/파일")
    print("=" * 50)
    
    import config
    
    dirs = [config.DATA_DIR, config.LOGS_DIR]
    for d in dirs:
        exists = d.exists()
        print(f"  {'✅' if exists else '❌'} {d}")
        if not exists:
            d.mkdir(exist_ok=True)
            print(f"     → 생성함")
    
    files = [
        config.THEME_DB_FILE,
        config.DATA_DIR / "risk_state.json",
        config.DATA_DIR / "agents" / "state.json",
    ]
    
    for f in files:
        exists = f.exists()
        print(f"  {'✅' if exists else '⚠️'} {f}")
    
    return True


def run_preflight():
    """전체 사전 체크 실행."""
    print("🚀 Stock Bot Pro - 시스템 사전 체크")
    print(f"📅 실행 시간: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {
        "환경설정": check_env(),
        "API연결": check_api_connection(),
        "계좌잔고": check_balance(),
        "리스크상태": check_risk_state(),
        "주문API": check_order_capability(),
        "뉴스스캐너": check_news_scanner(),
        "데이터파일": check_directories(),
    }
    
    # 결과 요약
    print("\n" + "=" * 50)
    print("📊 사전 체크 결과")
    print("=" * 50)
    
    all_pass = True
    for name, passed in results.items():
        status = "✅ 통과" if passed else "❌ 실패"
        print(f"  {status} - {name}")
        if not passed:
            all_pass = False
    
    print()
    # 7. 새 모듈 import 체크
    print("\n" + "=" * 50)
    print("📋 [추가] v2.0 모듈 import 체크")
    print("=" * 50)

    new_modules = [
        ("core.data_store", "DataStore (thread-safe)"),
        ("core.news_archive", "NewsArchive"),
        ("agents.entry_analyzer", "EntryAnalyzer"),
        ("agents.theme_accumulator", "ThemeAccumulator"),
        ("agents.risk_manager_agent", "RiskManagerAgent"),
    ]
    mod_ok = True
    for mod_name, display in new_modules:
        try:
            __import__(mod_name)
            print(f"  ✅ {display}")
        except Exception as e:
            print(f"  ⚠️ {display}: {e}")
            mod_ok = False

    if not mod_ok:
        results["v2모듈"] = False

    if all_pass:
        print("\n🎉 모든 체크 통과! 주식봇 시작 준비 완료!")
        print("\n실행 명령:")
        print("  cd ~/stock_bot && bash start_bot.sh")
        print("  또는")
        print("  cd ~/stock_bot && python3 run_agents.py")
        return 0
    else:
        print("⚠️ 일부 체크 실패. 위 항목을 확인해주세요.")
        return 1


if __name__ == "__main__":
    sys.exit(run_preflight())