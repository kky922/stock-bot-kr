"""
뉴스 축적 아카이브.
수집된 뉴스를 영역별로 저장·중복제거·신선도 관리합니다.
"""

import json
import hashlib
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
import config

logger = logging.getLogger(__name__)


class NewsArchive:
    """뉴스 축적 및 신선도 관리 (스레드 세이프)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._archive_dir = config.DATA_DIR / "news_archive"
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    def _hash(self, title: str, source: str) -> str:
        """뉴스 중복 판별용 해시."""
        return hashlib.md5(f"{title}|{source}".encode()).hexdigest()[:12]

    def _category_file(self, category: str) -> Path:
        return self._archive_dir / f"{category}.json"

    def _load_category(self, category: str) -> Dict[str, Any]:
        f = self._category_file(category)
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("⚠️ 뉴스 아카이브 로드 실패 (%s): %s — 빈 데이터로 초기화", category, e)  # [Claude Fix]
        return {"items": [], "hashes": []}

    def _save_category(self, category: str, data: Dict[str, Any]):
        self._category_file(category).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def add_news(self, category: str, news_items: List[Dict[str, Any]]) -> int:
        """영역에 뉴스 추가 — 중복 제거 후 저장. 추가된 건수 반환."""
        added = 0
        with self._lock:
            data = self._load_category(category)
            existing_hashes = set(data.get("hashes", []))
            items = data.get("items", [])

            for item in news_items:
                h = self._hash(item.get("title", ""), item.get("source", ""))
                if h in existing_hashes:
                    continue
                existing_hashes.add(h)
                item["_hash"] = h
                item["_category"] = category
                item["_archived_at"] = datetime.now(timezone.utc).isoformat()
                items.append(item)
                added += 1

            # 신선도 기준으로 정렬 (최신 우선), 최대 500건 유지
            items.sort(key=lambda x: x.get("_archived_at", ""), reverse=True)
            data["items"] = items[:500]
            data["hashes"] = list(existing_hashes)
            self._save_category(category, data)

        return added

    def get_fresh_news(self, category: str, max_age_seconds: int = None) -> List[Dict]:
        """신선한 뉴스만 반환 (기본: config.NEWS_FRESHNESS_THRESHOLD)."""
        if max_age_seconds is None:
            max_age_seconds = config.NEWS_FRESHNESS_THRESHOLD
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)

        with self._lock:
            data = self._load_category(category)

        fresh = []
        for item in data.get("items", []):
            pub = item.get("published_at", item.get("_archived_at", ""))
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt >= cutoff:
                    fresh.append(item)
            except Exception:
                fresh.append(item)  # 날짜 파싱 실패 시 포함 (정상 동작)
        return fresh

    def get_accumulated_signals(self, category: str, lookback_hours: int = 24) -> Dict[str, Any]:
        """영역별 축적 시그널 — 키워드 빈도, 테마 강도, 관련 종목."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        with self._lock:
            data = self._load_category(category)

        items = []
        for item in data.get("items", []):
            pub = item.get("published_at", item.get("_archived_at", ""))
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt >= cutoff:
                    items.append(item)
            except Exception:
                pass  # 날짜 파싱 실패 시 제외 (정상 동작)

        # [Claude Fix] 키워드 빈도 집계 — item["keywords"] 필드가 없으면 title에서 직접 추출
        _THEME_KEYWORDS = [
            "AI", "인공지능", "반도체", "NVIDIA", "GPU", "HBM", "TSMC",
            "방산", "방위", "미사일", "NATO",
            "배터리", "2차전지", "리튬", "전기차", "EV",
            "바이오", "신약", "FDA", "헬스케어", "제약",
            "로봇", "자동화", "휴머노이드",
            "양자", "quantum",
            "관세", "tariff", "무역", "chip", "반도체", "semiconductor",
            "텅스텐", "tungsten", "공급망", "supply chain",
        ]
        keyword_freq: Dict[str, int] = {}
        source_freq: Dict[str, int] = {}
        for item in items:
            # 저장된 keywords 필드 우선 사용
            for kw in item.get("keywords", []):
                keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
            # keywords 필드 없으면 title에서 직접 추출
            if not item.get("keywords"):
                title = item.get("title", "").lower()
                for kw in _THEME_KEYWORDS:
                    if kw.lower() in title:
                        keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
            source = item.get("source", "")
            if source:
                source_freq[source] = source_freq.get(source, 0) + 1

        # 관련 섹터 집계
        sector_freq: Dict[str, int] = {}
        for item in items:
            for sec in item.get("sectors", []):
                sector_freq[sec] = sector_freq.get(sec, 0) + 1

        # 평균 AI 점수
        scores = [item.get("score", 0) for item in items if item.get("score")]
        avg_score = sum(scores) / len(scores) if scores else 0
        unique_sources = len(source_freq)
        repeated_keywords = sum(max(0, freq - 1) for freq in keyword_freq.values())
        freshness_values = []
        for item in items:
            pub = item.get("published_at", item.get("_archived_at", ""))
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                age_hours = max(0.0, (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600)
                freshness_values.append(max(0.0, 1.0 - age_hours / max(1, lookback_hours)))
            except Exception:
                freshness_values.append(0.3)
        freshness_score = sum(freshness_values) / len(freshness_values) if freshness_values else 0
        keyword_density = (sum(keyword_freq.values()) / max(1, len(items)))
        source_diversity = unique_sources / max(1, len(items))

        return {
            "category": category,
            "lookback_hours": lookback_hours,
            "total_items": len(items),
            "avg_score": round(avg_score, 1),
            "keyword_freq": dict(sorted(keyword_freq.items(), key=lambda x: -x[1])[:20]),
            "source_freq": dict(sorted(source_freq.items(), key=lambda x: -x[1])[:10]),
            "sector_freq": dict(sorted(sector_freq.items(), key=lambda x: -x[1])[:10]),
            "unique_sources": unique_sources,
            "keyword_density": round(keyword_density, 2),
            "freshness_score": round(freshness_score, 3),
            "source_diversity": round(source_diversity, 3),
            "repeated_keywords": repeated_keywords,
            "theme_strength": min(10.0, len(items) * 0.3 + avg_score * 0.5),
        }

    def get_all_categories_summary(self) -> Dict[str, Dict]:
        """전체 영역 요약."""
        result = {}
        for cat in config.NEWS_SOURCES_BY_CATEGORY:
            result[cat] = self.get_accumulated_signals(cat)
        return result

    def cleanup_old(self, max_age_days: int = 7):
        """오래된 뉴스 정리."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        with self._lock:
            for cat in config.NEWS_SOURCES_BY_CATEGORY:
                data = self._load_category(cat)
                items = data.get("items", [])
                new_items = []
                new_hashes = []
                for item in items:
                    pub = item.get("published_at", item.get("_archived_at", ""))
                    try:
                        pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        if pub_dt >= cutoff:
                            new_items.append(item)
                            new_hashes.append(item.get("_hash", ""))
                    except Exception:
                        pass
                data["items"] = new_items
                data["hashes"] = new_hashes
                self._save_category(cat, data)
        logger.info("🧹 뉴스 아카이브 정리 완료 (%d일 이전 삭제)", max_age_days)
