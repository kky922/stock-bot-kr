"""
Stock Bot Multi-Agent System — 공식 실행 엔트리포인트 v2.

아키텍처: Orchestrator 기반 직렬 파이프라인
  뉴스수집 → 메가테마 → 종목후보 → 기술순위 → 5-Layer 진입 → 리스크 → 실행 → 감시

Usage:
    python run_agents.py              # 정상 실행 (스케줄 루프)
    python run_agents.py --test       # 연결 테스트만
    python run_agents.py --status     # 현재 상태만 출력
    python run_agents.py --once       # 파이프라인 1회만 실행
"""
from __future__ import annotations

import json
import atexit
import logging
import fcntl
import hashlib
import os
import signal
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).parent
sys.path.insert(0, str(ROOT_DIR))

import requests as _requests

import config
from hot_reload import HotReloader
from core.data_store import DataStore
from infra.context_from import check_tier2_trigger as _context_tier2_trigger
from kis_api import KISAPI
from agents.orchestrator import Orchestrator
from agents.trade_executor import TradeExecutorAgent
from agents.monitor_agent import MonitorAgent
from infra.runtime_policy import load_runtime_policy, runtime_entry_skip_reason


# ─── 텔레그램 알림 헬퍼 ─────────────────────────────────────────
# [Claude Fix] v2 파이프라인에서 매수/매도/오류 시 텔레그램으로 알림 전송
_PROCESS_LOCK_FILE = None
_TELEGRAM_DEDUPE_FILE = config.DATA_DIR / "agents" / "telegram_dedupe.json"
_ORDER_FAIL_COOLDOWN_FILE = config.DATA_DIR / "agents" / "order_fail_cooldown.json"
_PENDING_ENTRIES_FILE = config.DATA_DIR / "agents" / "pending_entries.json"


