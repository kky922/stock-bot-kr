"""
자금 분배 & 상관관계 필터 에이전트.
다종목 포지션의 자금 분배, 섹터 집중도, 상관관계를 관리합니다.
"""

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import json
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

import config
from core.data_store import DataStore

logger = logging.getLogger(__name__)

# 섹터 매핑 (종목코드 → 섹터)
SECTOR_MAP = {
    # 반도체
    "005930": "반도체", "000660": "반도체", "042700": "반도체", "058470": "반도체",
    "357780": "반도체", "108320": "반도체", "240810": "반도체", "039030": "반도체",
    "403870": "반도체", "272290": "반도체",
    # 방산
    "047810": "방산", "012450": "방산", "079550": "방산", "272210": "방산",
    "103140": "방산", "299660": "방산",
    # 2차전지
    "373220": "2차전지", "006400": "2차전지", "247540": "2차전지", "086520": "2차전지",
    "003670": "2차전지", "011790": "2차전지", "096770": "2차전지",
    # 바이오
    "207940": "바이오", "326030": "바이오", "068270": "바이오", "145020": "바이오",
    "004990": "바이오", "005180": "바이오", "196170": "바이오", "277810": "바이오",
    "328130": "바이오", "302440": "바이오", "084690": "바이오", "214150": "바이오",
    # 로봇
    "267260": "로봇", "090460": "로봇", "241560": "로봇", "049430": "로봇",
    # IT
    "035420": "IT", "036570": "IT", "035720": "IT", "017670": "IT",
    "030200": "IT", "032640": "IT", "141080": "IT",
    # 자동차
    "012330": "자동차", "005380": "자동차", "000270": "자동차", "018880": "자동차",
    "161390": "자동차", "265250": "자동차",
    # 엔터
    "352820": "엔터", "259960": "엔터", "251270": "엔터", "263750": "엔터",
    "041510": "엔터", "122870": "엔터", "035900": "엔터", "323130": "엔터",
    # 유통
    "139480": "유통", "023530": "유통", "004170": "유통",
    # 전기/에너지
    "060280": "전기", "006260": "전기", "001440": "전기",
    "009830": "전기", "034020": "전기", "025950": "전기",
    "245620": "전기", "336260": "전기",
    # 건설/인프라
    "000720": "건설", "006360": "건설", "047040": "건설", "000210": "건설",
    # 금융/보험
    "055550": "금융", "086790": "금융", "105560": "금융", "316140": "금융",
    "024110": "금융", "005830": "금융", "032830": "금융", "009450": "금융",
    # 철강/소재
    "004020": "철강", "005490": "철강", "010130": "철강",
    "098460": "철강", "058650": "철강",
    # 통신
    "017670": "통신", "030200": "통신", "032640": "통신",
    # 항공/우주
    "280360": "항공", "329180": "항공",
    # 조선/중공업
    "069660": "조선", "322000": "조선",
    # 화학
    "051910": "화학", "012030": "화학",
    # 기타 주요종목
    "011200": "물류", "009540": "물류", "032500": "물류",
    "006650": "에너지", "117580": "에너지", "010950": "에너지",
    "298040": "헬스케어",
    # 미국
    "NVDA": "반도체", "AMD": "반도체", "AVGO": "반도체", "INTC": "반도체",
    "TSLA": "2차전지", "LMT": "방산", "RTX": "방산",
    "JNJ": "바이오", "PFE": "바이오",
    "AAPL": "IT", "MSFT": "IT", "GOOGL": "IT", "META": "IT",
    "AMZN": "유통", "NFLX": "엔터",
}

# 높은 상관관계 섹터 그룹
CORRELATED_GROUPS = [
    {"반도체", "AI"},
    {"방산", "군수"},
    {"2차전지", "배터리"},
    {"바이오", "제약"},
]


