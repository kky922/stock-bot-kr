import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "scripts" / "stock_bot_autopilot_health.py"
spec = importlib.util.spec_from_file_location("stock_bot_autopilot_health", MODULE_PATH)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class AutopilotHealthTests(unittest.TestCase):
    def test_log_age_threshold_is_longer_off_market(self):
        with patch.object(health, "_is_kr_market_hours", return_value=False), \
             patch.object(health, "_is_us_market_hours", return_value=False):
            self.assertEqual(health._log_age_warning_threshold_min(), 45.0)

    def test_log_age_threshold_is_shorter_during_market_hours(self):
        with patch.object(health, "_is_kr_market_hours", return_value=True), \
             patch.object(health, "_is_us_market_hours", return_value=False):
            self.assertEqual(health._log_age_warning_threshold_min(), 20.0)

    def test_valuation_estimate_no_positions(self):
        result = health._open_position_valuation_estimate(
            open_slots=[],
            invested_amount=0,
        )
        self.assertEqual(result["quality"], "no_positions")
        self.assertEqual(result["unrealized_pnl_est"], 0.0)

    def test_valuation_estimate_single_profitable_position(self):
        open_slots = [
            {
                "entry_price": 1_000_000,
                "highest_price": 1_100_000,  # +10% watermark
                "quantity": 1,
            }
        ]
        result = health._open_position_valuation_estimate(
            open_slots=open_slots,
            invested_amount=1_000_000,
        )
        # Discounted: 1,000,000 + (100,000 * 0.85) = 1,085,000
        # PnL = 85,000 → return = 8.5%
        self.assertEqual(result["quality"], "position_based_estimate")
        self.assertEqual(result["open_market_value_est"], 1_085_000.0)
        self.assertEqual(result["unrealized_pnl_est"], 85_000.0)
        self.assertEqual(result["unrealized_return_pct_est"], 8.5)
        self.assertIn("85% 수준", result["note"])

    def test_valuation_estimate_flat_position(self):
        open_slots = [
            {
                "entry_price": 1_000_000,
                "highest_price": 1_000_000,  # never rose
                "quantity": 1,
            }
        ]
        result = health._open_position_valuation_estimate(
            open_slots=open_slots,
            invested_amount=1_000_000,
        )
        # Never above entry → estimate at entry → 0 PnL
        self.assertEqual(result["quality"], "position_based_estimate")
        self.assertEqual(result["open_market_value_est"], 1_000_000.0)
        self.assertEqual(result["unrealized_pnl_est"], 0.0)
        self.assertEqual(result["unrealized_return_pct_est"], 0.0)

    def test_valuation_estimate_multiple_positions(self):
        open_slots = [
            {
                "entry_price": 1_399_000,
                "highest_price": 1_468_000,  # +4.93%
                "quantity": 1,
            },
            {
                "entry_price": 103_275,
                "highest_price": 103_500,  # +0.22%
                "quantity": 8,
            },
        ]
        invested = 1_399_000 + (103_275 * 8)  # = 2,225,200
        result = health._open_position_valuation_estimate(
            open_slots=open_slots,
            invested_amount=invested,
        )
        # Slot 1: 1,399,000 + (69,000 * 0.85) = 1,457,650 → PnL = 58,650
        # Slot 2 each: 103,275 + (225 * 0.85) = 103,466 → PnL = 191 × 8 = 1,531
        # Total PnL = 60,181 → return = 2.7%
        self.assertEqual(result["quality"], "position_based_estimate")
        self.assertAlmostEqual(result["unrealized_pnl_est"], 60_181.0, delta=5)
        self.assertAlmostEqual(result["unrealized_return_pct_est"], 2.7, delta=0.1)

    def test_empty_pipeline_candidates_are_note_when_position_slots_are_full(self):
        issues = []
        notes = []
        suggestions = []

        health._classify_empty_pipeline_candidates(
            pipeline_candidate_count=0,
            open_slot_count=5,
            max_positions_per_market=3,
            issues=issues,
            notes=notes,
            suggestions=suggestions,
        )

        self.assertNotIn("최근 파이프라인 후보가 비어 있음", issues)
        self.assertIn("파이프라인 후보 0건은 포지션 슬롯 초과 상태에서 예상됨", notes)
        self.assertNotIn("뉴스 수집/테마 감지 입력이 정상인지 확인", suggestions)

    def test_position_slot_over_limit_is_issue_not_note(self):
        issues = []
        notes = []
        suggestions = []

        health._classify_position_slot_usage(
            open_slot_count=5,
            max_positions_per_market=3,
            issues=issues,
            notes=notes,
            suggestions=suggestions,
        )

        self.assertIn("포지션 슬롯 초과: 5/3", issues)
        self.assertNotIn("포지션 슬롯이 5개 사용 중", notes)
        self.assertIn("신규 진입 차단 상태 유지 및 슬롯 초과 원인 점검", suggestions)

    def test_position_slot_full_is_note_when_at_limit(self):
        issues = []
        notes = []
        suggestions = []

        health._classify_position_slot_usage(
            open_slot_count=3,
            max_positions_per_market=3,
            issues=issues,
            notes=notes,
            suggestions=suggestions,
        )

        self.assertEqual([], issues)
        self.assertIn("포지션 슬롯이 3/3개 사용 중", notes)
        self.assertIn("신규 진입보다 기존 포지션 관리 우선", suggestions)

    def test_signature_includes_position_over_limit_state(self):
        base = dict(
            stock_alive=True,
            dashboard_alive=True,
            agent_age_min=1.0,
            notable_errors={},
            consecutive_losses=0,
            recent_win_rate=70.0,
            latest_trade_time="2026-05-14T10:47:50+00:00",
            repeat_losers=[],
        )

        within_limit = health._build_signature(open_slots=[{}, {}, {}], max_positions_per_market=3, **base)
        over_limit = health._build_signature(open_slots=[{}, {}, {}, {}], max_positions_per_market=3, **base)

        self.assertFalse(within_limit["position_over_limit"])
        self.assertTrue(over_limit["position_over_limit"])
        self.assertNotEqual(within_limit, over_limit)


if __name__ == "__main__":
    unittest.main()
