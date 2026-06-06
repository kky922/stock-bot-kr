import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.trade_executor import TradeExecutorAgent


class FakeStore:
    def __init__(self, open_count, positions=None):
        self.open_count = open_count
        self.positions = positions or []
        self.saved_slots = {}
        self.recommendations = []

    def is_order_inflight(self, slot_id):
        return False

    def get_cooldown(self, slot_id):
        return ""

    def load_slot(self, slot_id):
        return self.saved_slots.get(slot_id)

    def find_slot_by_code(self, market, stock_code):
        return None

    def get_market_state(self, market):
        return {}

    def get_open_slot_count(self, market):
        return self.open_count

    def load_all_positions(self, market):
        return list(self.positions)

    def record_api_error(self, market, code, message):
        raise AssertionError("API should not be touched when slots are full")

    def clear_api_errors(self, market):
        pass

    def set_order_inflight(self, slot_id):
        raise AssertionError("order lock should not be set when slots are full")

    def clear_order_inflight(self, slot_id):
        pass

    def save_slot(self, slot_id, position):
        self.saved_slots[slot_id] = position

    def append_recommendation(self, recommendation):
        self.recommendations.append(recommendation)


class FakeKis:
    mode = "virtual"

    def buy_stock(self, code, quantity, price):
        raise AssertionError("buy_stock should not be called when order is blocked")


class FakeBreakerDecision:
    def __init__(self, allowed=True, reason="ok", severity="info"):
        self.allowed = allowed
        self.reason = reason
        self.severity = severity
        self.details = {"test": True}

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "severity": self.severity,
            "details": self.details,
        }


class FakeBreaker:
    def __init__(self, decision):
        self.decision = decision
        self.checked = []

    def check(self, symbol, *, action="entry", equity=None):
        self.checked.append({"symbol": symbol, "action": action, "equity": equity})
        return self.decision


class TradeExecutorGuardTests(unittest.TestCase):
    def test_execute_slot_buy_blocks_when_market_position_limit_is_already_full(self):
        store = FakeStore(open_count=3)
        executor = TradeExecutorAgent(store, kis_api=FakeKis())
        decision = {
            "market": "KR",
            "code": "005930",
            "name": "삼성전자",
            "quantity": 1,
            "price": 70000,
        }

        with patch("agents.trade_executor.config.MAX_POSITIONS_PER_MARKET", 3):
            result = executor.execute_slot_buy("KR_005930", decision)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "POSITION_LIMIT")
        self.assertIn("포지션 한도", result["message"])
        self.assertEqual(store.saved_slots, {})

    def test_execute_slot_buy_blocks_when_order_would_exceed_market_budget(self):
        store = FakeStore(
            open_count=2,
            positions=[
                {"invest_amount": 2_244_000},
                {"invest_amount": 1_983_000},
            ],
        )
        executor = TradeExecutorAgent(store, kis_api=FakeKis())
        decision = {
            "market": "KR",
            "code": "036570",
            "name": "엔씨소프트",
            "quantity": 8,
            "price": 284000,
        }

        with patch("agents.trade_executor.config.MAX_POSITIONS_PER_MARKET", 3), \
             patch("agents.trade_executor.config.KR_BUDGET", 5_000_000):
            result = executor.execute_slot_buy("KR_036570", decision)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "BUDGET_LIMIT")
        self.assertIn("예산", result["message"])
        self.assertEqual(store.saved_slots, {})

    def test_execute_slot_buy_blocks_when_circuit_breaker_is_tripped(self):
        store = FakeStore(open_count=0)
        executor = TradeExecutorAgent(store, kis_api=FakeKis())
        executor.breaker = FakeBreaker(FakeBreakerDecision(False, "manual_halt", "critical"))
        decision = {
            "market": "KR",
            "code": "005930",
            "name": "삼성전자",
            "quantity": 1,
            "price": 70000,
        }

        result = executor.execute_slot_buy("KR_005930", decision)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CIRCUIT_BREAKER")
        self.assertEqual(result["breaker"]["reason"], "manual_halt")
        self.assertEqual(store.saved_slots, {})
        self.assertEqual(store.recommendations[-1]["outcome"], "blocked_circuit_breaker")
        self.assertEqual(executor.breaker.checked[-1]["symbol"], "005930")
        self.assertEqual(executor.breaker.checked[-1]["action"], "slot_buy")


if __name__ == "__main__":
    unittest.main()
