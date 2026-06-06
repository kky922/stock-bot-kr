"""
주식 자동매매봇 설정 관리.
.env 파일에서 API 키 및 설정을 로드합니다.
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

# 프로젝트 루트
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")


# --- Hot reloadable params.json (strategy/risk only; secrets stay in .env) ---
def _load_params() -> dict:
    params_path = ROOT_DIR / "params.json"
    try:
        if params_path.exists():
            with params_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            params = {}
            params.update(raw.get("strategy", {}) or {})
            params.update(raw.get("risk", {}) or {})
            return params
    except Exception:
        pass
    return {}

_PARAMS = _load_params()


def _param(key: str, env_key: str, default, cast):
    if key in _PARAMS:
        try:
            return cast(_PARAMS[key])
        except (TypeError, ValueError):
            return cast(default)
    env_val = os.getenv(env_key)
    if env_val is not None:
        try:
            return cast(env_val)
        except (TypeError, ValueError):
            return cast(default)
    return cast(default)


def _param_list(key: str, default: list):
    value = _PARAMS.get(key, default)
    return list(value) if isinstance(value, (list, tuple)) else list(default)


# === 한국투자증권 (KIS) API ===
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_MODE = os.getenv("KIS_MODE", "virtual")  # virtual | real
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
KIS_ACCOUNT_PRODUCT = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
MOCK_LOSS_COOLDOWN_DIVISOR = int(os.getenv("MOCK_LOSS_COOLDOWN_DIVISOR", "1"))  # 모의투자 쿨다운 단축 (기본 1=변화 없음, 4=1/4)

# KIS API Base URL
if KIS_MODE == "real":
    KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
else:
    KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"

# === Z.AI (GLM-5.1) [DEPRECATED 2026-05-14] ===
# 잔고 소진으로 API 중단. 뉴스 분석은 키워드 기반 fallback으로 대체.
# 추후 외인수급/기술적 분석 모듈로 완전 대체 예정.
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-5.1")
ZAI_ENABLED = os.getenv("ZAI_ENABLED", "false").lower() == "true"

# === 텔레그램 ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === 거래 설정 ===
STOP_LOSS_PERCENT = _param("STOP_LOSS_PERCENT", "STOP_LOSS_PERCENT", 5.0, float)       # 손절 % (기본 5%)
TAKE_PROFIT_PERCENT = _param("TAKE_PROFIT_PERCENT", "TAKE_PROFIT_PERCENT", 10.0, float)  # 익절 % (기본 10%)
MAX_DAILY_LOSS_PERCENT = _param("MAX_DAILY_LOSS_PERCENT", "MAX_DAILY_LOSS_PERCENT", 5.0, float)  # 일일 최대 손실 %
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))  # 연속 손실 한계
POSITION_SIZE_PERCENT = float(os.getenv("POSITION_SIZE_PERCENT", "100.0"))  # 자금 대비 포지션 비율

# === 뉴스 스캐너 설정 ===
NEWS_SCAN_INTERVAL = int(os.getenv("NEWS_SCAN_INTERVAL", "1800"))  # 초 단위 (기본 30분)
SCAN_INTERVAL_MARKET = int(os.getenv("SCAN_INTERVAL_MARKET", "300"))  # 장중 스캔 주기 (초, 기본 5분)
SCAN_INTERVAL_OFF = int(os.getenv("SCAN_INTERVAL_OFF", "1800"))  # 장외 스캔 주기 (초, 기본 30분)

# 뉴스 신선도 기준 (초) — 이 시간 이상 지난 뉴스는 점수 깎임
NEWS_FRESHNESS_THRESHOLD = int(os.getenv("NEWS_FRESHNESS_THRESHOLD", "3600"))  # 1시간

# ── 5영역 뉴스 소스 (경제/기술/정치/글로벌/사회) ──

# [경제] 기존 경제/산업 키워드
NEWS_SOURCES_ECONOMY_KR = [
    "https://news.google.com/rss/search?q=텅스텐+OR+희토류+OR+반도체+OR+방산+OR+AI+OR+2차전지&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=주식+OR+증시+OR+코스피+OR+코스닥+OR+수출&hl=ko&gl=KR&ceid=KR:ko",
]
NEWS_SOURCES_ECONOMY_US = [
    "https://news.google.com/rss/search?q=semiconductor+OR+AI+OR+defense+OR+tungsten+OR+rare+earth&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=NVIDIA+OR+AMD+OR+TSMC+OR+ASML+OR+stock+market&hl=en&gl=US&ceid=US:en",
]

# [기술/과학] AI, 양자, 로봇, 바이오
NEWS_SOURCES_TECH_KR = [
    "https://news.google.com/rss/search?q=인공지능+OR+양자컴퓨터+OR+로봇+OR+자율주행+OR+바이오+OR+신약개발&hl=ko&gl=KR&ceid=KR:ko",
]
NEWS_SOURCES_TECH_US = [
    "https://news.google.com/rss/search?q=artificial+intelligence+OR+quantum+computing+OR+robotics+OR+autonomous+driving+OR+biotech&hl=en&gl=US&ceid=US:en",
]

# [정치/정책] 규제, 법안, 무역
NEWS_SOURCES_POLITICS_KR = [
    "https://news.google.com/rss/search?q=규제+OR+법안+OR+무역+OR+제재+OR+수출통제+OR+정책&hl=ko&gl=KR&ceid=KR:ko",
]
NEWS_SOURCES_POLITICS_US = [
    "https://news.google.com/rss/search?q=CHIPS+act+OR+trade+war+OR+sanctions+OR+export+control+OR+tariff&hl=en&gl=US&ceid=US:en",
]

# [글로벌/지정학] 전쟁, 분쟁, 공급망
NEWS_SOURCES_GLOBAL_KR = [
    "https://news.google.com/rss/search?q=전쟁+OR+분쟁+OR+공급망+OR+외교+OR+미중+OR+한반도&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=钨+OR+稀土+OR+半导体&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
]
NEWS_SOURCES_GLOBAL_US = [
    "https://news.google.com/rss/search?q=geopolitics+OR+supply+chain+OR+conflict+OR+sanctions+OR+Taiwan&hl=en&gl=US&ceid=US:en",
]

# [사회/인구] 인구, 소비, 라이프스타일
NEWS_SOURCES_SOCIETY_KR = [
    "https://news.google.com/rss/search?q=고령화+OR+인구+OR+소비트렌드+OR+ESG+OR+탄소중립&hl=ko&gl=KR&ceid=KR:ko",
]
NEWS_SOURCES_SOCIETY_US = [
    "https://news.google.com/rss/search?q=aging+population+OR+consumer+trend+OR+ESG+OR+carbon+neutral+OR+climate&hl=en&gl=US&ceid=US:en",
]

# 영역 태그 매핑
NEWS_SOURCES_BY_CATEGORY = {
    "economy": NEWS_SOURCES_ECONOMY_KR + NEWS_SOURCES_ECONOMY_US,
    "technology": NEWS_SOURCES_TECH_KR + NEWS_SOURCES_TECH_US,
    "politics": NEWS_SOURCES_POLITICS_KR + NEWS_SOURCES_POLITICS_US,
    "global": NEWS_SOURCES_GLOBAL_KR + NEWS_SOURCES_GLOBAL_US,
    "society": NEWS_SOURCES_SOCIETY_KR + NEWS_SOURCES_SOCIETY_US,
}

# 하위 호환 — 기존 변수 유지
NEWS_SOURCES_KR = NEWS_SOURCES_ECONOMY_KR + NEWS_SOURCES_TECH_KR + NEWS_SOURCES_POLITICS_KR + NEWS_SOURCES_GLOBAL_KR + NEWS_SOURCES_SOCIETY_KR
NEWS_SOURCES_US = NEWS_SOURCES_ECONOMY_US + NEWS_SOURCES_TECH_US + NEWS_SOURCES_POLITICS_US + NEWS_SOURCES_GLOBAL_US + NEWS_SOURCES_SOCIETY_US
NEWS_SOURCES = NEWS_SOURCES_KR + NEWS_SOURCES_US

# === 해외주식 (미국) 설정 ===
US_STOCK_ENABLED = os.getenv("US_STOCK_ENABLED", "true").lower() == "true"
KR_BUDGET = _param("KR_BUDGET", "KR_BUDGET", 5000000.0, float)       # 국내 전용 자금 (원)
US_BUDGET = float(os.getenv("US_BUDGET", "5000"))           # 미국 전용 자금 (USD)
MIN_ORDER_KRW = float(os.getenv("MIN_ORDER_KRW", "100000"))  # 국내 최소 주문금액 (원)
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "100"))     # 미국 최소 주문금액 (USD)
MIN_QTY = int(os.getenv("MIN_QTY", "1"))                     # 최소 주문 수량
MIN_ORDER_QUANTITY = _param("MIN_ORDER_QUANTITY", "MIN_ORDER_QUANTITY", 2, int)
MIN_ORDER_VALUE_KRW = _param("MIN_ORDER_VALUE_KRW", "MIN_ORDER_VALUE_KRW", 500000.0, float)

# 미국 장 운영 시간 (KST 기준)
US_MARKET_OPEN_KST = int(os.getenv("US_MARKET_OPEN_KST", "2330"))   # 23:30
US_MARKET_CLOSE_KST = int(os.getenv("US_MARKET_CLOSE_KST", "0600")) # 06:00

# 미국 관심 종목/ETF
US_WATCHLIST = os.getenv("US_WATCHLIST", "NVDA,AMD,AVGO,INTC,SMH,SOXX,QQQ,SPY").split(",")

# 동적 손절/익절 설정
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))               # ATR 기간
STOP_LOSS_ATR_MULTI = float(os.getenv("STOP_LOSS_ATR_MULTI", "2.0"))  # 손절 = ATR × 2.0
TAKE_PROFIT_ATR_MULTI = float(os.getenv("TAKE_PROFIT_ATR_MULTI", "3.0"))  # 익절 = ATR × 3.0
STOP_LOSS_MIN_PCT = float(os.getenv("STOP_LOSS_MIN_PCT", "5.0"))    # 최소 손절 % (3→5: 모의투자 stop-loss 너무 빡셈)
STOP_LOSS_MAX_PCT = float(os.getenv("STOP_LOSS_MAX_PCT", "10.0"))   # 최대 손절 % (8→10)
TRAILING_STOP_ATR_MULTI = float(os.getenv("TRAILING_STOP_ATR_MULTI", "1.5"))  # 트레일링 = ATR × 1.5
TRAILING_ACTIVATE_PCT = float(os.getenv("TRAILING_ACTIVATE_PCT", "5.0"))     # 트레일링 활성화 수익률 % (3→5: 너무 빨리 활성화)

# 분할 매수/매도 설정
SCALE_IN_STEPS = _param_list("SCALE_IN_STEPS", [0.50, 0.30, 0.20])      # 분할 매수 비율
SCALE_IN_THRESHOLDS = [0.0, 2.0, 5.0]    # 분할 매수 수익률 기준 (%)
SCALE_OUT_STEPS = [0.30, 0.40, 0.30]     # 분할 매도 비율

# === 다종목 포지션 설정 ===
MAX_POSITIONS_PER_MARKET = _param("MAX_POSITIONS_PER_MARKET", "MAX_POSITIONS_PER_MARKET", 3, int)  # 시장당 최대 종목 수
MAX_SAME_SECTOR = int(os.getenv("MAX_SAME_SECTOR", "2"))  # 같은 섹터 최대 종목 수

# === 시간 기반 청산 설정 ===
KR_EOD_EXIT_MINUTES = int(os.getenv("KR_EOD_EXIT_MINUTES", "30"))  # KR 장마감 N분 전 청산 (14:30)
US_EOD_EXIT_MINUTES = int(os.getenv("US_EOD_EXIT_MINUTES", "30"))  # US 장마감 N분 전 청산
POSITION_MAX_HOLD_HOURS = int(os.getenv("POSITION_MAX_HOLD_HOURS", "24"))  # 최대 보유 시간 (시간)
POSITION_FLAT_PCT = float(os.getenv("POSITION_FLAT_PCT", "0.5"))  # N시간 후 수익률 이것보다 낮으면 청산 (%)
BREAKEVEN_ACTIVATE_PCT = float(os.getenv("BREAKEVEN_ACTIVATE_PCT", "3.0"))  # 원금보호 활성화 최소 수익률 (%) — +3.0% 이상 도달 시 SL을 진입가로 상향 (1.0→3.0: 2026-05-23, 마이크로-손절 방지. PROPOSE-2026-05-23-breakeven-fix.md)
BREAKEVEN_MIN_PCT = float(os.getenv("BREAKEVEN_MIN_PCT", "1.0"))  # 원금보호 최소 임계값 (%) — ATR이 높은 종목도 최소 +1.0% 필요 (0.3→1.0: 2026-05-23, 고변동성 종목 틱노이즈 손절 방지. PROPOSE-2026-05-23-breakeven-fix.md)

# === 잔액 동기화 ===
BALANCE_SYNC_INTERVAL = int(os.getenv("BALANCE_SYNC_INTERVAL", "1800"))  # 잔액 동기화 주기 (초, 기본 30분)
ORDER_COOLDOWN_SECONDS = int(os.getenv("ORDER_COOLDOWN_SECONDS", "900"))  # 동일 종목 재진입 쿨다운
ORDER_INFLIGHT_TTL_SECONDS = int(os.getenv("ORDER_INFLIGHT_TTL_SECONDS", "120"))  # 주문 진행중 락 TTL
API_DEGRADED_RETRY_LIMIT = int(os.getenv("API_DEGRADED_RETRY_LIMIT", "3"))  # API 연속 장애 허용 횟수
# KIS VTS는 프로세스가 여러 개면 0.5초 간격에서도 EGW00201이 반복되어 기본값을 보수적으로 둔다.
KIS_MIN_REQUEST_INTERVAL = float(os.getenv("KIS_MIN_REQUEST_INTERVAL", "1.0"))
TELEGRAM_DEDUPE_SECONDS = int(os.getenv("TELEGRAM_DEDUPE_SECONDS", "21600"))  # 동일 알림 억제(기본 6시간)
ORDER_FAIL_COOLDOWN_SECONDS = int(os.getenv("ORDER_FAIL_COOLDOWN_SECONDS", "21600"))  # 동일 주문 실패 재시도 억제

# === AI 안전장치 ===
MAX_AI_POSITION_PCT = _param("MAX_AI_POSITION_PCT", "MAX_AI_POSITION_PCT", 50.0, float)  # AI 신호만으로 최대 자금 비율 %
AI_SCORE_MIN = int(os.getenv("AI_SCORE_MIN", "4"))  # 키워드 fallback 최소 신호 점수 (2→4: 약한 신호 차단, 진입 품질 향상)
THEME_STRENGTH_MIN = float(os.getenv("THEME_STRENGTH_MIN", "7.0"))  # 메가 테마 최소 강도
THEME_ANALYSIS_TIME = os.getenv("THEME_ANALYSIS_TIME", "08:30")  # 일일 테마 분석 시간

# === 진입 타점 설정 ===
ENTRY_MAX_CHASE_PCT = float(os.getenv("ENTRY_MAX_CHASE_PCT", "5.0"))  # 당일 상승 최대 허용 % (2→5: 추격 제한 완화)
ENTRY_SUPPORT_TOLERANCE = float(os.getenv("ENTRY_SUPPORT_TOLERANCE", "2.0"))  # 지지선 근처 허용 폭 % (1→2)
ENTRY_MIN_VOL_RATIO = float(os.getenv("ENTRY_MIN_VOL_RATIO", "0.8"))  # 최소 거래량 비율 (0.5→0.8: 정상 거래량 종목만 진입)
ENTRY_MAX_ATR_RATIO = float(os.getenv("ENTRY_MAX_ATR_RATIO", "8.0"))  # 최대 ATR/가격 비율 % (8.0%: 2026-05-21. 6.0→8.0. KR ATR 6-8% 자연범위. 씨젠 17.3% 차단 유지, HD현대일렉 7.7%/에스엠 6.4% Layer 6 통과. L3/L2는 여전히 차단 — 안전장치 유지)

# Pre-filter는 6-Layer보다 앞에서 "명백히 약한 후보"를 더 일찍 걸러낸다.
# 2026-06-04 완화: trend·volume 둘 다 약할 때만 reject (OR → AND).
# 6-Layer가 본필터 (Layer 2 trend votes>=1+override, Layer 3 volume 0.8)가 작동 중.
# 단일 약세(prefilter 부분 일치)는 6-Layer로 위임해 정밀 분석 기회를 준다.
PREFILTER_TREND_MA_RATIO = float(os.getenv("PREFILTER_TREND_MA_RATIO", "0.95"))
PREFILTER_VOLUME_RATIO = float(os.getenv("PREFILTER_VOLUME_RATIO", "0.6"))
PREFILTER_VOLUME_RATIO_INCREASING = float(os.getenv("PREFILTER_VOLUME_RATIO_INCREASING", "0.5"))
PREFILTER_VOLUME_RATIO_DECREASING = float(os.getenv("PREFILTER_VOLUME_RATIO_DECREASING", "0.8"))
MIN_BUY_VOTES_FOR_BUY = int(os.getenv("MIN_BUY_VOTES_FOR_BUY", "1"))  # BUY 판정 최소 투표 (2→1: 2026-06-04. 1표 buy 118건이 REJECT되던 병목 해소. 단, 6-Layer의 trend(votes>=1+override), volume, momentum, sr, volatility 안전장치는 그대로 유지. 단일지표 합의를 강제하면 filter_stats의 43% REJECT가 줄고 실제 매매 기회 확대. 사용자는 .env로 2/3으로 상향 가능.)
TECH_SCORE_MIN = float(os.getenv("TECH_SCORE_MIN", "10"))  # 기술 점수 최소값 (12→10: 2026-06-05. 3회 연속 scan에서 모든 후보가 9점으로 차단. 6-Layer가 본필터 역할. 10은 약세장에서도 일부 후보 통과, 6-Layer에서 실제 거부.)
TECH_SCORE_OVERRIDE = float(os.getenv("TECH_SCORE_OVERRIDE", "25"))  # 기술 점수 오버라이드 (40→25: 2026-05-21. KR 후보 최고점이 16-24로 40 도달 불가. 25는 KR 상위권이 Layer 2 bypass 가능, US 고점(38-52)도 override. Layer 2 완전 무력화 방지하면서 진입 기회 확보.)
WEAK_BUY_MIN_TECH_SCORE = float(os.getenv("WEAK_BUY_MIN_TECH_SCORE", "14"))  # WEAK_BUY 실행 기술점수 하한 (2026-05-29. 모의투자에서 WEAK_BUY 기준 entry_score=60 고정이라 품질 차별화 불가 → tech_score로 필터. KR 후보 14-16점은 상위권, 5-13은 하위. 14 이상만 실행.)
ENTRY_OPTIMAL_TIME_KR = (10, 14)  # 국내 최적 진입 시간 (시)
ENTRY_OPTIMAL_TIME_US = (1, 4)    # 미국 최적 진입 시간 (KST 시)

# === 미국장 라이브 전환 준비 ===
US_READINESS_MODE = os.getenv("US_READINESS_MODE", "true").lower() == "true"
US_REQUIRE_REAL_MODE = os.getenv("US_REQUIRE_REAL_MODE", "true").lower() == "true"
US_MICRO_LIVE_MAX_NOTIONAL = float(os.getenv("US_MICRO_LIVE_MAX_NOTIONAL", "300"))  # 초기 라이브 최대 주문금액(USD)
US_MIN_CASH_BUFFER = float(os.getenv("US_MIN_CASH_BUFFER", "100"))  # 미국장 주문 후 남길 현금 버퍼(USD)

# === 추천/선별 ===
RECOMMENDATION_LOOKBACK = int(os.getenv("RECOMMENDATION_LOOKBACK", "5"))
RECOMMENDATION_REPEAT_PENALTY = float(os.getenv("RECOMMENDATION_REPEAT_PENALTY", "7.5"))
ENTRY_FAILURE_PENALTY = float(os.getenv("ENTRY_FAILURE_PENALTY", "5.0"))
STOP_OUT_PENALTY = float(os.getenv("STOP_OUT_PENALTY", "6.0"))

# === 데이터 디렉토리 ===
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"
THEME_DB_FILE = DATA_DIR / "theme_db.json"
TRADE_HISTORY_FILE = LOGS_DIR / "trade_history.json"

# 디렉토리 생성
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


def get_config_summary() -> str:
    """설정 요약 문자열 반환."""
    return (
        f"📊 설정 요약\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏦 한투 모드: {'모의투자' if KIS_MODE == 'virtual' else '실전'}\n"
        f"📞 계좌: {KIS_ACCOUNT_NO}-{KIS_ACCOUNT_PRODUCT}\n"
        f"🛡️ 손절: {STOP_LOSS_PERCENT}% (ATR {STOP_LOSS_ATR_MULTI}x) | 익절: ATR {TAKE_PROFIT_ATR_MULTI}x\n"
        f"📉 일일최대손실: {MAX_DAILY_LOSS_PERCENT}%\n"
        f"🔄 연속손실한계: {MAX_CONSECUTIVE_LOSSES}회\n"
        f"💰 포지션비율: {POSITION_SIZE_PERCENT}%\n"
        f"📊 US 모멘텀 기반 시장국면 감지\n"
        f"📰 뉴스: 키워드 기반 (AI 분석 제거)\n"
        f"📡 스캔주기: {NEWS_SCAN_INTERVAL // 60}분"
    )


def get_mode_order_ids() -> dict:
    """현재 KIS 모드의 주문 TR ID 매핑."""
    if KIS_MODE == "real":
        return {
            "kr_buy": "TTTC0802U",
            "kr_sell": "TTTC0801U",
            "us_buy": "TTTS1002U",   # 해외주식 매수 (S 포함!)
            "us_sell": "TTTS1001U",  # 해외주식 매도 (S 포함!)
        }
    return {
        "kr_buy": "VTTC0802U",
        "kr_sell": "VTTC0801U",
        "us_buy": "VTTS1002U",   # 해외주식 모의 매수 (S 포함!)
        "us_sell": "VTTS1001U",  # 해외주식 모의 매도 (S 포함!)
    }


def validate_runtime_config() -> dict:
    """실행 시점 설정 검증."""
    errors = []
    warnings = []

    if KIS_MODE not in ("virtual", "real"):
        errors.append(f"KIS_MODE 값 오류: {KIS_MODE}")
    if not KIS_ACCOUNT_NO:
        errors.append("KIS_ACCOUNT_NO 미설정")
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        errors.append("KIS_APP_KEY/KIS_APP_SECRET 미설정")
    if MIN_QTY < 1:
        errors.append("MIN_QTY는 1 이상이어야 함")
    if MIN_ORDER_KRW <= 0 or MIN_ORDER_USD <= 0:
        errors.append("최소 주문금액은 0보다 커야 함")
    if KIS_MODE == "real":
        warnings.append("실전 모드 실행 중 — 주문 전 계좌 확인 필요")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "mode": KIS_MODE,
        "base_url": KIS_BASE_URL,
        "account": f"{KIS_ACCOUNT_NO}-{KIS_ACCOUNT_PRODUCT}",
        "order_ids": get_mode_order_ids(),
    }
