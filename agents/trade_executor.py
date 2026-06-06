"""
Trade Executor Agent — 매매 실행 에이전트.
국내/미국 다중 슬롯 분할 매수/매도를 실행합니다.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config

# 선택적 임포트
try:
    from agents.base_agent import BaseAgent, AgentStatus
    from core.message_bus import MessageBus, MessageType
    _HAS_BUS = True
except ImportError:
    _HAS_BUS = False

from core.data_store import DataStore
from circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class TradeExecutorAgent(BaseAgent if _HAS_BUS else object):
    """매매 실행 에이전트 — 국내/미국 다중 슬롯 분할 매수/매도."""

    def __init__(self, data_store: DataStore, message_bus=None, kis_api=None):
        if _HAS_BUS and message_bus:
            super().__init__("trade_executor", message_bus)
            self.bus.subscribe(self.name, MessageType.RISK_DECISION)
            self.bus.subscribe(self.name, MessageType.EXIT_SIGNAL)
            self.bus.subscribe(self.name, MessageType.SYSTEM_COMMAND)
        self._loop_interval = 5.0
        self.store = data_store
        self._lock = threading.Lock()
        self.breaker = CircuitBreaker()

        # KIS API — 외부 주입 우선, 없으면 새로 생성
        if kis_api is not None:
            self.kis = kis_api
        else:
            try:
                from kis_api import KISAPI
                self.kis = KISAPI()
            except Exception as e:
                self.kis = None
                logger.warning("⚠️ KIS API 로드 실패 — 모의투자 모드: %s", e)

    def process(self):
        """Risk Manager 승인 또는 Monitor 청산 신호 처리."""
        msg = self.receive_message(timeout=5.0)
        if not msg:
            return

        if msg.msg_type == MessageType.SYSTEM_COMMAND:
            cmd = msg.data.get("command")
            if cmd == "force_sell_kr":
                self._force_exit("KR")
            elif cmd == "force_sell_us":
                self._force_exit("US")
            elif cmd == "scale_in":
                market = msg.data.get("market", "KR")
                self.execute_scale_in(market)
            return

        if msg.msg_type == MessageType.RISK_DECISION:
            self._handle_buy(msg.data)

        elif msg.msg_type == MessageType.EXIT_SIGNAL:
            self._handle_sell(msg.data)

    def _handle_buy(self, decision: Dict):
        """분할 매수 실행."""
        market = decision.get("market", "KR")
        stock_code = decision.get("stock_code", "")
        stock_name = decision.get("stock_name", "")

        # 이미 포지션 있으면 스킵
        existing = self.store.load_position(market)
        if existing and existing.get("code"):
            logger.info("💰 [%s] 이미 포지션 보유: %s — 스킵", market, existing.get("name"))
            return

        scale_plan = decision.get("scale_in_plan", [])
        if not scale_plan:
            return

        # 1차 매수 (Step 1)
        step1 = scale_plan[0]
        quantity = step1.get("quantity", 0)
        price = decision.get("current_price", 0)

        if quantity <= 0 or price <= 0:
            return

        breaker_decision = self.breaker.check(stock_code, action="legacy_buy")
        if not breaker_decision.allowed:
            logger.warning("🛑 [%s] Circuit breaker blocked buy %s: %s", market, stock_code, breaker_decision.reason)
            return

        logger.info("💰 [%s] 1차 매수: %s %d주 @%.2f", market, stock_name, quantity, price)

        if market == "KR":
            result = self.kis.buy_stock(stock_code, quantity, int(price))
        else:
            result = self.kis.buy_us_stock(stock_code, quantity, price)

        if result.get("success"):
            self.breaker.record_api_success()
            # ATR 기반 동적 손절/익절
            _atr = decision.get("atr", 0)
            if _atr <= 0:
                _atr = price * 0.02
            _sl = price - _atr * config.STOP_LOSS_ATR_MULTI
            _sl = max(_sl, price * (1 - config.STOP_LOSS_MAX_PCT / 100))
            _sl = min(_sl, price * (1 - config.STOP_LOSS_MIN_PCT / 100))
            _tp = price + _atr * config.TAKE_PROFIT_ATR_MULTI

            # 포지션 기록
            position = {
                "code": stock_code,
                "name": stock_name,
                "entry_price": price,
                "quantity": quantity,
                "total_quantity": decision.get("total_quantity", quantity),
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "reason": decision.get("reason", ""),
                "order_id": result.get("order_id", ""),
                "stop_loss_price": round(_sl, 4),
                "take_profit_price": round(_tp, 4),
                "atr": decision.get("atr", 0),
                "trend": decision.get("trend", "neutral"),
                "scale_in_step": 1,
                "scale_in_plan": scale_plan,
                "scale_out_plan": decision.get("scale_out_plan", []),
                "entry_grade": decision.get("entry_grade", "full"),
                "market": market,
                "highest_price": price,
                "trailing_active": False,
            }
            self.store.save_position(market, position)

            # 실행 리포트 전송
            self.send_message(
                msg_type=MessageType.EXECUTION_REPORT,
                data={
                    "action": "buy_step1",
                    "market": market,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "quantity": quantity,
                    "price": price,
                    "reason": decision.get("reason", ""),
                },
                target="monitor",
            )

            logger.info("✅ [%s] 1차 매수 성공: %s %d주", market, stock_name, quantity)
        else:
            self.breaker.record_api_error(result.get("message", "매수 실패"))
            logger.error("❌ [%s] 매수 실패: %s", market, result.get("message", ""))

    def execute_scale_in(self, market: str):
        """분할 매수 다음 단계 실행 (Monitor에서 호출)."""
        position = self.store.load_position(market)
        if not position:
            return

        current_step = position.get("scale_in_step", 1)
        plan = position.get("scale_in_plan", [])

        if current_step >= len(plan):
            return  # 모든 단계 완료

        next_step = plan[current_step]  # 0-indexed
        code = position["code"]
        name = position["name"]

        # 현재가 조회
        if market == "KR":
            price_info = self.kis.get_stock_price(code)
            current_price = price_info.get("current", 0)
        else:
            price_info = self.kis.get_us_stock_price(code)
            current_price = price_info.get("current", 0)

        if current_price <= 0:
            return

        # 수익률 확인
        entry_price = position["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price * 100
        threshold = next_step.get("threshold_pct", 0)

        if pnl_pct < threshold:
            logger.info("📊 [%s] %d차 매수 대기 (수익률:%.1f%% < 기준:%.1f%%)",
                        market, current_step + 1, pnl_pct, threshold)
            return

        quantity = next_step.get("quantity", 0)
        if quantity <= 0:
            return

        breaker_decision = self.breaker.check(code, action="scale_in")
        if not breaker_decision.allowed:
            logger.warning("🛑 [%s] Circuit breaker blocked scale-in %s: %s", market, code, breaker_decision.reason)
            return

        logger.info("💰 [%s] %d차 매수: %s %d주 @%.2f",
                    market, current_step + 1, name, quantity, current_price)

        if market == "KR":
            result = self.kis.buy_stock(code, quantity, int(current_price))
        else:
            result = self.kis.buy_us_stock(code, quantity, current_price)

        if result.get("success"):
            self.breaker.record_api_success()
            # [Claude Fix] 분할 매수 시 가중평균 단가 갱신 + TP/SL 자동 재계산
            old_qty = position["quantity"]
            old_price = position["entry_price"]
            new_avg_price = (old_price * old_qty + current_price * quantity) / (old_qty + quantity)
            position["entry_price"] = round(new_avg_price, 4)
            position["quantity"] += quantity
            position["scale_in_step"] = current_step + 1
            position["highest_price"] = max(position.get("highest_price", 0), current_price)

            # [Claude Fix] 평균단가 변경 시 TP/SL도 재계산 — 기존엔 1차 매수가 기준으로 고정됐음
            atr = position.get("atr") or new_avg_price * 0.02
            import config as _cfg
            new_sl = new_avg_price - atr * _cfg.STOP_LOSS_ATR_MULTI
            new_sl = max(new_sl, new_avg_price * (1 - _cfg.STOP_LOSS_MAX_PCT / 100))
            new_sl = min(new_sl, new_avg_price * (1 - _cfg.STOP_LOSS_MIN_PCT / 100))
            new_tp = new_avg_price + atr * _cfg.TAKE_PROFIT_ATR_MULTI
            position["stop_loss_price"] = round(new_sl, 4)
            position["take_profit_price"] = round(new_tp, 4)

            self.store.save_position(market, position)

            self.send_message(
                msg_type=MessageType.EXECUTION_REPORT,
                data={
                    "action": f"buy_step{current_step + 1}",
                    "market": market,
                    "stock_code": code,
                    "stock_name": name,
                    "quantity": quantity,
                    "price": current_price,
                    "pnl_pct": pnl_pct,
                },
                target="monitor",
            )
        else:
            self.breaker.record_api_error(result.get("message", "분할 매수 실패"))

    def _handle_sell(self, exit_signal: Dict):
        """청산 신호 처리 (분할 매도 또는 전량 매도).

        [안전장치] 다중 슬롯 환경에서는 이 레거시 경로 대신 execute_slot_sell을 사용.
        save_position(market, None)은 해당 시장의 모든 슬롯을 삭제하므로
        다중 슬롯 보유 시 execute_slot_sell로 라우팅한다.
        """
        market = exit_signal.get("market", "KR")
        reason = exit_signal.get("reason", "신호 청산")
        partial_ratio = exit_signal.get("partial_ratio", 0)  # 0=전량

        position = self.store.load_position(market)
        if not position:
            return

        # [안전장치] 다중 슬롯 감지 → execute_slot_sell 위임
        all_slots = self.store.load_all_slots(market)
        slot_ids = [s for s, p in all_slots.items() if p and p.get("code")]
        if len(slot_ids) > 1:
            target_id = position.get("slot_id") or slot_ids[0]
            logger.warning(
                "⚠️ [안전장치] _handle_sell: %d개 슬롯 감지 → execute_slot_sell(%s) 사용",
                len(slot_ids), target_id,
            )
            self.execute_slot_sell(target_id, reason)
            return

        code = position["code"]
        name = position["name"]
        total_qty = position["quantity"]

        if partial_ratio > 0:
            # 분할 매도
            sell_qty = max(1, int(total_qty * partial_ratio))
        else:
            sell_qty = total_qty

        logger.info("💰 [%s] 매도: %s %d주 (%s)", market, name, sell_qty, reason)

        if market == "KR":
            result = self.kis.sell_stock(code, sell_qty)
        else:
            result = self.kis.sell_us_stock(code, sell_qty)

        if result.get("success"):
            entry_price = position["entry_price"]

            if market == "KR":
                price_info = self.kis.get_stock_price(code)
                sell_price = price_info.get("current", 0)
            else:
                price_info = self.kis.get_us_stock_price(code)
                sell_price = price_info.get("current", 0)

            pnl = (sell_price - entry_price) * sell_qty

            remaining = total_qty - sell_qty
            if remaining <= 0:
                # 전량 청산 — 단일 슬롯만 있으므로 안전
                self.store.save_position(market, None)
            else:
                position["quantity"] = remaining
                self.store.save_position(market, position)

            # 거래 기록
            self.store.append_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": market,
                "code": code,
                "name": name,
                "action": "sell",
                "quantity": sell_qty,
                "entry_price": entry_price,
                "sell_price": sell_price,
                "pnl": pnl,
                "reason": reason,
            })

            self.send_message(
                msg_type=MessageType.EXECUTION_REPORT,
                data={
                    "action": "sell",
                    "market": market,
                    "stock_code": code,
                    "stock_name": name,
                    "quantity": sell_qty,
                    "entry_price": entry_price,
                    "sell_price": sell_price,
                    "pnl": pnl,
                    "reason": reason,
                    "remaining": remaining,
                },
                target="monitor",
            )

            logger.info("✅ [%s] 매도 성공: %s %d주 PnL=%+.2f (%s)",
                        market, name, sell_qty, pnl, reason)
            equity = config.KR_BUDGET if market == "KR" else config.US_BUDGET
            self.breaker.record_trade_result(code, pnl, equity=equity)
        else:
            self.breaker.record_api_error(result.get("message", "매도 실패"))

    def execute_slot_buy(self, slot_id: str, decision: Dict) -> Dict[str, Any]:
        """다중 슬롯 매수 실행 (오케스트레이터에서 호출).

        slot_id: 슬롯 식별자 (예: "KR_005930")
        decision: {"code", "name", "market", "quantity", "price", "reason", ...}
        """
        with self._lock:
            market = decision.get("market", "KR")
            stock_code = decision.get("code", decision.get("stock_code", ""))
            stock_name = decision.get("name", decision.get("stock_name", ""))
            quantity = decision.get("quantity", 0)
            price = decision.get("price", decision.get("current_price", 0))

            if quantity <= 0 or price <= 0:
                return {"success": False, "message": "수량/가격 오류"}

            if self.store.is_order_inflight(slot_id):
                return {"success": False, "message": f"슬롯 {slot_id} 주문 진행 중"}

            cooldown_until = self.store.get_cooldown(slot_id)
            if cooldown_until:
                return {"success": False, "message": f"슬롯 {slot_id} 쿨다운 중", "cooldown_until": cooldown_until}

            # 슬롯 이미 사용 중인지 확인
            existing = self.store.load_slot(slot_id)
            if existing and existing.get("code"):
                return {"success": False, "message": f"슬롯 {slot_id} 이미 사용 중"}

            if self.store.find_slot_by_code(market, stock_code):
                return {"success": False, "message": f"{stock_code} 이미 보유 중"}

            # 실행 직전 최신 원장 기준으로 포지션 한도를 재확인한다.
            # 스캔 결과가 오래됐거나 같은 사이클에서 여러 주문이 순차 체결되면
            # 오케스트레이터의 사전 리스크 판단만으로는 한도 초과를 막지 못한다.
            open_count = self.store.get_open_slot_count(market)
            max_positions = config.MAX_POSITIONS_PER_MARKET
            if open_count >= max_positions:
                return {
                    "success": False,
                    "error_code": "POSITION_LIMIT",
                    "message": f"[{market}] 포지션 한도 초과 ({open_count}/{max_positions})",
                }

            # 실행 직전 남은 시장 예산도 재확인한다. 수량 계산 쪽 버그나
            # 오래된 신호가 들어와도 계좌/모의예산을 초과하는 주문은 막는다.
            budget = config.KR_BUDGET if market == "KR" else config.US_BUDGET
            invested = sum(
                float(position.get("invest_amount", 0) or 0)
                for position in self.store.load_all_positions(market)
            )
            remaining_budget = max(0.0, float(budget) - invested)
            order_value = float(quantity) * float(price)
            if order_value > remaining_budget:
                return {
                    "success": False,
                    "error_code": "BUDGET_LIMIT",
                    "message": (
                        f"[{market}] 예산 한도 초과 "
                        f"(주문={order_value:.0f}, 남은예산={remaining_budget:.0f}, 예산={float(budget):.0f})"
                    ),
                }

            market_state = self.store.get_market_state(market)
            if market_state.get("api_degraded_mode"):
                return {"success": False, "message": f"[{market}] api_degraded_mode"}

            breaker_decision = self.breaker.check(stock_code, action="slot_buy")
            if not breaker_decision.allowed:
                self.store.append_recommendation({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": market,
                    "code": stock_code,
                    "name": stock_name,
                    "outcome": "blocked_circuit_breaker",
                    "reason": breaker_decision.reason,
                    "severity": breaker_decision.severity,
                    "details": breaker_decision.details or {},
                })
                return {
                    "success": False,
                    "error_code": "CIRCUIT_BREAKER",
                    "message": f"Circuit breaker blocked entry: {breaker_decision.reason}",
                    "breaker": breaker_decision.to_dict(),
                }

            if market == "US" and config.US_READINESS_MODE and getattr(self.kis, "mode", config.KIS_MODE) == "real":
                readiness_payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": market,
                    "code": stock_code,
                    "name": stock_name,
                    "quantity": quantity,
                    "price": price,
                    "outcome": "readiness_only",
                    "reason": "US_READINESS_MODE",
                    "mode": getattr(self.kis, "mode", config.KIS_MODE),
                }
                self.store.append_recommendation(readiness_payload)
                return {
                    "success": False,
                    "error_code": "US_READINESS_MODE",
                    "message": "미국장은 readiness mode로 실제 주문 비활성화",
                    "readiness": True,
                }

            if market == "US" and config.US_REQUIRE_REAL_MODE and getattr(self.kis, "mode", config.KIS_MODE) not in ("real", "virtual"):
                # virtual 모드에서는 US_REQUIRE_REAL_MODE 검증 스킵 — 테스트 허용
                return {
                    "success": False,
                    "error_code": "US_REAL_MODE_REQUIRED",
                    "message": "미국장은 실전 모드에서만 주문 가능하도록 설정됨",
                }

            kis_mode = getattr(self.kis, "mode", config.KIS_MODE)
            if market == "US" and kis_mode == "real" and quantity * price > config.US_MICRO_LIVE_MAX_NOTIONAL:
                return {
                    "success": False,
                    "error_code": "US_MICRO_LIMIT",
                    "message": f"미국장 실전 주문금액 한도 초과 ({config.US_MICRO_LIVE_MAX_NOTIONAL:.0f} USD)",
                }

            logger.info("💰 [슬롯:%s] 매수: %s %d주 @%.0f", slot_id, stock_name, quantity, price)
            self.store.set_order_inflight(slot_id)
            try:
                # 실제 주문 (KIS API 있을 때만)
                if self.kis:
                    try:
                        if market == "KR":
                            result = self.kis.buy_stock(stock_code, quantity, int(price))
                        else:
                            result = self.kis.buy_us_stock(stock_code, quantity, price)
                        if not result.get("success"):
                            self.breaker.record_api_error(result.get("message", "주문 실패"))
                            self.store.record_api_error(market, result.get("error_code", "ORDER_FAIL"), result.get("message", "주문 실패"))
                            self.store.append_recommendation({
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "market": market,
                                "code": stock_code,
                                "name": stock_name,
                                "outcome": "order_fail",
                                "error_code": result.get("error_code", ""),
                                "reason": result.get("message", "주문 실패"),
                            })
                            return {
                                "success": False,
                                "message": result.get("message", "주문 실패"),
                                "error_code": result.get("error_code", ""),
                            }
                        self.breaker.record_api_success()
                        self.store.clear_api_errors(market)
                    except Exception as e:
                        logger.error("❌ 주문 실패: %s", e)
                        self.breaker.record_api_error(str(e))
                        self.store.record_api_error(market, "ORDER_EXCEPTION", str(e))
                        return {"success": False, "message": str(e)}
                else:
                    result = {"success": True, "order_id": f"SIM_{slot_id}"}

                # ATR 기반 동적 손절/익절 계산 — config에 STOP_LOSS_ATR_MULTI(2.0)/MIN(5%)/MAX(10%)가
                # 정의되어 있었지만 position 생성 시점에 전혀 활용되지 않음 (flat 5%만 사용).
                # execute_scale_in(214-218)의 ATR 계산 로직과 동일하게 맞춤.
                import config as _cfg
                atr_price = decision.get("atr", 0)
                if atr_price <= 0:
                    atr_price = price * 0.02  # ATR 미지수 시 추정
                sl_price = price - atr_price * _cfg.STOP_LOSS_ATR_MULTI
                sl_price = max(sl_price, price * (1 - _cfg.STOP_LOSS_MAX_PCT / 100))
                sl_price = min(sl_price, price * (1 - _cfg.STOP_LOSS_MIN_PCT / 100))
                tp_price = price + atr_price * _cfg.TAKE_PROFIT_ATR_MULTI

                # 슬롯 포지션 기록
                position = {
                    "slot_id": slot_id,
                    "code": stock_code,
                    "name": stock_name,
                    "entry_price": price,
                    "quantity": quantity,
                    "invest_amount": quantity * price,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "reason": decision.get("reason", ""),
                    "order_id": result.get("order_id", ""),
                    "order_state": "filled",
                    "stop_loss_price": round(sl_price, 4),
                    "take_profit_price": round(tp_price, 4),
                    "atr": decision.get("atr", 0),
                    "scale_in_step": 1,
                    "market": market,
                    "highest_price": price,
                    "theme": decision.get("theme", ""),
                    "strategy_id": decision.get("strategy_id", "theme_pipeline"),
                    "cooldown_until": "",
                    "last_order_attempt_at": datetime.now(timezone.utc).isoformat(),
                }
                self.store.save_slot(slot_id, position)
                self.store.append_recommendation({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": market,
                    "code": stock_code,
                    "name": stock_name,
                    "outcome": "buy",
                    "reason": decision.get("reason", ""),
                })

                logger.info("✅ [슬롯:%s] 매수 성공: %s %d주", slot_id, stock_name, quantity)
                return {"success": True, "slot_id": slot_id, "position": position}
            finally:
                self.store.clear_order_inflight(slot_id)
        
        

    def execute_slot_sell(self, slot_id: str, reason: str = "신호 청산") -> Dict[str, Any]:
        """슬롯 포지션 전량 매도."""
        with self._lock:
            position = self.store.load_slot(slot_id)
            if not position:
                return {"success": False, "message": f"슬롯 {slot_id} 포지션 없음"}

            code = position["code"]
            name = position["name"]
            quantity = position["quantity"]
            market = position.get("market", "KR")
            entry_price = position["entry_price"]

            logger.info("💰 [슬롯:%s] 매도: %s %d주 (%s)", slot_id, name, quantity, reason)

            if self.kis:
                try:
                    if market == "KR":
                        result = self.kis.sell_stock(code, quantity)
                    else:
                        result = self.kis.sell_us_stock(code, quantity)
                    sell_price = 0
                    if result.get("success"):
                        self.breaker.record_api_success()
                        if market == "KR":
                            sell_price = self.kis.get_stock_price(code).get("current", 0)
                        else:
                            sell_price = self.kis.get_us_stock_price(code).get("current", 0)
                        self.store.clear_api_errors(market)
                    else:
                        self.breaker.record_api_error(result.get("message", "매도 실패"))
                        self.store.record_api_error(market, result.get("error_code", "SELL_FAIL"), result.get("message", "매도 실패"))
                except Exception as e:
                    self.breaker.record_api_error(str(e))
                    self.store.record_api_error(market, "SELL_EXCEPTION", str(e))
                    return {"success": False, "message": str(e)}
            else:
                sell_price = entry_price * 1.01  # 시뮬레이션
                result = {"success": True}

            if result.get("success"):
                pnl = (sell_price - entry_price) * quantity
                pnl_pct = ((sell_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

                # 슬롯 초기화
                self.store.save_slot(slot_id, None)
                self.store.set_cooldown(slot_id)

                # 거래 기록
                self.store.append_trade({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "slot_id": slot_id,
                    "market": market,
                    "code": code,
                    "name": name,
                    "action": "sell",
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "sell_price": sell_price,
                    "pnl": pnl,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": reason,
                })
                if pnl < 0:
                    # 모의투자 모드에서는 쿨다운 기간 단축 (학습/반복 속도 향상)
                    if pnl_pct <= -5:
                        cooldown_seconds = 5 * 86400
                    elif pnl_pct <= -2:
                        cooldown_seconds = 3 * 86400
                    else:
                        cooldown_seconds = 86400
                    # 반복 손실 종목(3회+)은 풀 쿨다운 — 모의투자 단축 면제
                    repeat_loss_count = 0
                    try:
                        import json as _json
                        with open('data/agents/trade_history.json') as _f:
                            _th = _json.load(_f).get('trades', [])
                        repeat_loss_count = sum(
                            1 for _t in _th
                            if _t.get('code') == code
                            and _t.get('action') in ('sell', 'stop_loss')
                            and _t.get('pnl', 0) < 0
                        )
                    except (FileNotFoundError, _json.JSONDecodeError):
                        pass
                    use_full_cooldown = repeat_loss_count >= 2  # 현재 매도 포함 시 3회+
                    if config.KIS_MODE == "virtual" and hasattr(config, "MOCK_LOSS_COOLDOWN_DIVISOR"):
                        divisor = max(1, config.MOCK_LOSS_COOLDOWN_DIVISOR)
                        if divisor > 1 and not use_full_cooldown:
                            cooldown_seconds = max(3600, cooldown_seconds // divisor)
                        elif use_full_cooldown:
                            logger.info("🧊 [반복손실:%s] %s %s — 풀 쿨다운 %d초 (무효화 %d회차)", slot_id, code, name, cooldown_seconds, repeat_loss_count + 1)
                    self.store.set_symbol_cooldown(
                        market,
                        code,
                        seconds=cooldown_seconds,
                        reason=f"sell_loss pnl={pnl:.0f} ({pnl_pct:.1f}%) reason={reason}",
                    )
                    logger.info(
                        "🧊 [슬롯:%s] 손실 종목 쿨다운 적용: %s %s for %d초",
                        slot_id,
                        code,
                        name,
                        cooldown_seconds,
                    )
                self.store.append_recommendation({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market": market,
                    "code": code,
                    "name": name,
                    "outcome": "sell",
                    "reason": reason,
                })

                logger.info("✅ [슬롯:%s] 매도 성공: %s PnL=%+.0f (%.1f%%)",
                            slot_id, name, pnl, pnl_pct)
                equity = config.KR_BUDGET if market == "KR" else config.US_BUDGET
                self.breaker.record_trade_result(code, pnl, equity=equity)
                return {"success": True, "pnl": pnl, "pnl_pct": pnl_pct}
            else:
                return {"success": False, "message": result.get("message", "매도 실패")}

    def sell_all_slots(self, market: str = None) -> List[Dict]:
        """모든 슬롯(또는 특정 시장) 전량 매도."""
        results = []
        slots = self.store.load_all_slots(market)
        for slot_id, position in slots.items():
            if position and position.get("code"):
                r = self.execute_slot_sell(slot_id, "전량 청산")
                results.append({"slot_id": slot_id, **r})
        return results

    def _force_exit(self, market: str):
        """강제 전량 청산."""
        # 기존 단일 슬롯 호환
        self._handle_sell({"market": market, "reason": "수동 강제 청산", "partial_ratio": 0})
        # 다중 슬롯도 청산
        self.sell_all_slots(market)
