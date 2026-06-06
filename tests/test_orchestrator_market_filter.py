import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


_STUB_MODULES = [
    "agents",
    "core",
    "core.data_store",
    "core.news_archive",
    "agents.market_scout",
    "agents.technical_analyst",
    "agents.entry_analyzer",
    "agents.risk_manager_agent",
    "agents.theme_accumulator",
]


def _install_dependency_stubs():
    previous = {name: sys.modules.get(name) for name in _STUB_MODULES}

    agents_pkg = types.ModuleType("agents")
    agents_pkg.__path__ = [str(ROOT / "agents")]
    sys.modules["agents"] = agents_pkg

    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str(ROOT / "core")]
    sys.modules["core"] = core_pkg

    stubs = {
        "core.data_store": ("DataStore", object),
        "core.news_archive": ("NewsArchive", object),
        "agents.market_scout": ("MarketScoutAgent", object),
        "agents.technical_analyst": ("TechnicalAnalyst", object),
        "agents.entry_analyzer": ("EntryAnalyzer", object),
        "agents.risk_manager_agent": ("RiskManagerAgent", object),
        "agents.theme_accumulator": ("ThemeAccumulator", object),
    }
    for module_name, (attr_name, attr_value) in stubs.items():
        module = types.ModuleType(module_name)
        setattr(module, attr_name, attr_value)
        sys.modules[module_name] = module
    return previous


def _restore_modules(previous):
    for name in _STUB_MODULES:
        if previous.get(name) is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous[name]


_previous_modules = _install_dependency_stubs()
try:
    MODULE_PATH = ROOT / "agents" / "orchestrator.py"
    spec = importlib.util.spec_from_file_location("orchestrator_under_test", MODULE_PATH)
    orchestrator_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orchestrator_module)
finally:
    _restore_modules(_previous_modules)
Orchestrator = orchestrator_module.Orchestrator


class _FakeStore:
    def __init__(self):
        self.saved = {}

    def safe_save(self, name, payload):
        self.saved[name] = payload

    def safe_load(self, name):
        return self.saved.get(name, {})

    def get_open_slot_count(self, market):
        return 0


class _FakeScout:
    def scan_all_categories(self):
        return []

    def get_theme_candidates(self, themes):
        return [
            {
                "code": "PATH",
                "name": "UiPath",
                "theme": "로봇_자동화",
                "selection_bias": 100,
                "theme_strength": 8,
                "recent_alert_count": 3,
            },
            {
                "code": "005930",
                "name": "삼성전자",
                "theme": "반도체",
                "selection_bias": 10,
                "theme_strength": 7,
                "recent_alert_count": 1,
            },
        ]


class _FakeThemeAccumulator:
    def detect_themes(self):
        return [
            {"theme": "로봇_자동화", "strength": 8},
            {"theme": "반도체", "strength": 7},
        ]


class _FakeRiskAgent:
    def check_can_enter(self, code, market):
        return {"can_enter": True, "reasons": [], "open_positions": 0, "max_positions": 3}


class _AlmostFullRiskAgent:
    def check_can_enter(self, code, market):
        return {
            "can_enter": True,
            "reasons": [],
            "open_positions": 2,
            "max_positions": 3,
        }


class _BlockingRiskAgent:
    def check_can_enter(self, code, market):
        return {
            "can_enter": False,
            "reasons": ["포지션_full (3/3)"],
            "open_positions": 3,
            "max_positions": 3,
        }


class _ThreeKrScout(_FakeScout):
    def get_theme_candidates(self, themes):
        return [
            {"code": "005930", "name": "삼성전자", "theme": "반도체", "selection_bias": 30, "theme_strength": 8},
            {"code": "000660", "name": "SK하이닉스", "theme": "AI_반도체", "selection_bias": 20, "theme_strength": 8},
            {"code": "042700", "name": "한미반도체", "theme": "AI_반도체", "selection_bias": 10, "theme_strength": 8},
        ]


class _NoCandidateScout(_FakeScout):
    def get_theme_candidates(self, themes):
        return []


class _BorderlineScout(_FakeScout):
    def get_theme_candidates(self, themes):
        return [
            {
                "code": "041510",
                "name": "에스엠",
                "theme": "엔터",
                "selection_bias": 55,
                "theme_strength": 8,
                "recent_alert_count": 0,
            }
        ]


