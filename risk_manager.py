"""
리스크 관리 모듈.
손절/익절, 일일 최대 손실, 연속 손실 제한, 킬스위치를 관리합니다.
threading.Lock으로 스레드 세이프 보장.
AI 신호 전용 자금 제한(max_ai_position_pct) 적용.
"""

import json
import logging
import threading
from datetime import datetime, timezone, date
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)


class RiskManager:
    """리스크 관리 엔진 (스레드 세이프)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.kill_switch = False
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consecutive_losses = 0
        self.high_watermark = 0.0
        self.total_balance = 0.0
        self.daily_reset_date: str = ""
        self.trade_history: list = []
        self._load_state()

    def _state_file(self) -> str:
        return str(config.DATA_DIR / "risk_state.json")

    def _load_state(self):
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                s = json.load(f)
            self.kill_switch = s.get("kill_switch", False)
            self.daily_pnl = s.get("daily_pnl", 0.0)
            self.daily_trades = s.get("daily_trades", 0)
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.high_watermark = s.get("high_watermark", 0.0)
            self.total_balance = s.get("total_balance", 0.0)
            self.daily_reset_date = s.get("daily_reset_date", "")
            self.trade_history = s.get("trade_history", [])
        except Exception:
            pass

    def save_state(self):
        """상태 파일 저장 (스레드 세이프)."""
        with self._lock:
            state = {
                "kill_switch": self.kill_switch,
                "daily_pnl": self.daily_pnl,
                "daily_trades": self.daily_trades,
                "consecutive_losses": self.consecutive_losses,
                "high_watermark": self.high_watermark,
                "total_balance": self.total_balance,
                "daily_reset_date": self.daily_reset_date,
                "trade_history": self.trade_history[-100:],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_file(), "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

    def check_daily_reset(self):
        today = date.today().isoformat()
        if self.daily_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.daily_reset_date = today

    def can_trade(self) -> tuple[bool, str]:
        """거래 가능 여부 체크 (스레드 세이프)."""
        with self._lock:
            self.check_daily_reset()

            if self.kill_switch:
                return False, "🚨 킬스위치 활성화 중"

            max_daily_loss = self.total_balance * (config.MAX_DAILY_LOSS_PERCENT / 100)
            if self.daily_pnl < -max_daily_loss:
                return False, f"📉 일일 최대 손실 초과 ({self.daily_pnl:+.0f}원)"

            if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
                return False, f"🔄 연속 손실 {self.consecutive_losses}회 — 거래 중단"

            return True, "✅ 거래 가능"

    def check_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """손절 조건 확인."""
        if entry_price <= 0:
            return False
        loss_pct = (current_price - entry_price) / entry_price * 100
        return loss_pct <= -config.STOP_LOSS_PERCENT

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        """익절 조건 확인."""
        if entry_price <= 0:
            return False
        profit_pct = (current_price - entry_price) / entry_price * 100
        return profit_pct >= config.TAKE_PROFIT_PERCENT

    def record_trade(self, result: str, pnl: float, stock_code: str, stock_name: str):
        """거래 결과 기록 (스레드 세이프)."""
        with self._lock:
            self.daily_pnl += pnl
            self.daily_trades += 1

            if result == "loss":
                self.consecutive_losses += 1
            elif result == "win":
                self.consecutive_losses = 0

            self.trade_history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "code": stock_code,
                "name": stock_name,
                "result": result,
                "pnl": pnl,
            })

            # 자동 킬스위치: 일일 최대 손실 초과 시
            max_daily_loss = self.total_balance * (config.MAX_DAILY_LOSS_PERCENT / 100)
            if self.daily_pnl < -max_daily_loss:
                self.kill_switch = True
                logger.warning("🚨 일일 손실 한도 초과 — 킬스위치 자동 활성화")

        self.save_state()
        logger.info("📊 거래 기록: %s %s PnL=%+.0f원 (연속손실:%d)",
                     stock_name, result, pnl, self.consecutive_losses)

    def update_balance(self, balance: float):
        """잔고 업데이트 + HWM 갱신 (스레드 세이프)."""
        with self._lock:
            self.total_balance = balance
            if balance > self.high_watermark:
                self.high_watermark = balance
        self.save_state()

    def get_max_ai_position_size(self, market: str = "KR") -> float:
        """AI 신호만으로 진입 가능한 최대 포지션 크기.

        정책: AI 신호만으로는 절대 풀사이즈 진입 금지.
        최대 자산의 MAX_AI_POSITION_PCT% 까지만 허용.
        """
        ai_pct = getattr(config, "MAX_AI_POSITION_PCT", 30)
        if market == "KR":
            max_amount = self.total_balance * (ai_pct / 100)
        else:
            max_us_pct = getattr(config, "MAX_US_POSITION_PCT", 20)
            max_amount = self.total_balance * (min(ai_pct, max_us_pct) / 100)
        return max_amount

    def check_ai_position_limit(self, requested_amount: float, market: str = "KR") -> tuple[bool, float]:
        """AI 포지션 한도 체크. (허용여부, 최대허용금액) 반환."""
        max_size = self.get_max_ai_position_size(market)
        if requested_amount > max_size:
            return False, max_size
        return True, requested_amount

    def activate_kill_switch(self, reason: str = ""):
        self.kill_switch = True
        self.save_state()
        logger.warning("🚨 킬스위치 활성화: %s", reason)

    def deactivate_kill_switch(self):
        self.kill_switch = False
        self.consecutive_losses = 0
        self.save_state()
        logger.info("🟢 킬스위치 해제")

    def get_status_text(self) -> str:
        can, reason = self.can_trade()
        max_loss = self.total_balance * (config.MAX_DAILY_LOSS_PERCENT / 100)
        return (
            f"🛡️ 리스크 상태\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"거래: {'✅ 가능' if can else reason}\n"
            f"🚨 킬스위치: {'🔴 ON' if self.kill_switch else '🟢 OFF'}\n"
            f"💰 총자산: {self.total_balance:,.0f}원\n"
            f"📈 최고점: {self.high_watermark:,.0f}원\n"
            f"📊 일일PnL: {self.daily_pnl:+,.0f}원\n"
            f"📊 일일거래: {self.daily_trades}건\n"
            f"🔄 연속손실: {self.consecutive_losses}회 / {config.MAX_CONSECUTIVE_LOSSES}회\n"
            f"📉 손절한도: -{config.STOP_LOSS_PERCENT}%\n"
            f"📈 익절목표: +{config.TAKE_PROFIT_PERCENT}%"
        )