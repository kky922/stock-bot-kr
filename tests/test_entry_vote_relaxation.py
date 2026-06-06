"""
Regression tests for MIN_BUY_VOTES_FOR_BUY=1 + Layer 2 trend 가드 완화 (2026-06-04).

배경:
- 기존 (2026-05-29): MIN_BUY_VOTES_FOR_BUY=2 + trend_pass = buy_votes>=2
- 변경 (2026-06-04): MIN_BUY_VOTES_FOR_BUY=1 + trend_pass = buy_votes>=1
- 이유: filter_stats 500건 분석 결과 1표 buy 118건이 모두 REJECT되는 병목 해소

검증:
1. tech_signal="buy" + buy_votes=1 → trend layer PASS
2. tech_signal="hold" + buy_votes=0 + score>=25 → override 미작동 (0표는 안전)
3. tech_signal="hold" + buy_votes=1 + score<25 → trend FAIL
4. tech_signal="hold" + buy_votes=1 + score>=25 → override 작동 → trend PASS
"""
import unittest
from unittest.mock import patch

import config
from agents.entry_analyzer import EntryAnalyzer


def make_rising_daily_rows(count=30):
    """상승 추세 일봉 데이터"""
    rows = []
    base = 1000
    for i in range(count):
        close = base + i * 15
        rows.append({
            "date": f"202604{i + 1:02d}",
            "open": close - 5,
            "high": close + 20,
            "low": close - 10,
            "close": close,
            "volume": 100000 + i * 2000,
        })
    return rows


def make_flat_daily_rows(count=30):
    """횡보 일봉 데이터"""
    rows = []
    for i in range(count):
        close = 1000 + (i % 5) * 2
        rows.append({
            "date": f"202604{i + 1:02d}",
            "open": close,
            "high": close + 3,
            "low": close - 3,
            "close": close,
            "volume": 100000,
        })
    return rows


class EntryVoteRelaxationTests(unittest.TestCase):
    """2026-06-04: MIN_BUY_VOTES_FOR_BUY=1 + Layer 2 votes>=1 완화 회귀 테스트"""

    def setUp(self):
        self.analyzer = EntryAnalyzer()
        self._orig_min_votes = config.MIN_BUY_VOTES_FOR_BUY
        self._orig_override = config.TECH_SCORE_OVERRIDE
        config.MIN_BUY_VOTES_FOR_BUY = 1
        config.TECH_SCORE_OVERRIDE = 25.0

    def tearDown(self):
        config.MIN_BUY_VOTES_FOR_BUY = self._orig_min_votes
        config.TECH_SCORE_OVERRIDE = self._orig_override

    def test_trend_pass_with_1_vote_and_buy_signal(self):
        """buy_votes=1 + tech_signal='buy' → trend layer PASS (기존 FAIL → 변경 PASS)"""
        daily = make_rising_daily_rows()
        with patch.object(self.analyzer.tech, "analyze_all") as mock_sig:
            mock_sig.return_value = {
                "final_signal": "buy",
                "confidence": 0.5,
                "buy_votes": 1,
                "sell_votes": 0,
                "chase_warning": False,
                "details": {},
            }
            result = self.analyzer.analyze_entry("005930", "삼성전자", daily, tech_score=20.0)
            trend = result.get("layers", {}).get("trend", {})
            self.assertTrue(
                trend.get("pass"),
                f"1 vote + buy signal should pass (no override needed), got: {trend}"
            )

    def test_override_blocks_zero_vote_even_with_high_score(self):
        """buy_votes=0 + score>=25 → override 차단 (votes<min_override=1)"""
        daily = make_flat_daily_rows()
        with patch.object(self.analyzer.tech, "analyze_all") as mock_sig:
            mock_sig.return_value = {
                "final_signal": "hold",
                "confidence": 0.0,
                "buy_votes": 0,
                "sell_votes": 0,
                "chase_warning": False,
                "details": {},
            }
            result = self.analyzer.analyze_entry("005930", "삼성전자", daily, tech_score=30.0)
            trend = result.get("layers", {}).get("trend", {})
            self.assertFalse(
                trend.get("pass"),
                f"0 votes + score=30 should NOT pass (no override), got: {trend}"
            )
            # override 미작동 → reason에 "오버라이드" 단어가 없어야 함
            self.assertNotIn("오버라이드", trend.get("reason", ""))
            self.assertFalse(result.get("override_active"))

    def test_override_works_with_1_vote_and_high_score(self):
        """buy_votes=1 + score>=25 + hold signal → override 작동 → trend PASS"""
        daily = make_flat_daily_rows()
        with patch.object(self.analyzer.tech, "analyze_all") as mock_sig:
            mock_sig.return_value = {
                "final_signal": "hold",
                "confidence": 0.0,
                "buy_votes": 1,
                "sell_votes": 0,
                "chase_warning": False,
                "details": {},
            }
            result = self.analyzer.analyze_entry("005930", "삼성전자", daily, tech_score=30.0)
            trend = result.get("layers", {}).get("trend", {})
            self.assertTrue(
                trend.get("pass"),
                f"1 vote + score=30 should pass via override, got: {trend}"
            )

    def test_trend_fails_with_0_vote_0_signal(self):
        """buy_votes=0 + signal=hold + score<25 → trend FAIL (모든 조건 부족)"""
        daily = make_flat_daily_rows()
        with patch.object(self.analyzer.tech, "analyze_all") as mock_sig:
            mock_sig.return_value = {
                "final_signal": "hold",
                "confidence": 0.0,
                "buy_votes": 0,
                "sell_votes": 0,
                "chase_warning": False,
                "details": {},
            }
            result = self.analyzer.analyze_entry("005930", "삼성전자", daily, tech_score=10.0)
            trend = result.get("layers", {}).get("trend", {})
            self.assertFalse(
                trend.get("pass"),
                f"0 votes + score=10 should FAIL, got: {trend}"
            )


if __name__ == "__main__":
    unittest.main()
