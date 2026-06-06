"""
종목 매칭 모듈.
이슈 키워드 → 테마 DB → 관련 종목 매핑 + 기술적 진입 타이밍 분석.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import config
from technical import TechnicalAnalyzer

logger = logging.getLogger(__name__)


class StockMatcher:
    """이슈 → 종목 매칭 엔진."""

    def __init__(self, kis_api):
        self.kis_api = kis_api
        self.tech = TechnicalAnalyzer()
        self.theme_db = self._load_theme_db()

    def _load_theme_db(self) -> Dict:
        """테마-종목 매핑 DB 로드."""
        try:
            with open(config.THEME_DB_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("⚠️ theme_db.json 로드 실패 — 빈 DB 사용")
            return {"themes": {}}

    def match_issue_to_stocks(self, issue: Dict) -> List[Dict]:
        """이슈를 관련 KRX 종목 리스트로 매핑.

        Args:
            issue: news_scanner가 분석한 이슈 딕셔너리

        Returns:
            매칭된 종목 리스트 (기술적 분석 결과 포함)
        """
        sectors = issue.get("sectors", [])
        score = issue.get("score", 0)
        impact = issue.get("impact", "중립")

        if score < 5 or impact == "부정적":
            logger.info("⛔ 이슈 점수 낮음(%d) 또는 부정적 영향 — 스킵", score)
            return []

        matched = []
        themes = self.theme_db.get("themes", {})

        for theme_name, theme_data in themes.items():
            # 이슈 섹터와 테마 키워드 매칭
            keywords = theme_data.get("keywords", [])
            theme_matched = False
            for sector in sectors:
                for kw in keywords:
                    if sector.lower() in kw.lower() or kw.lower() in sector.lower():
                        theme_matched = True
                        break
                if theme_matched:
                    break

            if not theme_matched:
                continue

            # 매칭된 테마의 KRX 종목 분석
            for stock in theme_data.get("stocks_krx", []):
                stock_code = stock.get("code", "")
                stock_name = stock.get("name", "")

                try:
                    # 일봉 데이터로 기술적 분석
                    daily = self.kis_api.get_stock_daily(stock_code, period=30)
                    if not daily or len(daily) < 5:
                        continue

                    analysis = self.tech.analyze_all(daily)
                    price_info = self.kis_api.get_stock_price(stock_code)

                    matched.append({
                        "code": stock_code,
                        "name": stock_name,
                        "theme": theme_name,
                        "issue_score": score,
                        "tech_signal": analysis.get("final_signal", "hold"),
                        "tech_confidence": analysis.get("confidence", 0),
                        "buy_votes": analysis.get("buy_votes", 0),
                        "current_price": price_info.get("current", 0),
                        "change_rate": price_info.get("change_rate", 0),
                        "volume": price_info.get("volume", 0),
                    })

                    logger.info(
                        "  📊 %s(%s): 신호=%s, 투표=%d, 가격=%d",
                        stock_name, stock_code,
                        analysis.get("final_signal", "hold"),
                        analysis.get("buy_votes", 0),
                        price_info.get("current", 0),
                    )

                except Exception as e:
                    logger.error("❌ %s 분석 실패: %s", stock_name, e)

        # 우선순위 정렬: 이슈점수*기술신호 가중치
        def sort_key(s):
            issue_w = s.get("issue_score", 0) * 0.4
            tech_w = s.get("buy_votes", 0) * 10 * 0.6
            return issue_w + tech_w

        matched.sort(key=sort_key, reverse=True)
        return matched

    def find_best_stock(self, issue: Dict) -> Optional[Dict]:
        """이슈에 대해 최적의 진입 종목 1개 선택."""
        candidates = self.match_issue_to_stocks(issue)
        if not candidates:
            return None

        best = candidates[0]
        # 진입 조건: 기술적 신호가 buy 또는 watch
        if best.get("tech_signal") in ("buy", "watch"):
            return best

        logger.info("📊 최고 종목 %s — 기술적 신호 %s (진입 조건 미충족)",
                     best.get("name"), best.get("tech_signal"))
        return None