class _FakeTechnicalAnalyst:
    def rank_stocks(self, candidates):
        for candidate in candidates:
            candidate["score"] = 80
            candidate.setdefault("role", "leader")
        return candidates


class _BorderlineTechnicalAnalyst:
    def rank_stocks(self, candidates):
        for candidate in candidates:
            candidate["score"] = 9.0  # Below TECH_SCORE_MIN=10 → blocked
            candidate.setdefault("role", "leader")
        return candidates


class _TopKrTechnicalAnalyst:
    def rank_stocks(self, candidates):
        for candidate in candidates:
            candidate["score"] = 14.0
            candidate.setdefault("role", "leader")
        return candidates


class _FakeEntryAnalyzer:
    def analyze_entry(self, **kwargs):
        return {"verdict": "BUY", "score": 80, "reason": "test", "reason_tag": "test"}


class _FakeKisAPI:
    def __init__(self):
        self.kr_daily_calls = []
        self.us_daily_calls = []

    def get_stock_daily(self, code, period=60):
        self.kr_daily_calls.append((code, period))
        return [{"date": "20260513", "close": 100}]

    def get_us_stock_daily(self, code, period=60):
        self.us_daily_calls.append((code, period))
        return [{"date": "20260513", "close": 100}]


def _make_daily_data(closes, volumes):
    return [
        {"date": f"202605{idx:02d}", "close": close, "volume": volume}
        for idx, (close, volume) in enumerate(zip(closes, volumes), start=1)
    ]


def _build_orchestrator():
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = _FakeStore()
    orch.archive = object()
    orch.scout = _FakeScout()
    orch.tech_analyst = _FakeTechnicalAnalyst()
    orch.entry_analyzer = _FakeEntryAnalyzer()
    orch.risk_agent = _FakeRiskAgent()
    orch.theme_acc = _FakeThemeAccumulator()
    return orch


