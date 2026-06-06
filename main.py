"""
⚠️ DEPRECATED — 이 파일은 레거시(v1) 진입점입니다.
공식 진입점은 run_agents.py (멀티 에이전트 v2) 를 사용하세요.
  시작: ./start_bot.sh 또는 python3 run_agents.py

주식 자동매매봇 메인 실행 모듈 (v1 — 스캐너/트레이더/스케줄 중심).
이슈 스캔 → 종목 매칭 → 매매 실행 → 포지션 모니터링 루프.
"""

import warnings
warnings.warn(
    "main.py is deprecated. Use 'python3 run_agents.py' instead.",
    DeprecationWarning,
    stacklevel=2,
)

# [Claude Fix] v1 레거시 격리 — 실행 즉시 차단. run_agents.py 사용 필수.
raise SystemExit(
    "\n❌ main.py (v1 레거시)는 비활성화됐습니다.\n"
    "   실행: python3 run_agents.py\n"
    "   시작: bash start_bot.sh\n"
)

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

import schedule

import config
from kis_api import KISAPI
from news_scanner import NewsScanner
from stock_matcher import StockMatcher
from trader import Trader
from risk_manager import RiskManager

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOGS_DIR / "stock_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("stock_bot")


class StockBot:
    """주식 자동매매봇 메인 클래스."""

    def __init__(self):
        self.kis = KISAPI()
        self.scanner = NewsScanner()
        self.risk = RiskManager()
        self.trader = Trader(self.kis, self.risk)
        self.matcher = StockMatcher(self.kis)
        self.telegram_app = None
        self.running = False

    async def init_telegram(self):
        """텔레그램 봇 초기화."""
        if not config.TELEGRAM_BOT_TOKEN:
            logger.warning("⚠️ TELEGRAM_BOT_TOKEN 없음 — 텔레그램 비활성화")
            return

        from telegram.ext import ApplicationBuilder
        from telegram_bot import register_commands

        self.telegram_app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
        register_commands(self.telegram_app, stock_bot=self)
        await self.telegram_app.initialize()
        await self.telegram_app.start()
        await self.telegram_app.updater.start_polling()
        logger.info("🤖 텔레그램 봇 시작")

    async def send_telegram(self, text: str):
        """텔레그램 메시지 전송."""
        if not self.telegram_app or not config.TELEGRAM_CHAT_ID:
            return
        try:
            await self.telegram_app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID, text=text
            )
        except Exception as e:
            logger.error("❌ 텔레그램 전송 실패: %s", e)

    # ── 핵심 로직 ─────────────────────────────────────────

    async def run_scan_cycle(self):
        """이슈 스캔 → 종목 매칭 → 자동 진입 사이클."""
        logger.info("🔄 스캔 사이클 시작...")

        # 1. 이슈 스캔
        issues = self.scanner.scan()
        if not issues:
            logger.info("📰 이슈 없음 — 대기")
            return

        # 상위 이슈 텔레그램 알림
        top = issues[0]
        await self.send_telegram(
            f"📰 탐지 이슈: {top.get('title', 'N/A')}\n"
            f"점수: {top.get('score', 0)}/10 | {top.get('impact', '')}\n"
            f"섹터: {', '.join(top.get('sectors', []))}"
        )

        # 2. 이미 포지션 있으면 스킵
        if self.trader.has_position():
            logger.info("📋 이미 포지션 보유 중 — 진입 스킵")
            return

        # 3. 종목 매칭 (점수 7 이상 이슈만)
        for issue in issues:
            if issue.get("score", 0) < 7:
                continue

            best = self.matcher.find_best_stock(issue)
            if best:
                logger.info("🎯 최적 종목 발견: %s (%s)", best["name"], best["code"])
                result = self.trader.enter(
                    best["code"],
                    best["name"],
                    reason=f"이슈:{issue.get('title', '')[:30]} 테마:{best.get('theme', '')}",
                )
                if result.get("success"):
                    await self.send_telegram(
                        f"📈 자동 진입!\n"
                        f"📊 {result['stock']} {result['quantity']}주\n"
                        f"💰 @ {result['price']:,}원\n"
                        f"📝 {result['reason']}"
                    )
                else:
                    await self.send_telegram(f"⚠️ 진입 실패: {result.get('message')}")
                return

        logger.info("📊 적합한 진입 종목 없음")

    async def run_monitor_cycle(self):
        """포지션 모니터링 (손절/익절)."""
        if not self.trader.has_position():
            return

        result = self.trader.monitor_position()
        if result:
            await self.send_telegram(
                f"{'🎯' if '익절' in result.get('reason', '') else '🚨'} 자동 청산!\n"
                f"📊 {result['stock']} {result['quantity']}주\n"
                f"💰 {result['entry_price']:,} → {result['sell_price']:,}\n"
                f"📊 PnL: {result['pnl']:+,.0f}원\n"
                f"📝 {result['reason']}"
            )

    # ── 메인 루프 ─────────────────────────────────────────

    async def run(self):
        """봇 메인 루프."""
        logger.info("=" * 50)
        logger.info("🚀 주식 자동매매봇 시작")
        logger.info(config.get_config_summary())
        logger.info("=" * 50)

        # API 연결 테스트
        if not self.kis.test_connection():
            logger.error("❌ KIS API 연결 실패 — 종료")
            return

        # 잔고 초기화
        balance = self.kis.get_balance()
        self.risk.update_balance(balance.get("total_eval", 0))
        logger.info(f"💰 초기 잔고: {balance.get('total_eval', 0):,.0f}원")

        # 텔레그램 초기화
        await self.init_telegram()
        await self.send_telegram("🚀 주식 자동매매봇 시작!\n" + config.get_config_summary())

        self.running = True
        scan_count = 0

        try:
            while self.running:
                try:
                    # 이슈 스캔 (NEWS_SCAN_INTERVAL 간격)
                    await self.run_scan_cycle()
                    scan_count += 1

                    # 포지션 모니터링 (60초 간격)
                    for _ in range(config.NEWS_SCAN_INTERVAL // 60):
                        if not self.running:
                            break
                        await self.run_monitor_cycle()
                        await asyncio.sleep(60)

                except KeyboardInterrupt:
                    break
                except Exception as e:
                    logger.error("❌ 루프 오류: %s", e, exc_info=True)
                    await asyncio.sleep(60)

        finally:
            await self.shutdown()

    async def shutdown(self):
        """종료 처리."""
        self.running = False
        logger.info("🛑 주식봇 종료 중...")
        if self.telegram_app:
            await self.telegram_app.updater.stop()
            await self.telegram_app.stop()
            await self.telegram_app.shutdown()
        self.risk.save_state()
        logger.info("🛑 주식봇 종료 완료")


def main():
    """엔트리 포인트."""
    bot = StockBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()