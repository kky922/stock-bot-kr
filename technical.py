"""
기술적 분석 모듈.
이평선 크로스, RSI, 갭 전략, 변동성 돌파 전략을 제공합니다.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

import config  # MIN_BUY_VOTES_FOR_BUY 임포트

logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """기술적 분석 엔진."""

    @staticmethod
    def _sma(data: List[float], period: int) -> List[float]:
        """단순이동평균 (Simple Moving Average)."""
        result = []
        for i in range(len(data)):
            if i < period - 1:
                result.append(None)
            else:
                result.append(sum(data[i - period + 1:i + 1]) / period)
        return result

    @staticmethod
    def _ema(data: List[float], period: int) -> List[float]:
        """지수이동평균 (Exponential Moving Average)."""
        multiplier = 2.0 / (period + 1)
        result = [data[0]]
        for price in data[1:]:
            result.append((price - result[-1]) * multiplier + result[-1])
        return result

    def rsi(self, closes: List[float], period: int = 14) -> List[float]:
        """RSI (Relative Strength Index)."""
        if len(closes) < period + 1:
            return []

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0, d) for d in deltas]
        losses = [max(0, -d) for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_values = []
        for i in range(period, len(deltas)):
            if i == period:
                avg_gain = sum(gains[:period]) / period
                avg_loss = sum(losses[:period]) / period
            else:
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period

            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

        return rsi_values

    # ── 전략 1: 이평선 크로스 ─────────────────────────────

    def ma_cross_signal(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """5일/20일 이동평균선 크로스 신호.

        Returns:
            {"signal": "buy"|"sell"|"hold", "ma5": float, "ma20": float}
        """
        if len(daily_data) < 20:
            return {"signal": "hold", "reason": "데이터 부족 (20일 필요)"}

        closes = [d["close"] for d in daily_data]
        ma5 = self._sma(closes, 5)
        ma20 = self._sma(closes, 20)

        current_ma5 = ma5[-1]
        current_ma20 = ma20[-1]
        prev_ma5 = ma5[-2]
        prev_ma20 = ma20[-2]

        if current_ma5 is None or current_ma20 is None:
            return {"signal": "hold", "reason": "이평선 계산 불가"}

        # 골든크로스: 5일선이 20일선 위로 돌파 (신규 또는 지속적 상승추세)
        if prev_ma5 <= prev_ma20 and current_ma5 > current_ma20:
            signal = "buy"
            reason = f"골든크로스 (MA5:{current_ma5:.0f} > MA20:{current_ma20:.0f})"
        elif current_ma5 > current_ma20 * 1.02:
            # 지속적 상승추세: MA5가 MA20보다 2% 이상 높으면 크로스 이후에도 buy 신호 유지
            # (2026-05-18 fix: 기존엔 크로스 당일만 buy, 이후 hold로 떨어져서 30일간 51% 상승한
            #  삼성전자도 buy_votes=0 → 기술점수 21점 → TECH_SCORE_MIN 차단되는 구조적 문제 해결)
            gap_pct = (current_ma5 / current_ma20 - 1.0) * 100
            signal = "buy"
            reason = f"상승추세 지속 (MA5:{current_ma5:.0f} > MA20:{current_ma20:.0f}, +{gap_pct:.1f}%)"
        # 데드크로스: 5일선이 20일선 아래로 돌파
        elif prev_ma5 >= prev_ma20 and current_ma5 < current_ma20:
            signal = "sell"
            reason = f"데드크로스 (MA5:{current_ma5:.0f} < MA20:{current_ma20:.0f})"
        elif current_ma5 < current_ma20 * 0.98:
            gap_pct = (1.0 - current_ma5 / current_ma20) * 100
            signal = "sell"
            reason = f"하락추세 지속 (MA5:{current_ma5:.0f} < MA20:{current_ma20:.0f}, -{gap_pct:.1f}%)"
        else:
            signal = "hold"
            reason = f"MA5:{current_ma5:.0f} vs MA20:{current_ma20:.0f}"

        return {
            "signal": signal,
            "reason": reason,
            "ma5": current_ma5,
            "ma20": current_ma20,
            "trend": "up" if current_ma5 > current_ma20 else "down",
        }

    # ── 전략 2: 갭 상승 + 거래량 폭발 ──────────────────────

    def gap_signal(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """갭 상승 + 거래량 폭발 신호.

        조건: 오늘 시가 > 어제 고가 (갭상승) AND 오늘 거래량 > 어제 거래량 * 1.5
        """
        if len(daily_data) < 2:
            return {"signal": "hold", "reason": "데이터 부족"}

        today = daily_data[-1]
        yesterday = daily_data[-2]

        today_open = today.get("open", 0)
        yesterday_high = yesterday.get("high", 0)
        today_vol = today.get("volume", 0)
        yesterday_vol = yesterday.get("volume", 0)

        gap = today_open - yesterday_high
        gap_pct = (gap / yesterday_high * 100) if yesterday_high > 0 else 0
        vol_ratio = (today_vol / yesterday_vol) if yesterday_vol > 0 else 0

        is_gap_up = gap > 0
        is_vol_surge = vol_ratio >= 1.5

        if is_gap_up and is_vol_surge:
            return {
                "signal": "buy",
                "reason": f"갭상승 + 거래량폭발 (갭:{gap_pct:+.1f}%, 거래량:{vol_ratio:.1f}배)",
                "gap_pct": gap_pct,
                "vol_ratio": vol_ratio,
            }
        elif is_gap_up:
            return {
                "signal": "hold",
                "reason": f"갭상승만 있음 (갭:{gap_pct:+.1f}%, 거래량:{vol_ratio:.1f}배)",
            }
        else:
            return {
                "signal": "hold",
                "reason": f"갭없음 (갭:{gap_pct:+.1f}%)",
            }

    # ── 전략 3: RSI 역추세 ────────────────────────────────

    def rsi_signal(self, daily_data: List[Dict], period: int = 14) -> Dict[str, Any]:
        """RSI 과매수/과매도 신호.

        RSI < 30: 과매도 → 매수
        RSI > 70: 과매수 → 매도
        """
        if len(daily_data) < period + 1:
            return {"signal": "hold", "reason": f"데이터 부족 ({period + 1}일 필요)"}

        closes = [d["close"] for d in daily_data]
        rsi_values = self.rsi(closes, period)

        if not rsi_values:
            return {"signal": "hold", "reason": "RSI 계산 불가"}

        current_rsi = rsi_values[-1]

        if current_rsi < 30:
            return {
                "signal": "buy",
                "reason": f"과매도 구간 (RSI: {current_rsi:.1f})",
                "rsi": current_rsi,
            }
        elif current_rsi > 70:
            return {
                "signal": "sell",
                "reason": f"과매수 구간 (RSI: {current_rsi:.1f})",
                "rsi": current_rsi,
            }
        else:
            return {
                "signal": "hold",
                "reason": f"중립 구간 (RSI: {current_rsi:.1f})",
                "rsi": current_rsi,
            }

    # ── 전략 4: 변동성 돌파 (래리 윌리엄스) ────────────────

    def volatility_breakout_signal(self, daily_data: List[Dict], k: float = 0.5) -> Dict[str, Any]:
        """변동성 돌파 전략.

        돌파가 = 오늘시가 + (어제고가 - 어제저가) * k
        현재가 > 돌파가 → 매수
        """
        if len(daily_data) < 2:
            return {"signal": "hold", "reason": "데이터 부족"}

        today = daily_data[-1]
        yesterday = daily_data[-2]

        today_open = today.get("open", 0)
        yesterday_high = yesterday.get("high", 0)
        yesterday_low = yesterday.get("low", 0)
        current_price = today.get("close", 0)

        range_val = yesterday_high - yesterday_low
        target_price = today_open + range_val * k

        if current_price > target_price and range_val > 0:
            pct = (current_price - target_price) / target_price * 100
            return {
                "signal": "buy",
                "reason": f"변동성돌파 (목표:{target_price:.0f}, 현재:{current_price:.0f}, +{pct:.1f}%)",
                "target_price": target_price,
                "current_price": current_price,
                "range": range_val,
                "k": k,
            }
        else:
            return {
                "signal": "hold",
                "reason": f"돌파 미달 (목표:{target_price:.0f}, 현재:{current_price:.0f})",
                "target_price": target_price,
            }

    # ── ATR (Average True Range) ───────────────────────────

    def atr(self, daily_data: List[Dict], period: int = 14) -> Optional[float]:
        """ATR 계산 — 동적 손절/익절 기준."""
        if len(daily_data) < period + 1:
            return None

        tr_list = []
        for i in range(1, len(daily_data)):
            high = daily_data[i].get("high", 0)
            low = daily_data[i].get("low", 0)
            prev_close = daily_data[i - 1].get("close", 0)
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)

        if len(tr_list) < period:
            return None

        # 단순 평균
        return sum(tr_list[-period:]) / period

    # ── 지지/저항 레벨 ─────────────────────────────────────

    def support_resistance(self, daily_data: List[Dict], lookback: int = 20) -> Dict[str, Any]:
        """지지/저항 레벨 감지.

        최근 lookback 일 중 피벗 하이/로우를 기준으로 지지/저항을 반환합니다.
        """
        if len(daily_data) < lookback:
            return {"support": None, "resistance": None, "reason": "데이터 부족"}

        recent = daily_data[-lookback:]
        current_price = recent[-1].get("close", 0)

        highs = [d.get("high", 0) for d in recent]
        lows = [d.get("low", 0) for d in recent]

        # 피벗 포인트 기반
        pivot_high = max(highs)
        pivot_low = min(lows)

        # 최근 5일 기준 근거리 지지/저항
        recent_5 = daily_data[-5:]
        near_support = min(d.get("low", 0) for d in recent_5)
        near_resistance = max(d.get("high", 0) for d in recent_5)

        # 현재가 대비 위치 (%)
        support_pct = ((current_price - near_support) / current_price * 100) if current_price else 0
        resistance_pct = ((near_resistance - current_price) / current_price * 100) if current_price else 0

        return {
            "support": near_support,
            "resistance": near_resistance,
            "pivot_high": pivot_high,
            "pivot_low": pivot_low,
            "support_pct": round(support_pct, 2),
            "resistance_pct": round(resistance_pct, 2),
            "position": "near_resistance" if resistance_pct < 1.5 else ("near_support" if support_pct < 1.5 else "mid"),
        }

    # ── 거래량 분석 ─────────────────────────────────────────

    def volume_analysis(self, daily_data: List[Dict], period: int = 20) -> Dict[str, Any]:
        """거래량 분석 — 평균 대비 현재 거래량, 트렌드."""
        if len(daily_data) < period:
            return {"vol_ratio": 0, "signal": "hold", "reason": "데이터 부족"}

        recent_vols = [d.get("volume", 0) for d in daily_data[-period:]]
        current_vol = recent_vols[-1]
        avg_vol = sum(recent_vols) / len(recent_vols)
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

        # 거래량 트렌드 (최근 5일 vs 이전 5일)
        if len(recent_vols) >= 10:
            recent_5_avg = sum(recent_vols[-5:]) / 5
            prev_5_avg = sum(recent_vols[-10:-5]) / 5
            vol_trend = "increasing" if recent_5_avg > prev_5_avg * 1.2 else (
                "decreasing" if recent_5_avg < prev_5_avg * 0.8 else "stable"
            )
        else:
            vol_trend = "unknown"

        return {
            "current_vol": current_vol,
            "avg_vol": round(avg_vol, 0),
            "vol_ratio": round(vol_ratio, 2),
            "vol_trend": vol_trend,
            "signal": "buy" if vol_ratio >= 1.5 else ("sell" if vol_ratio < 0.5 else "hold"),
            "reason": f"거래량비율 {vol_ratio:.1f}x ({vol_trend})",
        }

    # ── 일중 모멘텀 ─────────────────────────────────────────

    def intraday_momentum(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """일중 모멘텀 분석 — 당일 상승률, 캔들 패턴."""
        if len(daily_data) < 2:
            return {"momentum": "neutral", "reason": "데이터 부족"}

        today = daily_data[-1]
        yesterday = daily_data[-2]

        close = today.get("close", 0)
        open_price = today.get("open", 0)
        high = today.get("high", 0)
        low = today.get("low", 0)
        prev_close = yesterday.get("close", 0)

        # 당일 등락률
        day_change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0

        # 상하꼬리 비율
        body = abs(close - open_price)
        total_range = high - low if high > low else 1
        upper_shadow = high - max(close, open_price)
        lower_shadow = min(close, open_price) - low

        # 캔들 패턴
        is_bullish = close > open_price
        body_ratio = body / total_range

        if body_ratio > 0.7 and is_bullish:
            pattern = "strong_bull"
        elif body_ratio > 0.7 and not is_bullish:
            pattern = "strong_bear"
        elif lower_shadow > body * 2:
            pattern = "hammer"  # 매수 반전
        elif upper_shadow > body * 2:
            pattern = "shooting_star"  # 매도 반전
        else:
            pattern = "neutral"

        return {
            "day_change_pct": round(day_change_pct, 2),
            "pattern": pattern,
            "is_bullish": is_bullish,
            "body_ratio": round(body_ratio, 2),
            "momentum": "up" if day_change_pct > 2 else ("down" if day_change_pct < -2 else "neutral"),
            "chase_warning": day_change_pct > 3.0,  # 당일 3% 이상 상승 시 추격 경고
        }

    # ── 볼린저 밴드 ─────────────────────────────────────────

    def bollinger_bands(self, daily_data: List[Dict], period: int = 20, std_mult: float = 2.0) -> Dict[str, Any]:
        """볼린저 밴드 — 과매수/과매도 판단."""
        if len(daily_data) < period:
            return {"signal": "hold", "reason": "데이터 부족"}

        closes = [d["close"] for d in daily_data[-period:]]
        sma = sum(closes) / len(closes)
        std = np.std(closes)

        upper = sma + std_mult * std
        lower = sma - std_mult * std
        current = closes[-1]

        # %B 계산
        bandwidth = upper - lower
        pct_b = (current - lower) / bandwidth if bandwidth > 0 else 0.5

        return {
            "upper": round(upper, 0),
            "middle": round(sma, 0),
            "lower": round(lower, 0),
            "pct_b": round(pct_b, 3),
            "bandwidth": round(bandwidth / sma * 100, 2) if sma > 0 else 0,
            "signal": "sell" if pct_b > 0.95 else ("buy" if pct_b < 0.05 else "hold"),
            "reason": f"%B={pct_b:.2f} 상단={upper:.0f} 하단={lower:.0f}",
        }

    # ── 종합 분석 ─────────────────────────────────────────

    def analyze_all(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """모든 전략 종합 분석 → 최종 신호 도출."""
        signals = {}

        # 각 전략 신호 수집
        strategies = [
            ("ma_cross", self.ma_cross_signal),
            ("gap", self.gap_signal),
            ("rsi", self.rsi_signal),
            ("volatility", self.volatility_breakout_signal),
        ]

        for name, func in strategies:
            try:
                signals[name] = func(daily_data)
            except Exception as e:
                signals[name] = {"signal": "hold", "reason": f"오류: {e}"}

        # 추가 지표
        try:
            signals["support_resistance"] = self.support_resistance(daily_data)
        except Exception:
            pass

        try:
            signals["volume"] = self.volume_analysis(daily_data)
        except Exception:
            pass

        try:
            signals["momentum"] = self.intraday_momentum(daily_data)
        except Exception:
            pass

        try:
            signals["bollinger"] = self.bollinger_bands(daily_data)
        except Exception:
            pass

        try:
            atr_val = self.atr(daily_data)
            signals["atr"] = {"value": atr_val}
        except Exception:
            pass

        # 투표 집계
        buy_votes = sum(1 for s in signals.values() if s.get("signal") == "buy")
        sell_votes = sum(1 for s in signals.values() if s.get("signal") == "sell")

        # 가중치: MIN_BUY_VOTES_FOR_BUY개 이상 → BUY, 동일 기준 sell → SELL
        if buy_votes >= config.MIN_BUY_VOTES_FOR_BUY:
            final_signal = "buy"
            confidence = min(buy_votes / 4.0, 1.0)
        elif sell_votes >= config.MIN_BUY_VOTES_FOR_BUY:
            final_signal = "sell"
            confidence = min(sell_votes / 4.0, 1.0)
        else:
            final_signal = "hold"
            confidence = 0.0

        # 추격 경고
        momentum = signals.get("momentum", {})
        chase_warning = momentum.get("chase_warning", False)

        return {
            "final_signal": final_signal,
            "confidence": confidence,
            "buy_votes": buy_votes,
            "sell_votes": sell_votes,
            "chase_warning": chase_warning,
            "details": signals,
        }
