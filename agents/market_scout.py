"""
Market Scout Agent — 시장 정찰 에이전트.
국내/미국 뉴스를 5개 영역으로 통합 스캔하고 시장 신호를 생성합니다.
뉴스 축적 아카이브에 저장하여 테마 감지 에이전트와 연동합니다.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
import requests

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config

# 선택적 임포트 — MessageBus가 없어도 동작
try:
    from agents.base_agent import BaseAgent, AgentStatus
    from core.message_bus import MessageBus, AgentMessage, MessageType
    _HAS_BUS = True
except ImportError:
    _HAS_BUS = False

logger = logging.getLogger(__name__)


# 종목코드 매핑 (종목명 → 코드)
STOCK_CODE_MAP = {
    "삼성전자": "005930", "SK하이닉스": "000660", "한미반도체": "042700",
    "리노공업": "058470", "한국항공우주": "047810", "한화에어로스페이스": "012450",
    "LIG넥스원": "079550", "풍산": "272210", "LG에너지솔루션": "373220",
    "삼성SDI": "006400", "에코프로비엠": "247540", "삼성바이오로직스": "207940",
    "셀트리온": "068270", "HD현대일렉트릭": "267260", "NAVER": "035420",
    "LX세미콘": "108320", "루닛": "328130", "씨젠": "096530",
    "SK바이오팜": "326030", "현대차": "005380",
}


class MarketScoutAgent(BaseAgent if _HAS_BUS else object):
    """시장 정찰 에이전트 — 뉴스 수집 → AI 분석 → MarketSignal 생성."""

    def __init__(self, data_store=None, news_archive=None, message_bus=None):
        if _HAS_BUS and message_bus:
            super().__init__("market_scout", message_bus)
            self.bus.subscribe(self.name, MessageType.SYSTEM_COMMAND)
        self._loop_interval = config.NEWS_SCAN_INTERVAL
        self.seen_titles: set = set()
        self.last_signals: List[Dict] = []

        # 뉴스 축적 아카이브
        if news_archive:
            self.archive = news_archive
        else:
            from core.news_archive import NewsArchive
            self.archive = NewsArchive()

        # 데이터 스토어
        self.store = data_store
        self.theme_db = self._load_theme_db()

    def process(self):
        """주기적으로 뉴스 스캔 → 신호 생성."""
        msg = self.receive_message(timeout=1.0)
        if msg and msg.msg_type == MessageType.SYSTEM_COMMAND:
            if msg.data.get("command") == "scan":
                self._run_scan()

        # 자동 스캔 (루프 간격 = NEWS_SCAN_INTERVAL)
        self.status = AgentStatus.PROCESSING
        self._run_scan()

    def _run_scan(self):
        """전체 스캔 파이프라인."""
        logger.info("🔍 Market Scout 스캔 시작...")

        # 1. 국내 뉴스 수집 + 분석
        kr_issues = self._scan_market("KR")

        # 2. 미국 뉴스 수집 + 분석
        # 2026-06-04: US_STOCK_ENABLED=false면 US 스캔을 아예 호출 안 함 (이중 방어).
        # 1차 방어: orchestrator의 market/code 게이트. 2차 방어: 스캔 단계에서 차단.
        us_issues = self._scan_market("US") if config.US_STOCK_ENABLED else []

        all_issues = kr_issues + us_issues
        if not all_issues:
            logger.info("🔍 탐지된 이슈 없음")
            return

        all_issues.sort(key=lambda x: x.get("score", 0), reverse=True)
        self.last_signals = all_issues

        # 3. 상위 이슈를 Technical Analyst에게 전달
        for issue in all_issues[:3]:
            self.send_message(
                msg_type=MessageType.MARKET_SIGNAL,
                data=issue,
                target="technical_analyst",
            )
            logger.info("📡 신호 전송: [%s] %s (점수:%d)",
                        issue.get("market", "?"),
                        issue.get("title", "")[:40],
                        issue.get("score", 0))

    def _scan_market(self, market: str) -> List[Dict]:
        """시장별 스캔."""
        sources = config.NEWS_SOURCES_KR if market == "KR" else config.NEWS_SOURCES_US
        articles = self._collect_news(sources, market)

        if not articles:
            return []

        issues = self._analyze_with_ai(articles, market)

        # 메타데이터 추가
        for issue in issues:
            issue["market"] = market
            issue["timestamp"] = datetime.now(timezone.utc).isoformat()
            issue["article_count"] = len(articles)

        return issues

    def _collect_news(self, sources: List[str], market: str) -> List[Dict]:
        """RSS 피드에서 뉴스 수집."""
        all_articles = []
        for url in sources:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:15]:
                    title = entry.get("title", "").strip()
                    if not title or title in self.seen_titles:
                        continue
                    self.seen_titles.add(title)
                    all_articles.append({
                        "title": title,
                        "link": entry.get("link", ""),
                        "summary": entry.get("summary", title),
                        "published": entry.get("published", ""),
                        "source": url[:50],
                        "market": market,
                    })
            except Exception as e:
                logger.error("❌ RSS 수집 실패 (%s): %s", url[:40], e)
        return all_articles

    def _analyze_with_ai(self, articles: List[Dict], market: str) -> List[Dict]:
        """키워드 기반 뉴스 분석 → 이슈 추출. (GLM API 중단으로 fallback만 사용)"""
        if not articles:
            return []
        # GLM API 잔고 소진 (2026-05-14). 키워드 빈도 기반 fallback 사용
        return self._keyword_fallback_issues(articles)

    def _keyword_fallback_issues(self, articles: List[Dict]) -> List[Dict]:
        """GLM 실패 시 키워드 빈도 기반 간단 이슈 추출.

        # [Claude Fix] GLM 타임아웃/오류 시에도 뉴스 이슈가 0이 되지 않도록
        # 키워드 빈도 기반 fallback 분석 추가.
        """
        keyword_map = {
            "반도체": {"sectors": ["반도체"], "impact": "긍정적"},
            "AI": {"sectors": ["AI", "소프트웨어"], "impact": "긍정적"},
            "인공지능": {"sectors": ["AI", "소프트웨어"], "impact": "긍정적"},
            "텅스텐": {"sectors": ["소재", "방산"], "impact": "긍정적"},
            "방산": {"sectors": ["방산"], "impact": "긍정적"},
            "금리": {"sectors": ["금융", "부동산"], "impact": "중립"},
            "관세": {"sectors": ["무역", "자동차"], "impact": "부정적"},
            "배터리": {"sectors": ["2차전지", "전기차"], "impact": "긍정적"},
            "바이오": {"sectors": ["바이오", "제약"], "impact": "긍정적"},
        }
        counts: Dict[str, int] = {}
        for a in articles:
            h = a.get("title", "") if isinstance(a, dict) else str(a)
            for kw in keyword_map:
                if kw in h:
                    counts[kw] = counts.get(kw, 0) + 1

        issues = []
        for kw, cnt in sorted(counts.items(), key=lambda x: -x[1])[:5]:
            if cnt >= 2:
                info = keyword_map[kw]
                issues.append({
                    "title": f"{kw} 관련 이슈 ({cnt}건)",
                    "score": min(5 + cnt, 9),
                    "persistence": "단기테마",
                    "sectors": info["sectors"],
                    "impact": info["impact"],
                    "summary": f"뉴스 {cnt}건에서 {kw} 키워드 감지 (GLM fallback)",
                    "suggested_stocks": [],
                })
        return issues

    def scan_all_categories(self) -> Dict[str, List[Dict]]:
        """5영역 RSS를 모두 스캔하여 아카이브에 저장."""
        results = {}
        for category, sources in config.NEWS_SOURCES_BY_CATEGORY.items():
            articles = self._collect_news(sources, category)
            if articles:
                # 아카이브에 저장
                added = self.archive.add_news(category, articles)
                results[category] = {
                    "collected": len(articles),
                    "archived": added,
                }
                logger.info("📰 [%s] %d건 수집, %d건 신규", category, len(articles), added)
        return results

    def _load_theme_db(self) -> Dict[str, Any]:
        db_path = ROOT_DIR / "data" / "theme_db.json"
        try:
            return json.loads(db_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("⚠️ theme_db 로드 실패: %s", e)
            return {"themes": {}}

    def _classify_role(self, index: int) -> str:
        if index == 0:
            return "leader"
        if index <= 2:
            return "momentum"
        return "watch"

    def _market_cap_bucket(self, market: str, index: int) -> str:
        if market == "US":
            return "large" if index < 2 else "mid"
        if index < 2:
            return "large"
        if index < 6:
            return "mid"
        return "small"

    def _recent_penalties(self, code: str, market: str) -> Dict[str, float]:
        recommendations = self.store.get_recommendations(limit=100) if self.store else []
        trades = self.store.get_trades(limit=100) if self.store else []

        recent_recommendations = [
            r for r in recommendations[-config.RECOMMENDATION_LOOKBACK:]
            if r.get("code") == code and r.get("market") == market
        ]
        recent_trade_failures = [
            r for r in recommendations[-config.RECOMMENDATION_LOOKBACK:]
            if r.get("code") == code and r.get("market") == market and r.get("outcome") == "order_fail"
        ]
        recent_stopouts = [
            t for t in trades[-20:]
            if t.get("code") == code and t.get("market") == market and "손절" in t.get("reason", "")
        ]
        repeat_penalty = max(0, len(recent_recommendations) - 1) * config.RECOMMENDATION_REPEAT_PENALTY
        entry_failure_penalty = len(recent_trade_failures) * config.ENTRY_FAILURE_PENALTY
        stopout_penalty = len(recent_stopouts) * config.STOP_OUT_PENALTY
        return {
            "recent_alert_count": len(recent_recommendations),
            "recent_entry_count": sum(1 for t in trades[-20:] if t.get("code") == code and t.get("market") == market and t.get("action") == "buy"),
            "recent_stopout_count": len(recent_stopouts),
            "repeat_penalty": repeat_penalty,
            "entry_failure_penalty": entry_failure_penalty,
            "stopout_penalty": stopout_penalty,
            "last_recommended_at": recent_recommendations[-1].get("timestamp") if recent_recommendations else None,
        }

    def get_theme_candidates(self, themes: List[Dict]) -> List[Dict]:
        """테마 기반 종목 후보 생성."""
        candidates = []
        seen_codes = set()

        for theme in themes:
            theme_name = theme["theme"]
            db_theme = self.theme_db.get("themes", {}).get(theme_name, {})
            kr_stocks = db_theme.get("stocks_krx", [])
            us_stocks = db_theme.get("stocks_us", [])

            # 메타가 없으면 기존 로직 fallback
            if not kr_stocks:
                kr_stocks = [{"code": code, "name": self._code_to_name(code)} for code in theme.get("related_stocks_kr", [])]
            if not us_stocks:
                us_stocks = [{"code": code, "name": code} for code in theme.get("related_stocks_us", [])]

            for idx, stock in enumerate(kr_stocks[:4]):
                code = stock.get("code", "")
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                penalties = self._recent_penalties(code, "KR")
                base_priority = 15 - idx * 1.5
                role = stock.get("role") or self._classify_role(idx)
                canonical_name = self._code_to_name(code)
                if canonical_name == code:
                    canonical_name = stock.get("name") or code
                if role == "leader":
                    base_priority += 6
                elif role == "momentum":
                    base_priority += 2
                candidates.append({
                    "code": code,
                    "name": canonical_name,
                    "theme": theme_name,
                    "theme_strength": theme["strength"],
                    "theme_article_count": theme.get("article_count", 0),
                    "news_score": theme["strength"],
                    "daily_data": [],
                    "role": role,
                    "market_cap_bucket": stock.get("market_cap_bucket") or self._market_cap_bucket("KR", idx),
                    "selection_bias": round(base_priority - penalties["repeat_penalty"] - penalties["entry_failure_penalty"] - penalties["stopout_penalty"], 2),
                    **penalties,
                })
            for idx, stock in enumerate(us_stocks[:4]):
                code = stock.get("code", "")
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                penalties = self._recent_penalties(code, "US")
                base_priority = 15 - idx * 1.4
                role = stock.get("role") or self._classify_role(idx)
                if role == "leader":
                    base_priority += 6
                elif role == "momentum":
                    base_priority += 2
                candidates.append({
                    "code": code,
                    "name": stock.get("name") or code,
                    "theme": theme_name,
                    "theme_strength": theme["strength"],
                    "theme_article_count": theme.get("article_count", 0),
                    "news_score": theme["strength"],
                    "daily_data": [],
                    "role": role,
                    "market_cap_bucket": stock.get("market_cap_bucket") or self._market_cap_bucket("US", idx),
                    "selection_bias": round(base_priority - penalties["repeat_penalty"] - penalties["entry_failure_penalty"] - penalties["stopout_penalty"], 2),
                    **penalties,
                })

        return candidates

    def _code_to_name(self, code: str) -> str:
        """종목코드 → 종목명."""
        reverse_map = {v: k for k, v in STOCK_CODE_MAP.items()}
        return reverse_map.get(code, code)

    def get_fresh_signals(self, category: str = None) -> List[Dict]:
        """신선한 뉴스 신호만 반환."""
        if category:
            return self.archive.get_fresh_news(category)
        all_fresh = []
        for cat in config.NEWS_SOURCES_BY_CATEGORY:
            all_fresh.extend(self.archive.get_fresh_news(cat))
        return all_fresh

    def force_scan(self) -> List[Dict]:
        """수동 스캔 트리거."""
        self._run_scan()
        return self.last_signals
