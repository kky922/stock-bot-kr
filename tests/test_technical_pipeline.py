import unittest

from agents.entry_analyzer import EntryAnalyzer
from agents.technical_analyst import TechnicalAnalyst


def make_daily_rows(count=30):
    rows = []
    for i in range(count):
        close = 1000 + i * 10
        rows.append({
            "date": f"202604{i + 1:02d}",
            "open": close - 5,
            "high": close + 15,
            "low": close - 15,
            "close": close,
            "volume": 100000 + i * 1000,
        })
    return rows


class TechnicalPipelineTests(unittest.TestCase):
    def test_rank_stocks_preserves_daily_data_for_entry_analysis(self):
        daily_data = make_daily_rows()
        candidates = [{
            "code": "005930",
            "name": "삼성전자",
            "daily_data": daily_data,
            "theme": "AI_반도체",
            "theme_strength": 8.5,
            "role": "leader",
            "news_score": 5,
        }]

        ranked = TechnicalAnalyst().rank_stocks(candidates)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["daily_data"], daily_data)

    def test_entry_analyzer_no_longer_receives_missing_daily_data_after_rank(self):
        daily_data = make_daily_rows()
        candidates = [{
            "code": "005930",
            "name": "삼성전자",
            "daily_data": daily_data,
            "theme": "AI_반도체",
            "theme_strength": 8.5,
            "role": "leader",
            "news_score": 5,
        }]

        signal = TechnicalAnalyst().rank_stocks(candidates)[0]
        result = EntryAnalyzer().analyze_entry(
            stock_code=signal["code"],
            stock_name=signal["name"],
            daily_data=signal.get("daily_data", []),
            news_score=signal.get("news_score", 0),
            ai_score=signal.get("total_score", 0) / 10,
            theme_strength=signal.get("theme_score", 0),
        )

        self.assertNotEqual(result["reason_tag"], "daily_data_missing")
        self.assertNotEqual(result["verdict"], "WAIT")


if __name__ == "__main__":
    unittest.main()