class RiskManagerAgent:
    """자금 분배 & 상관관계 필터."""

    def __init__(self, data_store: DataStore = None):
        self.store = data_store or DataStore()
        self._lock = threading.Lock()

    def get_sector(self, stock_code: str) -> str:
        """종목의 섹터 반환."""
        return SECTOR_MAP.get(stock_code, "unknown")

    @staticmethod
    def _load_permanent_exclude() -> set:
        """영구 배제 종목 목록 로드 (data/agents/permanent_exclude.json)"""
        try:
            excl_path = Path(__file__).parent.parent / "data" / "agents" / "permanent_exclude.json"
            if excl_path.exists():
                data = json.loads(excl_path.read_text())
                return {e["code"] for e in data.get("excluded_symbols", [])}
        except Exception:
            pass
        return set()

    def check_can_enter(self, stock_code: str, market: str = "KR") -> Dict[str, Any]:
        """진입 가능 여부 확인.

        검사 항목:
        0. 영구 배제 종목 (permanent_exclude.json)
        1. 최대 포지션 수
        2. 같은 섹터 집중도
        3. 이미 보유한 종목 중복 진입 방지
        4. 상관관계 체크
        5. 종목 손실 쿨다운
        6. API degraded 모드
        """
        reasons = []
        can_enter = True

        # 0. 영구 배제 종목 체크
        excluded = self._load_permanent_exclude()
        if stock_code in excluded:
            can_enter = False
            reasons.append(f"permanent_exclude")
            logger.info("🚫 [리스크] 영구 배제 종목: %s — 진입 차단", stock_code)

        with self._lock:
            # 1. 포지션 수
            open_count = self.store.get_open_slot_count(market)
            max_positions = config.MAX_POSITIONS_PER_MARKET
            if open_count >= max_positions:
                can_enter = False
                reasons.append(f"포지션_full ({open_count}/{max_positions})")

            # 2. 섹터 집중도
            sector = self.get_sector(stock_code)
            positions = self.store.load_all_positions(market)
            same_sector = sum(
                1 for p in positions
                if self.get_sector(p.get("code", "")) == sector
            )
            if same_sector >= config.MAX_SAME_SECTOR:
                can_enter = False
                reasons.append(f"섹터집중 ({sector}: {same_sector}/{config.MAX_SAME_SECTOR})")

            # 3. 이미 보유한 종목 중복 진입 방지
            if self.store.has_position_code(market, stock_code):
                can_enter = False
                held = self.store.find_slot_by_code(market, stock_code)
                held_name = held.get("name", stock_code) if held else stock_code
                reasons.append(f"already_held({held_name})")

            # 4. 상관관계 체크
            corr_warning = self._check_correlation(stock_code, positions)
            if corr_warning:
                reasons.append(corr_warning)

            # 5. 종목 손실 쿨다운
            symbol_cooldown = self.store.get_symbol_cooldown(market, stock_code)
            if symbol_cooldown:
                can_enter = False
                until = symbol_cooldown.get("until", "")
                reason = symbol_cooldown.get("reason", "")
                reasons.append(f"loss_cooldown({until[:19]})")
                if reason:
                    reasons.append(reason)

            # 5. API degraded 모드
            market_state = self.store.get_market_state(market)
            if market_state.get("api_degraded_mode"):
                # 가상 모드에서 US 시장의 api_degraded_mode는
                # KIS VTS가 US 주문을 지원하지 않는 정상 상태이므로 진입을 차단하지 않음
                # fix #7a: 모의투자 US 파이프라인 전체 테스트를 위해 필요
                if not (market == "US" and getattr(config, "KIS_MODE", "real") == "virtual"):
                    can_enter = False
                    reasons.append("api_degraded_mode")

        return {
            "can_enter": can_enter,
            "reasons": reasons,
            "open_positions": open_count,
            "max_positions": max_positions,
            "sector": sector,
            "same_sector_count": same_sector,
        }

    def calculate_position_size(
        self,
        stock_code: str,
        current_price: float,
        market: str = "KR",
        is_ai_signal: bool = True,
    ) -> Dict[str, Any]:
        """포지션 크기 계산.

        - 시장별 예산 분배
        - AI 신호 시 자금 제한
        - 분할 매수 적용
        """
        budget = config.KR_BUDGET if market == "KR" else config.US_BUDGET
        open_count = self.store.get_open_slot_count(market)

        # 남은 슬롯당 예산
        remaining_slots = max(1, config.MAX_POSITIONS_PER_MARKET - open_count)
        per_slot_budget = budget / remaining_slots

        # AI 신호만으로 진입 시 자금 제한
        if is_ai_signal:
            max_ai_budget = budget * config.MAX_AI_POSITION_PCT / 100
            per_slot_budget = min(per_slot_budget, max_ai_budget)

        # 분할 매수 1차 비율 적용
        first_scale = config.SCALE_IN_STEPS[0]  # 50%
        invest_amount = per_slot_budget * first_scale

        # 주식 수 계산
        if market == "KR":
            quantity = int(invest_amount / current_price) if current_price > 0 else 0
            actual_amount = quantity * current_price
        else:
            quantity = int(invest_amount / current_price) if current_price > 0 else 0
            actual_amount = quantity * current_price

        return {
            "quantity": quantity,
            "invest_amount": round(actual_amount, 2),
            "per_slot_budget": round(per_slot_budget, 2),
            "scale_step": 0,
            "scale_ratio": first_scale,
            "remaining_budget": round(budget - actual_amount, 2),
            "is_ai_signal": is_ai_signal,
        }

    def _check_correlation(self, stock_code: str, positions: List[Dict]) -> Optional[str]:
        """상관관계 높은 섹터 중복 경고."""
        target_sector = self.get_sector(stock_code)
        held_sectors = [self.get_sector(p.get("code", "")) for p in positions]

        for group in CORRELATED_GROUPS:
            if target_sector in group:
                correlated = [s for s in held_sectors if s in group]
                if len(correlated) >= 2:
                    return f"상관섹터중복 ({', '.join(group)})"
        return None

    def get_portfolio_summary(self, market: str = "KR") -> Dict[str, Any]:
        """포트폴리오 요약."""
        with self._lock:
            positions = self.store.load_all_positions(market)

        sector_dist = {}
        total_value = 0
        for p in positions:
            sector = self.get_sector(p.get("code", ""))
            value = p.get("invest_amount", 0)
            sector_dist[sector] = sector_dist.get(sector, 0) + value
            total_value += value

        budget = config.KR_BUDGET if market == "KR" else config.US_BUDGET
        usage_pct = (total_value / budget * 100) if budget > 0 else 0

        return {
            "market": market,
            "total_positions": len(positions),
            "total_value": round(total_value, 2),
            "budget": budget,
            "usage_pct": round(usage_pct, 1),
            "sector_distribution": sector_dist,
        }
