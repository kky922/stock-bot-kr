"""
매매 실행 모듈.
진입/청산/포지션 관리를 담당합니다.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from kis_api import KISAPI
from risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Trader:
    """주식 매매 실행기."""

    def __init__(self, kis_api: KISAPI, risk_manager: RiskManager):
        self.kis = kis_api
        self.risk = risk_manager
        self.position: Optional[Dict] = None  # 현재 보유 포지션
        self._load_position()

    def _position_file(self) -> str:
        return str(config.DATA_DIR / "position.json")

    def _load_position(self):
        try:
            with open(self._position_file(), encoding="utf-8") as f:
                self.position = json.load(f)
            logger.info("📋 포지션 로드: %s", self.position.get("name", "없음"))
        except Exception:
            self.position = None

    def _save_position(self):
        with open(self._position_file(), "w", encoding="utf-8") as f:
            json.dump(self.position, f, indent=2, ensure_ascii=False)

    def has_position(self) -> bool:
        return self.position is not None

    # ── 매수 진입 ─────────────────────────────────────────

    def enter(self, stock_code: str, stock_name: str, reason: str = "") -> Dict:
        """종목 매수 진입."""
        # 리스크 체크
        can, msg = self.risk.can_trade()
        if not can:
            logger.warning("⛔ 진입 불가: %s", msg)
            return {"success": False, "message": msg}

        # 이미 포지션 있으면 진입 불가
        if self.has_position():
            logger.warning("⛔ 이미 포지션 보유 중: %s", self.position.get("name"))
            return {"success": False, "message": f"이미 {self.position['name']} 보유 중"}

        # 잔고 조회 → 매수 수량 계산
        balance = self.kis.get_balance()
        deposit = balance.get("total_deposit", 0)
        if deposit <= 0:
            return {"success": False, "message": "예수금 부족"}

        price_info = self.kis.get_stock_price(stock_code)
        current_price = price_info.get("current", 0)
        if current_price <= 0:
            return {"success": False, "message": "현재가 조회 실패"}

        # 포지션 사이즈 계산 (예수금의 POSITION_SIZE%)
        budget = deposit * (config.POSITION_SIZE_PERCENT / 100)
        quantity = int(budget // current_price)
        if quantity <= 0:
            return {"success": False, "message": f"매수 불가 (예수금:{deposit:,.0f}, 가격:{current_price:,})"}

        # 주문 실행
        result = self.kis.buy_stock(stock_code, quantity, current_price)

        if result.get("success"):
            self.position = {
                "code": stock_code,
                "name": stock_name,
                "entry_price": current_price,
                "quantity": quantity,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "order_id": result.get("order_id", ""),
            }
            self._save_position()
            self.risk.update_balance(deposit - (current_price * quantity))

            logger.info("✅ 진입 성공: %s %d주 @%d (%s)", stock_name, quantity, current_price, reason)
            return {
                "success": True,
                "stock": stock_name,
                "quantity": quantity,
                "price": current_price,
                "reason": reason,
            }
        else:
            logger.error("❌ 진입 실패: %s", result.get("message"))
            return result

    # ── 매도 청산 ─────────────────────────────────────────

    def exit(self, reason: str = "수동") -> Dict:
        """보유 포지션 전량 매도."""
        if not self.has_position():
            return {"success": False, "message": "포지션 없음"}

        code = self.position["code"]
        name = self.position["name"]
        qty = self.position["quantity"]
        entry_price = self.position["entry_price"]

        # [Claude Fix] 매도 주문 전에 현재가를 미리 조회 — 매도 후 조회하면 체결 후 시세가
        # 변동돼 실제 체결가와 다를 수 있음. 매도 직전 호가를 예상 체결가로 사용.
        price_info_before = self.kis.get_stock_price(code)
        estimated_sell_price = price_info_before.get("current", 0)

        result = self.kis.sell_stock(code, qty)

        if result.get("success"):
            # PnL 계산 (매도 직전 조회가 실제 체결가에 더 근접)
            sell_price = estimated_sell_price if estimated_sell_price > 0 else entry_price
            pnl = (sell_price - entry_price) * qty

            # 거래 결과 기록
            trade_result = "win" if pnl > 0 else "loss"
            self.risk.record_trade(trade_result, pnl, code, name)

            # 잔고 업데이트
            balance = self.kis.get_balance()
            self.risk.update_balance(balance.get("total_eval", 0))

            logger.info("✅ 청산 성공: %s %d주 @%d PnL=%+.0f원 (%s)",
                        name, qty, sell_price, pnl, reason)

            self.position = None
            self._save_position()

            return {
                "success": True,
                "stock": name,
                "quantity": qty,
                "entry_price": entry_price,
                "sell_price": sell_price,
                "pnl": pnl,
                "reason": reason,
            }
        else:
            logger.error("❌ 청산 실패: %s", result.get("message"))
            return result

    # ── 자동 모니터링 ─────────────────────────────────────

    def monitor_position(self) -> Optional[Dict]:
        """포지션 자동 모니터링 (손절/익절 체크)."""
        if not self.has_position():
            return None

        code = self.position["code"]
        entry_price = self.position["entry_price"]
        name = self.position["name"]

        price_info = self.kis.get_stock_price(code)
        current_price = price_info.get("current", 0)

        if current_price <= 0:
            return None

        pnl_pct = (current_price - entry_price) / entry_price * 100

        # 손절 체크
        if self.risk.check_stop_loss(entry_price, current_price):
            logger.warning("🚨 손절 트리거: %s %.1f%%", name, pnl_pct)
            return self.exit(f"손절 ({pnl_pct:.1f}%)")

        # 익절 체크
        if self.risk.check_take_profit(entry_price, current_price):
            logger.info("🎯 익절 트리거: %s %.1f%%", name, pnl_pct)
            return self.exit(f"익절 ({pnl_pct:.1f}%)")

        return None

    def get_position_text(self) -> str:
        """현재 포지션 상태 텍스트."""
        if not self.has_position():
            return "📋 포지션 없음"

        p = self.position
        price_info = self.kis.get_stock_price(p["code"])
        current = price_info.get("current", 0)
        pnl = (current - p["entry_price"]) * p["quantity"]
        pnl_pct = (current - p["entry_price"]) / p["entry_price"] * 100 if p["entry_price"] > 0 else 0

        return (
            f"📋 보유 포지션\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 {p['name']} ({p['code']})\n"
            f"📈 매수가: {p['entry_price']:,}원\n"
            f"📈 현재가: {current:,}원\n"
            f"📊 수량: {p['quantity']}주\n"
            f"{'🟢' if pnl >= 0 else '🔴'} PnL: {pnl:+,.0f}원 ({pnl_pct:+.1f}%)\n"
            f"📝 사유: {p.get('reason', 'N/A')}\n"
            f"⏰ 진입: {p.get('entry_time', 'N/A')[:16]}"
        )