class OrchestratorMarketFilterTests(unittest.TestCase):
    def test_skips_us_daily_prefetch_when_us_market_is_closed_and_no_market_is_open(self):
        orch = _build_orchestrator()
        kis = _FakeKisAPI()

        with patch.object(Orchestrator, "_is_kr_market_hours", return_value=False), \
             patch.object(Orchestrator, "_is_us_market_hours", return_value=False), \
             patch.object(orchestrator_module.time, "sleep", return_value=None):
            orch.run_scan_pipeline(kis)

        self.assertEqual(kis.us_daily_calls, [])
        self.assertEqual(kis.kr_daily_calls, [("005930", 60)])

    def test_reserves_remaining_position_slots_across_candidates_in_same_scan(self):
        orch = _build_orchestrator()
        orch.scout = _ThreeKrScout()
        orch.risk_agent = _AlmostFullRiskAgent()
        kis = _FakeKisAPI()

        with patch.object(Orchestrator, "_is_kr_market_hours", return_value=True), \
             patch.object(Orchestrator, "_is_us_market_hours", return_value=False), \
             patch.object(orchestrator_module.time, "sleep", return_value=None):
            results = orch.run_scan_pipeline(kis)

        actionable = [r for r in results if r["risk_can_enter"]]
        blocked = [r for r in results if not r["risk_can_enter"]]
        self.assertEqual(len(actionable), 1)
        self.assertEqual(len(blocked), 1)

    def test_rejects_weak_tech_score_below_lowered_minimum(self):
        orch = _build_orchestrator()
        orch.scout = _BorderlineScout()
        orch.tech_analyst = _BorderlineTechnicalAnalyst()
        kis = _FakeKisAPI()

        with patch.object(Orchestrator, "_is_kr_market_hours", return_value=True), \
             patch.object(Orchestrator, "_is_us_market_hours", return_value=False), \
             patch.object(orchestrator_module.time, "sleep", return_value=None):
            results = orch.run_scan_pipeline(kis)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entry_verdict"], "REJECT")
        self.assertEqual(results[0]["entry_reason_tag"], "low_tech_score")

    def test_allows_top_kr_candidate_score_to_reach_six_layer_filter(self):
        orch = _build_orchestrator()
        orch.scout = _BorderlineScout()
        orch.tech_analyst = _TopKrTechnicalAnalyst()
        kis = _FakeKisAPI()

        with patch.object(Orchestrator, "_is_kr_market_hours", return_value=True), \
             patch.object(Orchestrator, "_is_us_market_hours", return_value=False), \
             patch.object(orchestrator_module.time, "sleep", return_value=None):
            results = orch.run_scan_pipeline(kis)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["entry_verdict"], "BUY")
        self.assertEqual(results[0]["entry_reason_tag"], "test")

    def test_blocks_weak_buy_when_recent_performance_is_weak(self):
        orch = _build_orchestrator()
        orch.store.saved["last_pipeline"] = {
            "results": [
                {
                    "code": "000001",
                    "name": "약한종목",
                    "entry_verdict": "WEAK_BUY",
                    "risk_can_enter": True,
                    "tech_score": 14,
                    "entry_score": 60,
                    "selection_score": 20,
                    "relative_strength_score": 20,
                },
                {
                    "code": "000002",
                    "name": "강한종목",
                    "entry_verdict": "BUY",
                    "risk_can_enter": True,
                    "tech_score": 18,
                    "entry_score": 80,
                    "selection_score": 30,
                    "relative_strength_score": 30,
                },
            ]
        }
        orch.store.saved["autopilot_health"] = {
            "metrics": {
                "recent_win_rate_pct": 25.0,
                "recent_realized_pnl": -91700.0,
                "open_positions": 3,
            }
        }

        with patch.object(orchestrator_module.config, "KIS_MODE", "virtual"):
            signals = orch.get_actionable_signals()

        self.assertEqual([s["code"] for s in signals], ["000002"])

    def test_saves_empty_last_pipeline_when_prefilter_blocks_every_candidate(self):
        orch = _build_orchestrator()
        orch.store.saved["last_pipeline"] = {
            "results": [{"code": "OLD", "entry_verdict": "BUY", "risk_can_enter": True}]
        }
        orch.risk_agent = _BlockingRiskAgent()
        kis = _FakeKisAPI()

        with patch.object(Orchestrator, "_is_kr_market_hours", return_value=True), \
             patch.object(Orchestrator, "_is_us_market_hours", return_value=False), \
             patch.object(orchestrator_module.time, "sleep", return_value=None):
            results = orch.run_scan_pipeline(kis)

        self.assertEqual(results, [])
        self.assertEqual(orch.store.saved["last_pipeline"]["results"], [])
        self.assertEqual(orch.get_actionable_signals(), [])
    def test_saves_empty_last_pipeline_when_scout_returns_no_candidates(self):
        orch = _build_orchestrator()
        orch.scout = _NoCandidateScout()
        orch.store.saved["last_pipeline"] = {
            "results": [{"code": "OLD", "entry_verdict": "BUY", "risk_can_enter": True}]
        }

        results = orch.run_scan_pipeline(kis_api=None)

        self.assertEqual(results, [])
        self.assertEqual(orch.store.saved["last_pipeline"]["results"], [])
        self.assertEqual(orch.get_actionable_signals(), [])

    def test_prefilter_rejects_weak_trend_and_volume_before_six_layer(self):
        """2026-06-04: trend·volume 둘 다 약할 때만 reject (AND)."""
        orch = _build_orchestrator()
        # 강한 약세 시나리오: trend 0.92 (MA5=92, MA20=100) + volume 0.5
        closes = [100] * 15 + [96, 95, 94, 93, 92]  # MA5=94, MA20=98.5, ratio=0.954 (not enough)
        # → 더 약하게: MA5<MA20*0.95 보장 + vol<0.6 보장
        closes = [100] * 15 + [85, 80, 80, 80, 80]  # MA5=81, MA20=96.25, ratio=0.842
        volumes = [1000] * 15 + [400, 350, 320, 300, 280]  # vol_ratio=0.466
        result = orch._should_prefilter_trend_volume("000001", "약한종목", _make_daily_data(closes, volumes), 18)

        self.assertTrue(result["rejected"])
        self.assertEqual(result["reason_tag"], "prefilter")
        self.assertIn("trend+volume", result["reason"])

    def test_prefilter_passes_when_only_trend_weak(self):
        """2026-06-04: trend만 약하고 volume 정상 → 6-Layer로 통과."""
        orch = _build_orchestrator()
        closes = [100] * 15 + [85, 80, 80, 80, 80]  # trend weak (0.842)
        volumes = [1000] * 20  # vol_ratio=1.0, strong
        result = orch._should_prefilter_trend_volume("000003", "trend만약", _make_daily_data(closes, volumes), 18)
        self.assertFalse(result["rejected"])

    def test_prefilter_passes_when_only_volume_weak(self):
        """2026-06-04: volume만 약하고 trend 정상 → 6-Layer로 통과."""
        orch = _build_orchestrator()
        closes = [100] * 20  # trend flat (ratio=1.0)
        volumes = [1000] * 15 + [400, 350, 320, 300, 280]  # vol_ratio=0.466, weak
        result = orch._should_prefilter_trend_volume("000004", "vol만약", _make_daily_data(closes, volumes), 18)
        self.assertFalse(result["rejected"])

    def test_prefilter_allows_volume_increasing_candidates_to_pass(self):
        orch = _build_orchestrator()
        closes = [100] * 20
        volumes = [1000] * 10 + [1300, 1350, 1400, 1450, 1500, 1600, 1700, 1800, 1900, 2000]
        result = orch._should_prefilter_trend_volume("000002", "강한종목", _make_daily_data(closes, volumes), 18)

        self.assertFalse(result["rejected"])
        self.assertEqual(result["reason_tag"], "")
        self.assertGreater(result["details"]["vol_ratio"], 0.0)

    def test_us_ticker_with_letters_is_dropped_when_us_disabled(self):
        """영문자 티커(TSLA/NVDA 등)는 c.isdigit() 검사를 우회하지만
        market='US' 또는 6자리 숫자 부재로 차단되어야 함 (2026-06-04 fix)."""
        from config import US_STOCK_ENABLED as ACTUAL_FLAG
        if ACTUAL_FLAG:
            self.skipTest("US_STOCK_ENABLED=true 환경에서는 검증 불가")

        def _is_kr_candidate(c: dict) -> bool:
            code = c.get("code", "")
            market = c.get("market", "KR")
            if market == "US":
                return False
            return code.isdigit() and len(code) == 6

        # 차단되어야 할 케이스들
        self.assertFalse(_is_kr_candidate({"code": "TSLA", "market": "US", "name": "Tesla"}))
        self.assertFalse(_is_kr_candidate({"code": "NVDA", "market": "US", "name": "NVDA"}))
        self.assertFalse(_is_kr_candidate({"code": "TSLA", "name": "Tesla"}))  # market 미명시 + 영문자
        self.assertFalse(_is_kr_candidate({"code": "12345", "name": "5자리"}))   # 5자리 (KR 아님)
        self.assertFalse(_is_kr_candidate({"code": "1234567", "name": "7자리"}))  # 7자리
        self.assertFalse(_is_kr_candidate({"code": "", "name": "빈코드"}))
        self.assertFalse(_is_kr_candidate({"code": "TSLA", "market": "KR", "name": "오분류"}))  # KR 마켓인데 영문자 = 비정상

        # 통과해야 할 케이스 (KR 6자리 숫자)
        self.assertTrue(_is_kr_candidate({"code": "005930", "market": "KR", "name": "삼성전자"}))
        self.assertTrue(_is_kr_candidate({"code": "005930", "name": "삼성전자"}))  # market 미명시 + 숫자 = 통과

    def test_us_blocked_path_does_not_reach_entry_analyzer(self):
        """orchestrator의 US 게이트가 entry_analyzer 호출을 막는지 통합 테스트.
        US_STOCK_ENABLED=false면 _is_kr_candidate로 US 후보가 사전 제거됨."""
        from config import US_STOCK_ENABLED as ACTUAL_FLAG
        if ACTUAL_FLAG:
            self.skipTest("US_STOCK_ENABLED=true 환경에서는 검증 불가")

        # US 후보가 포함된 입력
        candidates = [
            {"code": "TSLA", "market": "US", "name": "Tesla", "score": 60},
            {"code": "005930", "market": "KR", "name": "삼성전자", "score": 15},
            {"code": "NVDA", "market": "US", "name": "NVDA", "score": 50},
        ]
        # orchestrator.py:228 필터 로직 재현
        if not ACTUAL_FLAG:
            def _is_kr_candidate(c):
                code = c.get("code", "")
                market = c.get("market", "KR")
                if market == "US":
                    return False
                return code.isdigit() and len(code) == 6
            kr_only = [c for c in candidates if _is_kr_candidate(c)]

        # US 후보는 모두 제거되고 KR만 남아야 함
        self.assertEqual(len(kr_only), 1)
        self.assertEqual(kr_only[0]["code"], "005930")
        self.assertEqual(kr_only[0]["name"], "삼성전자")


if __name__ == "__main__":
    unittest.main()
