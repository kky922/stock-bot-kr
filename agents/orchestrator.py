"""
오케스트레이터 에이전트.
전체 파이프라인을 조율: 뉴스수집 → 테마감지 → 종목선정 → 타점검증 → 자금관리 → 실행.
"""

import json
import logging
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config
from core.data_store import DataStore
from core.news_archive import NewsArchive
from agents.market_scout import MarketScoutAgent
from agents.technical_analyst import TechnicalAnalyst
from agents.entry_analyzer import EntryAnalyzer
from agents.risk_manager_agent import RiskManagerAgent
from agents.theme_accumulator import ThemeAccumulator
from agents.market_regime import check_us_market_regime

logger = logging.getLogger(__name__)


class Orchestrator:
    """전체 트레이딩 파이프라인 오케스트레이터."""

    def __init__(self, data_store: DataStore = None):
        self.store = data_store or DataStore()
        self.archive = NewsArchive()
        self.scout = MarketScoutAgent(self.store, self.archive)
        self.tech_analyst = TechnicalAnalyst()
        self.entry_analyzer = EntryAnalyzer()
        self.risk_agent = RiskManagerAgent(self.store)
        self.theme_acc = ThemeAccumulator(self.store, self.archive)
        self._lock = threading.Lock()
        self._running = False
        # 거부 종목 쿨다운: {code: last_reject_timestamp}
        # REJECT/WEAK_BUY된 종목은 30분간 재분석 제외
        self._reject_cooldown: Dict[str, datetime] = {}
        self._REJECT_COOLDOWN_SECONDS = 1800  # 30분

    def _calculate_volume_trend(self, volumes: List[float]) -> str:
        """최근 5일 거래량이 이전 5일 대비 어떤 추세인지 판단한다."""
        if len(volumes) < 10:
            return "unknown"
        recent_5_avg = sum(volumes[-5:]) / 5
        prev_5_avg = sum(volumes[-10:-5]) / 5
        if prev_5_avg <= 0:
            return "unknown"
        if recent_5_avg > prev_5_avg * 1.2:
            return "increasing"
        if recent_5_avg < prev_5_avg * 0.8:
            return "decreasing"
        return "stable"

    def _should_prefilter_trend_volume(self, code: str, name: str, daily_data: List[Dict], tech_score: float):
        """trend/volume 조기 차단 여부를 계산한다.

        6-Layer의 Layer 2(추세)와 Layer 3(거래량)보다 살짝 느슨한 기준을 써서
        명백히 약한 후보를 더 일찍 걸러낸다.
        """
        closes = [d.get("close", 0) for d in daily_data if d.get("close", 0) > 0]
        volumes = [d.get("volume", 0) for d in daily_data if d.get("volume", 0) > 0]
        if len(closes) < 20 or len(volumes) < 20:
            return {"rejected": False, "reason": "", "reason_tag": "", "details": {}}

        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        trend_ratio = ma5 / ma20 if ma20 > 0 else 0
        trend_weak = trend_ratio < config.PREFILTER_TREND_MA_RATIO

        avg_vol_5 = sum(volumes[-5:]) / 5
        avg_vol_20 = sum(volumes[-20:]) / 20
        vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0
        vol_trend = self._calculate_volume_trend(volumes)
        volume_threshold = config.PREFILTER_VOLUME_RATIO
        if vol_trend == "increasing":
            volume_threshold = config.PREFILTER_VOLUME_RATIO_INCREASING
        elif vol_trend == "decreasing":
            volume_threshold = config.PREFILTER_VOLUME_RATIO_DECREASING
        volume_weak = vol_ratio < volume_threshold

        # 2026-06-04: trend OR volume 중 하나만 약하면 6-Layer로 보냄.
        # 둘 다 약해야 reject (본필터는 6-Layer).
        if not (trend_weak and volume_weak):
            return {
                "rejected": False,
                "reason": "",
                "reason_tag": "",
                "details": {
                    "ma5": ma5,
                    "ma20": ma20,
                    "trend_ratio": trend_ratio,
                    "avg_vol_5": avg_vol_5,
                    "avg_vol_20": avg_vol_20,
                    "vol_ratio": vol_ratio,
                    "vol_trend": vol_trend,
                    "volume_threshold": volume_threshold,
                },
            }

        parts = []
        if trend_weak:
            parts.append(f"trend: MA5={ma5:.0f} < MA20*{config.PREFILTER_TREND_MA_RATIO:.2f}={ma20 * config.PREFILTER_TREND_MA_RATIO:.0f}")
        if volume_weak:
            parts.append(
                f"volume: 5일평균 {avg_vol_5:.0f} < 20일평균*{volume_threshold:.2f}={avg_vol_20 * volume_threshold:.0f} ({vol_trend})"
            )

        logger.info(
            "  ⛡️ %s %s 선필터(trend+volume): %s, tech_score=%d < override=%d → 6-Layer 스킵",
            code,
            name,
            "; ".join(parts),
            tech_score,
            config.TECH_SCORE_OVERRIDE,
        )
        return {
            "rejected": True,
            "reason_tag": "prefilter",
            "reason": f"선필터 차단 (trend+volume, tech_score={tech_score:.0f})",
            "details": {
                "ma5": ma5,
                "ma20": ma20,
                "trend_ratio": trend_ratio,
                "avg_vol_5": avg_vol_5,
                "avg_vol_20": avg_vol_20,
                "vol_ratio": vol_ratio,
                "vol_trend": vol_trend,
                "volume_threshold": volume_threshold,
            },
        }

    def _load_autopilot_health_metrics(self) -> Dict[str, Any]:
        """최근 health 스냅샷에서 성과 지표를 읽는다.

        우선 순위:
        1) store에 캐시된 autopilot_health
        2) data/agents/autopilot_state.json
        3) data/agents/autopilot_latest.json

        실패 시 빈 dict를 반환한다.
        """
        cached = {}
        try:
            cached = self.store.safe_load("autopilot_health") or {}
        except Exception:
            cached = {}

        if isinstance(cached, dict):
            if isinstance(cached.get("metrics"), dict):
                return cached["metrics"]
            if any(k in cached for k in ("recent_win_rate_pct", "recent_realized_pnl", "open_positions")):
                return cached

        for filename in ("autopilot_state.json", "autopilot_latest.json"):
            path = ROOT_DIR / "data" / "agents" / filename
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(payload, dict):
                if isinstance(payload.get("metrics"), dict):
                    return payload["metrics"]
                if any(k in payload for k in ("recent_win_rate_pct", "recent_realized_pnl", "open_positions")):
                    return payload
        return {}

    def run_scan_pipeline(self, kis_api=None) -> List[Dict[str, Any]]:
        """전체 스캔 파이프라인 실행.

        1. 뉴스 수집 & 아카이브
        2. 메가 테마 감지
        3. 종목 후보 선정
        4. 기술 분석 순위
        5. 진입 타점 검증
        6. 자금 관리 필터
        """
        results = []

        # Step 1: 뉴스 스캔 & 아카이브
        logger.info("📡 Step 1: 뉴스 수집...")
        scan_results = self.scout.scan_all_categories()

        # Step 2: 메가 테마 감지
        logger.info("🎯 Step 2: 메가 테마 감지...")
        themes = self.theme_acc.detect_themes()
        if not themes:
            logger.info("📊 활성 메가 테마 없음")
            return results

        # Step 3 (조기종료): 양쪽 시장 모두 풀슬롯이면 후보 선정/분석 스킵
        kr_slots = self.store.get_open_slot_count("KR")
        us_slots = self.store.get_open_slot_count("US")
        max_pos = config.MAX_POSITIONS_PER_MARKET
        if kr_slots >= max_pos and us_slots >= max_pos:
            logger.info(
                "⏭️  Step 3-6 스킵: KR=%d/%d US=%d/%d 모두 풀슬롯",
                kr_slots, max_pos, us_slots, max_pos,
            )
            self.store.safe_save("last_pipeline", {
                "results": results,
                "themes": [{"theme": t["theme"], "strength": t["strength"]} for t in themes],
                "scanned_at": datetime.now().isoformat(),
            })
            return results

        # Step 4: 테마 기반 종목 후보 수집
        logger.info("📋 Step 4: 종목 후보 선정...")
        candidates = self.scout.get_theme_candidates(themes)
        if not candidates:
            self.store.safe_save("last_pipeline", {
                "results": results,
                "themes": [{"theme": t["theme"], "strength": t["strength"]} for t in themes],
                "scanned_at": datetime.now().isoformat(),
            })
            return results

        # US 비활성화 시 US 종목 후보 제외 — API 호출/기술분석/진입평가 절약
        # 2026-06-04: c.isdigit()만으로는 영문자 티커(TSLA, NVDA 등)를 못 거름.
        # market 필드 명시 체크 + 6자리 숫자 KR 코드 동시 검증으로 이중 방어.
        if not config.US_STOCK_ENABLED:
            def _is_kr_candidate(c: dict) -> bool:
                code = c.get("code", "")
                market = c.get("market", "KR")
                # market 명시 = US면 차단
                if market == "US":
                    return False
                # KR 6자리 숫자 코드면 통과
                return code.isdigit() and len(code) == 6

            kr_candidates = [c for c in candidates if _is_kr_candidate(c)]
            us_dropped = len(candidates) - len(kr_candidates)
            if us_dropped:
                logger.info("  ⏭️ US_STOCK_ENABLED=false → US 후보 %d건 제외 (KR %d건 유지)", us_dropped, len(kr_candidates))
            candidates = kr_candidates
            if not candidates:
                logger.info("📊 US 비활성화 + KR 후보 없음")
                return results

        # 테마 강도 맵 구성 (종목 → 테마강도)
        theme_strength_map = {}
        for t in themes:
            theme_name = t.get("theme", "")
            strength = t.get("strength", 0)
            theme_strength_map[theme_name] = strength

        # Step 4: 기술 분석용 일봉 선조회
        logger.info("📊 Step 4: 기술 분석...")
        kr_open = self._is_kr_market_hours()
        us_open = self._is_us_market_hours()

        # 쿨다운 만료 정리 (매 스캔 1회)
        now = datetime.now()
        reject_cooldown = getattr(self, "_reject_cooldown", {}) or {}
        self._reject_cooldown = {
            code: ts for code, ts in reject_cooldown.items()
            if (now - ts).total_seconds() < self._REJECT_COOLDOWN_SECONDS
        }

        prefiltered_candidates = []
        skipped_cooldown = 0
        for candidate in candidates:
            code = candidate["code"]
            market = "KR" if code.isdigit() else "US"
            # 쿨다운 중인 종목 스킵
            if code in self._reject_cooldown:
                remaining = self._REJECT_COOLDOWN_SECONDS - (now - self._reject_cooldown[code]).total_seconds()
                skipped_cooldown += 1
                logger.debug("  ⏳ %s 쿨다운 중 (%.0f초 남음)", code, remaining)
                continue
            risk_check = self.risk_agent.check_can_enter(code, market)
            if not risk_check.get("can_enter", False):
                logger.info(
                    "⏭️ %s %s 사전 제외: %s",
                    code,
                    candidate.get("name", ""),
                    ", ".join(risk_check.get("reasons", [])) or "risk_block",
                )
                continue
            prefiltered_candidates.append(candidate)

        if skipped_cooldown:
            logger.info("  ⏳ 쿨다운 제외: %d건 (30분 내 거부 이력)", skipped_cooldown)

        # 일봉 조회는 최종 우선순위 후보에만 수행해 API 호출/레이트리밋을 줄인다.
        fetch_candidates = prefiltered_candidates
        if kr_open ^ us_open:
            open_market = "KR" if kr_open else "US"
            fetch_candidates = [
                c for c in prefiltered_candidates
                if (c["code"].isdigit() and open_market == "KR")
                or (not c["code"].isdigit() and open_market == "US")
            ]
        elif not kr_open and not us_open:
            # 장외에는 신규 주문을 실행하지 않으므로, KIS VTS에서 반복적으로
            # 빈 응답/레이트리밋을 만드는 미국 일봉 선조회는 건너뛴다.
            # KR 일봉은 개장 전 후보 준비에 쓰일 수 있어 유지한다.
            fetch_candidates = [c for c in prefiltered_candidates if c["code"].isdigit()]

        fetch_candidates = sorted(
            fetch_candidates,
            key=lambda c: (
                c.get("selection_bias", 0),
                c.get("theme_strength", 0),
                c.get("recent_alert_count", 0),
            ),
            reverse=True,
        )[:6]

        enriched_candidates = []
        if kis_api:
            for candidate in fetch_candidates:
                code = candidate["code"]
                market = "KR" if code.isdigit() else "US"
                try:
                    if market == "KR":
                        daily_data = kis_api.get_stock_daily(code, period=60)
                    else:
                        daily_data = kis_api.get_us_stock_daily(code, period=60)
                    if daily_data:
                        candidate["daily_data"] = daily_data
                        enriched_candidates.append(candidate)
                    # Rate limit 방지: 호출 간 간격을 더 넉넉히 둬서 EGW00201 재시도를 줄임
                    time.sleep(2.0)
                except Exception as e:
                    logger.warning("  ⚠️ %s 일봉 조회 실패: %s", code, e)
        else:
            enriched_candidates = fetch_candidates

        ranked = self.tech_analyst.rank_stocks(enriched_candidates)
        if not ranked:
            logger.info("📭 기술 분석 통과 후보 없음")
            self.store.safe_save("last_pipeline", {
                "results": results,
                "themes": [{"theme": t["theme"], "strength": t["strength"]} for t in themes],
                "scanned_at": datetime.now().isoformat(),
            })
            return results

        # 같은 테마에서 리더 1개 + 보조 1개만 유지
        per_theme_counts: Dict[str, int] = {}
        filtered_ranked = []
        for candidate in ranked:
            theme_name = candidate.get("theme", "")
            count = per_theme_counts.get(theme_name, 0)
            max_per_theme = 1 if candidate.get("role") == "leader" else 2
            if count >= max_per_theme:
                continue
            per_theme_counts[theme_name] = count + 1
            filtered_ranked.append(candidate)

        kr_candidates = [c for c in filtered_ranked if c["code"].isdigit()]
        us_candidates = [c for c in filtered_ranked if not c["code"].isdigit()]

        # 열린 시장의 종목을 먼저, 닫힌 시장은 나중에
        prioritized = []
        # 2026-05-17: 3→4 상향 — 신세계(tech=86, STRONG_BUY)가 selection_bias로 4위 밀려나
        #            분석조차 못 받는 현상 방지. API 부하 1회 추가 (0.5초/스캔).
        if us_open:
            prioritized.extend(us_candidates[:4])
        if kr_open:
            prioritized.extend(kr_candidates[:4])
        # 둘 다 닫혀있으면 기존 동작 유지 (KR 우선)
        if not kr_open and not us_open:
            prioritized = ranked[:4]
        # 최대 5개까지만 분석 (API 호출 절약)
        prioritized = prioritized[:5]

        logger.info("  📊 후보 분포: KR=%d US=%d (KR장=%s US장=%s) → 분석 %d건",
                     len(kr_candidates), len(us_candidates),
                     "🟢" if kr_open else "🔴", "🟢" if us_open else "🔴",
                     len(prioritized))

        # US 시장 국면 체크 (GLM 뉴스 대체 — 정량적 모멘텀 기반)
        us_regime = check_us_market_regime()
        if us_open and us_regime.get("skip_us"):
            # 약세장: US 후보 제거
            logger.info("🔴 US 약세장 감지 — US 진입 스킵 (점수:%d)", us_regime["score"])
            us_open = False
            prioritized = [c for c in prioritized if c["code"].isdigit()]
        else:
            logger.info("🟢 US 시장 국면: %s (점수:%d, pos_size:%.1fx)",
                         us_regime["regime"], us_regime["score"], us_regime["position_size_mult"])

        # Step 5-6: 상위 후보 진입 검증
        # 같은 스캔에서 여러 후보가 동시에 통과해도 실제 주문 전 원장은 아직
        # 갱신되지 않는다. 남은 슬롯을 로컬로 예약해 MAX_POSITIONS_PER_MARKET를
        # 초과하는 actionable 신호가 한 번에 생성되지 않게 막는다.
        reserved_slots_by_market: Dict[str, int] = {}
        for candidate in prioritized:
            code = candidate["code"]
            name = candidate["name"]
            market = "KR" if code.isdigit() else "US"

            daily_data = candidate.get("daily_data", [])
            logger.info("  📊 %s 일봉 %d건 사용", code, len(daily_data))

            # 종목의 테마 강도 조회
            candidate_theme = candidate.get("theme", "")
            t_strength = theme_strength_map.get(candidate_theme, 0)

            # 기술 점수 최소값 필터: 약한 기술 지표 진입 차단
            tech_score = candidate.get("score", 0)
            if tech_score < config.TECH_SCORE_MIN:
                logger.info("  ⛔ %s %s 기술점수 미달: %d < %d — 진입 차단",
                            code, name, tech_score, config.TECH_SCORE_MIN)
                entry_result = {
                    "stock_code": code,
                    "stock_name": name,
                    "verdict": "REJECT",
                    "score": 0,
                    "layers_passed": 0,
                    "layers_total": 5,
                    "reason_tag": "low_tech_score",
                    "reason": f"기술점수 {tech_score}/{config.TECH_SCORE_MIN} 미달",
                    "analyzed_at": datetime.now().isoformat(),
                }
            else:
                # ── Pre-filter: trend/volume 6-Layer 전 조기 차단 ──
                # daily_data로 간단 계산 — 별도 API 호출 없음
                # 의도: 명백히 실패할 종목을 6-Layer 분석 전에 차단 → 필터 통계 정확성 유지
                prefilter = self._should_prefilter_trend_volume(code, name, daily_data, tech_score)
                if prefilter["rejected"]:
                    entry_result = {
                        "stock_code": code,
                        "stock_name": name,
                        "verdict": "REJECT",
                        "score": 0,
                        "layers_passed": 0,
                        "layers_total": 5,
                        "reason_tag": prefilter.get("reason_tag", "prefilter"),
                        "reason": prefilter.get("reason", f"선필터 차단 (tech_score={tech_score})"),
                        "analyzed_at": datetime.now().isoformat(),
                    }
                else:
                    entry_result = self.entry_analyzer.analyze_entry(
                        stock_code=code,
                        stock_name=name,
                        daily_data=daily_data,
                        news_score=candidate.get("news_score", 0),
                        ai_score=candidate.get("total_score", 0) / 10,
                        theme_strength=t_strength,
                        tech_score=candidate.get("score", 0),
                        atr=candidate.get("atr", 0) or 0,
                        current_price=candidate.get("current_price", 0) or 0,
                    )

            # 필터 통계 저장 — 모든 평가 결과 기록 (진입/차단 관계없음)
            self._save_filter_stats(entry_result, candidate, market, tech_score, t_strength)

            risk_check = self.risk_agent.check_can_enter(code, market)
            entry_verdict = entry_result.get("verdict")
            # 2026-05-29: WEAK_BUY 진입 차단 (virtual 포함)
            # - WEAK_BUY는 Layer 2(trend) 실패 → 기술 신호 없는 진입
            # - 가상모드에서도 low-quality 진입은 백테스트 왜곡 유발
            # - 데이터 수집은 BUY/STRONG_BUY로 충분
            _allowed_verdicts = ("STRONG_BUY", "BUY")
            if risk_check.get("can_enter", False) and entry_verdict in _allowed_verdicts:
                if "open_positions" in risk_check:
                    open_positions = int(risk_check.get("open_positions") or 0)
                else:
                    open_positions = int(self.store.get_open_slot_count(market) or 0)
                max_positions = int(risk_check.get("max_positions") or config.MAX_POSITIONS_PER_MARKET)
                reserved = reserved_slots_by_market.get(market, 0)
                if open_positions + reserved >= max_positions:
                    risk_check = dict(risk_check)
                    risk_check["can_enter"] = False
                    reasons = list(risk_check.get("reasons", []))
                    reasons.append("scan_slot_reserved")
                    risk_check["reasons"] = reasons
                else:
                    reserved_slots_by_market[market] = reserved + 1

            # REJECT/WEAK_BUY → 쿨다운 등록 (재분석 방지)
            if entry_verdict in ("REJECT", "WEAK_BUY"):
                self._reject_cooldown[code] = datetime.now()

            results.append({
                "code": code,
                "name": name,
                "market": market,
                "current_price": candidate.get("current_price", 0),
                "tech_score": candidate.get("score", 0),
                "entry_verdict": entry_verdict,
                "entry_score": entry_result.get("score", 0),
                "entry_reason": entry_result.get("reason", ""),
                "entry_reason_tag": entry_result.get("reason_tag", ""),
                "risk_can_enter": risk_check.get("can_enter", False),
                "risk_reasons": risk_check.get("reasons", []),
                "theme": candidate.get("theme", ""),
                "selection_score": candidate.get("selection_score", 0),
                "relative_strength_score": candidate.get("relative_strength_score", 0),
                "volume_score": candidate.get("volume_score", 0),
                "breakout_score": candidate.get("breakout_score", 0),
                "atr": candidate.get("atr", 0),
                "role": candidate.get("role", "watch"),
                "recent_alert_count": candidate.get("recent_alert_count", 0),
            })

            logger.info(
                "  %s %s: 기술=%d 타점=%s(%d) 자금=%s",
                code, name,
                candidate.get("score", 0),
                entry_result.get("verdict"), entry_result.get("score", 0),
                "OK" if risk_check.get("can_enter") else "BLOCK",
            )

        # 최종 점수 기준 내림차순 정렬: 고품질 진입(BUY 97)이 저품질(BUY 80)보다 먼저 처리됨
        results.sort(key=lambda r: r.get("entry_score", 80), reverse=True)

        self.store.safe_save("last_pipeline", {
            "results": results,
            "themes": [{"theme": t["theme"], "strength": t["strength"]} for t in themes],
            "us_regime": {"regime": us_regime["regime"], "score": us_regime["score"]},
            "scanned_at": datetime.now().isoformat(),
        })

        return results

    @staticmethod
    def _is_kr_market_hours() -> bool:
        """한국 장시간 체크 (KST 09:10~15:30, 평일).
        run_agents.py와 동일 기준 (09:10 시작).
        """
        now = datetime.now()
        if now.weekday() >= 5:  # 주말
            return False
        t = now.time()
        return dt_time(9, 10) <= t <= dt_time(15, 30)

    @staticmethod
    def _is_us_market_hours() -> bool:
        """미국 장시간 체크 (KST 기준).
        EDT(썸머타임, 3~11월): 22:30~05:00 KST
        EST(겨울타임, 11~3월): 23:30~06:00 KST
        run_agents.py와 동일한 정확한 매핑 사용.
        """
        now = datetime.now()
        t = now.time()
        weekday = now.weekday()

        # 썸머타임 적용
        month = now.month
        is_edt = 3 <= month <= 10
        if is_edt:
            close_t = dt_time(5, 0)
            open_t = dt_time(22, 30)
        else:
            close_t = dt_time(6, 0)
            open_t = dt_time(23, 30)

        # 정확한 KST → US 장 시간 매핑
        if weekday == 5:   # 토요일: 00:00~close_t 까지만 (금요일 US 세션 잔여)
            return t <= close_t
        if weekday == 6:   # 일요일: US 시장 닫힘 (미국은 토요일)
            return False
        if weekday == 0:   # 월요일: open_t 이후만
            return t >= open_t
        # 화~금 (weekday 1~4): open_t 이후 or close_t 이전
        return t >= open_t or t <= close_t

    def get_actionable_signals(self) -> List[Dict]:
        """실행 가능한 신호만 반환 (타점 BUY + 자금 OK, 모의투자는 WEAK_BUY 포함)."""
        last = self.store.safe_load("last_pipeline")
        results = last.get("results", [])
        allowed = ("STRONG_BUY", "BUY", "WEAK_BUY") if config.KIS_MODE == "virtual" else ("STRONG_BUY", "BUY")
        filtered = [
            r for r in results
            if r.get("entry_verdict") in allowed
            and r.get("risk_can_enter")
        ]
        # WEAK_BUY 실행 게이트: entry_score=60 고정이라 tech_score로 품질 차별화 (2026-05-29)
        weak_buy_min = float(getattr(config, "WEAK_BUY_MIN_TECH_SCORE", 14) or 14)
        health = self._load_autopilot_health_metrics()
        recent_win_rate = float(health.get("recent_win_rate_pct", 0) or 0)
        recent_realized_pnl = float(health.get("recent_realized_pnl", 0) or 0)
        open_positions = int(health.get("open_positions", 0) or 0)
        max_positions = int(getattr(config, "MAX_POSITIONS_PER_MARKET", 3) or 3)
        recent_stress = recent_win_rate < 40.0 and recent_realized_pnl < 0
        very_recent_stress = recent_win_rate < 30.0 and recent_realized_pnl < 0

        # 최근 성과가 나쁘고 슬롯 압력이 높으면 WEAK_BUY를 더 보수적으로 거른다.
        # - 최근 승률 40% 미만 + PnL 음수면 +3
        # - 최근 승률 30% 미만 + PnL 음수면 추가 +3
        # - 슬롯이 거의 찼으면 +2
        if recent_stress:
            weak_buy_min += 3
        if very_recent_stress:
            weak_buy_min += 3
        if open_positions >= max(0, max_positions - 1):
            weak_buy_min += 2
        weak_buy_min = min(weak_buy_min, 30.0)

        # WEAK_BUY는 진입 점수가 고정값이라, 최근 성과가 나쁠수록 보조 점수도 같이 본다.
        min_selection_score = 0.0
        min_relative_strength = 0.0
        min_volume_score = 0.0
        if recent_stress:
            min_selection_score = 60.0
            min_relative_strength = 2.0
            min_volume_score = 4.0
        if very_recent_stress:
            min_selection_score = max(min_selection_score, 68.0)
            min_relative_strength = max(min_relative_strength, 3.5)
            min_volume_score = max(min_volume_score, 6.0)
        if open_positions >= max(0, max_positions - 1):
            min_selection_score = max(min_selection_score, 70.0)
            min_relative_strength = max(min_relative_strength, 4.0)
            min_volume_score = max(min_volume_score, 6.0)

        before_count = len(filtered)
        filtered = [
            r for r in filtered
            if r.get("entry_verdict") != "WEAK_BUY"
            or (
                float(r.get("tech_score", 0) or 0) >= weak_buy_min
                and float(r.get("selection_score", 0) or 0) >= min_selection_score
                and float(r.get("relative_strength_score", 0) or 0) >= min_relative_strength
                and float(r.get("volume_score", 0) or 0) >= min_volume_score
            )
        ]
        if before_count != len(filtered):
            logging.info(
                "[WEAK_BUY-GATE] %d건 차단 (tech>=%.0f, sel>=%.0f, rs>=%.1f, vol>=%.1f, recent_win_rate=%.1f%%, recent_pnl=%.0f, open_positions=%d/%d, %d→%d)",
                before_count - len(filtered), weak_buy_min, min_selection_score, min_relative_strength, min_volume_score,
                recent_win_rate, recent_realized_pnl,
                open_positions, max_positions, before_count, len(filtered),
            )
        filtered.sort(
            key=lambda r: (
                float(r.get("entry_score", 0) or 0),
                float(r.get("selection_score", 0) or 0),
                float(r.get("relative_strength_score", 0) or 0),
            ),
            reverse=True,
        )
        if config.KIS_MODE == "virtual":
            max_virtual_entries = int(getattr(config, "MAX_VIRTUAL_ENTRY_CANDIDATES", 3) or 3)
            return filtered[:max(1, max_virtual_entries)]
        return filtered

    def get_status(self) -> Dict[str, Any]:
        """오케스트레이터 상태."""
        last = self.store.safe_load("last_pipeline")
        themes = self.theme_acc.get_active_themes()
        kr_summary = self.risk_agent.get_portfolio_summary("KR")
        us_summary = self.risk_agent.get_portfolio_summary("US")

        return {
            "last_scan": last.get("scanned_at"),
            "active_themes": len(themes),
            "kr_positions": kr_summary["total_positions"],
            "us_positions": us_summary["total_positions"],
            "kr_usage": kr_summary["usage_pct"],
            "us_usage": us_summary["usage_pct"],
        }

    # ── 필터 통계 추적 ──────────────────────────────────────────
    _FILTER_STATS_PATH = ROOT_DIR / "data" / "agents" / "filter_stats.json"
    _filter_stats_lock = threading.Lock()

    def _save_filter_stats(
        self,
        entry_result: Dict,
        candidate: Dict,
        market: str,
        tech_score: float,
        theme_strength: float,
    ):
        """각 평가 결과를 filter_stats.json에 추가 기록한다.

        이 데이터로 어떤 필터가 얼마나 차단하는지, 어떤 조건에서
        승인/차단되는지 추적 가능하다. 교차 분석으로 필터 임계값 최적화에 활용.
        """
        try:
            layers = entry_result.get("layers", {})
            layer_details = {}
            for layer_name, layer_data in layers.items():
                if isinstance(layer_data, dict):
                    layer_details[layer_name] = {
                        "pass": layer_data.get("pass", False),
                        "reason": layer_data.get("reason", ""),
                    }

            record = {
                "timestamp": datetime.now().isoformat(),
                "stock_code": entry_result.get("stock_code", candidate.get("code", "")),
                "stock_name": entry_result.get("stock_name", candidate.get("name", "")),
                "market": market,
                "theme": candidate.get("theme", ""),
                "selection_score": candidate.get("score", 0),
                "tech_score": tech_score,
                "tech_score_min": config.TECH_SCORE_MIN,
                "tech_score_override": config.TECH_SCORE_OVERRIDE,
                "verdict": entry_result.get("verdict", "?"),
                "entry_score": entry_result.get("score", 0),
                "reason_tag": entry_result.get("reason_tag", ""),
                "reason": entry_result.get("reason", ""),
                "layers_passed": entry_result.get("layers_passed", 0),
                "layers_total": entry_result.get("layers_total", 0),
                "layer_details": layer_details,
                "ai_score_min": config.AI_SCORE_MIN,
                "min_buy_votes": config.MIN_BUY_VOTES_FOR_BUY,
                "theme_strength": theme_strength,
                "atr": candidate.get("atr", 0),
                "current_price": candidate.get("current_price", 0),
                "price_52w_high": candidate.get("price_52w_high", 0),
                "vol_ratio": candidate.get("vol_ratio", 0),
                "override_active": entry_result.get("override_active", False),
                "buy_votes": entry_result.get("buy_votes", 0),
            }

            with self._filter_stats_lock:
                path = self._FILTER_STATS_PATH
                existing = []
                if path.exists():
                    try:
                        raw = path.read_text(encoding="utf-8").strip()
                        if raw:
                            existing = json.loads(raw)
                            if not isinstance(existing, list):
                                existing = []
                    except (json.JSONDecodeError, OSError):
                        existing = []

                existing.append(record)
                # 최근 500건만 유지 (오래된 데이터는 디스크 절약 + 성능)
                if len(existing) > 500:
                    existing = existing[-500:]

                path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("필터 통계 저장 실패 (비치명): %s", e)
