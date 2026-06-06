"""
텔레그램 원격 제어 모듈.
주식봇 상태 조회, 매수/매도, 리스크 제어 명령어.
"""

import logging
import os
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import config

logger = logging.getLogger(__name__)

ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _auth(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    chat = update.effective_chat
    return chat and str(chat.id) == str(ALLOWED_CHAT_ID)


async def _reply(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text)


def get_bot_instance(context):
    return context.application.bot_data.get("stock_bot")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        await _reply(update, "🚫 권한 없음")
        return
    await _reply(update,
        "🤖 주식 자동매매봇\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📊 조회:\n"
        "  /status — 전체 상태\n"
        "  /bal — 잔고 조회\n"
        "  /pos — 포지션 현황\n"
        "  /risk — 리스크 상태\n"
        "  /scan — 이슈 스캔 실행\n\n"
        "💹 거래:\n"
        "  /buy 종목코드 — 매수\n"
        "  /sell — 전량 매도\n\n"
        "🚨 제어:\n"
        "  /kill — 킬스위치 ON\n"
        "  /unkill — 킬스위치 OFF\n\n"
        "  /help — 도움말"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        await _reply(update, "⚠️ 봇 인스턴스 없음")
        return
    text = (
        f"📊 주식봇 상태\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏦 모드: {'모의투자' if config.KIS_MODE == 'virtual' else '실전'}\n"
        f"📰 이슈: {len(bot.scanner.issues)}개\n"
        f"📋 포지션: {'보유중' if bot.trader.has_position() else '없음'}\n"
        f"{bot.risk.get_status_text()}\n"
        f"\n🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await _reply(update, text)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    bal = bot.kis.get_balance()
    text = (
        f"💰 잔고 현황\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 예수금: {bal.get('total_deposit', 0):,.0f}원\n"
        f"📊 총평가: {bal.get('total_eval', 0):,.0f}원\n"
        f"📈 총PnL: {bal.get('total_pnl', 0):+,.0f}원\n"
    )
    stocks = bal.get("stocks", [])
    if stocks:
        text += "\n📋 보유 종목:"
        for s in stocks[:5]:
            text += f"\n  {s['name']} {s['quantity']}주 {s['pnl_rate']:+.1f}%"
    await _reply(update, text)


async def cmd_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    await _reply(update, bot.trader.get_position_text())


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    await _reply(update, bot.risk.get_status_text())


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    await _reply(update, "📰 이슈 스캔 실행 중...")
    issues = bot.scanner.scan()
    if not issues:
        await _reply(update, "📰 분석된 이슈 없음")
        return
    text = "📰 이슈 스캔 결과\n━━━━━━━━━━━━━━━━━━\n"
    for i, issue in enumerate(issues[:5], 1):
        text += f"\n{i}. {issue.get('title', 'N/A')}\n"
        text += f"   점수: {issue.get('score', 0)}/10 | {issue.get('impact', '')}\n"
        text += f"   섹터: {', '.join(issue.get('sectors', []))}\n"
    await _reply(update, text)


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    if not context.args:
        await _reply(update, "사용법: /buy 종목코드 [종목명]")
        return
    code = context.args[0]
    name = context.args[1] if len(context.args) > 1 else code
    await _reply(update, f"📈 매수 시도: {name} ({code})...")
    result = bot.trader.enter(code, name, "텔레그램 수동 매수")
    if result.get("success"):
        await _reply(update,
            f"✅ 매수 성공!\n"
            f"📊 {result['stock']} {result['quantity']}주\n"
            f"💰 @ {result['price']:,}원"
        )
    else:
        await _reply(update, f"❌ 매수 실패: {result.get('message', '알 수 없음')}")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if not bot:
        return
    result = bot.trader.exit("텔레그램 수동 매도")
    if result.get("success"):
        await _reply(update,
            f"✅ 매도 성공!\n"
            f"📊 {result['stock']} {result['quantity']}주\n"
            f"💰 매수:{result['entry_price']:,} → 매도:{result['sell_price']:,}\n"
            f"📊 PnL: {result['pnl']:+,.0f}원"
        )
    else:
        await _reply(update, f"❌ 매도 실패: {result.get('message', '알 수 없음')}")


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if bot:
        bot.risk.activate_kill_switch("텔레그램 원격 제어")
    await _reply(update, "🚨 킬스위치 활성화! 모든 매수 차단. 해제: /unkill")


async def cmd_unkill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    bot = get_bot_instance(context)
    if bot:
        bot.risk.deactivate_kill_switch()
    await _reply(update, "🟢 킬스위치 해제! 매매 재개.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


def register_commands(application, stock_bot=None):
    if stock_bot:
        application.bot_data["stock_bot"] = stock_bot
    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("status", cmd_status),
        CommandHandler("bal", cmd_balance),
        CommandHandler("pos", cmd_pos),
        CommandHandler("risk", cmd_risk),
        CommandHandler("scan", cmd_scan),
        CommandHandler("buy", cmd_buy),
        CommandHandler("sell", cmd_sell),
        CommandHandler("kill", cmd_kill),
        CommandHandler("unkill", cmd_unkill),
    ]
    for h in handlers:
        application.add_handler(h)
    logger.info("🤖 텔레그램 명령어 %d개 등록 완료", len(handlers))