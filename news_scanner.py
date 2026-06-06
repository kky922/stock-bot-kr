""""
이슈 스캐너 모듈.
뉴스 RSS 수집 → 키워드 기반 이슈 스코어링.

[DEPRECATED 2026-05-14] GLM-5.1 API 잔고 소진으로 AI 분석 제거.
키워드 빈도 기반 fallback만 유지. 추후 외인수급/기술적 분석으로 대체 예정.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
import requests

import config

logger = logging.getLogger(__name__)


class NewsScanner:
    """뉴스 기반 이슈 스캐너."""

    def __init__(self):
        self.seen_titles: set = set()
        self.issues: List[Dict] = []

    # ── 뉴스 수집 ────────────────────────────────────────

    def _fetch_rss(self, url: str) -> List[Dict]:
        """RSS 피드에서 뉴스 수집."""
        articles = []
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                if not title or title in self.seen_titles:
                    continue
                self.seen_titles.add(title)
                articles.append({
                    "title": title,
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", title),
                    "published": entry.get("published", ""),
                    "source": url,
                })
        except Exception as e:
            logger.error("❌ RSS 수집 실패 (%s): %s", url[:50], e)
        return articles

    def collect_news(self) -> List[Dict]:
        """모든 소스에서 뉴스 수집."""
        all_articles = []
        for source in config.NEWS_SOURCES:
            articles = self._fetch_rss(source)
            all_articles.extend(articles)
            logger.info("📰 수집: %d건 (%s)", len(articles), source[:40])
        return all_articles

    # ── GLM AI 분석 ──────────────────────────────────────

    def _analyze_with_glm(self, articles: List[Dict]) -> List[Dict]:
        """GLM-5.1로 뉴스 분석 → 이슈 추출."""
        if not articles:
            return []

        # 뉴스 타이틀 요약 (최대 10개)
        titles_text = "\n".join(f"- {a['title']}" for a in articles[:10])

        prompt = f"""다음 뉴스 헤드라인을 분석해서 주식 시장에 영향을 줄 수 있는 핵심 이슈를 추출하세요.

뉴스 헤드라인:
{titles_text}

분석 기준:
1. 각 이슈에 대해 강도 점수(1~10)를 매기세요
2. 이슈 지속성을 판단하세요 (단발성/중기테마/장기테마)
3. 관련 산업/섹터를 지정하세요
4. 한국 주식시장에 미치는 영향을 평가하세요

JSON 형식으로 응답:
{{
  "issues": [
    {{
      "title": "이슈 제목",
      "score": 8,
      "persistence": "중기테마",
      "sectors": ["반도체", "방산"],
      "impact": "긍정적/부정적/중립",
      "summary": "한 줄 요약"
    }}
  ]
}}

오직 JSON만 응답하세요."""

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.ZAI_API_KEY}",
            }
            body = {
                "model": config.ZAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 2000,
            }

            resp = requests.post(
                f"{config.ZAI_BASE_URL}/chat/completions",
                headers=headers,
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

            # JSON 파싱 (코드블록 제거)
            content = re.sub(r"```json\s*|```\s*", "", content).strip()
            result = json.loads(content)
            return result.get("issues", [])

        except json.JSONDecodeError:
            logger.warning("⚠️ GLM 응답 JSON 파싱 실패 — 응답이 JSON 형식이 아님")
            return []
        except Exception as e:
            logger.error("❌ GLM 분석 실패: %s", e)
            return []

    def _validate_ai_issues(self, issues: List[Dict]) -> List[Dict]:
        """AI 응답 검증: 스키마, 점수 범위, 신뢰도 필터."""
        validated = []
        for issue in issues:
            # 필수 필드 확인
            if not issue.get("title"):
                continue
            # 점수 범위 (1~10)
            score = issue.get("score", 0)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(10, score))
            issue["score"] = score

            # 영향도 정규화
            impact = issue.get("impact", "중립")
            if impact not in ("긍정적", "부정적", "중립"):
                issue["impact"] = "중립"

            # 지속성 정규화
            persistence = issue.get("persistence", "")
            if persistence not in ("단발성", "중기테마", "장기테마"):
                issue["persistence"] = "단발성"

            # 섹터 리스트 보장
            if not isinstance(issue.get("sectors"), list):
                issue["sectors"] = []

            validated.append(issue)

        return validated

    # ── 메인 스캔 ────────────────────────────────────────

    def scan(self) -> List[Dict]:
        """키워드 기반 이슈 스캔. (GLM API 중단으로 fallback만 사용)"""
        logger.info("📰 이슈 스캔 시작...")
        articles = self.collect_news()
        if not articles:
            logger.info("📰 새 뉴스 없음")
            return []

        logger.info("📰 수집된 뉴스: %d건", len(articles))

        # GLM API 중단으로 키워드 빈도 기반 이슈 추출 (market_scout의 로직 참고)
        issues = self._keyword_issues(articles)
        if not issues:
            logger.info("📰 분석된 이슈 없음")
            return []

        for issue in issues:
            issue["timestamp"] = datetime.now(timezone.utc).isoformat()
            issue["article_count"] = len(articles)

        issues.sort(key=lambda x: x.get("score", 0), reverse=True)
        self.issues = issues

        logger.info("📰 분석 완료: %d개 이슈 (최고 점수: %d)", len(issues), issues[0].get("score", 0))
        return issues

    def _keyword_issues(self, articles: List[Dict]) -> List[Dict]:
        """GLM 없이 키워드 빈도로 이슈 추출."""
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
            h = a.get("title", "")
            for kw in keyword_map:
                if kw in h:
                    counts[kw] = counts.get(kw, 0) + 1

        issues = []
        for kw, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count < 1:
                continue
            info = keyword_map[kw]
            score = min(count * 2, 10)
            issues.append({
                "title": f"{kw} 관련 뉴스 급증 ({count}건)",
                "score": score,
                "persistence": "중기테마" if score >= 6 else "단발성",
                "sectors": info["sectors"],
                "impact": info["impact"],
                "summary": f"최근 {count}건의 {kw} 관련 뉴스 감지",
            })
        return issues

    def get_top_issue(self) -> Optional[Dict]:
        """가장 점수가 높은 이슈 반환."""
        if self.issues:
            return self.issues[0]
        return None