def _load_json_file(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("JSON 로드 실패(%s): %s", path, e)
    return default


def _save_json_file(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as e:
        logger.warning("JSON 저장 실패(%s): %s", path, e)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_pending_entries() -> list:
    """저장된 장마감/쿨다운 대기 진입 목록을 불러온다."""
    return _load_json_file(_PENDING_ENTRIES_FILE, [])


def _save_pending_entries(entries: list):
    """대기 진입 목록을 파일에 저장한다. 빈 목록도 저장(초기화)한다."""
    _save_json_file(_PENDING_ENTRIES_FILE, entries)


def _tg_should_send(key: str, cooldown_seconds: int = None) -> bool:
    cooldown = cooldown_seconds or config.TELEGRAM_DEDUPE_SECONDS
    now = time.time()
    state = _load_json_file(_TELEGRAM_DEDUPE_FILE, {"sent": {}})
    sent = state.get("sent", {})
    last = float(sent.get(key, 0) or 0)
    if last and now - last < cooldown:
        logger.info("🔕 텔레그램 중복 억제: key=%s remain=%ds", key[:16], int(cooldown - (now - last)))
        return False

    # 오래된 fingerprint는 정리해서 파일이 무한히 커지지 않게 한다.
    max_age = max(cooldown * 2, 86400)
    state["sent"] = {k: v for k, v in sent.items() if now - float(v or 0) < max_age}
    state["sent"][key] = now
    _save_json_file(_TELEGRAM_DEDUPE_FILE, state)
    return True


def _tg(text: str):
    """텔레그램 메시지 전송 (단방향 push)."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.warning("텔레그램 알림 실패: %s", e)


def _tg_dedupe(text: str, key: str = None, cooldown_seconds: int = None):
    """반복성이 높은 알림 전용 발송 헬퍼."""
    dedupe_key = key or _fingerprint(text)
    if _tg_should_send(dedupe_key, cooldown_seconds):
        _tg(text)


def _order_fail_key(market: str, code: str, reason: str) -> str:
    return f"{market}:{code}:{_fingerprint(reason or '')[:16]}"


def _order_fail_in_cooldown(market: str, code: str, reason: str = "") -> bool:
    now = time.time()
    state = _load_json_file(_ORDER_FAIL_COOLDOWN_FILE, {"entries": {}})
    entries = state.get("entries", {})
    if reason:
        keys = [_order_fail_key(market, code, reason)]
    else:
        prefix = f"{market}:{code}:"
        keys = [k for k in entries if k.startswith(prefix)]
    for key in keys:
        last = float(entries.get(key, 0) or 0)
        if last and now - last < config.ORDER_FAIL_COOLDOWN_SECONDS:
            return True
    return False


def _mark_order_fail_cooldown(market: str, code: str, reason: str = ""):
    key = _order_fail_key(market, code, reason)
    now = time.time()
    state = _load_json_file(_ORDER_FAIL_COOLDOWN_FILE, {"entries": {}})
    entries = state.get("entries", {})
    max_age = max(config.ORDER_FAIL_COOLDOWN_SECONDS * 2, 86400)
    state["entries"] = {k: v for k, v in entries.items() if now - float(v or 0) < max_age}
    state["entries"][key] = now
    _save_json_file(_ORDER_FAIL_COOLDOWN_FILE, state)


def _record_order_fail(store: DataStore, signal: dict, error_code: str, message: str):
    entry_verdict = signal.get("entry_verdict") or signal.get("verdict") or signal.get("signal_verdict") or "?"
    entry_score = signal.get("entry_score", signal.get("score", 0))
    store.append_recommendation({
        "timestamp": datetime.now().isoformat(),
        "market": signal.get("market"),
        "code": signal.get("code"),
        "name": signal.get("name"),
        "theme": signal.get("theme", ""),
        "selection_score": signal.get("selection_score", 0),
        "relative_strength_score": signal.get("relative_strength_score", 0),
        "volume_score": signal.get("volume_score", 0),
        "breakout_score": signal.get("breakout_score", 0),
        "recent_alert_count": signal.get("recent_alert_count", 0),
        "entry_verdict": entry_verdict,
        "entry_score": entry_score,
        "outcome": "order_fail",
        "error_code": error_code,
        "message": message,
    })


def _record_blocked_candidate(store: DataStore, signal: dict, reason_tag: str, reason_text: str):
    """실행 불가 후보를 추천 히스토리에 남겨 슬롯 병목을 가시화한다."""
    reasons = signal.get("risk_reasons", []) or []
    entry_verdict = signal.get("entry_verdict") or signal.get("verdict") or signal.get("signal_verdict") or "?"
    entry_score = signal.get("entry_score", signal.get("score", 0))
    store.append_recommendation({
        "timestamp": datetime.now().isoformat(),
        "market": signal.get("market"),
        "code": signal.get("code"),
        "name": signal.get("name"),
        "theme": signal.get("theme", ""),
        "selection_score": signal.get("selection_score", 0),
        "relative_strength_score": signal.get("relative_strength_score", 0),
        "volume_score": signal.get("volume_score", 0),
        "breakout_score": signal.get("breakout_score", 0),
        "recent_alert_count": signal.get("recent_alert_count", 0),
        "entry_verdict": entry_verdict,
        "entry_score": entry_score,
        "outcome": reason_tag,
        "blocked_reason": reason_text,
        "risk_reasons": reasons,
        "is_slot_full_block": any("포지션_full" in str(r) for r in reasons),
        "is_sector_block": any("섹터집중" in str(r) for r in reasons),
        "is_duplicate_block": any("already_held" in str(r) for r in reasons),
        "is_cooldown_block": any("cooldown" in str(r) for r in reasons),
    })



def _parse_ts(value: object):
    try:
        value = str(value).strip()
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None



def _slot_rebalance_score(slot: dict, now_dt: datetime | None = None) -> float:
    """슬롯을 얼마나 빨리 정리할지 판단하는 약세 점수."""
    now_dt = now_dt or datetime.now(timezone.utc)

    def _num(key: str) -> float:
        try:
            return float(slot.get(key, 0) or 0)
        except Exception:
            return 0.0

    entry_price = _num("entry_price")
    highest_price = _num("highest_price")
    stop_loss_price = _num("stop_loss_price")
    take_profit_price = _num("take_profit_price")

    age_hours = 0.0
    entry_time = _parse_ts(slot.get("entry_time")) or _parse_ts(slot.get("synced_at"))
    if entry_time is not None:
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now_dt - entry_time).total_seconds() / 3600.0)

    gain_pct = 0.0
    if entry_price > 0 and highest_price > 0:
        gain_pct = max(0.0, (highest_price / entry_price - 1.0) * 100.0)

    stop_room_pct = 0.0
    if entry_price > 0 and stop_loss_price > 0:
        stop_room_pct = max(0.0, (entry_price - stop_loss_price) / entry_price * 100.0)

    target_room_pct = 0.0
    if entry_price > 0 and take_profit_price > 0:
        target_room_pct = max(0.0, (take_profit_price - entry_price) / entry_price * 100.0)

    score = 0.0
    score += min(age_hours / 2.0, 6.0)
    score += min(gain_pct / 2.0, 5.0)
    score += min(target_room_pct / 3.0, 2.0)
    score += min(stop_room_pct / 4.0, 1.0)

    if not highest_price:
        score += 0.5

    return round(score, 2)



def _select_replacement_slot(store, market: str, signal: dict):
    """3/3에서도 더 좋은 후보면 약한 슬롯을 밀어내도록 교체 대상 슬롯을 고른다."""
    max_positions = config.MAX_POSITIONS_PER_MARKET
    if store.get_open_slot_count(market) < max_positions:
        return None

    slots_data = store.load_all_slots(market) or {}
    slots = slots_data.get("slots", {}) if isinstance(slots_data, dict) else {}
    occupied = [(slot_id, slot) for slot_id, slot in slots.items() if isinstance(slot, dict) and slot.get("code")]
    if not occupied:
        return None

    now_dt = datetime.now(timezone.utc)
    weakest_slot_id, weakest_slot = max(occupied, key=lambda item: _slot_rebalance_score(item[1], now_dt=now_dt))
    weakest_score = _slot_rebalance_score(weakest_slot, now_dt=now_dt)

    candidate_score = float(signal.get("entry_score", signal.get("score", 0)) or 0)
    candidate_priority = candidate_score / 10.0
    min_entry_score = float(getattr(config, "PORTFOLIO_REBALANCE_MIN_ENTRY_SCORE", 75) or 75)
    margin = float(getattr(config, "PORTFOLIO_REBALANCE_MARGIN", 0.75) or 0.75)

    if candidate_score < min_entry_score:
        return None

    if candidate_score < 90 and candidate_priority < weakest_score + margin:
        return None

    return {
        "slot_id": weakest_slot_id,
        "slot": weakest_slot,
        "slot_score": weakest_score,
        "candidate_score": candidate_score,
        "candidate_priority": round(candidate_priority, 2),
    }


def _acquire_process_lock() -> bool:
    """run_agents.py 중복 실행을 방지하는 advisory lock."""
    global _PROCESS_LOCK_FILE
    lock_path = config.DATA_DIR / "agents" / "run_agents.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _PROCESS_LOCK_FILE = lock_path.open("w")
    try:
        fcntl.flock(_PROCESS_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("❌ 다른 run_agents.py 인스턴스가 이미 실행 중입니다. 중복 실행을 종료합니다.")
        return False
    _PROCESS_LOCK_FILE.write(str(os.getpid()))
    _PROCESS_LOCK_FILE.truncate()
    _PROCESS_LOCK_FILE.flush()
    return True

# 로깅 설정 (RotatingFileHandler — 50MB × 3백업)
from logging.handlers import RotatingFileHandler

_file_handler = RotatingFileHandler(
    config.LOGS_DIR / "agent_system.log",
    maxBytes=50 * 1024 * 1024,  # 50 MB
    backupCount=3,
    encoding="utf-8",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _file_handler,
    ],
)
logger = logging.getLogger("agent_system")


# ─── 장시간 판단 ────────────────────────────────────────────────
def _is_kr_market_hours() -> bool:
    """한국 장 시간 (09:10~15:30). 개장 직후 10분은 KIS VTS 서버 불안정으로 제외."""
    now = datetime.now()
    t = now.time()
    weekday = now.weekday()
    if weekday >= 5:  # 주말
        return False
    return dt_time(9, 10) <= t <= dt_time(15, 30)


def _is_us_market_hours() -> bool:
    """미국 장 시간 (한국시간 기준).
    EDT(썸머타임, 3~11월): 22:30~05:00 KST
    EST(겨울타임, 11~3월): 23:30~06:00 KST
    
    KST 기준 US 장 매핑:
      월요일 22:30 ~ 화요일 05:00  = US Monday session
      화요일 22:30 ~ 수요일 05:00  = US Tuesday session
      수요일 22:30 ~ 목요일 05:00  = US Wednesday session
      목요일 22:30 ~ 금요일 05:00  = US Thursday session
      금요일 22:30 ~ 토요일 05:00  = US Friday session
    """
    now = datetime.now()
    t = now.time()
    weekday = now.weekday()

    # [Claude Fix] 썸머타임(EDT, 3~11월) 적용 — 기존 23:30 고정은 겨울(EST)만 맞음
    month = now.month
    is_edt = 3 <= month <= 10  # 대략적 EDT 기간
    if is_edt:
        open_t, close_t = dt_time(22, 30), dt_time(5, 0)
    else:
        open_t, close_t = dt_time(23, 30), dt_time(6, 0)

    # [Fix] 정확한 KST → US 장 시간 매핑
    if weekday == 5:   # 토요일: 00:00~close_t 까지만 (금요일 US 세션 잔여)
        return t <= close_t
    if weekday == 6:   # 일요일: US 시장 닫힘 (미국은 토요일)
        return False
    if weekday == 0:   # 월요일: open_t 이후만 (이전은 일요일 US = 닫힘)
        return t >= open_t
    # 화~금 (weekday 1~4): open_t 이후 or close_t 이전
    return t >= open_t or t <= close_t


def _get_scan_interval() -> int:
    """장시간에 따른 스캔 주기 (초). 주말에는 1시간, 장중=300s, 장외평일=1800s."""
    if _is_kr_market_hours() or _is_us_market_hours():
        return config.SCAN_INTERVAL_MARKET  # 장중: 짧은 주기
    now = datetime.now()
    if now.weekday() >= 5:  # 주말 — 불필요한 API 호출 감소
        return 3600
    return config.SCAN_INTERVAL_OFF  # 장외: 긴 주기


def _seconds_until_next_market_open() -> int:
    """다음 개장까지 남은 시간(초). 이미 장중이면 0 반환."""
    if _is_kr_market_hours() or _is_us_market_hours():
        return 0

    now = datetime.now()
    min_sec = float("inf")

    # KR: 09:00 KST 평일
    for days_ahead in (0, 1):
        d = now + timedelta(days=days_ahead)
        if d.weekday() < 5:  # 평일
            kr_open = d.replace(hour=9, minute=0, second=0, microsecond=0)
            secs = (kr_open - now).total_seconds()
            if secs > 0 and secs < min_sec:
                min_sec = secs

    # US: 22:30 KST (EDT, 3~10월) / 23:30 KST (EST, 11~2월)
    month = now.month
    us_hour = 22 if 3 <= month <= 10 else 23
    for days_ahead in (0, 1):
        d = now + timedelta(days=days_ahead)
        # US market opens KST Mon-Fri 22:30/23:30 → US Mon-Fri session
        if d.weekday() < 5:
            us_open = d.replace(hour=us_hour, minute=30, second=0, microsecond=0)
            secs = (us_open - now).total_seconds()
            if secs > 0 and secs < min_sec:
                min_sec = secs

    if min_sec == float("inf"):
        return 3600  # 안전 기본값 1시간
    return int(min_sec)


def _record_skip(skip_stats: dict, reason: str, **payload):
    """스킵 원인을 구조화해 누적/로그합니다."""
    skip_stats[reason] = skip_stats.get(reason, 0) + 1
    log_payload = {"reason": reason, **payload}
    logger.info("⏭️ skip_reason=%s", json.dumps(log_payload, ensure_ascii=False, sort_keys=True))


def _get_us_operation_mode() -> str:
    """현재 미국장 운용 모드 설명 문자열."""
    if not config.US_STOCK_ENABLED:
        return "비활성"
    if config.KIS_MODE == "virtual":
        return "virtual readiness"
    if config.US_READINESS_MODE:
        return "readiness"
    return "live autotrade"


def _get_us_cash_snapshot(store: DataStore) -> dict:
    """미국장 마지막 정상 현금 스냅샷."""
    market_state = store.get_market_state("US")
    return {
        "available_usd": float(market_state.get("last_good_deposit", 0) or 0),
        "total_usd": float(market_state.get("last_good_balance", 0) or 0),
        "balance_sync_status": market_state.get("balance_sync_status", "unknown"),
        "last_error": market_state.get("last_balance_sync_error", ""),
        "status_code": market_state.get("last_balance_status_code", ""),
    }


# ─── 연결 테스트 ────────────────────────────────────────────────
def test_connections():
    """API 연결 테스트."""
    print("\n" + "=" * 60)
    print("🔍 Stock Bot Multi-Agent System v2 — 연결 테스트")
    print("=" * 60)

    # KIS API 테스트
    print("\n🏦 한국투자증권 API...")
    kis = KISAPI()
    if kis.test_connection():
        print("  ✅ 연결 성공")
        bal = kis.get_balance()
        print(f"  💰 국내 잔고: {bal.get('total_eval', 0):,.0f}원")

        if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
            us_bal = kis.get_us_balance()
            print(f"  💰 미국 잔고: ${us_bal.get('total_usd', 0):,.2f}")
            print(f"  💵 미국 가용현금: ${us_bal.get('available_usd', 0):,.2f}")
            print(f"  🧾 미국 잔고 상태: {us_bal.get('balance_status', 'unknown')} {us_bal.get('status_code', '')}".rstrip())
    else:
        print("  ❌ 연결 실패")
        return False

    # 환율 테스트
    if config.US_STOCK_ENABLED:
        rate = kis.get_exchange_rate()
        print(f"  💱 USD/KRW 환율: {rate:,.1f}")

    # AI API 테스트 — Z.AI GLM [DEPRECATED 2026-05-14]
    print("\n🤖 AI API (ZAI)...")
    if config.ZAI_ENABLED:
        try:
            import requests
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.ZAI_API_KEY}",
            }
            body = {
                "model": config.ZAI_MODEL,
                "messages": [{"role": "user", "content": "테스트"}],
                "max_tokens": 10,
            }
            resp = requests.post(
                f"{config.ZAI_BASE_URL}/chat/completions",
                headers=headers, json=body, timeout=10,
            )
            if resp.status_code == 200:
                print("  ✅ GLM API 연결 성공")
            else:
                print(f"  ❌ GLM API 오류: {resp.status_code}")
        except Exception as e:
            print(f"  ❌ GLM API 연결 실패: {e}")
    else:
        print("  ⏭️ ZAI 비활성화 (ZAI_ENABLED=false)")

    # 텔레그램 테스트
    print("\n📱 텔레그램...")
    if config.TELEGRAM_BOT_TOKEN:
        print("  ✅ 토큰 설정됨")
    else:
        print("  ⚠️ 토큰 없음 — 텔레그램 비활성화")

    # 설정 요약
    print("\n" + "-" * 60)
    print(config.get_config_summary())
    print(f"🇺🇸 미국주식: {'활성' if config.US_STOCK_ENABLED else '비활성'}")
    print(f"🇺🇸 미국장 모드: {_get_us_operation_mode()}")
    print(f"💰 국내 자금: {config.KR_BUDGET:,.0f}원")
    print(f"💰 미국 자금: ${config.US_BUDGET:,.0f}")
    print(f"📊 동적 손절: ATR × {config.STOP_LOSS_ATR_MULTI} (범위: {config.STOP_LOSS_MIN_PCT}%~{config.STOP_LOSS_MAX_PCT}%)")
    print(f"📊 동적 익절: ATR × {config.TAKE_PROFIT_ATR_MULTI}")
    print(f"📈 트레일링: ATR × {config.TRAILING_STOP_ATR_MULTI} (활성화: +{config.TRAILING_ACTIVATE_PCT}%)")
    print(f"📊 분할 매수: {config.SCALE_IN_STEPS}")
    print(f"📊 분할 매도: {config.SCALE_OUT_STEPS}")
    print("=" * 60)

    # 파이프라인 구성요소 테스트
    print("\n🔧 파이프라인 구성요소...")
    try:
        store = DataStore()
        orch = Orchestrator(store)
        print("  ✅ Orchestrator 생성 성공")
        status = orch.get_status()
        print(f"  📊 활성 테마: {status['active_themes']}")
        print(f"  📊 KR 포지션: {status['kr_positions']}")
        print(f"  📊 US 포지션: {status['us_positions']}")
    except Exception as e:
        print(f"  ❌ Orchestrator 생성 실패: {e}")
        return False

    print("\n✅ 모든 연결 테스트 통과!")
    return True


# ─── 상태 출력 ──────────────────────────────────────────────────
def show_status():
    """현재 시스템 상태 출력."""
    store = DataStore()
    orch = Orchestrator(store)

    print("\n" + "=" * 60)
    print("📊 Stock Bot Multi-Agent System v2 — 현재 상태")
    print("=" * 60)

    # 오케스트레이터 상태
    status = orch.get_status()
    print(f"\n🎯 활성 테마: {status['active_themes']}")
    print(f"🕐 마지막 스캔: {status['last_scan'] or '없음'}")
    print(f"⚙️ 미국장 운용 모드: {_get_us_operation_mode()}")
    print(f"🇰🇷 KR 포지션: {status['kr_positions']} (자금 사용률: {status['kr_usage']:.1f}%)")
    print(f"🇺🇸 US 포지션: {status['us_positions']} (자금 사용률: {status['us_usage']:.1f}%)")
    if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
        us_cash = _get_us_cash_snapshot(store)
        print(
            f"💵 미국 가용현금: ${us_cash['available_usd']:,.2f} "
            f"(총평가: ${us_cash['total_usd']:,.2f}, sync={us_cash['balance_sync_status']})"
        )
        if us_cash["status_code"]:
            print(f"  🧾 미국 잔고 상태코드: {us_cash['status_code']}")
        if us_cash["last_error"]:
            print(f"  ⚠️ 미국 잔고 오류: {us_cash['last_error']}")

    # 실행 가능한 신호
    signals = orch.get_actionable_signals()
    if signals:
        print(f"\n🚀 실행 가능한 신호 ({len(signals)}건):")
        for s in signals:
            print(f"  {s['code']} {s['name']}: "
                  f"타점={s['entry_verdict']}({s['entry_score']}) "
                  f"기술={s['tech_score']} 테마={s.get('theme', '')}")
    else:
        print("\n📋 실행 가능한 신호 없음")

    # 포지션 상세
    for market, label in [("KR", "🇰🇷 국내"), ("US", "🇺🇸 미국")]:
        positions = store.load_all_positions(market)
        if positions:
            print(f"\n{label} 포지션:")
            for pos in positions:
                print(f"  종목: {pos.get('name', '?')} ({pos.get('code', '?')})")
                print(f"  진입가: {pos.get('entry_price', 0):,.2f}")
                print(f"  수량: {pos.get('quantity', 0)}")
                print(f"  손절가: {pos.get('stop_loss_price', 0):,.2f}")
                print(f"  익절가: {pos.get('take_profit_price', 0):,.2f}")
                print(f"  최고가: {pos.get('highest_price', 0):,.2f}")
                print(f"  트레일링: {'🟢 활성' if pos.get('trailing_active') else '🔴 비활성'}")
        else:
            print(f"\n{label} 포지션: 없음")

    # 최근 거래
    trades = store.get_trades(limit=5)
    if trades:
        print(f"\n📋 최근 거래 ({len(trades)}건):")
        for t in trades[-5:]:
            pnl = t.get("pnl", 0)
            print(f"  {t.get('timestamp', '')[:16]} | "
                  f"[{t.get('market', '?')}] {t.get('name', '?')} "
                  f"PnL: {pnl:+,.2f} ({t.get('reason', '')})")

    print("=" * 60)


# ─── 계좌 포지션 동기화 ──────────────────────────────────────────
def _sync_positions_from_account(kis_api: KISAPI, store: DataStore):
    """계좌 실제 보유 종목과 DataStore 슬롯을 양방향 동기화."""
    try:
        balance = kis_api.get_balance()
        if balance.get("balance_status") == "failed":
            logger.warning("⚠️ 잔고 조회 실패 상태라 슬롯 동기화 제거를 건너뜁니다.")
            return
        stocks = balance.get("stocks", [])
        account_codes = {
            str(stock.get("code", "")).strip()
            for stock in stocks
            if str(stock.get("code", "")).strip()
        }

        # 계좌에 더 이상 없는 KR 슬롯은 stale 상태로 보고 제거한다.
        # 단, 생성된 지 30분 미만인 슬롯은 KIS VTS 포지션 반영 지연을 고려하여 제거하지 않음 (2026-05-18 fix)
        _stale_slot_grace_minutes = 30
        _now_utc = datetime.now(timezone.utc)
        for slot_id, position in list(store.load_all_slots("KR").items()):
            code = str(position.get("code", "")).strip()
            if not code or code in account_codes:
                continue
            # KIS VTS는 매수 체결 후 포지션이 계좌 잔고 API에 반영되기까지
            # 수 분~수십 분이 걸릴 수 있음. 최근 진입한 슬롯은 보호한다.
            entry_time_str = position.get("entry_time", "")
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                    age_mins = (_now_utc - entry_dt).total_seconds() / 60.0
                    if age_mins < _stale_slot_grace_minutes:
                        logger.info(
                            "🧹 stale 스킵 (진입 %d분 전): %s %s — KIS VTS 반영 지연 추정",
                            int(age_mins), slot_id, position.get("name", code),
                        )
                        continue
                except (ValueError, TypeError):
                    pass
            store.save_slot(slot_id, None)
            store.append_trade({
                "timestamp": datetime.now().isoformat(),
                "slot_id": slot_id,
                "market": "KR",
                "code": code,
                "name": position.get("name", code),
                "action": "reconcile_remove",
                "quantity": position.get("quantity", 0),
                "entry_price": position.get("entry_price", 0),
                "reason": "계좌 잔고에 없는 봇 슬롯 제거",
            })
            logger.warning("🧹 stale 슬롯 제거: %s %s", slot_id, position.get("name", code))

        for stock in stocks:
            code = str(stock.get("code", "")).strip()
            if not code:
                continue
            slot_id = f"KR_{code}"
            existing = store.load_slot(slot_id)
            if existing and existing.get("code"):
                quantity = int(stock.get("quantity", 0))
                avg_price = float(stock.get("avg_price", 0))
                existing_quantity = int(existing.get("quantity", 0) or 0)
                existing_entry = float(existing.get("entry_price", 0) or 0)
                if quantity > 0 and avg_price > 0 and (
                    existing_quantity != quantity or round(existing_entry, 3) != round(avg_price, 3)
                ):
                    updated = dict(existing)
                    updated.update({
                        "quantity": quantity,
                        "total_quantity": quantity,
                        "entry_price": avg_price,
                        "stop_loss_price": round(avg_price * (1 - config.STOP_LOSS_MIN_PCT / 100)),
                        "take_profit_price": round(avg_price * (1 + config.TAKE_PROFIT_PERCENT / 100)),
                        "entry_time": existing.get("entry_time") or existing.get("synced_at") or datetime.now().isoformat(),
                        "synced_at": datetime.now().isoformat(),
                    })
                    # 2026-05-29: breakeven_protect가 활성화된 slot은 SL/TP를 재계산하지 않음
                    if existing.get("breakeven_protect"):
                        updated["stop_loss_price"] = existing["stop_loss_price"]
                        updated["breakeven_protect"] = True
                    store.save_slot(slot_id, updated)
                    store.append_trade({
                        "timestamp": datetime.now().isoformat(),
                        "slot_id": slot_id,
                        "market": "KR",
                        "code": code,
                        "name": stock.get("name", code),
                        "action": "reconcile_update",
                        "old_quantity": existing_quantity,
                        "quantity": quantity,
                        "old_entry_price": existing_entry,
                        "entry_price": avg_price,
                        "reason": "계좌 잔고 기준으로 봇 슬롯 수량/평단 동기화",
                    })
                    logger.warning(
                        "🔄 슬롯 동기화: %s %s 수량 %s→%s, 평단 %.3f→%.3f",
                        slot_id, stock.get("name", code), existing_quantity, quantity, existing_entry, avg_price
                    )
                continue  # 이미 봇이 추적 중인 포지션
            # 봇이 모르는 포지션 = 수동 매수 → DataStore에 등록
            name = stock.get("name", code)
            avg_price = float(stock.get("avg_price", 0))
            quantity = int(stock.get("quantity", 0))
            if quantity <= 0 or avg_price <= 0:
                continue
            position = {
                "code": code,
                "name": name,
                "market": "KR",
                "entry_price": avg_price,
                "quantity": quantity,
                "total_quantity": quantity,
                "stop_loss_price": round(avg_price * (1 - config.STOP_LOSS_MIN_PCT / 100)),
                "take_profit_price": round(avg_price * (1 + config.TAKE_PROFIT_PERCENT / 100)),
                "highest_price": avg_price,
                "trailing_active": False,
                "source": "manual",
                "entry_time": datetime.now().isoformat(),
                "synced_at": datetime.now().isoformat(),
                "order_state": "filled",
            }
            store.save_slot(slot_id, position)
            logger.info("📌 수동 포지션 등록: %s %s %d주 @ %d", name, code, quantity, avg_price)
            _tg(
                f"📌 <b>수동 포지션 감지 — 봇이 감시 시작</b>\n"
                f"종목: {name} ({code})\n"
                f"수량: {quantity}주 @ {avg_price:,.0f}원\n"
                f"손절: {position['stop_loss_price']:,} | 익절: {position['take_profit_price']:,}"
            )
    except Exception as e:
        logger.warning("⚠️ 포지션 동기화 실패: %s", e)


def _has_position_slot_capacity(store: DataStore, market: str) -> bool:
    """현재 슬롯 원장 기준으로 신규 진입 여유가 있는지 최종 확인한다."""
    open_count = store.get_open_slot_count(market)
    return open_count < config.MAX_POSITIONS_PER_MARKET


def _calculate_first_order_quantity(store: DataStore, market: str, current_price: float) -> int:
    """첫 진입 주문 수량을 남은 시장 예산 기준으로 계산한다."""
    if current_price <= 0:
        return 0

    budget = config.KR_BUDGET if market == "KR" else config.US_BUDGET
    positions = store.load_all_positions(market)
    invested = sum(float(p.get("invest_amount", 0) or 0) for p in positions)
    remaining_budget = max(0.0, budget - invested)
    if remaining_budget <= 0:
        return 0

    max_affordable_qty = int(remaining_budget / current_price)
    if max_affordable_qty <= 0:
        return 0

    first_ratio = config.SCALE_IN_STEPS[0] if isinstance(config.SCALE_IN_STEPS, list) else 0.5
    quantity = int(remaining_budget * first_ratio / current_price)

    min_qty = max(1, int(config.MIN_QTY))
    if quantity < min_qty and min_qty <= max_affordable_qty:
        quantity = min_qty

    if market == "KR":
        min_order_quantity = max(1, int(config.MIN_ORDER_QUANTITY))
        if quantity < min_order_quantity:
            if max_affordable_qty >= min_order_quantity:
                logger.info(
                    "📈 [주문수량] 최소 매수 수량 보장: %s주 → %s주 (현재가 %.0f원)",
                    quantity,
                    min_order_quantity,
                    current_price,
                )
                quantity = min_order_quantity
            else:
                logger.info(
                    "⏭️ [주문수량] 최소 매수 수량 미달로 skip: quantity=%s min=%s max_affordable=%s price=%.0f",
                    quantity,
                    min_order_quantity,
                    max_affordable_qty,
                    current_price,
                )
                return 0

    if quantity <= 0:
        return 0
    return min(quantity, max_affordable_qty)


# ─── 대기 진입(Pending Entry) 처리 ──────────────────────────────
def _process_pending_entries(orch: Orchestrator, executor: TradeExecutorAgent, kis_api: KISAPI,
                              runtime_policy=None):
    """시장이 방금 열렸다면 저장된 대기 진입 목록을 처리한다.

    오프시간에 발견된 BUY(94) 후보(신세계, 현대차 등)가 다음 스캔에서
    테마 가중치 변화로 후보 풀에서 사라져 영영 진입 기회를 잃는 문제 해결.
    """
    pending = _load_pending_entries()
    if not pending:
        return 0

    kr_open = _is_kr_market_hours()
    us_open = _is_us_market_hours()
    now = datetime.now()
    stale_cutoff = now - timedelta(hours=24)
    bought = 0
    remaining = []

    for entry in pending:
        saved_at_str = entry.get("saved_at", "")
        try:
            saved_at = datetime.fromisoformat(saved_at_str) if saved_at_str else now
        except Exception:
            saved_at = now

        if saved_at < stale_cutoff:
            logger.info("⏭️ [펜딩] 24시간 초과 폐기: %s %s", entry.get("code"), entry.get("name"))
            continue

        market = entry["market"]
        code = entry["code"]
        name = entry.get("name", code)
        verdict = entry.get("verdict", "?")
        score = entry.get("score", 0)

        if verdict not in ("STRONG_BUY", "BUY"):
            continue  # 실행 불가 판정은 재대기 불필요

        # 아직 시장이 닫힘 → 재대기
        if market == "KR" and not kr_open:
            remaining.append(entry)
            continue
        if market == "US" and not us_open:
            remaining.append(entry)
            continue

        # 런타임 정책 확인 (P0 / P1 심볼 제외)
        if runtime_policy is not None:
            skip_reason = runtime_entry_skip_reason(entry, runtime_policy)
            if skip_reason:
                logger.info("⏭️ [펜딩] 정책 차단 %s: %s %s", skip_reason, code, name)
                _record_blocked_candidate(orch.store, entry, "runtime_policy_block", skip_reason)
                continue

        # 슬롯 용량
        if not _has_position_slot_capacity(orch.store, market):
            remaining.append(entry)
            continue

        # 중복 보유
        if orch.store.find_slot_by_code(market, code):
            logger.info("⏭️ [펜딩] 이미 보유 중: %s %s — 폐기", code, name)
            continue  # 이미 보유 — 재대기 불필요

        # 쿨다운
        slot_id = f"{market}_{code}"
        if orch.store.get_cooldown(slot_id):
            remaining.append(entry)
            continue
        if _order_fail_in_cooldown(market, code):
            remaining.append(entry)
            continue

        # 현재가 조회
        try:
            if market == "KR":
                price_data = kis_api.get_stock_price(code)
                current_price = price_data.get("current_price", 0) or price_data.get("stck_prpr", 0)
                current_price = int(current_price)
            else:
                price_data = kis_api.get_us_stock_price(code)
                current_price = price_data.get("current_price", 0) or price_data.get("last", 0)
            if current_price <= 0:
                logger.warning("⚠️ [펜딩] 현재가 조회 실패: %s %s", code, name)
                remaining.append(entry)
                continue
        except Exception as e:
            logger.warning("⚠️ [펜딩] 현재가 조회 오류 %s %s: %s", code, name, e)
            remaining.append(entry)
            continue

        # 수량 계산
        quantity = _calculate_first_order_quantity(orch.store, market, current_price)
        if quantity <= 0:
            logger.info("⏭️ [펜딩] 수량 부족: %s %s (price=%d)", code, name, current_price)
            continue  # 예산 부족 — 재대기 불필요

        # SL/TP 계산
        sl_price = int(current_price * (1 - config.STOP_LOSS_MIN_PCT / 100))
        tp_price = int(current_price * (1 + (config.STOP_LOSS_MIN_PCT / 100)
                                            * config.TAKE_PROFIT_ATR_MULTI / config.STOP_LOSS_ATR_MULTI))
        if market == "KR":
            from kis_api import KISAPI
            sl_price = KISAPI._round_kr_price_to_tick(int(sl_price))
            tp_price = KISAPI._round_kr_price_to_tick(int(tp_price))

        decision = {
            "code": code,
            "name": name,
            "market": market,
            "price": current_price,
            "quantity": quantity,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "atr": 0,
            "reason": f"펜딩 진입 (점수={score})",
            "theme": entry.get("theme", ""),
        }

        try:
            result = executor.execute_slot_buy(slot_id, decision)
            if result.get("success"):
                bought += 1
                logger.info("✅ [펜딩] 매수 실행 완료: %s %s %d주 @%d SL=%d TP=%d",
                            code, name, quantity, current_price, sl_price, tp_price)
                _tg(
                    f"📈 <b>[펜딩] 매수 실행!</b>\n"
                    f"종목: {name} ({code}) [{market}]\n"
                    f"수량: {quantity}주 @ {current_price:,}\n"
                    f"손절: {sl_price:,} | 익절: {tp_price:,}\n"
                    f"테마: {entry.get('theme', '-')} | 점수: {score}\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                )
            else:
                msg = result.get("message", "")
                logger.info("⏭️ [펜딩] 매수 실패 %s %s: %s", code, name, msg)
                if "슬롯" in msg or "수량" in msg or "쿨다운" in msg:
                    remaining.append(entry)  # 일시적 — 재시도 가능
                # 그 외 실패(호가단위 등)는 재대기 불필요
        except Exception as e:
            logger.error("❌ [펜딩] 매수 예외 %s %s: %s", code, name, e)
            remaining.append(entry)

    _save_pending_entries(remaining)
    return bought


# ─── 파이프라인 1회 실행 ─────────────────────────────────────────
def run_pipeline_once(kis_api: KISAPI, orch: Orchestrator,
                      executor: TradeExecutorAgent):
    """전체 파이프라인 1회 실행: 스캔 → 진입 (감시는 별도 스레드)."""
    logger.info("=" * 50)
    logger.info("📡 파이프라인 실행 시작")
    logger.info("=" * 50)

    # 0) 계좌 포지션 동기화 (수동 매수 감지)
    _sync_positions_from_account(kis_api, orch.store)

    # 0.25) 런타임 정책 로드 (P0/P1 TODO 상태)
    runtime_policy = load_runtime_policy(
        orch.store.load_all_slots().values() if hasattr(orch.store, 'load_all_slots') else None
    )
    if runtime_policy and runtime_policy.block_new_entries:
        logger.warning("🚫 런타임 정책 활성화: block_new_entries=True (%s)",
                       "; ".join(runtime_policy.reasons))
    elif runtime_policy and runtime_policy.conservative_mode:
        logger.warning("⚠️ 런타임 정책: conservative_mode — 일부 심볼 제외: %s",
                       ", ".join(runtime_policy.excluded_codes | runtime_policy.excluded_names))

    # 0.5) 저장된 대기 진입(Pending Entry) 처리
    # 장마감/쿨다운으로 미처 진입하지 못한 좋은 후보가
    # 다음 스캔에서 후보 풀 변화로 사라지는 문제 해결.
    # 시장이 열리면 가장 먼저 처리하고, 그 후에 정규 스캔 실행.
    pending_bought = _process_pending_entries(orch, executor, kis_api, runtime_policy=runtime_policy)
    if pending_bought:
        logger.info("✅ [펜딩] %d건 매수 완료 — 정규 파이프라인 계속 실행", pending_bought)
        # pending_entries.json은 _process_pending_entries 내부에서 저장됨

    # 1) 스캔 파이프라인 (뉴스→테마→후보→기술→진입→리스크)
    results = orch.run_scan_pipeline(kis_api=kis_api)

    skip_stats = {}
    pending_entries = []  # 장 마감/슬롯 쿨다운으로 대기 중인 후보 (2026-05-18 추가)
    buy_success = 0
    buy_fail = 0
    sell_success = 0
    sell_fail = 0
    us_balance_cache = None

    if not results:
        logger.info("📭 실행 가능한 후보 없음 — 감시만 진행")
    else:
        for candidate in results:
            allowed_verdicts = ("STRONG_BUY", "BUY", "WEAK_BUY") if config.KIS_MODE == "virtual" else ("STRONG_BUY", "BUY")
            candidate_verdict = candidate.get("entry_verdict") or candidate.get("verdict") or candidate.get("signal_verdict") or "?"
            candidate_score = candidate.get("entry_score", candidate.get("score", 0))
            if candidate_verdict not in allowed_verdicts:
                verdict = candidate_verdict
                entry_reason = candidate.get("entry_reason", "")
                _record_skip(
                    skip_stats,
                    "entry_reject",
                    market=candidate.get("market"),
                    code=candidate.get("code"),
                    verdict=verdict,
                    entry_reason=entry_reason,
                )
                _record_blocked_candidate(
                    orch.store,
                    candidate,
                    reason_tag="blocked_entry_verdict",
                    reason_text=f"entry_verdict={verdict} | {entry_reason}".strip(),
                )
            elif not candidate.get("risk_can_enter"):
                risk_reasons = candidate.get("risk_reasons", []) or []
                _record_skip(
                    skip_stats,
                    "risk_block",
                    market=candidate.get("market"),
                    code=candidate.get("code"),
                    risk_reasons=risk_reasons,
                )
                blocked_reason = ", ".join(risk_reasons[:3]) if risk_reasons else "risk_block"
                _record_blocked_candidate(
                    orch.store,
                    candidate,
                    reason_tag="blocked_risk",
                    reason_text=blocked_reason,
                )

        # 2) 실행 가능한 신호 → 매수 실행
        actionable = orch.get_actionable_signals()
        for signal in actionable:
            market = signal["market"]
            code = signal["code"]
            name = signal["name"]
            score = signal.get("entry_score", 0)
            orch.store.append_recommendation({
                "timestamp": datetime.now().isoformat(),
                "market": market,
                "code": code,
                "name": name,
                "theme": signal.get("theme", ""),
                "selection_score": signal.get("selection_score", 0),
                "relative_strength_score": signal.get("relative_strength_score", 0),
                "volume_score": signal.get("volume_score", 0),
                "breakout_score": signal.get("breakout_score", 0),
                "recent_alert_count": signal.get("recent_alert_count", 0),
                "entry_verdict": signal.get("entry_verdict") or signal.get("verdict") or signal.get("signal_verdict") or "?",
                "entry_score": signal.get("entry_score", signal.get("score", 0)),
                "risk_can_enter": signal.get("risk_can_enter", None),
                "outcome": "candidate",
            })

            # [Claude Fix] 장 시간 체크 — 장 마감 시에는 매수 실행 안 함
            if market == "KR" and not _is_kr_market_hours():
                logger.info("⏸️ [KR] 장 마감 — 매수 대기: %s %s", code, name)
                _record_skip(skip_stats, "market_closed", market=market, code=code, name=name)
                pending_entries.append({
                    "market": market, "code": code, "name": name,
                    "verdict": signal.get("entry_verdict", "?"),
                    "score": signal.get("entry_score", 0),
                    "theme": signal.get("theme", ""),
                    "reason": "market_closed",
                    "saved_at": datetime.now().isoformat(),
                })
                continue
            if market == "US" and not _is_us_market_hours():
                logger.info("⏸️ [US] 장 마감 — 매수 대기: %s %s", code, name)
                _record_skip(skip_stats, "market_closed", market=market, code=code, name=name)
                pending_entries.append({
                    "market": market, "code": code, "name": name,
                    "verdict": signal.get("entry_verdict", "?"),
                    "score": signal.get("entry_score", 0),
                    "theme": signal.get("theme", ""),
                    "reason": "market_closed",
                    "saved_at": datetime.now().isoformat(),
                })
                continue

            # 런타임 정책 확인 (P0 / P1 심볼 제외)
            skip_reason = runtime_entry_skip_reason(signal, runtime_policy)
            if skip_reason:
                _record_skip(skip_stats, "runtime_policy_block", market=market, code=code, name=name)
                _record_blocked_candidate(orch.store, signal, "runtime_policy_block", skip_reason)
                logger.info("🚫 [%s] 정책 차단 %s: %s %s", market, skip_reason, code, name)
                continue

            if _order_fail_in_cooldown(market, code):
                _record_skip(skip_stats, "order_fail_cooldown", market=market, code=code, name=name)
                logger.info("🔕 주문 실패 쿨다운 — 주문 스킵: %s %s", code, name)
                continue

            # Pre-check: execute_slot_buy 내부 슬롯 쿨다운 검증을 통과할 수 있는지 사전 확인
            # (이미 보유/이미 사용 중도 여기서 잡아 order_fail_cooldown 중복을 방지)
            _slot_id = f"{market}_{code}"
            _slot_cd = orch.store.get_cooldown(_slot_id)
            if _slot_cd:
                _record_skip(skip_stats, "slot_cooldown", market=market, code=code, name=name)
                logger.info("🕐 [%s] 슬롯 쿨다운 — 매수 대기: %s %s", market, code, name)
                pending_entries.append({
                    "market": market, "code": code, "name": name,
                    "verdict": signal.get("entry_verdict", "?"),
                    "score": signal.get("entry_score", 0),
                    "theme": signal.get("theme", ""),
                    "reason": "slot_cooldown",
                    "saved_at": datetime.now().isoformat(),
                })
                continue

            if not _has_position_slot_capacity(orch.store, market):
                open_count = orch.store.get_open_slot_count(market)
                max_positions = config.MAX_POSITIONS_PER_MARKET
                replacement = _select_replacement_slot(orch.store, market, signal)
                if replacement is None:
                    _record_skip(
                        skip_stats,
                        "slot_capacity_block",
                        market=market,
                        code=code,
                        name=name,
                        open_positions=open_count,
                        max_positions=max_positions,
                    )
                    _record_blocked_candidate(
                        orch.store,
                        signal,
                        reason_tag="blocked_slot_capacity",
                        reason_text=f"open_positions={open_count}/{max_positions}",
                    )
                    logger.info(
                        "🛑 [%s] 최종 슬롯 한도 도달 — 매수 스킵: %s %s (%d/%d)",
                        market, code, name, open_count, max_positions,
                    )
                    continue

                weak_slot = replacement["slot"]
                weak_slot_id = replacement["slot_id"]
                weak_code = weak_slot.get("code", "?")
                weak_name = weak_slot.get("name") or weak_code
                logger.info(
                    "♻️ [%s] 슬롯 교체 후보 감지: 신규 %s %s(score=%.1f) > 약한 슬롯 %s %s(score=%.2f)",
                    market,
                    code,
                    name,
                    float(replacement["candidate_score"]),
                    weak_code,
                    weak_name,
                    float(replacement["slot_score"]),
                )
                sell_reason = (
                    f"포트폴리오 교체: new={code} score={float(replacement['candidate_score']):.1f} "
                    f"> weak={weak_code} score={float(replacement['slot_score']):.2f}"
                )
                sell_result = executor.execute_slot_sell(weak_slot_id, reason=sell_reason)
                if not sell_result.get("success"):
                    logger.warning(
                        "⚠️ [%s] 슬롯 교체용 청산 실패 — 매수 보류: %s %s | weak=%s %s",
                        market,
                        code,
                        name,
                        weak_code,
                        weak_name,
                    )
                    _record_skip(
                        skip_stats,
                        "slot_rebalance_sell_fail",
                        market=market,
                        code=weak_code,
                        name=weak_name,
                    )
                    continue
                logger.info(
                    "✅ [%s] 약한 슬롯 정리 완료 — 새 후보 진입 준비: %s %s", market, code, name
                )

            logger.info("🚀 실행 신호: %s %s (%s) 타점점수=%d",
                        code, name, market, score)

            # 현재가 조회 (5회 재시도 포함) — 실패 시 이번 사이클 스킵
            try:
                if market == "KR":
                    price_info = kis_api.get_stock_price(code)
                else:
                    price_info = kis_api.get_us_stock_price(code)
                current_price = price_info.get("current", 0)
            except Exception as e:
                logger.error("❌ 현재가 조회 실패 %s: %s — 30분 후 재시도", code, e)
                _record_skip(skip_stats, "price_fail", market=market, code=code, name=name, error=str(e))
                continue

            if current_price <= 0:
                logger.warning("⚠️ 현재가 0 — 이번 사이클 스킵, 30분 후 재시도: %s", code)
                _record_skip(skip_stats, "price_invalid", market=market, code=code, name=name)
                continue

            if market == "US" and config.KIS_MODE != "real":
                # [개선] 모의투자에서도 US 주문 테스트 — US_BUDGET을 가용자금으로 사용
                available_usd = config.US_BUDGET
                total_usd = config.US_BUDGET
                balance_status = "ok"
            elif market == "US":
                try:
                    if us_balance_cache is None:
                        us_balance_cache = kis_api.get_us_balance()
                    available_usd = float(us_balance_cache.get("available_usd", 0) or 0)
                    total_usd = float(us_balance_cache.get("total_usd", 0) or 0)
                    balance_status = us_balance_cache.get("balance_status", "unknown")
                    balance_status_code = us_balance_cache.get("status_code", "")
                except Exception as e:
                    logger.warning("⚠️ 미국 가용 현금 조회 실패 %s: %s", code, e)
                    _record_skip(skip_stats, "usd_cash_check_fail", market=market, code=code, name=name, error=str(e))
                    continue
                if balance_status in {"unsupported", "failed"}:
                    logger.warning(
                        "⚠️ [US] 가용 USD 조회 불가 — 주문 스킵: %s %s (status=%s code=%s)",
                        code, name, balance_status, balance_status_code,
                    )
                    _record_skip(
                        skip_stats,
                        "usd_cash_unknown",
                        market=market,
                        code=code,
                        name=name,
                        balance_status=balance_status,
                        status_code=balance_status_code,
                    )
                    continue
                if available_usd <= 0:
                    logger.warning("⚠️ [US] 가용 USD 부족 — 주문 스킵: %s %s (available=$%.2f total=$%.2f)", code, name, available_usd, total_usd)
                    _record_skip(
                        skip_stats,
                        "usd_cash_unavailable",
                        market=market,
                        code=code,
                        name=name,
                        available_usd=round(available_usd, 2),
                        total_usd=round(total_usd, 2),
                    )
                    continue

            # 1차 매수 수량 = 남은 시장 예산 × 첫 비율 ÷ 현재가
            quantity = _calculate_first_order_quantity(orch.store, market, current_price)
            order_value = quantity * current_price
            min_order_value = config.MIN_ORDER_KRW if market == "KR" else config.MIN_ORDER_USD
            if order_value < min_order_value:
                _record_skip(
                    skip_stats,
                    "min_order_block",
                    market=market,
                    code=code,
                    quantity=quantity,
                    current_price=current_price,
                    order_value=round(order_value, 2),
                    min_order_value=min_order_value,
                )
                continue

            # ATR 없을 때 고정 % 기반 SL/TP (호가단위 반올림)
            sl_price = round(current_price * (1 - config.STOP_LOSS_MIN_PCT / 100))
            tp_price = round(current_price * (1 + (config.STOP_LOSS_MIN_PCT / 100)
                                               * config.TAKE_PROFIT_ATR_MULTI / config.STOP_LOSS_ATR_MULTI))
            from kis_api import KISAPI
            sl_price = KISAPI._round_kr_price_to_tick(int(sl_price))
            tp_price = KISAPI._round_kr_price_to_tick(int(tp_price))

            slot_id = f"{market}_{code}"
            decision = {
                "code": code,
                "name": name,
                "market": market,
                "price": current_price,
                "quantity": quantity,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
                "atr": signal.get("atr", 0),
                "reason": f"파이프라인 진입 (점수={score})",
                "theme": signal.get("theme", ""),
            }

            # [2026-05-16] VTS 모드 미국 주문: KIS VTS는 해외주문 미지원 → API 호출 없이 스킵
            if market == "US" and config.KIS_MODE == "virtual":
                _record_skip(skip_stats, "us_virtual_skip", market=market, code=code, name=name)
                logger.info(
                    "🧪 [US] VTS 모드 — 주문 실행 생략: %s %s (SL=%d TP=%d)",
                    code, name, sl_price, tp_price,
                )
                continue

            try:
                # [Claude Fix] execute_slot_buy 사용 — _handle_buy는 scale_in_plan 없으면 silent return
                result = executor.execute_slot_buy(slot_id, decision)
                if result.get("success"):
                    buy_success += 1
                    logger.info("✅ 매수 실행 완료: %s %s %d주 @%d SL=%d TP=%d",
                                code, name, quantity, current_price, sl_price, tp_price)
                    _tg(
                        f"📈 <b>매수 실행!</b>\n"
                        f"종목: {name} ({code}) [{market}]\n"
                        f"수량: {quantity}주 @ {current_price:,}\n"
                        f"손절: {sl_price:,} | 익절: {tp_price:,}\n"
                        f"테마: {signal.get('theme', '-')} | 점수: {score}\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                    )
                else:
                    if result.get("readiness"):
                        _record_skip(skip_stats, "us_readiness_mode", market=market, code=code, name=name)
                        logger.info("🧪 [%s] readiness mode: %s %s 주문 검증만 수행", market, code, name)
                        continue
                    buy_fail += 1
                    err_code = result.get("error_code", "")
                    err_msg = result.get("message", "unknown")
                    _record_skip(
                        skip_stats,
                        "order_fail",
                        market=market,
                        code=code,
                        name=name,
                        error_code=err_code,
                        message=err_msg,
                    )
                    _record_order_fail(orch.store, signal, err_code, err_msg)
                    # 내부 검증 오류(슬롯 쿨다운/이미사용/이미보유)는 API 장애가 아니므로 order_fail_cooldown 제외
                    # VTS 모드 미국 주문 실패도 구조적 한계이므로 제외
                    if not (err_msg.startswith(("슬롯", "수량/가격")) or err_code == "POSITION_LIMIT" or (market == "US" and config.KIS_MODE == "virtual")):
                        _mark_order_fail_cooldown(market, code, err_msg)
                    logger.error(
                        "❌ 매수 주문 실패 %s %s: [%s] %s",
                        code, name, err_code, err_msg,
                    )
                    # [Fix] 주문 실패 시 텔레그램 즉시 알림 — 서버 장애 감지
                    _tg_dedupe(
                        f"❌ <b>매수 주문 실패</b>\n"
                        f"종목: {name} ({code}) [{market}]\n"
                        f"에러: [{err_code}] {err_msg[:200]}\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
                        key=f"order_fail:{market}:{code}:{_fingerprint(err_msg)[:16]}",
                        cooldown_seconds=config.ORDER_FAIL_COOLDOWN_SECONDS,
                    )
            except Exception as e:
                buy_fail += 1
                _record_skip(skip_stats, "order_exception", market=market, code=code, name=name, error=str(e))
                logger.error("❌ 매수 실행 실패 %s %s: %s", code, name, e)
                _record_order_fail(orch.store, signal, "exception", str(e))
                _mark_order_fail_cooldown(market, code, str(e))
                _tg_dedupe(
                    f"❌ 매수 실행 실패: {name} ({code})\n오류: {e}",
                    key=f"order_exception:{market}:{code}:{_fingerprint(str(e))[:16]}",
                    cooldown_seconds=config.ORDER_FAIL_COOLDOWN_SECONDS,
                )

    # 3) 포지션 감시는 별도 모니터 스레드에서 60초마다 실행
    #    (파이프라인에서 중복 호출하지 않음)

    # 4) 대기 진입 목록을 파일에 저장 (다음 스캔에서도 유지)
    # [Bug fix 2026-05-18] 현재 스캔의 펜딩만 저장하면 기존 항목이 소멸됨.
    # 신세계/현대차 BUY(94)가 다음 스캔에서 []로 덮어씌워져 영영 사라진 문제 해결.
    # 기존 항목을 로드하여 병합, 같은 (market, code) 중복 방지.
    _existing_pending = _load_pending_entries()
    _existing_codes = {(e["market"], e["code"]) for e in _existing_pending if isinstance(e, dict)}
    _new_pending = [e for e in pending_entries if (e["market"], e["code"]) not in _existing_codes]
    _save_pending_entries(_existing_pending + _new_pending)

    # WEAK_BUY 게이트 진단: 기술점수 미달로 차단된 WEAK_BUY 건수 계산
    _actionable = orch.get_actionable_signals()
    _weak_buy_total = sum(1 for r in results if r.get("entry_verdict") == "WEAK_BUY")
    _weak_buy_actionable = sum(1 for s in _actionable if s.get("entry_verdict") == "WEAK_BUY")
    _weak_buy_gate_blocked = _weak_buy_total - _weak_buy_actionable
    _weak_buy_min_score = float(getattr(config, "WEAK_BUY_MIN_TECH_SCORE", 14) or 14)

    logger.info(
        "📊 pipeline_summary=%s",
        json.dumps(
            {
                "candidate_count": len(results),
                "actionable_count": len(_actionable),
                "weak_buy_total": _weak_buy_total,
                "weak_buy_gate_blocked": _weak_buy_gate_blocked,
                "weak_buy_min_score": _weak_buy_min_score,
                "buy_success": buy_success,
                "buy_fail": buy_fail,
                "sell_success": sell_success,
                "sell_fail": sell_fail,
                "skip_stats": skip_stats,
                "pending_entries": pending_entries,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    # 텔레그램 스캔 결과 요약 — 항상 발송 (장시간 상태 표시)
    if results:
        kr_open = _is_kr_market_hours()
        us_open = _is_us_market_hours()
        verdict_emoji = {"STRONG_BUY": "🚀", "BUY": "✅", "WEAK_BUY": "🟡", "CAUTION": "⏳", "REJECT": "❌", "WAIT": "⏸️"}

        # 장 상태 라벨
        mkt_status_parts = []
        if kr_open:
            mkt_status_parts.append("🇰🇷장중")
        else:
            mkt_status_parts.append("🇰🇷장마감")
        if us_open:
            mkt_status_parts.append("🇺🇸장중")
        else:
            mkt_status_parts.append("🇺🇸장마감")
        mkt_status = " | ".join(mkt_status_parts)

        lines = [f"📊 <b>스캔 결과</b> {datetime.now().strftime('%H:%M')}\n🕐 {mkt_status}"]

        for r in results:
            v = r.get("entry_verdict", "?")
            em = verdict_emoji.get(v, "❓")
            reason = r.get("entry_reason", "")
            market_tag = r.get("market", "?")
            # 장 마감 상태에서는 해당 시장 종목에 ⚠️ 표시
            can_trade = (market_tag == "KR" and kr_open) or (market_tag == "US" and us_open)
            trade_warn = "" if can_trade else " ⚠️장마감"
            lines.append(f"{em} [{market_tag}] {r['name']}: {v}{trade_warn}" + (f" ({reason})" if reason else ""))

        if buy_success or sell_success:
            lines.append(f"\n💹 매수 {buy_success}건 | 매도 {sell_success}건 완료")
        if skip_stats.get("us_readiness_mode"):
            lines.append(f"\n🧪 미국장 readiness 검증 {skip_stats['us_readiness_mode']}건")

        # 장외 시간에는 "다음 장 시작 시 대기" 메시지 추가
        if not kr_open and not us_open:
            now = datetime.now()
            if now.weekday() < 5:
                # 평일: 다음 장 시간 안내
                if now.hour < 9:
                    lines.append(f"\n⏰ 🇰🇷 한국장 오전 9:10 개장 대기")
                elif now.hour < 22:
                    lines.append(f"\n⏰ 🇺🇸 미국장 밤 22:30 개장 대기 (EDT)")
                else:
                    lines.append(f"\n⏰ 🇺🇸 미국장 진입 중")
            else:
                lines.append(f"\n🏖️ 주말 — 장 미개장")

        scan_key_payload = {
            "results": [
                {
                    "market": r.get("market"),
                    "code": r.get("code"),
                    "verdict": r.get("entry_verdict"),
                    "risk": r.get("risk_can_enter"),
                    "reasons": r.get("risk_reasons", []),
                }
                for r in results
            ],
            "skip_stats": skip_stats,
            "kr_open": kr_open,
            "us_open": us_open,
        }
        _tg_dedupe(
            "\n".join(lines),
            key="scan_summary:" + _fingerprint(json.dumps(scan_key_payload, ensure_ascii=False, sort_keys=True)),
            cooldown_seconds=config.TELEGRAM_DEDUPE_SECONDS,
        )

    logger.info("📡 파이프라인 실행 완료")
    return results


# ─── 메인 실행 루프 ─────────────────────────────────────────────
def run_system():
    """멀티 에이전트 시스템 실행 (스케줄 루프)."""
    if not _acquire_process_lock():
        return

    logger.info("=" * 60)
    logger.info("🚀 Stock Bot Multi-Agent System v2.0")
    logger.info("🇰🇷 국내(KRX) + 🇺🇸 미국(NYSE/NASDAQ)")
    logger.info("=" * 60)

    cfg_check = config.validate_runtime_config()
    logger.info(
        "🧪 runtime_config=%s",
        json.dumps(
            {
                "mode": cfg_check.get("mode"),
                "account": cfg_check.get("account"),
                "base_url": cfg_check.get("base_url"),
                "order_ids": cfg_check.get("order_ids"),
                "warnings": cfg_check.get("warnings", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    if not cfg_check.get("ok"):
        for err in cfg_check.get("errors", []):
            logger.error("❌ 설정 오류: %s", err)
        logger.error("❌ 설정 검증 실패 — 종료")
        return

    # 1. 연결 테스트
    kis = KISAPI()
    if not kis.test_connection():
        logger.error("❌ KIS API 연결 실패 — 종료")
        return

    # 1.5 주문 API 가용성 사전 체크 (EGW00356 서버 차단 감지)
    logger.info("🔍 주문 API 가용성 확인 중...")
    order_check = kis.test_order_capability()
    if not order_check.get("can_trade", False):
        logger.error("🚨 주문 API 불가! — 주문이 차단된 상태입니다")
        err_detail = "\n".join(order_check.get("errors", ["알 수 없는 오류"]))
        _tg(
            f"🚨 <b>주문 API 차단 감지!</b>\n"
            f"모드: {config.KIS_MODE}\n"
            f"국내: {'✅' if order_check.get('can_order_domestic') else '❌'}\n"
            f"해외: {'✅' if order_check.get('can_order_overseas') else '❌'}\n"
            f"에러:\n{err_detail}\n\n"
            f"해결: KIS 고객센터(1588-6611) 또는\n"
            f"./switch_mode.sh real"
        )
        # 경고만 하고 계속 실행 (감시는 가능)
        logger.warning("⚠️ 감시 모드로만 실행 — 매수 불가")
    else:
        logger.info("✅ 주문 API 정상 — 거래 가능")

    # 2. 인프라 초기화
    def _params_log(event_type, message, detail=""):
        logger.info("%s | %s | %s", event_type, message, detail)

    reloader = HotReloader(
        params_path=str(config.ROOT_DIR / "params.json"),
        config_module=config,
        history_dir=str(config.ROOT_DIR / "params_history"),
        check_interval=5.0,
        log_func=_params_log,
    )
    reloader.start()
    atexit.register(reloader.stop)

    store = DataStore()
    orch = Orchestrator(store)
    executor = TradeExecutorAgent(store, kis_api=kis)
    monitor = MonitorAgent(store)
    store.update_market_state("KR", {
        "api_degraded_mode": not order_check.get("can_order_domestic", False),
        "balance_sync_status": "unknown",
    })
    store.update_market_state("US", {
        "api_degraded_mode": not order_check.get("can_order_overseas", False),
        "balance_sync_status": "unknown",
        "readiness_mode": config.US_READINESS_MODE,
    })

    # 3. 그레이스풀 셧다운
    shutdown = False

    def _signal_handler(sig, frame):
        nonlocal shutdown
        shutdown = True
        sig_name = signal.Signals(sig).name
        logger.info("⏹️ 종료 신호 수신: %s", sig_name)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── 모니터 전용 스레드 (1분 주기 손절/익절) ──────────────────
    import threading as _threading

    def _monitor_loop():
        """파이프라인과 독립된 1분 주기 포지션 감시 스레드."""
        logger.info("🔍 모니터 스레드 시작 (60초 주기)")
        while not shutdown:
            try:
                # 항상 monitor_all_slots() 호출 — 내부에서 시장 개장 여부와 무관하게
                # 체류시간 SL 강화(_tighten_sl_by_age)를 먼저 실행하고,
                # 장중에만 청산 신호를 처리함. (2026-05-18: 주말/장외 SL 강화 누락 버그 수정)
                exit_signals = monitor.monitor_all_slots()
                for es in exit_signals:
                    slot_id = es.get("slot_id")
                    reason = es.get("reason", "신호 청산")
                    if es.get("action") == "sell" and slot_id:
                        logger.info("🔔 [모니터] 청산 신호: %s — %s", slot_id, reason)
                        sell_result = executor.execute_slot_sell(slot_id, reason)
                        if sell_result.get("success"):
                            pnl = sell_result.get("pnl", 0)
                            pnl_pct = sell_result.get("pnl_pct", 0)
                            logger.info("✅ [모니터] 청산 완료: %s PnL=%+.0f (%.1f%%)", slot_id, pnl, pnl_pct)
                            _tg(
                                f"🔔 <b>포지션 청산</b>\n"
                                f"슬롯: {slot_id}\n"
                                f"사유: {reason}\n"
                                f"손익: {pnl:+,.0f}원 ({pnl_pct:+.1f}%)\n"
                                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                            )
                        else:
                            logger.warning("⚠️ [모니터] 청산 실패: %s — %s", slot_id, sell_result.get("message", ""))
            except Exception as e:
                logger.error("❌ [모니터 스레드] 오류: %s", e)
            # 60초 대기 (1초 단위로 shutdown 체크)
            for _ in range(60):
                if shutdown:
                    break
                time.sleep(1)
        logger.info("🔍 모니터 스레드 종료")

    monitor_thread = _threading.Thread(target=_monitor_loop, daemon=True, name="monitor-loop")
    monitor_thread.start()

    # 4. 메인 루프
    logger.info("🤖 시스템 실행 중... (Ctrl+C로 종료)")
    logger.info("🇺🇸 미국장 운용 모드: %s", _get_us_operation_mode())
    # [Claude Fix] 봇 시작 알림
    _tg(
        f"🚀 <b>Stock Bot 시작</b>\n"
        f"모드: {'🔵 모의투자' if config.KIS_MODE == 'virtual' else '🔴 실전'}\n"
        f"미국장: {_get_us_operation_mode()}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    cycle = 0
    _prev_kr_open = None  # 장 시작/종료 알림용
    _prev_us_open = None  # 미국 장 시작/종료 알림용
    _prev_date = None     # 일일 리셋 감지용
    _last_balance_sync = 0  # 잔액 동기화 마지막 시간
    _last_bridge_hash: str | None = None  # Two-Tier: context_from 해시 추적

    while not shutdown:
        cycle += 1
        now = datetime.now()
        kr_open = _is_kr_market_hours()
        us_open = _is_us_market_hours()
        today_str = now.strftime("%Y-%m-%d")

        # ── Tier 1: context_from 해시 체크 (매 사이클, 비용 없음) ──
        _run_tier2, _bridge_ctx = _context_tier2_trigger("stock-bot")
        if _bridge_ctx.hash != _last_bridge_hash:
            _last_bridge_hash = _bridge_ctx.hash
            logger.info(
                "context_from[stock-bot]: bridge updated hash=%s regime=%s bias=%s targets=%s",
                _bridge_ctx.hash, _bridge_ctx.regime,
                _bridge_ctx.action_bias, _bridge_ctx.targets,
            )
        if _run_tier2:
            logger.info(
                "context_from[stock-bot]: Tier2 triggered — regime=%s bias=%s confidence=%s",
                _bridge_ctx.regime, _bridge_ctx.action_bias, _bridge_ctx.confidence,
            )
            _tg_dedupe(
                f"📡 <b>[주식봇] 브리지 신호 수신</b>\n"
                f"Regime: {_bridge_ctx.regime} | Bias: {_bridge_ctx.action_bias}\n"
                f"신뢰도: {_bridge_ctx.confidence} | 긴급도: {_bridge_ctx.urgency}\n"
                f"리스크: {_bridge_ctx.risk_flags[:80]}",
                key=f"bridge_ctx:{_bridge_ctx.hash}",
                cooldown_seconds=14400,
            )

        logger.info("🕐 [%d] %s | KR장=%s US장=%s",
                     cycle, now.strftime("%Y-%m-%d %H:%M:%S"),
                     "🟢" if kr_open else "🔴",
                     "🟢" if us_open else "🔴")

        # ── 일일 리셋: 날짜가 바뀌면 risk_state 초기화 ──
        if _prev_date is not None and today_str != _prev_date:
            logger.info("🔄 날짜 변경 감지 (%s → %s) — risk_state 일일 리셋", _prev_date, today_str)
            try:
                risk_state = store.safe_load("risk_state")
                risk_state["daily_pnl"] = 0.0
                risk_state["daily_trades"] = 0
                risk_state["consecutive_losses"] = max(0, risk_state.get("consecutive_losses", 0))
                risk_state["saved_at"] = now.isoformat()
                store.safe_save("risk_state", risk_state)
                _tg(f"🔄 <b>일일 리셋</b> ({today_str})\n손익/거래횟수 초기화")
            except Exception as e:
                logger.warning("⚠️ 일일 리셋 실패: %s", e)
        _prev_date = today_str

        # [Claude Fix] 장 시작/종료 시 텔레그램 알림
        if _prev_kr_open is not None and kr_open != _prev_kr_open:
            if kr_open:
                _tg("🔔 <b>🇰🇷 한국 장 시작!</b> 매매 활성화")
            else:
                _tg("🔔 <b>🇰🇷 한국 장 마감.</b> 매매 중단")
        _prev_kr_open = kr_open

        # 미국 장 시작/종료 알림
        if _prev_us_open is not None and us_open != _prev_us_open:
            if us_open:
                _tg("🔔 <b>🇺🇸 미국 장 시작!</b> (EDT 22:30~05:00) 매매 활성화")
            else:
                _tg("🔔 <b>🇺🇸 미국 장 마감.</b> 매매 중단")
        _prev_us_open = us_open

        # ── 잔액 동기화: 주기적으로 KIS API에서 실제 잔액 조회 ──
        sync_interval = getattr(config, 'BALANCE_SYNC_INTERVAL', 1800)
        if now.timestamp() - _last_balance_sync >= sync_interval:
            try:
                bal = kis.get_balance()
                total_eval = bal.get("total_eval", 0)
                deposit = bal.get("total_deposit", bal.get("deposit", 0))  # [Fix] KIS API 키는 total_deposit
                if total_eval > 0 or deposit > 0:
                    logger.info("💰 잔액 동기화: 예수금=%s 총평가=%s", f"{deposit:,.0f}", f"{total_eval:,.0f}")
                    risk_state = store.safe_load("risk_state")
                    risk_state["total_balance"] = total_eval
                    risk_state["deposit"] = deposit
                    risk_state["balance_synced_at"] = now.isoformat()
                    store.safe_save("risk_state", risk_state)
                    store.record_last_good_balance("KR", total_eval, deposit)
                    store.clear_api_errors("KR")
                else:
                    store.mark_balance_sync_failed("KR", "balance zero after sync")
                    logger.warning("⚠️ KR 잔액 동기화 결과가 0으로 반환됨 — 마지막 정상 잔고 유지")

                if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
                    us_bal = kis.get_us_balance()
                    us_total = us_bal.get("total_usd", 0)
                    us_deposit = us_bal.get("available_usd", 0)
                    us_status = us_bal.get("balance_status", "unknown")
                    store.update_market_state("US", {
                        "last_balance_status_code": us_bal.get("status_code", ""),
                        "last_balance_status": us_status,
                    })
                    if us_total > 0 or us_deposit > 0:
                        store.record_last_good_balance("US", us_total, us_deposit)
                        store.clear_api_errors("US")
                    else:
                        if us_status == "unsupported":
                            store.mark_balance_sync_failed("US", f"us balance unsupported: {us_bal.get('status_message', '')}")
                        elif us_status == "failed":
                            store.mark_balance_sync_failed("US", f"us balance failed: {us_bal.get('status_message', '')}")
                        else:
                            store.mark_balance_sync_failed("US", "us balance zero after sync")
                _last_balance_sync = now.timestamp()
            except Exception as e:
                logger.warning("⚠️ 잔액 동기화 실패: %s", e)
                store.mark_balance_sync_failed("KR", str(e))
                if config.US_STOCK_ENABLED:
                    store.mark_balance_sync_failed("US", str(e))

        pipeline_results = []
        pipeline_actionable = 0
        pipeline_candidates = 0
        try:
            pipeline_results = run_pipeline_once(kis, orch, executor)
            # Extract actionable count from pipeline results
            for r in pipeline_results:
                if r.get("entry_verdict") in ("STRONG_BUY", "BUY"):
                    pipeline_actionable += 1
            if pipeline_results:
                pipeline_candidates = len(pipeline_results)
        except Exception as e:
            logger.error("❌ 파이프라인 오류: %s", e, exc_info=True)

        # 대기 (장시간에 따라 다른 주기)
        interval = _get_scan_interval()

        # Pre-open fast scan: 장외지만 곧 개장하는 시장이 있으면 주기 조정
        # 예) US 22:30 개장, 현재 21:50 → 2400초 남음, off-hours 주기 1800초
        #     → 1800초면 22:19에 스캔 (장마감) → 다음 01:19까지 3시간 손실
        #     → 개장 시간에 맞춰 2400초로 조정 (2026-05-18: interval*2 기준)
        if interval > 60:
            secs_to_open = _seconds_until_next_market_open()
            if 0 < secs_to_open < max(interval * 2, 1200):
                capped = max(60, secs_to_open)
                if abs(capped - interval) > 10:
                    logger.info(
                        "⏰ 시장 개장 예정 감지 (%d초 후) — 스캔 주기 %d→%d초 조정",
                        secs_to_open, interval, capped,
                    )
                    interval = capped

        # 슬롯 과점/만땅일 때 빠른 재확인:
        # 디컨제스천이 슬롯을 해소하면 다음 스캔이 오래 기다리지 않고 바로 진입 기회를 잡음
        # 장중에만 의미 있음 — 장외에는 디컨제스천(매도) 불가 (2026-05-18 fix)
        if not pipeline_results and (_is_kr_market_hours() or _is_us_market_hours()):
            kr_slots = store.get_open_slot_count("KR")
            us_slots = store.get_open_slot_count("US")
            max_pos = getattr(config, "MAX_POSITIONS_PER_MARKET", 3)
            if kr_slots >= max_pos or us_slots >= max_pos:
                fast_interval = min(60, interval)
                if fast_interval < interval:
                    logger.info(
                        "⚡ 슬롯 만땅 감지 (KR=%d/%d US=%d/%d) — %d초 후 재확인",
                        kr_slots, max_pos, us_slots, max_pos, fast_interval,
                    )
                    interval = fast_interval

        # 0-actionable 백오프: 연속으로 실행 가능 후보가 없으면 스캔 주기 확장
        # API 호출 낭비 방지 + 로그 노이즈 감소 (2026-05-18 추가)
        if pipeline_actionable == 0 and interval >= 300:
            _consecutive_zero_actionable = getattr(run_system, "_consecutive_zero_actionable", 0) + 1
            run_system._consecutive_zero_actionable = _consecutive_zero_actionable
            if _consecutive_zero_actionable >= 3:
                backoff = min(900, 300 * (2 ** (_consecutive_zero_actionable // 3 - 1)))
                if backoff > interval:
                    logger.info(
                        "⏸️ 0-actionable %d회 연속 — 주기 %d→%d초 확장",
                        _consecutive_zero_actionable, interval, backoff,
                    )
                    interval = backoff
        else:
            # 실행 가능 후보가 등장하면 카운터 리셋
            prev = getattr(run_system, "_consecutive_zero_actionable", 0)
            if prev > 0:
                logger.info("⚡ 실행 가능 후보 등장 — 0-actionable 카운터 리셋 (%d→0)", prev)
            run_system._consecutive_zero_actionable = 0

        logger.info("⏳ 다음 스캔까지 %d초 대기...", interval)

        # 인터럽트 가능한 대기
        for _ in range(interval):
            if shutdown:
                break
            time.sleep(1)

    logger.info("🛑 시스템 종료 완료")


# ─── 엔트리포인트 ────────────────────────────────────────────────
def main():
    """엔트리포인트."""
    if "--test" in sys.argv:
        test_connections()
    elif "--status" in sys.argv:
        show_status()
    elif "--once" in sys.argv:
        # 파이프라인 1회만 실행
        kis = KISAPI()
        if not kis.test_connection():
            print("❌ KIS API 연결 실패")
            sys.exit(1)
        store = DataStore()
        orch = Orchestrator(store)
        executor = TradeExecutorAgent(store, kis_api=kis)
        monitor = MonitorAgent(store)
        results = run_pipeline_once(kis, orch, executor)
        print(f"\n📊 결과: {len(results)}건 처리")
        for r in results:
            print(f"  {r['code']} {r['name']}: "
                  f"타점={r['entry_verdict']} 자금={'OK' if r['risk_can_enter'] else 'BLOCK'}")
    else:
        run_system()


if __name__ == "__main__":
    main()
