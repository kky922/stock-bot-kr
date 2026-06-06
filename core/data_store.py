"""
공통 데이터 저장소.
에이전트 상태, 거래 이력, 포지션, 런타임 헬스 데이터를 JSON 파일로 관리합니다.
스레드 세이프 — 모든 쓰기 연산이 단일 락으로 보호됩니다.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
import config

logger = logging.getLogger(__name__)


class DataStore:
    """JSON 기반 공유 데이터 저장소 (스레드 세이프).

    NOTE: _global_lock은 클래스 레벨 싱글톤 락이다. 오케스트레이터, 트레이드 실행자,
    리스크 매니저 등 여러 에이전트가 각자 DataStore 인스턴스를 들고 있어도
    포지션 원장(all_slots.json) 등의 쓰기/읽기가 하나의 락으로 직렬화된다.
    인스턴스 레벨 락(self._lock)이었다면 get_open_slot_count → save_slot 사이의
    경합으로 MAX_POSITIONS_PER_MARKET 초과 진입이 발생할 수 있다.
    """

    _global_lock = threading.Lock()

    def __init__(self):
        self._lock = DataStore._global_lock
        self._data_dir = config.DATA_DIR / "agents"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Any] = {}

    def _file(self, name: str) -> Path:
        return self._data_dir / f"{name}.json"

    def save(self, name: str, data: Dict[str, Any]):
        """데이터를 JSON 파일로 저장 (락 내부에서만 호출)."""
        data["_saved_at"] = datetime.now(timezone.utc).isoformat()
        self._cache[name] = data
        try:
            self._file(name).write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("❌ DataStore 저장 실패 (%s): %s", name, e)

    def load(self, name: str) -> Dict[str, Any]:
        """JSON 파일에서 데이터 로드 (락 내부에서만 호출).

        여러 에이전트/스레드가 각자 DataStore 인스턴스를 들고 같은 JSON 원장을
        갱신한다. 메모리 캐시를 우선하면 다른 인스턴스의 매수/매도/계좌동기화
        결과를 보지 못해 포지션 수 제한 같은 안전 가드가 낡은 상태로 판단된다.
        따라서 정상 경로에서는 항상 디스크의 최신 스냅샷을 읽고, 읽기 실패 시에만
        마지막 캐시를 폴백으로 사용한다.
        """
        try:
            f = self._file(name)
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                self._cache[name] = data
                return data
        except Exception as e:
            logger.error("❌ DataStore 로드 실패 (%s): %s", name, e)
            if name in self._cache:
                return self._cache[name]
        return {}

    def _load_slots_unlocked(self) -> Dict[str, Dict[str, Any]]:
        all_slots = self.load("all_slots")
        return dict(all_slots.get("slots", {}))

    def _load_cooldowns_unlocked(self) -> Dict[str, str]:
        return dict(self.load("cooldowns").get("entries", {}))

    def _is_future_timestamp(self, value: str) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except Exception:
            return False

    def _save_slots_unlocked(self, slots: Dict[str, Any]):
        self.save("all_slots", {"slots": slots})

    def _active_slots_unlocked(self, market: str = None) -> Dict[str, Dict]:
        slots = self._load_slots_unlocked()
        cooldowns = self._load_cooldowns_unlocked()
        active = {}
        now = datetime.now(timezone.utc)
        dirty = False
        for slot_id, pos in slots.items():
            if not pos or not pos.get("code"):
                continue
            if market and pos.get("market", "KR") != market:
                continue
            cooldown_until = cooldowns.get(slot_id, "")
            if cooldown_until:
                try:
                    expiry = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
                    if expiry > now:
                        continue
                    cooldowns.pop(slot_id, None)
                    dirty = True
                except Exception:
                    cooldowns.pop(slot_id, None)
                    dirty = True
            active[slot_id] = pos
        if dirty:
            self.save("cooldowns", {"entries": cooldowns})
        return active

    # ── 스레드 세이프 공개 메서드 ──────────────────────────

    def safe_save(self, name: str, data: Dict[str, Any]):
        with self._lock:
            self.save(name, data)

    def safe_load(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return self.load(name)

    def safe_update(self, name: str, updates: Dict[str, Any]):
        with self._lock:
            data = self.load(name)
            data.update(updates)
            self.save(name, data)

    # ── 거래 / 추천 이력 ───────────────────────────────────

    def append_trade(self, trade: Dict[str, Any]):
        with self._lock:
            history = self.load("trade_history")
            trades = history.get("trades", [])
            trades.append(trade)
            history["trades"] = trades[-200:]
            self.save("trade_history", history)

    def get_trades(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            history = self.load("trade_history")
            return history.get("trades", [])[-limit:]

    def append_recommendation(self, recommendation: Dict[str, Any]):
        with self._lock:
            history = self.load("recommendation_history")
            entries = history.get("entries", [])
            entries.append(recommendation)
            history["entries"] = entries[-300:]
            self.save("recommendation_history", history)

    def get_recommendations(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            history = self.load("recommendation_history")
            return history.get("entries", [])[-limit:]

    # ── 다종목 포지션 관리 ────────────────────────────────

    def save_position(self, market: str, position: Optional[Dict]):
        """하위 호환용 시장별 포지션 저장. 내부적으로는 슬롯 원장을 사용."""
        with self._lock:
            slots = self._load_slots_unlocked()
            if position is None:
                for slot_id in list(slots):
                    if slots[slot_id].get("market", "KR") == market:
                        slots.pop(slot_id, None)
                self._save_slots_unlocked(slots)
                return

            slot_id = position.get("slot_id") or f"{market}_{position.get('code', '')}"
            position["slot_id"] = slot_id
            slots[slot_id] = position
            self._save_slots_unlocked(slots)

    def load_position(self, market: str) -> Optional[Dict]:
        """하위 호환용 시장별 첫 포지션 반환."""
        with self._lock:
            slots = list(self._active_slots_unlocked(market).values())
            return slots[0] if slots else None

    def load_all_positions(self, market: str) -> List[Dict]:
        with self._lock:
            return list(self._active_slots_unlocked(market).values())

    def save_slot(self, slot_id: str, position: Optional[Dict]):
        with self._lock:
            slots = self._load_slots_unlocked()
            if position is None:
                slots.pop(slot_id, None)
            else:
                position["slot_id"] = slot_id
                slots[slot_id] = position
            self._save_slots_unlocked(slots)

    def load_slot(self, slot_id: str) -> Optional[Dict]:
        with self._lock:
            return self._load_slots_unlocked().get(slot_id)

    def load_all_slots(self, market: str = None) -> Dict[str, Dict]:
        with self._lock:
            slots = self._load_slots_unlocked()
            if market is None:
                return dict(slots)
            return {sid: pos for sid, pos in slots.items() if pos.get("market", "KR") == market}

    def remove_position_by_code(self, market: str, stock_code: str) -> bool:
        with self._lock:
            slots = self._load_slots_unlocked()
            removed = False
            for slot_id, pos in list(slots.items()):
                if pos.get("market", "KR") == market and pos.get("code") == stock_code:
                    slots.pop(slot_id, None)
                    removed = True
            if removed:
                self._save_slots_unlocked(slots)
            return removed

    def find_slot_by_code(self, market: str, stock_code: str) -> Optional[Dict]:
        with self._lock:
            for slot in self._active_slots_unlocked(market).values():
                if slot.get("code") == stock_code:
                    return slot
            return None

    def get_open_slot_count(self, market: str) -> int:
        with self._lock:
            return len(self._active_slots_unlocked(market))

    def has_position_code(self, market: str, stock_code: str) -> bool:
        return self.find_slot_by_code(market, stock_code) is not None

    # ── 주문 보호 / 쿨다운 ───────────────────────────────

    def is_order_inflight(self, slot_id: str) -> bool:
        with self._lock:
            state = self.load("order_guards")
            inflight = state.get("inflight", {})
            expiry = inflight.get(slot_id, "")
            if self._is_future_timestamp(expiry):
                return True
            if expiry:
                inflight.pop(slot_id, None)
                state["inflight"] = inflight
                self.save("order_guards", state)
            return False

    def set_order_inflight(self, slot_id: str, ttl_seconds: int = None):
        if ttl_seconds is None:
            ttl_seconds = config.ORDER_INFLIGHT_TTL_SECONDS
        with self._lock:
            state = self.load("order_guards")
            inflight = state.get("inflight", {})
            inflight[slot_id] = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
            state["inflight"] = inflight
            self.save("order_guards", state)

    def clear_order_inflight(self, slot_id: str):
        with self._lock:
            state = self.load("order_guards")
            inflight = state.get("inflight", {})
            inflight.pop(slot_id, None)
            state["inflight"] = inflight
            self.save("order_guards", state)

    def set_cooldown(self, slot_id: str, seconds: int = None):
        if seconds is None:
            seconds = config.ORDER_COOLDOWN_SECONDS
        with self._lock:
            state = self.load("cooldowns")
            entries = state.get("entries", {})
            entries[slot_id] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            state["entries"] = entries
            self.save("cooldowns", state)

    def get_cooldown(self, slot_id: str) -> Optional[str]:
        with self._lock:
            state = self.load("cooldowns")
            entries = state.get("entries", {})
            expiry = entries.get(slot_id, "")
            if self._is_future_timestamp(expiry):
                return expiry
            if expiry:
                entries.pop(slot_id, None)
                state["entries"] = entries
                self.save("cooldowns", state)
            return None

    def _symbol_cooldown_key(self, market: str, stock_code: str) -> str:
        return f"{market}:{stock_code}"

    def set_symbol_cooldown(self, market: str, stock_code: str, seconds: int = None, reason: str = ""):
        if seconds is None:
            seconds = max(config.ORDER_COOLDOWN_SECONDS, 86400)
        with self._lock:
            state = self.load("symbol_cooldowns")
            entries = state.get("entries", {})
            key = self._symbol_cooldown_key(market, stock_code)
            entries[key] = {
                "until": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                "reason": reason[:200],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            state["entries"] = entries
            self.save("symbol_cooldowns", state)

    def get_symbol_cooldown(self, market: str, stock_code: str) -> Optional[Dict[str, str]]:
        with self._lock:
            state = self.load("symbol_cooldowns")
            entries = state.get("entries", {})
            key = self._symbol_cooldown_key(market, stock_code)
            entry = entries.get(key)
            if not entry:
                return None
            if isinstance(entry, str):
                until = entry
                reason = ""
            else:
                until = str(entry.get("until", ""))
                reason = str(entry.get("reason", ""))
            if self._is_future_timestamp(until):
                return {"until": until, "reason": reason}
            entries.pop(key, None)
            state["entries"] = entries
            self.save("symbol_cooldowns", state)
            return None

    # ── 시장 런타임 상태 ────────────────────────────────

    def get_market_state(self, market: str) -> Dict[str, Any]:
        with self._lock:
            state = self.load("market_runtime")
            return dict(state.get(market, {}))

    def update_market_state(self, market: str, updates: Dict[str, Any]):
        with self._lock:
            state = self.load("market_runtime")
            market_state = state.get(market, {})
            market_state.update(updates)
            state[market] = market_state
            self.save("market_runtime", state)

    def record_api_error(self, market: str, error_code: str, message: str = "") -> Dict[str, Any]:
        with self._lock:
            state = self.load("market_runtime")
            market_state = state.get(market, {})
            failures = int(market_state.get("consecutive_api_failures", 0)) + 1
            # 가상 모드 US: KIS VTS가 US 주문/잔고를 지원하지 않는 정상 상태
            # → api_degraded_mode로 표시하지 않음 (fix #7a: US 파이프라인 테스트 유지)
            is_expected_us_virtual = (
                market == "US"
                and getattr(config, "KIS_MODE", "real") == "virtual"
            )
            market_state.update({
                "consecutive_api_failures": failures,
                "last_error_code": error_code,
                "last_error_message": message[:300],
                "last_error_at": datetime.now(timezone.utc).isoformat(),
                "api_degraded_mode": not is_expected_us_virtual and failures >= config.API_DEGRADED_RETRY_LIMIT,
            })
            state[market] = market_state
            self.save("market_runtime", state)
            return dict(market_state)

    def clear_api_errors(self, market: str):
        with self._lock:
            state = self.load("market_runtime")
            market_state = state.get(market, {})
            market_state["consecutive_api_failures"] = 0
            market_state["api_degraded_mode"] = False
            market_state["last_error_code"] = ""
            state[market] = market_state
            self.save("market_runtime", state)

    def record_last_good_balance(self, market: str, total_balance: float, deposit: float):
        with self._lock:
            state = self.load("market_runtime")
            market_state = state.get(market, {})
            market_state.update({
                "last_good_balance": total_balance,
                "last_good_deposit": deposit,
                "balance_sync_status": "ok",
                "balance_synced_at": datetime.now(timezone.utc).isoformat(),
            })
            state[market] = market_state
            self.save("market_runtime", state)

    def mark_balance_sync_failed(self, market: str, error: str = ""):
        with self._lock:
            state = self.load("market_runtime")
            market_state = state.get(market, {})
            market_state.update({
                "balance_sync_status": "failed",
                "last_balance_sync_error": error[:300],
                "last_balance_sync_error_at": datetime.now(timezone.utc).isoformat(),
            })
            state[market] = market_state
            self.save("market_runtime", state)

    # ── 테마 상태 ────────────────────────────────────────

    def save_theme_state(self, theme_state: Dict[str, Any]):
        with self._lock:
            theme_state["_updated_at"] = datetime.now(timezone.utc).isoformat()
            self.save("theme_state", theme_state)

    def load_theme_state(self) -> Dict[str, Any]:
        with self._lock:
            return self.load("theme_state")

    # ── 피드백 ───────────────────────────────────────────

    def save_feedback(self, feedback: Dict[str, Any]):
        with self._lock:
            fb = self.load("feedback")
            entries = fb.get("entries", [])
            entries.append(feedback)
            fb["entries"] = entries[-100:]
            self.save("feedback", fb)

    def get_feedback(self, limit: int = 20) -> List[Dict]:
        with self._lock:
            fb = self.load("feedback")
            return fb.get("entries", [])[-limit:]
