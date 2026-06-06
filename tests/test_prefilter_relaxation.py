"""
PREFILTER 완화 (2026-06-04) 회귀 테스트.
- trend·volume 둘 다 약해야 reject (AND).
- 하나만 약하면 6-Layer로 통과.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from unittest.mock import patch
from agents.orchestrator import Orchestrator
import config


def make_daily(closes, volumes):
    return [
        {"close": c, "volume": v}
        for c, v in zip(closes, volumes)
    ]


def test_both_weak_rejects():
    """trend 약세 (MA5 < MA20) + volume 약세 (5일 < 20일*0.6) → reject"""
    # MA5 < MA20: 30일 종가가 100 → 90 → 100 → 80 → 100 (5일 평균 90) vs MA20 더 낮게 잡힘
    # MA20 ≈ 평균(20일). 5일평균 < 20일평균*0.95
    closes = [100] * 15 + [110] * 5  # 5일평균 110 vs 20일평균 102.5 → ratio 1.07 (strong, not weak)
    # 이건 strong. weak를 만들려면 후반 5일이 전반보다 낮아야.
    # 20일: 전반 15일 120, 후반 5일 90 → MA20 = (15*120 + 5*90)/20 = (1800+450)/20 = 112.5
    # MA5 = 90. ratio = 90/112.5 = 0.8 < 0.95 → trend weak
    closes = [120] * 15 + [90] * 5
    volumes = [100000] * 15 + [10000] * 5  # vol_ratio = (5*10k)/(20*100k) = 0.1 < 0.6 → volume weak
    daily = make_daily(closes, volumes)
    orch = Orchestrator()
    result = orch._should_prefilter_trend_volume("005930", "삼성전자", daily, tech_score=30)
    assert result["rejected"] is True, f"둘 다 약세 → reject 기대, got {result}"
    assert result["reason_tag"] == "prefilter"
    assert "선필터" in result["reason"]


def test_only_trend_weak_passes():
    """trend 약세 + volume 정상 → 6-Layer로 통과"""
    closes = [120] * 15 + [90] * 5  # trend weak
    volumes = [100000] * 20  # vol_ratio = 1.0, not weak
    daily = make_daily(closes, volumes)
    orch = Orchestrator()
    result = orch._should_prefilter_trend_volume("005930", "삼성전자", daily, tech_score=30)
    assert result["rejected"] is False, f"volume만 정상 → 통과 기대, got {result}"


def test_only_volume_weak_passes():
    """trend 정상 + volume 약세 → 6-Layer로 통과"""
    closes = [90] * 15 + [120] * 5  # MA5=120, MA20=97.5, ratio=1.23 (strong, not weak)
    volumes = [100000] * 15 + [10000] * 5  # vol_ratio = 0.1, weak
    daily = make_daily(closes, volumes)
    orch = Orchestrator()
    result = orch._should_prefilter_trend_volume("005930", "삼성전자", daily, tech_score=30)
    assert result["rejected"] is False, f"trend만 정상 → 통과 기대, got {result}"


def test_both_strong_passes():
    """둘 다 강세 → 통과"""
    closes = [90] * 15 + [120] * 5  # strong trend
    volumes = [100000] * 15 + [200000] * 5  # vol_ratio = 2.0, strong volume
    daily = make_daily(closes, volumes)
    orch = Orchestrator()
    result = orch._should_prefilter_trend_volume("005930", "삼성전자", daily, tech_score=30)
    assert result["rejected"] is False


def test_insufficient_data_passes():
    """데이터 20일 미만 → 통과 (분석 skip)"""
    closes = [100] * 10
    volumes = [100000] * 10
    daily = make_daily(closes, volumes)
    orch = Orchestrator()
    result = orch._should_prefilter_trend_volume("005930", "삼성전자", daily, tech_score=30)
    assert result["rejected"] is False


if __name__ == "__main__":
    test_both_weak_rejects()
    print("✓ test_both_weak_rejects")
    test_only_trend_weak_passes()
    print("✓ test_only_trend_weak_passes")
    test_only_volume_weak_passes()
    print("✓ test_only_volume_weak_passes")
    test_both_strong_passes()
    print("✓ test_both_strong_passes")
    test_insufficient_data_passes()
    print("✓ test_insufficient_data_passes")
    print("\n=== All PREFILTER relaxation tests passed ===")
