import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.modules.setdefault("feedparser", types.SimpleNamespace(parse=lambda *args, **kwargs: None))

import run_agents


class FakeStore:
    def __init__(self, positions, slots=None):
        self.positions = positions
        self.slots = slots or {}

    def load_all_positions(self, market):
        return self.positions.get(market, [])

    def load_all_slots(self, market):
        return {"slots": self.slots.get(market, {})}

    def get_open_slot_count(self, market):
        return len(self.positions.get(market, []))


class OrderSizingGuardTests(unittest.TestCase):
    def test_first_order_quantity_uses_remaining_market_budget(self):
        store = FakeStore({
            "KR": [
                {"invest_amount": 2_000_000},
                {"invest_amount": 2_000_000},
            ]
        })

        with patch.object(run_agents.config, "KR_BUDGET", 5_000_000), \
             patch.object(run_agents.config, "SCALE_IN_STEPS", [0.5]), \
             patch.object(run_agents.config, "MIN_QTY", 1):
            quantity = run_agents._calculate_first_order_quantity(
                store=store,
                market="KR",
                current_price=100_000,
            )

        # Remaining budget is 1,000,000; first scale is 50%, so notional is 500,000.
        self.assertEqual(quantity, 5)

    def test_first_order_quantity_guarantees_min_order_quantity_when_affordable(self):
        store = FakeStore({
            "KR": [
                {"invest_amount": 3_500_000},
            ]
        })

        with patch.object(run_agents.config, "KR_BUDGET", 5_000_000), \
             patch.object(run_agents.config, "SCALE_IN_STEPS", [0.5]), \
             patch.object(run_agents.config, "MIN_QTY", 1), \
             patch.object(run_agents.config, "MIN_ORDER_QUANTITY", 2), \
             patch.object(run_agents.config, "MIN_ORDER_VALUE_KRW", 500_000):
            quantity = run_agents._calculate_first_order_quantity(
                store=store,
                market="KR",
                current_price=600_000,
            )

        self.assertEqual(quantity, 2)

    def test_first_order_quantity_skips_when_min_order_quantity_not_affordable(self):
        store = FakeStore({
            "KR": [
                {"invest_amount": 4_850_000},
            ]
        })

        with patch.object(run_agents.config, "KR_BUDGET", 5_000_000), \
             patch.object(run_agents.config, "SCALE_IN_STEPS", [1.0]), \
             patch.object(run_agents.config, "MIN_QTY", 1), \
             patch.object(run_agents.config, "MIN_ORDER_QUANTITY", 2), \
             patch.object(run_agents.config, "MIN_ORDER_VALUE_KRW", 500_000):
            quantity = run_agents._calculate_first_order_quantity(
                store=store,
                market="KR",
                current_price=100_000,
            )

        self.assertEqual(quantity, 0)

    def test_slot_capacity_blocks_late_batch_entries_after_limit_reached(self):
        store = FakeStore({
            "KR": [
                {"code": "A", "invest_amount": 1_000_000},
                {"code": "B", "invest_amount": 1_000_000},
                {"code": "C", "invest_amount": 1_000_000},
            ]
        })

        with patch.object(run_agents.config, "MAX_POSITIONS_PER_MARKET", 3):
            has_capacity = run_agents._has_position_slot_capacity(
                store=store,
                market="KR",
            )

        self.assertFalse(has_capacity)

    def test_select_replacement_slot_picks_the_weakest_slot_when_candidate_is_strong_enough(self):
        store = FakeStore(
            positions={
                "KR": [
                    {"code": "A"},
                    {"code": "B"},
                    {"code": "C"},
                ]
            },
            slots={
                "KR": {
                    "KR_A": {
                        "code": "A",
                        "name": "강한슬롯",
                        "highest_price": 0,
                        "entry_price": 100,
                        "stop_loss_price": 0,
                        "take_profit_price": 0,
                    },
                    "KR_B": {
                        "code": "B",
                        "name": "약한슬롯",
                        "entry_time": "2026-05-20T00:00:00+00:00",
                        "highest_price": 0,
                        "entry_price": 100,
                        "stop_loss_price": 0,
                        "take_profit_price": 0,
                    },
                    "KR_C": {
                        "code": "C",
                        "name": "중간슬롯",
                        "highest_price": 0,
                        "entry_price": 100,
                        "stop_loss_price": 0,
                        "take_profit_price": 0,
                    },
                }
            },
        )
        signal = {"entry_score": 80, "score": 80}

        with patch.object(run_agents.config, "MAX_POSITIONS_PER_MARKET", 3), \
             patch.object(run_agents.config, "PORTFOLIO_REBALANCE_MIN_ENTRY_SCORE", 75, create=True), \
             patch.object(run_agents.config, "PORTFOLIO_REBALANCE_MARGIN", 0.75, create=True):
            replacement = run_agents._select_replacement_slot(store, "KR", signal)

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["slot_id"], "KR_B")
        self.assertEqual(replacement["slot"]["code"], "B")
        self.assertGreater(replacement["candidate_priority"], replacement["slot_score"])

    def test_select_replacement_slot_rejects_weak_buy_scores(self):
        store = FakeStore(
            positions={"KR": [{"code": "A"}, {"code": "B"}, {"code": "C"}]},
            slots={"KR": {"KR_A": {"code": "A"}, "KR_B": {"code": "B"}, "KR_C": {"code": "C"}}},
        )

        with patch.object(run_agents.config, "MAX_POSITIONS_PER_MARKET", 3), \
             patch.object(run_agents.config, "PORTFOLIO_REBALANCE_MIN_ENTRY_SCORE", 75, create=True), \
             patch.object(run_agents.config, "PORTFOLIO_REBALANCE_MARGIN", 0.75, create=True):
            replacement = run_agents._select_replacement_slot(store, "KR", {"entry_score": 60})

        self.assertIsNone(replacement)

    def test_status_skips_us_cash_snapshot_in_virtual_mode(self):
        fake_orch = Mock()
        fake_orch.get_status.return_value = {
            "active_themes": 0,
            "last_scan": None,
            "kr_positions": 0,
            "kr_usage": 0.0,
            "us_positions": 0,
            "us_usage": 0.0,
        }
        fake_orch.get_actionable_signals.return_value = []
        fake_store = Mock()
        fake_store.load_all_positions.return_value = []
        fake_store.get_trades.return_value = []

        with patch.object(run_agents.config, "US_STOCK_ENABLED", True), \
             patch.object(run_agents.config, "KIS_MODE", "virtual"), \
             patch.object(run_agents, "DataStore", return_value=fake_store), \
             patch.object(run_agents, "Orchestrator", return_value=fake_orch), \
             patch.object(run_agents, "_get_us_cash_snapshot", side_effect=AssertionError("US cash snapshot should be skipped")):
            output = io.StringIO()
            with redirect_stdout(output):
                run_agents.show_status()

        self.assertIn("미국장 운용 모드: virtual readiness", output.getvalue())
        self.assertNotIn("미국 잔고 오류", output.getvalue())


if __name__ == "__main__":
    unittest.main()
