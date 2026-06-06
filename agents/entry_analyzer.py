"""
6-Layer 진입 타점 검증 에이전트.
뉴스 신호 → 기술적 확인 → 거래량 → 모멘텀 → 지지/저항 → 변동성 순으로 검증합니다.

Layer 6 (변동성): ATR/가격 비율이 ENTRY_MAX_ATR_RATIO를 초과하면 진입 차단.
- 기본값 8.0%: KR 활동주 자연 ATR(6~8%)은 6-Layer까지 보내고, 10%+ 극단 변동성은 차단.
- 변동성 완화 후에도 Layer 2/3/4/5가 본필터로 남아 진입 품질을 제한한다.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config
from technical import TechnicalAnalyzer

logger = logging.getLogger(__name__)


class EntryAnalyzer:
    """5-Layer 진입 타점 검증."""

    def __init__(self):
        self.tech = TechnicalAnalyzer()

    def analyze_entry(
        self,
        stock_code: str,
        stock_name: str,
        daily_data: list,
        news_score: float = 0,
        ai_score: float = 0,
        theme_strength: float = 0,
        tech_score: float = 0,
        atr: float = 0,
        current_price: float = 0,
    ) -> Dict[str, Any]:
        """6-Layer 진입 검증.

        Layer 1: 뉴스/AI 신호 품질
        Layer 2: 기술적 추세 확인
        Layer 3: 거래량 확인
        Layer 4: 모멘텀 & 추격 위험
        Layer 5: 지지/저항 위치
        Layer 6: 변동성 (ATR/가격 비율)
        """
        # 일봉 데이터 없으면 기술 분석 불가 → 진입 거부
        if not daily_data:
            logger.info("⛔ %s (%s) 일봉 데이터 없음 — 진입 보류", stock_name, stock_code)
            return {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "verdict": "WAIT",
                "score": 0,
                "layers_passed": 0,
                "layers_total": 5,
                "layers": {},
                "reason_tag": "daily_data_missing",
                "reason": "일봉 데이터 없음",
                "analyzed_at": datetime.now().isoformat(),
            }

        layers = {}

        # ── Layer 1: 뉴스/AI 신호 ──
        signal_score = max(news_score, ai_score)
        # 테마 후보로 선정된 종목은 테마 강도를 신호 점수 하한선으로 보정
        if signal_score < theme_strength / 2.0:
            signal_score = theme_strength / 2.0
            logger.info("  🔄 %s 신호점수 보정: 테마강도 %.1f → 신호 %.1f", stock_name, theme_strength, signal_score)
        layers["signal"] = {
            "news_score": news_score,
            "ai_score": ai_score,
            "theme_strength": theme_strength,
            "best_score": signal_score,
            "pass": signal_score >= config.AI_SCORE_MIN,
            "reason": f"신호점수 {signal_score:.1f}/10 (최소 {config.AI_SCORE_MIN})",
        }

        # ── Layer 2: 기술적 추세 ──
        tech_result = self.tech.analyze_all(daily_data)
        tech_signal = tech_result.get("final_signal", "hold")
        tech_confidence = tech_result.get("confidence", 0)
        buy_votes = tech_result.get("buy_votes", 0)

        # "buy" + 최소 1표 동시 충족 시 통과 (2026-06-04 완화)
        # 기존 (2026-05-29): buy_votes >= 2 — 1표짜리 118건이 모두 REJECT되는 병목
        # 변경: buy_votes >= 1 — technical.py의 5개 전략 중 1개라도 buy면 통과
        # 안전장치: tech_score 오버라이드 경로도 votes >= 1 + score >= 25로 좁힘
        # 근거: filter_stats 500건 분석 결과 43%(131건)가 REJECT, 그중 90%가 trend layer fail.
        #       단일지표 합의를 강제하면 매매 기회 자체가 사라짐. Layer 3-6이 여전히 활성 안전망.
        trend_pass = tech_signal == "buy" and buy_votes >= 1

        # 기술 점수 오버라이드: 연속형 점수(0-100)로 이산형 투표 부족 보완.
        # 2026-06-04부터 buy_votes >= 1 가드 (기존 2에서 완화). 점수만 높은 0표 후보는 Layer 2 실패 유지.
        override_active = False
        if not trend_pass and tech_score >= config.TECH_SCORE_OVERRIDE:
            # 2026-06-04: buy_votes >= 1 로 완화.
            # 근거: 1표 buy와 동일한 안전장치 레벨. score >= 25 + votes >= 1이면 override 활성.
            #       0표는 테마/연속형 점수가 높아도 기술적 합의 0이므로 Layer 2 실패 유지.
            min_override_votes = 1
            if buy_votes >= min_override_votes:
                trend_pass = True
                override_active = True
                # Override 시 이산형 투표(buy_votes) 대신 연속형 점수(tech_score)로 confidence 보정
                tech_confidence = max(tech_confidence, buy_votes / 4.0, tech_score / 100.0)
                logger.info(
                    "  🔄 %s 기술점수 오버라이드: score=%d votes=%d/%d(min_override=%d) → Layer 2 통과",
                    stock_name, tech_score, buy_votes, config.MIN_BUY_VOTES_FOR_BUY, min_override_votes,
                )
            else:
                logger.info(
                    "  ⛔ %s 기술점수 오버라이드 차단: score=%d votes=%d/%d — buy_votes<%d → Layer 2 실패",
                    stock_name, tech_score, buy_votes, config.MIN_BUY_VOTES_FOR_BUY, min_override_votes,
                )

        layers["trend"] = {
            "signal": tech_signal,
            "confidence": tech_confidence,
            "pass": trend_pass,
            "reason": f"기술적 {tech_signal} (신뢰 {tech_confidence:.0%})"
                      + (f" ← 기술점수 오버라이드({tech_score}점)" if override_active else ""),
            "details": tech_result,
        }

        # ── Layer 3: 거래량 ──
        # vol_trend 반영: increasing이면 기준 완화(0.6x), decreasing이면 강화(1.0x)
        vol_result = self.tech.volume_analysis(daily_data)
        vol_ratio = vol_result.get("vol_ratio", 0)
        vol_trend = vol_result.get("vol_trend", "stable")
        base_threshold = config.ENTRY_MIN_VOL_RATIO  # 0.8
        if vol_trend == "increasing":
            vol_threshold = base_threshold * 0.75  # 0.6 — 거래량 증가 추세이면 진입 기회 확대
        elif vol_trend == "decreasing":
            vol_threshold = base_threshold * 1.25  # 1.0 — 거래량 감소 추세는 리스크
        else:
            vol_threshold = base_threshold
        vol_pass = vol_ratio >= vol_threshold
        layers["volume"] = {
            "vol_ratio": vol_ratio,
            "vol_trend": vol_trend,
            "vol_threshold": round(vol_threshold, 2),
            "pass": vol_pass,
            "reason": f"거래량 {vol_ratio:.1f}x(추세:{vol_trend}) 최소 {vol_threshold:.2f}x",
        }

        # ── Layer 4: 모멘텀 & 추격 위험 ──
        momentum = self.tech.intraday_momentum(daily_data)
        day_change = momentum.get("day_change_pct", 0)
        chase_warning = momentum.get("chase_warning", False)
        has_momentum_data = momentum.get("has_data", len(daily_data) >= 2)
        # 단기하락추세 감지: 현재가 < 5일SMA * 0.993 → 하락 채널 진입 차단 (강화: -1.0% → -0.7%)
        current_close = daily_data[-1].get("close", 0) if daily_data else 0
        ma5 = None
        ma_cross_result = tech_result.get("ma_cross", {})
        if ma_cross_result.get("ma5") and current_close > 0:
            ma5 = ma_cross_result["ma5"]
            price_below_ma5_pct = (current_close - ma5) / ma5 * 100
        else:
            price_below_ma5_pct = 0  # 데이터 부족 시 통과
        downtrend_warning = ma5 is not None and price_below_ma5_pct < -0.7
        layers["momentum"] = {
            "day_change_pct": day_change,
            "pattern": momentum.get("pattern", "neutral"),
            "chase_warning": chase_warning,
            "downtrend_warning": downtrend_warning,
            "price_below_ma5_pct": round(price_below_ma5_pct, 2),
            "pass": has_momentum_data and not chase_warning and not downtrend_warning
                    and day_change <= config.ENTRY_MAX_CHASE_PCT,
            "reason": f"당일 {day_change:+.1f}% (한계 {config.ENTRY_MAX_CHASE_PCT}%)"
                      + (" ⚠️추격위험" if chase_warning else "")
                      + (f" ⬇️하락추세(5일SMA 대비 {price_below_ma5_pct:+.1f}%)" if downtrend_warning else ""),
        }

        # ── Layer 5: 지지/저항 ──
        sr = self.tech.support_resistance(daily_data)
        support = sr.get("support") or 0  # [Claude Fix] None 방어 — 일봉 데이터 없을 때 None 반환됨
        resistance = sr.get("resistance") or 0
        position = sr.get("position", "mid")
        current_price = daily_data[-1].get("close", 0) if daily_data else 0

        # 현재가가 지지선 근처면 OK, 저항선 근처면 주의
        near_support = (support > 0 and current_price > 0
                        and (current_price - support) / current_price * 100 < config.ENTRY_SUPPORT_TOLERANCE)
        near_resistance = position == "near_resistance"
        layers["sr"] = {
            "support": support,
            "resistance": resistance,
            "position": position,
            "pass": not near_resistance or near_support,
            "reason": f"지지={support:.0f} 저항={resistance:.0f} 위치={position}",
        }

        # ── Layer 6: 변동성 (ATR/가격 비율) ──
        # ATR이 현재가 대비 ENTRY_MAX_ATR_RATIO를 초과하면 진입 차단.
        # KR 시장은 6~8% ATR이 자연범위라 기본값 8.0으로 운용한다.
        # 10%+ 극단 변동성은 계속 차단하고, Layer 2/3/4/5가 진입 품질을 제한한다.
        atr_ratio = (atr / current_price * 100) if atr > 0 and current_price > 0 else 0
        vol_pass = atr_ratio <= config.ENTRY_MAX_ATR_RATIO
        layers["volatility"] = {
            "atr": round(atr, 0),
            "current_price": current_price,
            "atr_ratio_pct": round(atr_ratio, 2),
            "pass": vol_pass,
            "reason": f"ATR {atr:,.0f}/{current_price:,.0f}={atr_ratio:.1f}% (최대 {config.ENTRY_MAX_ATR_RATIO}%)"
                      + ("" if vol_pass else " ⚠️변동성 과다"),
        }

        # ── 최종 판정 ──
        passed_layers = sum(1 for v in layers.values() if v.get("pass", False))
        total_layers = len(layers)

        if passed_layers == total_layers:
            if override_active and buy_votes < config.MIN_BUY_VOTES_FOR_BUY:
                # Override로 5/5 도달 시 STRONG_BUY(100) 금지
                # 4/5와 동일 confidence_bonus 로직 사용 (5/5가 4/5보다 낮은 점수 방지)
                # tech_score confidence 보정으로 고기술점수 종목 차별화
                confidence_bonus = int(max(0, min(tech_confidence, 1.0)) * 17)
                score = 80 + min(confidence_bonus, 17)
                verdict = "BUY"
                layers["trend"]["reason"] += f" ← 오버라이드(5/5→BUY({score}), buy_votes<{config.MIN_BUY_VOTES_FOR_BUY})"
            else:
                verdict = "STRONG_BUY"
                score = 100
        elif passed_layers >= total_layers - 1:
            # Layer 2(trend)가 실패하면 기술 신호가 없음 → 강등
            # BUY(80→97)는 Layer 2 통과 시에만 부여, 실패 시 WEAK_BUY(60)
            trend_pass = layers.get("trend", {}).get("pass", False)
            tech_signal = layers.get("trend", {}).get("signal", "hold")
            if trend_pass:
                verdict = "BUY"
                # 기술 신뢰도 기반 차별화: buy_votes 2→4 기준 confidence 0.5→1.0
                # 80점 기준점에 최대 +17점 보너스 → BUY(92~97) 범위
                confidence_bonus = int(max(0, min(tech_confidence, 1.0)) * 17)
                score = 80 + min(confidence_bonus, 17)
            else:
                # WEAK_BUY(60): Layer 2(trend) 실패 → 기술 신호 없는 진입
                # 참고: WEAK_BUY는 run_agents.py에서 실행 차단 (STRONG_BUY/BUY만 실행)
                # WEAK_BUY는 로그/모니터링용 정보성 판정 (실제 매수 불가)
                verdict = "WEAK_BUY"
                score = 60
                layers["trend"]["reason"] += " → Layer 2 실패로 WEAK_BUY 강등"
        elif passed_layers >= total_layers - 2:
            # 4/6 레이어 통과: 기본 WEAK_BUY(60)
            # override_active 시: 기술점수 기준(TECH_SCORE_OVERRIDE=40)을
            # 충족했으므로 BUY(80)로 승격. Override가 WEAK_BUY block과
            # 상충되어 사문화되는 구조적 문제 해결 (2026-05-19)
            if override_active:
                verdict = "BUY"
                score = 80
                layers["trend"]["reason"] += \
                    f" ← 오버라이드(4/6→BUY({score}))"
            else:
                verdict = "WEAK_BUY"
                score = 60
        else:
            verdict = "REJECT"
            score = 0

        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "verdict": verdict,
            "score": score,
            "layers_passed": passed_layers,
            "layers_total": total_layers,
            "layers": layers,
            "reason_tag": "layer_evaluation",
            "reason": f"{passed_layers}/{total_layers} 레이어 통과",
            "analyzed_at": datetime.now().isoformat(),
            "override_active": override_active,
            "buy_votes": buy_votes,
        }