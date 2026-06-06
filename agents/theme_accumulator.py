"""
메가 테마 누적 감지 에이전트.
5개 영역의 뉴스 축적 데이터에서 메가 테마를 감지합니다.
같은 키워드/섹터가 여러 영역에서 반복 등장하면 테마로 승격합니다.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config
from core.data_store import DataStore
from core.news_archive import NewsArchive

logger = logging.getLogger(__name__)


def _load_theme_keywords():
    """theme_db.json에서 테마 키워드 로드 + 하드코딩 fallback."""
    # 하드코딩 기본값 (fallback)
    _hardcoded = {
        "AI_반도체": {
            "keywords": ["AI", "인공지능", "반도체", "NVIDIA", "NVDA", "GPU", "HBM", "TSMC", "파운드리"],
            "sectors": ["반도체", "AI"],
            "related_stocks_kr": ["005930", "000660", "042700", "058470"],
            "related_stocks_us": ["NVDA", "AMD", "AVGO", "INTC"],
        },
        "방산_군사": {
            "keywords": ["방산", "방위", "미사일", "전투기", "군사", "무기", "NATO"],
            "sectors": ["방산", "군수"],
            "related_stocks_kr": ["047810", "012450", "079550", "272210"],
            "related_stocks_us": ["LMT", "RTX", "NOC", "BA"],
        },
        "2차전지_배터리": {
            "keywords": ["2차전지", "배터리", "리튬", "전기차", "EV", "양극재", "전해액"],
            "sectors": ["2차전지", "배터리"],
            "related_stocks_kr": ["373220", "006400", "247540", "086520"],
            "related_stocks_us": ["TSLA", "RIVN", "SQM", "ALB"],
        },
        "바이오_헬스케어": {
            "keywords": ["바이오", "신약", "임상", "FDA", "헬스케어", "제약", "의료"],
            "sectors": ["바이오", "제약", "의료"],
            "related_stocks_kr": ["207940", "326030", "068270", "145020"],
            "related_stocks_us": ["JNJ", "PFE", "MRNA", "LLY"],
        },
        "로봇_자동화": {
            "keywords": ["로봇", "자동화", "휴머노이드", "공장자동화", "테슬라봇"],
            "sectors": ["로봇", "자동화"],
            "related_stocks_kr": ["267260", "108320", "090460", "223220"],
            "related_stocks_us": ["ABB", "FANUY", "ISRG"],
        },
        "양자컴퓨팅": {
            "keywords": ["양자", "quantum", "슈퍼컴퓨터", "qubit"],
            "sectors": ["양자", "컴퓨팅"],
            "related_stocks_kr": ["035420", "036570", "307980"],
            "related_stocks_us": ["IBM", "GOOG", "MSFT", "IONQ"],
        },
    }

    # theme_db.json에서 추가 테마 로드 시도
    try:
        import json as _json
        _db_path = Path(__file__).parent.parent / "data" / "theme_db.json"
        if _db_path.exists():
            with open(_db_path, encoding="utf-8") as f:
                db = _json.load(f)
            themes_dict = db.get("themes", {})
            loaded = 0
            for theme_name, theme_data in themes_dict.items():
                if not isinstance(theme_data, dict):
                    continue
                # theme_db.json 키 → THEME_KEYWORDS 호환 포맷으로 변환
                keywords = theme_data.get("keywords", [])
                sectors = theme_data.get("sectors", [])
                kr_stocks = [s.get("code") for s in theme_data.get("stocks_krx", []) if isinstance(s, dict) and s.get("code")]
                us_stocks = [s.get("code") for s in theme_data.get("stocks_us", []) if isinstance(s, dict) and s.get("code")]

                if keywords:  # 키워드가 있어야 테마로 등록
                    _hardcoded[theme_name] = {
                        "keywords": keywords,
                        "sectors": sectors,
                        "related_stocks_kr": kr_stocks,
                        "related_stocks_us": us_stocks,
                    }
                    loaded += 1
            logger.info("📋 theme_db.json에서 %d개 테마 로드 완료 (총 %d개)", loaded, len(_hardcoded))
    except Exception as e:
        logger.warning("⚠️ theme_db.json 로드 실패, 하드코딩 사용: %s", e)

    return _hardcoded


THEME_KEYWORDS = _load_theme_keywords()


class ThemeAccumulator:
    """메가 테마 누적 감지."""

    def __init__(self, data_store: DataStore = None, news_archive: NewsArchive = None):
        self.store = data_store or DataStore()
        self.archive = news_archive or NewsArchive()

    def detect_themes(self) -> List[Dict[str, Any]]:
        """5영역 축적 데이터에서 메가 테마 감지."""
        themes = []

        # 전체 영역 요약
        all_signals = self.archive.get_all_categories_summary()

        for theme_name, theme_def in THEME_KEYWORDS.items():
            theme_score = self._calculate_theme_score(theme_def, all_signals)
            if theme_score["total_strength"] >= config.THEME_STRENGTH_MIN:
                themes.append({
                    "theme": theme_name,
                    "strength": theme_score["total_strength"],
                    "categories_hit": theme_score["categories_hit"],
                    "keyword_matches": theme_score["keyword_matches"],
                    "article_count": theme_score.get("article_count", 0),
                    "source_score": theme_score.get("source_score", 0),
                    "freshness": theme_score.get("freshness", 0),
                    "density": theme_score.get("density", 0),
                    "related_stocks_kr": theme_def["related_stocks_kr"],
                    "related_stocks_us": theme_def["related_stocks_us"],
                    "detected_at": datetime.now().isoformat(),
                })

        # 강도 순 정렬
        themes.sort(key=lambda x: x["strength"], reverse=True)

        # 저장
        if themes:
            self.store.save_theme_state({
                "themes": themes,
                "detected_at": datetime.now().isoformat(),
                "total_themes": len(themes),
            })
            logger.info("🎯 감지된 메가 테마: %s",
                        ", ".join(f"{t['theme']}({t['strength']:.1f})" for t in themes[:3]))

        return themes

    def _calculate_theme_score(
        self, theme_def: Dict, all_signals: Dict[str, Dict]
    ) -> Dict[str, Any]:
        """테마 점수 계산 — 여러 영역에서 얼마나 등장했는지."""
        keywords = theme_def["keywords"]
        total_strength = 0.0
        categories_hit = 0
        keyword_matches = {}
        total_articles = 0
        unique_source_score = 0.0
        freshness_score = 0.0
        density_score = 0.0
        repetition_penalty = 0.0

        for cat_name, cat_signals in all_signals.items():
            cat_freq = cat_signals.get("keyword_freq", {})
            cat_total_items = cat_signals.get("total_items", 0)
            cat_unique_sources = cat_signals.get("unique_sources", 0)
            cat_density = cat_signals.get("keyword_density", 0.0)
            cat_freshness = cat_signals.get("freshness_score", 0.0)
            cat_repeated = cat_signals.get("repeated_keywords", 0)

            # 테마 키워드가 이 영역에서 얼마나 등장했는지
            matches = 0
            for kw in keywords:
                kw_lower = kw.lower()
                for cat_kw, freq in cat_freq.items():
                    if kw_lower in cat_kw.lower() or cat_kw.lower() in kw_lower:
                        matches += freq
                        keyword_matches[kw] = keyword_matches.get(kw, 0) + freq

            if matches > 0:
                categories_hit += 1
                total_articles += cat_total_items
                unique_source_score += min(cat_unique_sources, 5) * 0.45
                freshness_score += cat_freshness * 2.2
                density_score += min(cat_density, 4.0) * 0.8
                repetition_penalty += min(cat_repeated * 0.12, 1.5)
                total_strength += min(matches, 6) * 0.75

        if categories_hit == 0:
            return {
                "total_strength": 0.0,
                "categories_hit": 0,
                "keyword_matches": {},
                "article_count": 0,
                "source_score": 0.0,
                "freshness": 0.0,
                "density": 0.0,
            }

        category_bonus = categories_hit * 0.9
        article_score = min(total_articles, 10) * 0.18
        total_strength = total_strength + category_bonus + article_score + unique_source_score + freshness_score + density_score
        total_strength -= repetition_penalty
        total_strength = max(0.0, total_strength)
        # 포화 완화: 최대 9.6 부근에서 완만해지도록 압축
        compressed = (total_strength / (6.5 + total_strength)) * 10.0

        return {
            "total_strength": round(min(compressed, 9.6), 1),
            "categories_hit": categories_hit,
            "keyword_matches": keyword_matches,
            "article_count": total_articles,
            "source_score": round(unique_source_score, 2),
            "freshness": round(freshness_score, 2),
            "density": round(density_score, 2),
        }

    def get_active_themes(self) -> List[Dict[str, Any]]:
        """저장된 활성 테마 조회."""
        state = self.store.load_theme_state()
        return state.get("themes", [])

    def get_theme_for_stock(self, stock_code: str, market: str = "KR") -> Optional[Dict]:
        """특정 종목이 속한 테마 찾기."""
        for theme in self.get_active_themes():
            if market == "KR" and stock_code in theme.get("related_stocks_kr", []):
                return theme
            if market == "US" and stock_code in theme.get("related_stocks_us", []):
                return theme
        return None
