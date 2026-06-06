"""
Market Regime Agent — 시장 국면 감지 모듈.
US 지수(SPY, QQQ)의 방향성과 모멘텀을 평가하여 포지션 사이즈/리스크 조절.

2026-05-14: GLM 뉴스 파이프라인 대체. 정량적 지수 기반 리스크 관리.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# 지수별 심볼
INDEX_TICKERS = {
    "SPY": "SPY",  # S&P 500
    "QQQ": "QQQ",  # Nasdaq 100
}

# 메모리 캐시 (1회/5분)
_cache: Dict[str, dict] = {}
_cache_ts: float = 0
_CACHE_TTL = 300  # 5초 → 300초 (5분)


def _get_index_price(symbol: str) -> Optional[Dict]:
    """Yahoo Finance API로 지수 데이터 조회."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if resp.status_code != 200:
            logger.warning("⚠️ %s 지수 조회 실패: HTTP %d", symbol, resp.status_code)
            return None

        data = resp.json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return None

        meta = result.get("meta", {})
        indicators = result.get("indicators", {})
        adjclose_list = indicators.get("adjclose", [{}])[0].get("adjclose")
        closes = indicators.get("quote", [{}])[0].get("close")

        prices = adjclose_list or closes
        if not prices or len(prices) < 5:
            return None

        current = prices[-1]
        # 단순 이동평균
        ma5 = sum(prices[-5:]) / min(5, len(prices[-5:]))
        ma20 = sum(prices[-min(20, len(prices)):]) / min(20, len(prices))
        ma_20_list = prices[-20:] if len(prices) >= 20 else prices
        ma20 = sum(ma_20_list) / len(ma_20_list)

        # 모멘텀: 5일, 20일
        mom_5d = (prices[-1] - prices[-max(6, len(prices))]) / prices[-max(6, len(prices))] * 100 if len(prices) >= 6 else 0
        mom_20d = (prices[-1] - prices[-max(21, len(prices))]) / prices[-max(21, len(prices))] * 100 if len(prices) >= 21 else 0

        return {
            "symbol": symbol,
            "current_price": round(current, 2),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "above_ma5": current > ma5,
            "above_ma20": current > ma20,
            "momentum_5d": round(mom_5d, 2),
            "momentum_20d": round(mom_20d, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning("⚠️ %s 지수 조회 예외: %s", symbol, e)
        return None


def check_us_market_regime() -> Dict:
    """US 시장 국면 평가.

    Returns:
        - regime: "bullish" / "neutral" / "bearish"
        - score: 0~100 (클수록 낙관)
        - position_size_mult: 포지션 크기 조절 배수 (0.0~1.0)
        - skip_us: US 진입 완전 스킵 여부
        - detail: 지수별 상태
    """
    global _cache, _cache_ts
    now = time.time()

    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache["result"]

    spy = _get_index_price("SPY")
    qqq = _get_index_price("QQQ")

    result = {
        "regime": "neutral",
        "score": 50,
        "position_size_mult": 1.0,
        "skip_us": False,
        "detail": {"spy": spy, "qqq": qqq},
    }

    if not spy and not qqq:
        # 데이터 없으면 보수적 (중립 유지)
        _cache = {"result": result, "ts": now}
        return result

    scores = []
    for idx_data in [spy, qqq]:
        if not idx_data:
            continue
        score = 50  # baseline
        # 20일선 위면 +20, 아래면 -20
        if idx_data["above_ma20"]:
            score += 20
        else:
            score -= 20
        # 5일선 위면 +10
        if idx_data["above_ma5"]:
            score += 10
        else:
            score -= 10
        # 5일 모멘텀
        score += max(-10, min(10, idx_data["momentum_5d"] * 2))
        # 20일 모멘텀
        score += max(-15, min(15, idx_data["momentum_20d"]))
        scores.append(max(0, min(100, score)))

    avg_score = sum(scores) / len(scores) if scores else 50
    result["score"] = round(avg_score)

    if avg_score >= 65:
        result["regime"] = "bullish"
        result["position_size_mult"] = 1.0
        result["skip_us"] = False
    elif avg_score >= 40:
        result["regime"] = "neutral"
        result["position_size_mult"] = 0.7
        result["skip_us"] = False
    else:
        result["regime"] = "bearish"
        result["position_size_mult"] = 0.3
        result["skip_us"] = True  # 약세장 US 진입 금지

    logger.info(
        "📊 US 시장 국면: %s (점수:%d) — pos_size:%.1fx, skip_us:%s",
        result["regime"], result["score"],
        result["position_size_mult"], result["skip_us"],
    )

    _cache = {"result": result, "ts": now}
    return result
