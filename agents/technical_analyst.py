"""
복수 종목 기술 분석 에이전트.
후보 종목들의 기술적 상태와 상대강도를 분석하여 순위를 매깁니다.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from technical import TechnicalAnalyzer

logger = logging.getLogger(__name__)


class TechnicalAnalyst:
    """복수 종목 기술 분석."""

    def __init__(self):
        self.tech = TechnicalAnalyzer()

    def _relative_strength(self, daily_data: List[Dict]) -> Dict[str, float]:
        closes = [d.get("close", 0) for d in daily_data if d.get("close", 0) > 0]
        if len(closes) < 20:
            return {"rs_3d": 0.0, "rs_5d": 0.0, "high20_proximity": 0.0, "high60_proximity": 0.0}

        current = closes[-1]
        rs_3d = ((current / closes[-4]) - 1) * 100 if len(closes) >= 4 and closes[-4] > 0 else 0.0
        rs_5d = ((current / closes[-6]) - 1) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0.0
        high20 = max(closes[-20:])
        high60 = max(closes[-60:]) if len(closes) >= 60 else max(closes)
        high20_proximity = ((current / high20) - 1) * 100 if high20 > 0 else 0.0
        high60_proximity = ((current / high60) - 1) * 100 if high60 > 0 else 0.0
        return {
            "rs_3d": round(rs_3d, 2),
            "rs_5d": round(rs_5d, 2),
            "high20_proximity": round(high20_proximity, 2),
            "high60_proximity": round(high60_proximity, 2),
        }

    def _volume_score(self, daily_data: List[Dict], vol_ratio: float) -> float:
        recent_values = []
        for d in daily_data[-5:]:
            close = d.get("close", 0)
            volume = d.get("volume", 0)
            recent_values.append(close * volume)
        avg_value = sum(recent_values) / len(recent_values) if recent_values else 0
        score = 0.0
        if vol_ratio >= 2.0:
            score += 18
        elif vol_ratio >= 1.3:
            score += 12
        elif vol_ratio >= 1.0:
            score += 7
        elif vol_ratio >= 0.7:
            score += 3

        if avg_value >= 20_000_000_000:
            score += 8
        elif avg_value >= 5_000_000_000:
            score += 4
        return score

    def analyze_stock(self, code: str, name: str, daily_data: List[Dict], candidate: Dict[str, Any]) -> Dict[str, Any]:
        """단일 종목 기술 분석."""
        if not daily_data or len(daily_data) < 20:
            return {
                "code": code,
                "name": name,
                "score": 0,
                "selection_score": 0,
                "signal": "hold",
                "reason": "데이터 부족",
                "eligible": False,
            }

        result = self.tech.analyze_all(daily_data)
        current_price = daily_data[-1].get("close", 0)
        rs = self._relative_strength(daily_data)
        ma_signal = result.get("details", {}).get("ma_cross", {})
        volume = result.get("details", {}).get("volume", {})
        bollinger = result.get("details", {}).get("bollinger", {})
        momentum = result.get("details", {}).get("momentum", {})

        score = 0.0
        reasons = []

        if ma_signal.get("signal") == "buy":
            score += 20
            reasons.append("골든크로스")
        elif ma_signal.get("trend") == "up":
            score += 14
            reasons.append("상승추세")

        rsi_val = result.get("details", {}).get("rsi", {}).get("rsi", 50)
        if 35 <= rsi_val <= 65:
            score += 8
            reasons.append(f"RSI균형({rsi_val:.0f})")
        elif rsi_val < 35:
            score += 5
            reasons.append(f"RSI과매도({rsi_val:.0f})")

        vol_ratio = volume.get("vol_ratio", 0)
        volume_score = self._volume_score(daily_data, vol_ratio)
        score += volume_score
        if vol_ratio >= 1.2:
            reasons.append(f"거래량({vol_ratio:.1f}x)")

        score += max(0.0, min(18.0, rs["rs_3d"] * 2.0))
        score += max(0.0, min(14.0, rs["rs_5d"] * 1.4))
        score += max(0.0, 8.0 + rs["high20_proximity"] * 1.5)
        score += max(0.0, 6.0 + rs["high60_proximity"] * 0.8)

        if bollinger.get("pct_b", 0.5) >= 0.55:
            score += 6
            reasons.append("상단밴드근접")

        if momentum.get("pattern") in ("strong_bull", "hammer"):
            score += 8
            reasons.append(momentum.get("pattern"))

        if result.get("chase_warning"):
            score -= 10
            reasons.append("추격위험")

        role = candidate.get("role", "watch")
        selection_score = score
        if role == "leader":
            selection_score += 8
        elif role == "momentum":
            selection_score += 4

        if candidate.get("market_cap_bucket") == "large":
            selection_score -= 2.5

        selection_score += candidate.get("selection_bias", 0)
        selection_score += max(0.0, candidate.get("theme_strength", 0) * 1.2)

        selection_score -= candidate.get("repeat_penalty", 0)
        selection_score -= candidate.get("entry_failure_penalty", 0)
        selection_score -= candidate.get("stopout_penalty", 0)

        return {
            "code": code,
            "name": name,
            "score": round(max(0.0, min(100.0, score)), 2),
            "selection_score": round(max(0.0, min(120.0, selection_score)), 2),
            "signal": result.get("final_signal", "hold"),
            "confidence": result.get("confidence", 0),
            "current_price": current_price,
            "reasons": reasons,
            "atr": result.get("details", {}).get("atr", {}).get("value"),
            "support_resistance": result.get("details", {}).get("support_resistance", {}),
            "relative_strength_score": round(max(0.0, min(40.0, rs["rs_3d"] * 2.0 + rs["rs_5d"] * 1.4)), 2),
            "volume_score": round(volume_score, 2),
            "breakout_score": round(max(0.0, 8.0 + rs["high20_proximity"] * 1.5 + rs["high60_proximity"] * 0.8), 2),
            "eligible": result.get("final_signal", "hold") in ("buy", "hold") and current_price > 0,
            "analyzed_at": datetime.now().isoformat(),
        }

    def rank_stocks(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """실제 일봉 데이터가 있는 후보만 기술 분석으로 순위화."""
        results = []
        for c in candidates:
            daily_data = c.get("daily_data", [])
            if not daily_data or len(daily_data) < 20:
                logger.info("⏭️ 기술순위 제외: %s %s (일봉 부족)", c.get("code"), c.get("name"))
                continue
            analysis = self.analyze_stock(c["code"], c["name"], daily_data, c)
            if not analysis.get("eligible"):
                continue
            analysis["daily_data"] = daily_data
            analysis["news_score"] = c.get("news_score", 0)
            analysis["theme"] = c.get("theme", "")
            analysis["role"] = c.get("role", "watch")
            analysis["market_cap_bucket"] = c.get("market_cap_bucket", "mid")
            analysis["recent_alert_count"] = c.get("recent_alert_count", 0)
            analysis["recent_entry_count"] = c.get("recent_entry_count", 0)
            analysis["recent_stopout_count"] = c.get("recent_stopout_count", 0)
            analysis["last_recommended_at"] = c.get("last_recommended_at")
            analysis["theme_score"] = round(c.get("theme_strength", 0), 2)
            analysis["selection_bias"] = round(c.get("selection_bias", 0), 2)
            analysis["total_score"] = round(
                analysis["selection_score"] * 0.8 + c.get("theme_strength", 0) * 2.0,
                2,
            )
            results.append(analysis)

        results.sort(key=lambda x: (x["total_score"], x["selection_score"]), reverse=True)
        return results
