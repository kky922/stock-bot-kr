"""
Monitor Agent — 모니터링 에이전트.
트레일링 스톱, 분할 매수 트리거, 분할 매도, 피드백 루프를 관리합니다.
다중 슬롯 포지션 모니터링을 지원합니다.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config

# 선택적 임포트
try:
    from agents.base_agent import BaseAgent, AgentStatus
    from core.message_bus import MessageBus, MessageType, AgentMessage  # [Claude Fix] AgentMessage 누락 추가
    _HAS_BUS = True
except ImportError:
    _HAS_BUS = False

from core.data_store import DataStore

logger = logging.getLogger(__name__)


class MonitorAgent(BaseAgent if _HAS_BUS else object):
    """모니터링 에이전트 — 트레일링 스톱 + 분할 매수/매도 트리거."""

    def __init__(self, data_store: DataStore, message_bus=None):
        if _HAS_BUS and message_bus:
            super().__init__("monitor", message_bus)
            self.bus.subscribe(self.name, MessageType.EXECUTION_REPORT)
            self.bus.subscribe(self.name, MessageType.SYSTEM_COMMAND)
        self._loop_interval = 60.0  # 60초마다 포지션 체크
        self.store = data_store

        # KIS API 지연 임포트
        try:
            from kis_api import KISAPI
            self.kis = KISAPI()
        except Exception as e:
            self.kis = None
            logger.warning("⚠️ KIS API 로드 실패 — 모의투자 모드: %s", e)  # [Claude Fix]

    def process(self):
        """포지션 모니터링 + 실행 리포트 처리."""
        # 1. 실행 리포트 수신
        msg = self.receive_message(timeout=1.0)
        if msg and msg.msg_type == MessageType.EXECUTION_REPORT:
            self._handle_report(msg.data)

        # 2. 포지션 모니터링 (KR + US)
        for market in ["KR", "US"]:
            if market == "US" and not config.US_STOCK_ENABLED:
                continue
            self._monitor_position(market)

    @staticmethod
    def _is_market_open(market: str) -> bool:
        """장시간 체크. 장 닫힌 시장의 슬롯은 모니터링 스킵."""
        from datetime import datetime, time as dt_time
        now = datetime.now()
        t = now.time()
        wd = now.weekday()

        if market == "KR":
            if wd >= 5:
                return False
            return dt_time(9, 0) <= t <= dt_time(15, 30)
        else:  # US
            if wd == 4 and t >= dt_time(22, 30):
                return False
            if wd == 5:
                return False
            if wd == 6 and t < dt_time(5, 0):
                return True
            if wd <= 4:
                return t >= dt_time(22, 30) or t < dt_time(5, 0)
            return False

    def _monitor_position(self, market: str):
        """포지션 실시간 모니터링."""
        # [Fix] 장 닫힌 시장은 스킵 → 불필요한 API 호출 방지
        if not self._is_market_open(market):
            return

        position = self.store.load_position(market)
        if not position:
            return

        code = position["code"]
        name = position["name"]
        entry_price = position["entry_price"]
        atr = position.get("atr") or entry_price * 0.02
        highest_price = position.get("highest_price", entry_price)

        # 현재가 조회
        if market == "KR":
            price_info = self.kis.get_stock_price(code)
            current_price = price_info.get("current", 0)
        else:
            price_info = self.kis.get_us_stock_price(code)
            current_price = price_info.get("current", 0)

        if current_price <= 0:
            return

        pnl_pct = (current_price - entry_price) / entry_price * 100

        # 최고가 업데이트
        if current_price > highest_price:
            position["highest_price"] = current_price
            highest_price = current_price
            self.store.save_position(market, position)

        # ── 1. 손절 체크 ──
        sl_price = position.get("stop_loss_price", entry_price * 0.95)
        if current_price <= sl_price:
            logger.warning("🚨 [%s] 손절 트리거: %s %.1f%% (SL:%.2f)",
                           market, name, pnl_pct, sl_price)
            self.bus.send(AgentMessage(
                msg_type=MessageType.EXIT_SIGNAL,
                sender=self.name,
                data={"market": market, "reason": f"손절 ({pnl_pct:.1f}%)"},
            ), target="trade_executor")
            return

        # ── 2. 트레일링 스톱 체크 ──
        trailing_active = position.get("trailing_active", False)

        if not trailing_active and pnl_pct >= config.TRAILING_ACTIVATE_PCT:
            position["trailing_active"] = True
            self.store.save_position(market, position)
            logger.info("📈 [%s] 트레일링 스톱 활성화: %s (+%.1f%%)", market, name, pnl_pct)

        if trailing_active:
            trailing_price = highest_price - (atr * config.TRAILING_STOP_ATR_MULTI)
            if current_price <= trailing_price:
                logger.info("🎯 [%s] 트레일링 스톱 익절: %s (최고:%.2f → 현재:%.2f)",
                            market, name, highest_price, current_price)
                self.bus.send(AgentMessage(
                    msg_type=MessageType.EXIT_SIGNAL,
                    sender=self.name,
                    data={"market": market, "reason": f"트레일링 익절 ({pnl_pct:+.1f}%)"},
                ), target="trade_executor")
                return

        # ── 3. 익절 체크 ──
        tp_price = position.get("take_profit_price", entry_price * 1.10)
        if current_price >= tp_price:
            logger.info("🎯 [%s] 익절 트리거: %s +%.1f%%", market, name, pnl_pct)
            self.bus.send(AgentMessage(
                msg_type=MessageType.EXIT_SIGNAL,
                sender=self.name,
                data={
                    "market": market,
                    "reason": f"익절 ({pnl_pct:+.1f}%)",
                    "partial_ratio": config.SCALE_OUT_STEPS[0],  # 1차 분할 매도
                },
            ), target="trade_executor")
            return

        # ── 4. 분할 매수 트리거 ──
        scale_step = position.get("scale_in_step", 1)
        plan = position.get("scale_in_plan", [])
        if scale_step < len(plan):
            next_threshold = plan[scale_step].get("threshold_pct", 999)
            if pnl_pct >= next_threshold:
                logger.info("📊 [%s] %d차 매수 조건 충족: %s +%.1f%%",
                            market, scale_step + 1, name, pnl_pct)
                # Trade Executor에게 분할 매수 지시
                self.bus.send(AgentMessage(
                    msg_type=MessageType.SYSTEM_COMMAND,
                    sender=self.name,
                    data={"command": "scale_in", "market": market},
                ), target="trade_executor")

    def _handle_report(self, report: Dict):
        """실행 리포트 처리 → 피드백 생성."""
        action = report.get("action", "")
        market = report.get("market", "KR")

        if action.startswith("sell"):
            # 거래 완료 → 피드백 생성
            pnl = report.get("pnl", 0)
            reason = report.get("reason", "")
            stock_name = report.get("stock_name", "")

            feedback = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": market,
                "stock_name": stock_name,
                "pnl": pnl,
                "reason": reason,
                "result": "win" if pnl > 0 else "loss",
            }
            self.store.save_feedback(feedback)

            logger.info("📊 거래 완료: [%s] %s PnL=%+.2f (%s)",
                        market, stock_name, pnl, reason)

    # ── 다중 슬롯 모니터링 ──

    def _tighten_sl_by_age(self, slot_id: str, position: Dict) -> bool:
        """시장 개장 여부와 무관하게 체류시간 기반 SL 강화 (가격 데이터 불필요).

        2026-05-18: 이마트(58.5h, breakeven=False, SL=-5% 그대로) 사례 발견 후 분리.
        2026-05-29: 고정 % → ATR 기반으로 변경. 기존 -3%/-2% 고정 타이트닝이
        ±0.5% 내 손절 11건/23건의 주원인 → 각 종목 ATR 비례로 완화.
        """
        if position.get("breakeven_protect", False):
            return False
        entry_price = position.get("entry_price", 0)
        if entry_price <= 0:
            return False
        entry_time_str = position.get("entry_time", "")
        if not entry_time_str:
            entry_time_str = position.get("synced_at", "")
            if not entry_time_str:
                return False
        try:
            entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                KST = timezone(timedelta(hours=9))
                entry_dt = entry_dt.replace(tzinfo=KST).astimezone(timezone.utc)
            age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            current_sl = position.get("stop_loss_price", entry_price * 0.95)

            # ATR 기반 타이트닝: 종목의 자연 변동성을 존중
            atr = position.get("atr", 0)
            if atr <= 0:
                atr = entry_price * 0.02  # fallback: 2%

            tightened = False
            # 24시간 경과 → ATR × 1.5 (진입 ATR×2.0에서 완화)
            # 예: ATR=5%이면 SL = -7.5% (기존 고정 -3% 대비 넉넉)
            if age_hours >= 24:
                sl_24 = entry_price - atr * 1.5
                sl_24 = max(sl_24, entry_price * (1 - config.STOP_LOSS_MAX_PCT / 100))
                if current_sl < sl_24:
                    position["stop_loss_price"] = round(sl_24, 4)
                    tightened = True
            # 48시간 경과 → ATR × 1.0 (진입 ATR×2.0의 절반)
            # 예: ATR=5%이면 SL = -5% (기존 고정 -2% 대비 2.5배 넉넉)
            if age_hours >= 48:
                sl_48 = entry_price - atr * 1.0
                sl_48 = max(sl_48, entry_price * (1 - config.STOP_LOSS_MAX_PCT / 100))
                if position["stop_loss_price"] < sl_48:
                    position["stop_loss_price"] = round(sl_48, 4)
                    tightened = True
            if tightened:
                self.store.save_slot(slot_id, position)
                sl_pct = (position["stop_loss_price"] - entry_price) / entry_price * 100
                logger.info("⏳ [슬롯:%s] 체류시간 SL 강화: %s (%.0fh, ATR=%.1f%%) → SL=%.2f (%.1f%%)",
                            slot_id, position.get("name", ""), age_hours, atr / entry_price * 100,
                            position["stop_loss_price"], sl_pct)
            return tightened
        except Exception:
            return False

    def monitor_all_slots(self) -> List[Dict[str, Any]]:
        """모든 활성 슬롯 모니터링 → 청산 필요 슬롯 반환."""
        exit_signals = []
        slots = self.store.load_all_slots()

        for slot_id, position in slots.items():
            if not position or not position.get("code"):
                continue

            # 2026-05-31: 체류시간 SL 강화 비활성화 (virtual-mode).
            # ATR 기반 진입 SL(STOP_LOSS_ATR_MULTI=2.0, clamp 7-12%)가 완전한 시스템.
            # 시간 기반 타이트닝은 ±0.5% 조기손절 11건/23건의 원인. 기존 ATR 개선 후에도
            # 저ATR 종목(삼성전자 2%)의 SL을 -4%→-3%→-2%로 과도하게 조여 손실을 고착화.
            # 퇴출은 ATR 기반 SL + 트레일링/익절에 위임. 모의투자 기간 충분한 데이터 수집 목적.
            # self._tighten_sl_by_age(slot_id, position)

            # [Fix] 장 닫힌 시장의 슬롯은 모니터링 스킵
            market = position.get("market", "KR")
            if not self._is_market_open(market):
                continue

            signal = self._check_slot_exit(slot_id, position)
            if signal:
                exit_signals.append(signal)

        # ── 디컨제스천: 슬롯 초과 시 가장 약한 포지션 자동 청산 ──
        # 다른 청산 신호(trailing/손절/익절/시간초과)와 관계없이 항상 실행.
        # 단, 이미 청산 예정인 슬롯은 제외하고 추가 청산 대상 선정.
        already_selling = {s["slot_id"] for s in exit_signals}
        max_per_market = getattr(config, "MAX_POSITIONS_PER_MARKET", 3)
        for market_code in ("KR", "US"):
            if not self._is_market_open(market_code):
                continue
            market_slots = {
                sid: pos for sid, pos in slots.items()
                if pos and pos.get("code")
                and pos.get("market") == market_code
                and sid not in already_selling
            }
            excess = len(market_slots) - max_per_market
            if excess > 0:
                logger.warning(
                    "🧹 [디컨제스천] %s 포지션 초과 (%d/%d) → %d개 일괄 청산",
                    market_code, len(market_slots), max_per_market, excess,
                )
                for _ in range(excess):
                    weakest = self._find_weakest_slot(market_slots)
                    if not weakest:
                        break
                    exit_signals.append({
                        "slot_id": weakest["slot_id"],
                        "action": "sell",
                        "reason": (
                            f"슬롯초과 청산 ({len(market_slots)}/{max_per_market}, "
                            f"최고수익률 {weakest.get('watermark_return', 0):+.1f}%)"
                        ),
                    })
                    already_selling.add(weakest["slot_id"])
                    # Remove this slot for next iteration
                    market_slots = {
                        sid: pos for sid, pos in market_slots.items()
                        if sid != weakest["slot_id"]
                    }

        return exit_signals

    @staticmethod
    def _find_weakest_slot(market_slots: Dict[str, Dict]) -> Optional[Dict]:
        """슬롯 초과 시 가장 청산 우선순위가 높은 포지션을 찾는다.
        
        기준: 오래 보유 + 낮은 최고 수익률 + 손절가 근접 = 높은 점수
        엔씨소프트 예: 보유 12h + watermark 0% + stop_gap 5% = 0.5 + 7 = 7.5점
        휴젤 예: 보유 62h + watermark 2.9% + stop_gap 5% = 2.6 - 7.1 + 7 = 2.5점
        """
        best = None
        best_score = -999.0

        for slot_id, position in market_slots.items():
            entry_price = float(position.get("entry_price", 0) or 0)
            highest_price = float(position.get("highest_price", 0) or entry_price)
            stop_loss_price = float(position.get("stop_loss_price", 0) or 0)
            entry_time = position.get("entry_time", "") or position.get("synced_at", "")

            age_hours = 0.0
            if entry_time:
                try:
                    entry_dt = datetime.fromisoformat(
                        entry_time.replace("Z", "+00:00")
                    )
                    age_hours = (
                        datetime.now(timezone.utc) - entry_dt
                    ).total_seconds() / 3600.0
                except Exception:
                    pass

            if entry_price <= 0:
                continue

            watermark_return = (
                (highest_price - entry_price) / entry_price * 100.0
            )
            # Score: 오래됐고 수익률 낮고 손절가에 가까울수록 높음
            score = (age_hours / 24.0) - (watermark_return * 2.5)
            if stop_loss_price > 0:
                stop_gap_pct = (
                    (entry_price - stop_loss_price) / entry_price * 100.0
                )
                score += max(0.0, 12.0 - stop_gap_pct)
            # 변동성 가중: ATR 비율이 높은 종목(손절 위험↑) 청산 우선
            # 비에이치(ATR 9.3%) +2.79 vs 엔씨(ATR 6.0%) +1.80
            atr = float(position.get("atr", 0) or 0)
            if atr > 0 and entry_price > 0:
                atr_ratio_pct = atr / entry_price * 100.0
                score += min(atr_ratio_pct * 0.3, 5.0)  # cap at +5pt

            if score > best_score:
                best_score = score
                best = {
                    "slot_id": slot_id,
                    "code": position.get("code"),
                    "name": position.get("name"),
                    "age_hours": round(age_hours, 1),
                    "watermark_return": round(watermark_return, 2),
                    "score": round(score, 2),
                }

        return best

    def _check_slot_exit(self, slot_id: str, position: Dict) -> Optional[Dict]:
        """개별 슬롯 청산 조건 확인."""
        code = position["code"]
        name = position["name"]
        entry_price = position["entry_price"]
        market = position.get("market", "KR")
        atr = position.get("atr") or entry_price * 0.02
        highest_price = position.get("highest_price", entry_price)

        # 현재가 조회
        current_price = self._get_current_price(code, market)
        if current_price <= 0:
            return None

        pnl_pct = (current_price - entry_price) / entry_price * 100

        # 최고가 업데이트
        if current_price > highest_price:
            position["highest_price"] = current_price
            highest_price = current_price  # local var sync — trailing/익절 체크에서 최신값 사용
            self.store.save_slot(slot_id, position)

        # 손절 체크
        sl_price = position.get("stop_loss_price", entry_price * 0.95)
        if current_price <= sl_price:
            logger.warning("🚨 [슬롯:%s] 손절: %s %.1f%%", slot_id, name, pnl_pct)
            return {"slot_id": slot_id, "action": "sell", "reason": f"손절 ({pnl_pct:.1f}%)"}

        # ── 원금보호: ATR 비례 임계값 적용 ──
        # 2026-05-18: 고변동성 종목(비에이치 ATR 9.3%, 현대차 7.4%)이
        # breakeven(1%) 미도달로 SL(-7%) 직행하는 문제 해결.
        # ATR 비율이 높을수록 임계값 하향: effective = max(MIN, BASE - max(0, atr_ratio-3)*0.20)
        # 기본 ATR 3% ~ 1.0%, ATR 6% ~ 0.40%, ATR 9.3% ~ 0.30%
        breakeven_active = position.get("breakeven_protect", False)
        atr_ratio_pct = (atr / entry_price * 100) if atr > 0 and entry_price > 0 else 0
        if atr_ratio_pct > 3.0:
            effective_breakeven_pct = max(
                config.BREAKEVEN_MIN_PCT,
                config.BREAKEVEN_ACTIVATE_PCT - (atr_ratio_pct - 3.0) * 0.20
            )
        else:
            effective_breakeven_pct = config.BREAKEVEN_ACTIVATE_PCT
        if not breakeven_active and highest_price > entry_price * (1 + effective_breakeven_pct / 100):
            position["stop_loss_price"] = int(entry_price)
            position["breakeven_protect"] = True
            self.store.save_slot(slot_id, position)
            logger.info("🛡️ [슬롯:%s] 원금보호 활성화: %s +%.1f%% (임계:%.2f%%, ATR비율:%.1f%%) → SL=진입가(%.0f)",
                        slot_id, name, pnl_pct, effective_breakeven_pct, atr_ratio_pct, entry_price)

        # ── 2026-05-18: 체류시간 SL 강화는 _tighten_sl_by_age()로 이관됨 ──
        # 시장 개장 여부와 무관하게 monitor_all_slots() 진입 시 먼저 실행됨.
        
        # 트레일링 스톱
        if position.get("trailing_active"):
            trailing_price = highest_price - (atr * config.TRAILING_STOP_ATR_MULTI)
            if current_price <= trailing_price:
                logger.info("🎯 [슬롯:%s] 트레일링 익절: %s", slot_id, name)
                return {"slot_id": slot_id, "action": "sell", "reason": f"트레일링 ({pnl_pct:+.1f}%)"}
        elif pnl_pct >= config.TRAILING_ACTIVATE_PCT:
            position["trailing_active"] = True
            self.store.save_slot(slot_id, position)
            logger.info("📈 [슬롯:%s] 트레일링 활성화: %s +%.1f%%", slot_id, name, pnl_pct)

        # ── 4. 시간 기반 청산 체크 ──
        # POSITION_MAX_HOLD_HOURS 이후 watermark return이 POSITION_FLAT_PCT 미만이면 청산
        entry_time_str = position.get("entry_time", "")
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                if age_hours >= config.POSITION_MAX_HOLD_HOURS:
                    watermark_return_pct = (highest_price - entry_price) / entry_price * 100
                    if watermark_return_pct < config.POSITION_FLAT_PCT:
                        logger.info("⏰ [슬롯:%s] 시간초과 청산: %s (보유 %.1f시간, 최고수익률 %.2f%% < %.2f%%)",
                                     slot_id, name, age_hours, watermark_return_pct, config.POSITION_FLAT_PCT)
                        return {"slot_id": slot_id, "action": "sell",
                                "reason": f"시간초과 청산 ({age_hours:.0f}h, 최고수익률 {watermark_return_pct:+.1f}%)"}
            except Exception as e:
                logger.warning("⚠️ 시간 계산 오류 %s: %s", slot_id, e)

        # ── 5. 익절 체크 ──
        tp_price = position.get("take_profit_price", entry_price * 1.10)
        if current_price >= tp_price:
            logger.info("🎯 [슬롯:%s] 익절: %s +%.1f%%", slot_id, name, pnl_pct)
            return {"slot_id": slot_id, "action": "sell", "reason": f"익절 ({pnl_pct:+.1f}%)"}

        return None

    def _get_current_price(self, code: str, market: str) -> float:
        """현재가 조회."""
        if not self.kis:
            return 0.0
        try:
            if market == "KR":
                return self.kis.get_stock_price(code).get("current", 0)
            else:
                return self.kis.get_us_stock_price(code).get("current", 0)
        except Exception as e:
            logger.error("❌ 가격 조회 실패 %s: %s", code, e)
            return 0.0

    def get_slot_status(self) -> List[Dict]:
        """모든 슬롯 상태 요약."""
        slots = self.store.load_all_slots()
        status = []
        for slot_id, pos in slots.items():
            if not pos or not pos.get("code"):
                continue
            current = self._get_current_price(pos["code"], pos.get("market", "KR"))
            entry = pos["entry_price"]
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else 0
            status.append({
                "slot_id": slot_id,
                "code": pos["code"],
                "name": pos["name"],
                "market": pos.get("market", "KR"),
                "entry_price": entry,
                "current_price": current,
                "pnl_pct": round(pnl_pct, 2),
                "quantity": pos.get("quantity", 0),
                "theme": pos.get("theme", ""),
                "trailing": pos.get("trailing_active", False),
            })
        return status
