"""
Stock Bot Multi-Agent System v2.0
==================================
국내(KRX) + 미국(NYSE/NASDAQ) 동시 운영 8-Agent 아키텍처

에이전트:
- MarketScoutAgent: 시장 정찰 (5영역 RSS + 뉴스 아카이브)
- ThemeAccumulator: 메가 테마 감지 (다영역 교차 검증)
- TechnicalAnalyst: 복수 종목 기술 분석 (지지/저항·거래량)
- EntryAnalyzer: 5-Layer 타점 검증 (진입 타이밍)
- RiskManagerAgent: 리스크 관리 (다중 슬롯 자금 분배)
- TradeExecutorAgent: 매매 실행 (다중 슬롯 매수/매도)
- MonitorAgent: 모니터링 (슬롯별 트레일링 스톱)
- Orchestrator: 전체 조율 (Theme 연동 파이프라인)
"""

# 패키지 import 시 모든 에이전트를 즉시 로드하면 선택 의존성(feedparser 등)이
# 없는 테스트/진단 환경에서 `from agents.trade_executor import ...` 같은 독립 모듈
# import까지 실패한다. 공개 심볼은 지연 로드해 필요한 에이전트의 의존성만 요구한다.
_AGENT_EXPORTS = {
    "BaseAgent": ("agents.base_agent", "BaseAgent"),
    "MarketScoutAgent": ("agents.market_scout", "MarketScoutAgent"),
    "TechnicalAnalyst": ("agents.technical_analyst", "TechnicalAnalyst"),
    "TechnicalAnalystAgent": ("agents.technical_analyst", "TechnicalAnalyst"),
    "ThemeAccumulator": ("agents.theme_accumulator", "ThemeAccumulator"),
    "EntryAnalyzer": ("agents.entry_analyzer", "EntryAnalyzer"),
    "RiskManagerAgent": ("agents.risk_manager_agent", "RiskManagerAgent"),
    "TradeExecutorAgent": ("agents.trade_executor", "TradeExecutorAgent"),
    "MonitorAgent": ("agents.monitor_agent", "MonitorAgent"),
    "Orchestrator": ("agents.orchestrator", "Orchestrator"),
    "OrchestratorAgent": ("agents.orchestrator", "Orchestrator"),
}


def __getattr__(name):
    if name not in _AGENT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _AGENT_EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = list(_AGENT_EXPORTS)
__version__ = '2.0.0'
