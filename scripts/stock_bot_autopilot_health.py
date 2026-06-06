#!/usr/bin/env python3
"""Stock bot health collector.

Runs silently when everything looks fine.
When it finds issues or improvement opportunities, it prints a compact JSON
report to stdout and also stores it under data/agents/autopilot_latest.json.

This script is intentionally deterministic so a cron job can run it in no-agent
mode and feed its output into a separate LLM review job.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config

DATA = ROOT / "data" / "agents"
LOGS = ROOT / "logs"
OUTFILE = DATA / "autopilot_latest.json"
STATE_FILE = DATA / "autopilot_state.json"
HISTORY_FILE = DATA / "autopilot_history.jsonl"

RECENT_LOG_LINES = 200
RECENT_TRADE_COUNT = 20
SILENT_RESET_SECONDS = 6 * 60 * 60

PATTERNS = {
    "rate_limit": re.compile(r"Rate limit\(EGW00201\)", re.I),
    "empty_response": re.compile(r"빈 응답", re.I),
    "order_fail": re.compile(r"order_fail|주문 .* 실패|EGW00356", re.I),
    "market_closed": re.compile(r"market_closed|장 마감", re.I),
    "api_degraded": re.compile(r"api_degraded|degraded_mode", re.I),
    "balance_zero": re.compile(r"잔액 동기화 결과가 0|balance zero", re.I),
    "unsupported_us": re.compile(r"unsupported|없는 서비스 코드", re.I),
    # Avoid counting every handled log-level ERROR as an exception; reserve this
    # bucket for true tracebacks / unhandled exceptions / critical failures.
    "exception": re.compile(r"Traceback|Unhandled exception|\bException\b|\bCRITICAL\b", re.I),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _read_log_delta(path: Path, previous_bytes: int | None, limit: int = RECENT_LOG_LINES) -> Tuple[List[str], int]:
    """Read only the newly appended log lines since the last byte offset.

    If the file was rotated or truncated, fall back to the beginning.
    """
    if not path.exists():
        return [], 0
    try:
        size = path.stat().st_size
        start = int(previous_bytes or 0)
        if start < 0 or start > size:
            start = 0
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="ignore")
        lines = [line.rstrip("\n") for line in text.splitlines()]
        if len(lines) > limit:
            lines = lines[-limit:]
        return lines, size
    except Exception:
        return [], 0


def _file_age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 60.0
    except Exception:
        return None


def _is_kr_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.strptime("09:10", "%H:%M").time() <= t <= datetime.strptime("15:30", "%H:%M").time()


def _is_us_market_hours() -> bool:
    now = datetime.now()
    t = now.time()
    weekday = now.weekday()

    month = now.month
    is_edt = 3 <= month <= 10
    if is_edt:
        open_t, close_t = datetime.strptime("22:30", "%H:%M").time(), datetime.strptime("05:00", "%H:%M").time()
    else:
        open_t, close_t = datetime.strptime("23:30", "%H:%M").time(), datetime.strptime("06:00", "%H:%M").time()

    if weekday == 5:
        return t <= close_t
    if weekday == 6:
        return False
    if weekday == 0:
        return t >= open_t
    return t >= open_t or t <= close_t


def _log_age_warning_threshold_min() -> float:
    """Choose a stale-log warning threshold that matches the active scan cadence."""
    if _is_kr_market_hours() or _is_us_market_hours():
        return 20.0
    off_market_minutes = float(getattr(config, "SCAN_INTERVAL_OFF", 1800)) / 60.0
    return max(45.0, off_market_minutes + 15.0)


def _open_position_valuation_estimate(
    open_slots: List[Dict[str, Any]],
    invested_amount: float,
    total_balance: float = 0.0,
    deposit: float = 0.0,
) -> Dict[str, Any]:
    """Estimate open-position PnL from position-level data.

    Uses each slot's highest_price with a discount factor (85%) as a proxy for
    current price.  This is more reliable than total_balance - deposit during mock
    mode, because KIS VTS total_balance includes accumulated realized gains that
    inflate the subtraction result to implausible levels (e.g. 356% return for two
    modestly profitable positions).

    The discount factor is conservative: it assumes current price sits between
    entry and highest, closer to the peak than to entry.
    """
    if not open_slots or invested_amount <= 0:
        return {
            "open_market_value_est": 0.0,
            "unrealized_pnl_est": 0.0,
            "unrealized_return_pct_est": 0.0,
            "quality": "no_positions",
            "note": "추정 가능한 오픈 포지션 없음",
        }

    DISCOUNT = 0.85
    position_value = 0.0
    position_pnl = 0.0

    for slot in open_slots:
        entry = float(slot.get("entry_price", 0) or 0)
        high = float(slot.get("highest_price", 0) or 0)
        qty = float(slot.get("quantity", 0) or 0)
        if not entry or not qty:
            continue
        if high > entry:
            est_current = entry + (high - entry) * DISCOUNT
        else:
            est_current = entry

        position_value += est_current * qty
        position_pnl += (est_current - entry) * qty

    unrealized_pnl_est = round(position_pnl, 2)
    unrealized_return_pct_est = round(
        position_pnl / invested_amount * 100.0, 2
    ) if invested_amount else 0.0

    return {
        "open_market_value_est": round(invested_amount + position_pnl, 2),
        "unrealized_pnl_est": unrealized_pnl_est,
        "unrealized_return_pct_est": unrealized_return_pct_est,
        "quality": "position_based_estimate",
        "note": (
            f"포지션 {len(open_slots)}개의 최고가 대비 {DISCOUNT*100:.0f}% 수준으로 추정 "
            "(실시간 시세 부재 시)"
        ),
    }


def _classify_empty_pipeline_candidates(
    pipeline_candidate_count: int,
    open_slot_count: int,
    max_positions_per_market: int,
    issues: List[str],
    notes: List[str],
    suggestions: List[str],
) -> None:
    """Classify an empty pipeline result as actionable only when slots are available."""
    if pipeline_candidate_count != 0:
        return
    if open_slot_count >= max_positions_per_market:
        notes.append("파이프라인 후보 0건은 포지션 슬롯 초과 상태에서 예상됨")
        return
    issues.append("최근 파이프라인 후보가 비어 있음")
    suggestions.append("뉴스 수집/테마 감지 입력이 정상인지 확인")


def _classify_position_slot_usage(
    open_slot_count: int,
    max_positions_per_market: int,
    issues: List[str],
    notes: List[str],
    suggestions: List[str],
) -> None:
    """Classify slot usage against the configured per-market cap.

    Being at the cap is expected during a fully invested mock run. Exceeding the
    cap is not expected and should stay visible as a readiness/risk issue even
    when the risk manager now blocks further entries.
    """
    if open_slot_count < max_positions_per_market:
        return
    if open_slot_count > max_positions_per_market:
        issues.append(f"포지션 슬롯 초과: {open_slot_count}/{max_positions_per_market}")
        suggestions.append("신규 진입 차단 상태 유지 및 슬롯 초과 원인 점검")
        return
    notes.append(f"포지션 슬롯이 {open_slot_count}/{max_positions_per_market}개 사용 중")
    suggestions.append("신규 진입보다 기존 포지션 관리 우선")


def _filter_strategy_diagnostics(filter_stats: Any) -> Dict[str, Any]:
    """Summarize the most common filter bottlenecks for strategy feedback."""
    if isinstance(filter_stats, dict):
        records = filter_stats.get("records", []) or []
    elif isinstance(filter_stats, list):
        records = filter_stats
    else:
        records = []

    verdict_counts: Counter[str] = Counter()
    layer_fail_counts: Counter[str] = Counter()
    combo_counts: Counter[Tuple[str, ...]] = Counter()

    for record in records:
        if not isinstance(record, dict):
            continue
        verdict_counts[str(record.get("verdict", "?"))] += 1
        layer_details = record.get("layer_details") or {}
        failed_layers: List[str] = []
        if isinstance(layer_details, dict):
            for layer_name, detail in layer_details.items():
                if isinstance(detail, dict) and not detail.get("pass", False):
                    failed_layers.append(str(layer_name))
                    layer_fail_counts[str(layer_name)] += 1
        combo_counts[tuple(sorted(failed_layers))] += 1

    total = sum(verdict_counts.values())
    notes: List[str] = []
    suggestions: List[str] = []

    if total > 0:
        trend_fail_pct = (layer_fail_counts.get("trend", 0) / total) * 100.0
        volume_fail_pct = (layer_fail_counts.get("volume", 0) / total) * 100.0
        weak_buy_pct = (verdict_counts.get("WEAK_BUY", 0) / total) * 100.0
        reject_pct = (verdict_counts.get("REJECT", 0) / total) * 100.0

        if trend_fail_pct >= 60.0 and volume_fail_pct >= 50.0:
            notes.append(
                f"필터 병목은 trend({trend_fail_pct:.1f}%) + volume({volume_fail_pct:.1f}%)에 집중"
            )
            suggestions.append(
                "상단 후보 스크리닝에서 trend/volume를 먼저 걸러 6레이어 평가 낭비를 줄이기"
            )

        if weak_buy_pct >= 40.0:
            notes.append(f"WEAK_BUY 비중이 {weak_buy_pct:.1f}%로 높음")
            suggestions.append(
                "virtual이라도 WEAK_BUY를 전부 실행하지 말고 entry_score 상위권만 우선 진입 검토"
            )

        if reject_pct >= 40.0:
            notes.append(f"REJECT 비중이 {reject_pct:.1f}%로 높음")
            suggestions.append(
                "기술 분석 전에 거래량/추세 선필터를 추가해 후보군 자체를 줄이기"
            )

    top_combos: List[Dict[str, Any]] = []
    for combo, count in combo_counts.most_common(5):
        top_combos.append({"layers": list(combo), "count": count})

    top_combo = top_combos[0] if top_combos else None
    if top_combo and top_combo["layers"] == ["trend", "volume"]:
        notes.append(f"가장 흔한 탈락 조합은 trend+volume ({top_combo['count']}건)")
        suggestions.append("trend+volume 조합이 자주 깨지면 진입 전 장세 필터/수급 필터를 강화하기")

    return {
        "record_count": total,
        "verdict_counts": dict(verdict_counts),
        "layer_fail_counts": dict(layer_fail_counts),
        "top_fail_combos": top_combos,
        "notes": list(dict.fromkeys(notes)),
        "suggestions": list(dict.fromkeys(suggestions)),
    }


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_proc_alive(pid_file: Path) -> Tuple[bool, int | None]:
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, pid


def _count_patterns(lines: List[str]) -> Dict[str, int]:
    text = "\n".join(lines)
    return {name: len(rx.findall(text)) for name, rx in PATTERNS.items()}


def _recent_sells(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sells = [t for t in trades if t.get("action") == "sell"]
    return sells[-RECENT_TRADE_COUNT:]


def _open_slots(slots_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    slots = slots_data.get("slots", {}) if isinstance(slots_data, dict) else {}
    return list(slots.values())


def _market_counts(open_slots: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter()
    for s in open_slots:
        counts[str(s.get("market", "?"))] += 1
    return dict(counts)


def _latest_trade_time(trades: List[Dict[str, Any]]) -> str | None:
    times = [t for t in (_parse_ts(x.get("timestamp", "")) for x in trades) if t]
    if not times:
        return None
    return max(times).isoformat()


def _repeat_losers(recent_sells: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """감지: 동일 종목 2회 이상 손실 (유의미한 손실만, pnl_pct < -0.5%)."""
    by_code: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"losses": 0, "trades": 0, "pnl": 0.0, "name": ""})
    for t in recent_sells:
        code = str(t.get("code", ""))
        if not code:
            continue
        row = by_code[code]
        row["trades"] += 1
        row["pnl"] += float(t.get("pnl", 0) or 0)
        row["name"] = t.get("name", row["name"])
        pnl_pct = float(t.get("pnl_pct", 0) or 0)
        if float(t.get("pnl", 0) or 0) < 0 and pnl_pct < -0.5:
            row["losses"] += 1
    losers = []
    for code, row in by_code.items():
        if row["losses"] >= 2:
            losers.append({"code": code, **row})
    losers.sort(key=lambda x: (x["losses"], -x["pnl"]), reverse=True)
    return losers


def _exit_reason_stats(sells: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
    for trade in sells:
        reason = str(trade.get("reason", ""))
        if "트레일링" in reason:
            key = "trailing"
        elif "익절" in reason:
            key = "take_profit"
        elif "손절" in reason:
            key = "stop_loss"
        else:
            key = "other"
        pnl = float(trade.get("pnl", 0) or 0)
        row = stats[key]
        row["count"] += 1
        row["pnl"] += pnl
        if pnl > 0:
            row["wins"] += 1
        elif pnl < 0:
            row["losses"] += 1
    return {
        key: {
            "count": row["count"],
            "pnl": round(row["pnl"], 2),
            "wins": row["wins"],
            "losses": row["losses"],
        }
        for key, row in sorted(stats.items())
    }


def _symbol_performance(sells: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"name": "", "trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
    for trade in sells:
        code = str(trade.get("code", ""))
        if not code:
            continue
        pnl = float(trade.get("pnl", 0) or 0)
        row = by_code[code]
        row["name"] = trade.get("name") or row["name"]
        row["trades"] += 1
        row["pnl"] += pnl
        if pnl > 0:
            row["wins"] += 1
        elif pnl < 0:
            row["losses"] += 1
    rows = []
    for code, row in by_code.items():
        trades = int(row["trades"] or 0)
        rows.append({
            "code": code,
            "name": row["name"],
            "trades": trades,
            "pnl": round(float(row["pnl"]), 2),
            "win_rate_pct": round(row["wins"] / trades * 100.0, 1) if trades else 0.0,
            "losses": row["losses"],
        })
    rows.sort(key=lambda x: abs(float(x.get("pnl", 0))), reverse=True)
    return rows[:limit]


def _position_decongestion_candidates(open_slots: List[Dict[str, Any]], now_dt: datetime, limit: int = 5) -> List[Dict[str, Any]]:
    """Rank open positions by how urgently they deserve a review.

    This does *not* tell the bot to sell anything automatically. It only exposes
    a compact, deterministic priority list so the health report can explain why
    the account is congested and which positions are the weakest links.
    """
    rows: List[Dict[str, Any]] = []
    for slot in open_slots:
        entry_price = float(slot.get("entry_price", 0) or 0)
        highest_price = float(slot.get("highest_price", 0) or 0)
        stop_loss_price = float(slot.get("stop_loss_price", 0) or 0)
        take_profit_price = float(slot.get("take_profit_price", 0) or 0)
        entry_time = _parse_ts(str(slot.get("entry_time", "") or "")) or _parse_ts(str(slot.get("synced_at", "") or ""))
        age_hours = ((now_dt - entry_time).total_seconds() / 3600.0) if entry_time else None
        high_watermark_return_pct = ((highest_price - entry_price) / entry_price * 100.0) if entry_price and highest_price else None
        stop_gap_pct = ((entry_price - stop_loss_price) / entry_price * 100.0) if entry_price and stop_loss_price else None
        take_profit_gap_pct = ((take_profit_price - entry_price) / entry_price * 100.0) if entry_price and take_profit_price else None
        score = 0.0
        if age_hours is not None:
            score += min(age_hours, 240.0) / 24.0
        if high_watermark_return_pct is not None:
            score -= high_watermark_return_pct * 2.5
        if stop_gap_pct is not None:
            score += max(0.0, 12.0 - stop_gap_pct)
        if take_profit_gap_pct is not None:
            score += max(0.0, 8.0 - take_profit_gap_pct) * 0.25
        # 변동성 가중: ATR 비율이 높은 종목(손절 위험↑) 청산 우선
        # monitor_agent._find_weakest_slot()과 일관성 유지 (2026-05-19 추가)
        atr = float(slot.get("atr", 0) or 0)
        if atr > 0 and entry_price > 0:
            atr_ratio_pct = atr / entry_price * 100.0
            score += min(atr_ratio_pct * 0.3, 5.0)  # cap at +5pt
        rows.append({
            "slot_id": slot.get("slot_id"),
            "code": slot.get("code"),
            "name": slot.get("name"),
            "market": slot.get("market"),
            "theme": slot.get("theme"),
            "entry_price": entry_price,
            "highest_price": highest_price,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "high_watermark_return_pct": round(high_watermark_return_pct, 2) if high_watermark_return_pct is not None else None,
            "stop_gap_pct": round(stop_gap_pct, 2) if stop_gap_pct is not None else None,
            "take_profit_gap_pct": round(take_profit_gap_pct, 2) if take_profit_gap_pct is not None else None,
            "decongestion_score": round(score, 2),
        })
    rows.sort(key=lambda x: (x["decongestion_score"], x["age_hours"] or 0), reverse=True)
    return rows[:limit]


def _append_history(snapshot: Dict[str, Any]) -> None:
    """Append a compact time-series row for live-readiness trend review."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    compact = {
        "generated_at": snapshot.get("generated_at"),
        "health": snapshot.get("health"),
        "signals": snapshot.get("signals", {}),
        "metrics": snapshot.get("metrics", {}),
        "issues": snapshot.get("issues", []),
        "notes": snapshot.get("notes", []),
    }
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(compact, ensure_ascii=False, default=str) + "\n")


def _load_state() -> Dict[str, Any]:
    return _read_json(STATE_FILE, {}) if STATE_FILE.exists() else {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _bucket(value: float | int | None, step: int) -> int:
    if value is None:
        return -1
    try:
        return int(float(value) // step)
    except Exception:
        return -1


def _build_signature(
    *,
    stock_alive: bool,
    dashboard_alive: bool,
    agent_age_min: float | None,
    notable_errors: Dict[str, int],
    consecutive_losses: int,
    recent_win_rate: float,
    open_slots: List[Dict[str, Any]],
    max_positions_per_market: int,
    latest_trade_time: str | None,
    repeat_losers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "stock_alive": stock_alive,
        "dashboard_alive": dashboard_alive,
        "agent_age_bucket": _bucket(agent_age_min, 15),
        "rate_limit_bucket": _bucket(notable_errors.get("rate_limit", 0), 50),
        "empty_response_bucket": _bucket(notable_errors.get("empty_response", 0), 50),
        "order_fail_bucket": _bucket(notable_errors.get("order_fail", 0), 1),
        # Mock-environment US service/balance responses are expected noise and
        # should not churn the dedupe signature.
        "balance_zero_bucket": _bucket(notable_errors.get("balance_zero", 0), 1),
        "consecutive_losses": consecutive_losses,
        "recent_win_rate_bucket": _bucket(recent_win_rate, 10),
        "open_positions": len(open_slots),
        "position_over_limit": len(open_slots) > max_positions_per_market,
        "repeat_loser_codes": [x.get("code") for x in repeat_losers[:3]],
        "latest_trade_time": latest_trade_time,
    }


def _should_emit(signature: Dict[str, Any], *, now_ts: float) -> bool:
    state = _load_state()
    last_signature = state.get("last_signature")
    last_sent_at = float(state.get("last_sent_at", 0) or 0)

    if signature != last_signature:
        return True
    if now_ts - last_sent_at >= SILENT_RESET_SECONDS:
        return True
    return False


def _persist_state(signature: Dict[str, Any], emitted: bool, report: Dict[str, Any]) -> None:
    state = {
        "last_signature": signature,
        "last_sent_at": datetime.now(timezone.utc).timestamp() if emitted else _load_state().get("last_sent_at", 0),
        "last_report": report,
        "log_offsets": report.get("log_offsets", {}),
        "updated_at": _now_iso(),
    }
    _save_state(state)


def _write_snapshot(snapshot: Dict[str, Any]) -> None:
    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _build_report() -> Dict[str, Any]:
    prior_state = _load_state()
    log_offsets = prior_state.get("log_offsets", {}) if isinstance(prior_state, dict) else {}

    pid_dir = ROOT / "pids"
    stock_alive, stock_pid = _is_proc_alive(pid_dir / "stock_bot.pid")
    dashboard_alive, dashboard_pid = _is_proc_alive(pid_dir / "dashboard.pid")

    trade_history = _read_json(DATA / "trade_history.json", {"trades": []})
    all_slots = _read_json(DATA / "all_slots.json", {"slots": {}})
    risk_state = _read_json(DATA / "risk_state.json", {})
    last_pipeline = _read_json(DATA / "last_pipeline.json", {})
    recommendation_history = _read_json(DATA / "recommendation_history.json", {"entries": []})
    filter_stats = _read_json(DATA / "filter_stats.json", [])
    market_runtime = _read_json(DATA / "market_runtime.json", {})
    symbol_cooldowns = _read_json(DATA / "symbol_cooldowns.json", {"entries": {}})
    symbol_cooldowns_exists = (DATA / "symbol_cooldowns.json").exists()

    trades = trade_history.get("trades", []) if isinstance(trade_history, dict) else []
    sells = [t for t in trades if t.get("action") == "sell"]
    recent_sells = _recent_sells(trades)
    open_slots = _open_slots(all_slots)
    open_positions_detail = [
        {
            "slot_id": str(s.get("code") or s.get("slot_id") or ""),
            "name": s.get("name"),
            "code": s.get("code"),
            "market": s.get("market"),
            "quantity": s.get("quantity"),
            "entry_price": s.get("entry_price"),
            "invest_amount": s.get("invest_amount"),
            "highest_price": s.get("highest_price"),
            "theme": s.get("theme"),
            "order_state": s.get("order_state"),
        }
        for s in open_slots
    ]
    market_counts = _market_counts(open_slots)
    now_dt = datetime.now(timezone.utc)
    active_symbol_cooldowns: List[Dict[str, Any]] = []
    for key, entry in (symbol_cooldowns.get("entries", {}) or {}).items():
        if isinstance(entry, str):
            until = entry
            reason = ""
        else:
            until = str(entry.get("until", ""))
            reason = str(entry.get("reason", ""))
        dt = _parse_ts(until)
        if dt and dt > now_dt:
            market, _, code = key.partition(":")
            active_symbol_cooldowns.append({
                "market": market,
                "code": code,
                "until": until,
                "reason": reason,
            })

    agent_log = LOGS / "agent_system.log"
    stock_log = LOGS / "stock_bot.log"
    agent_tail, agent_log_bytes = _read_log_delta(agent_log, log_offsets.get("agent_bytes", 0))
    stock_tail, stock_log_bytes = _read_log_delta(stock_log, log_offsets.get("stock_bytes", 0))
    agent_counts = _count_patterns(agent_tail)
    stock_counts = _count_patterns(stock_tail)

    agent_age_min = _file_age_minutes(agent_log)
    stock_age_min = _file_age_minutes(stock_log)

    realized_pnl = sum(float(t.get("pnl", 0) or 0) for t in sells)
    win_count = sum(1 for t in sells if float(t.get("pnl", 0) or 0) > 0)
    total_sells = len(sells)
    win_rate = (win_count / total_sells * 100.0) if total_sells else 0.0

    recent_realized_pnl = sum(float(t.get("pnl", 0) or 0) for t in recent_sells)
    recent_win_count = sum(1 for t in recent_sells if float(t.get("pnl", 0) or 0) > 0)
    recent_win_rate = (recent_win_count / len(recent_sells) * 100.0) if recent_sells else 0.0

    risk_daily_pnl = float(risk_state.get("daily_pnl", 0) or 0)
    risk_daily_trades = int(risk_state.get("daily_trades", 0) or 0)
    consecutive_losses = int(risk_state.get("consecutive_losses", 0) or 0)
    total_balance = float(risk_state.get("total_balance", 0) or 0)
    deposit = float(risk_state.get("deposit", 0) or 0)
    invested_amount = sum(float(s.get("invest_amount", 0) or 0) for s in open_slots)
    valuation_estimate = _open_position_valuation_estimate(open_slots, invested_amount, total_balance, deposit)
    open_market_value_est = valuation_estimate["open_market_value_est"]
    unrealized_pnl_est = valuation_estimate["unrealized_pnl_est"]
    unrealized_return_pct_est = valuation_estimate["unrealized_return_pct_est"]
    exit_reason_stats = _exit_reason_stats(sells)
    symbol_performance = _symbol_performance(sells)
    decongestion_candidates = _position_decongestion_candidates(open_slots, now_dt, limit=5)

    pipeline_candidate_count = len(last_pipeline.get("results", [])) if isinstance(last_pipeline, dict) else 0
    pipeline_theme_count = len(last_pipeline.get("themes", [])) if isinstance(last_pipeline, dict) else 0
    recommendation_entries = recommendation_history.get("entries", []) if isinstance(recommendation_history, dict) else []
    blocked_recommendations = [x for x in recommendation_entries if str(x.get("outcome", "")).startswith("blocked_")]
    blocked_recommendation_outcomes = Counter(str(x.get("outcome", "blocked_unknown")) for x in blocked_recommendations)
    blocked_slot_full = [x for x in blocked_recommendations if x.get("outcome") == "blocked_slot_capacity" or x.get("is_slot_full_block")]
    blocked_recommendations_recent = blocked_recommendations[-10:]
    candidate_recommendations = [x for x in recommendation_entries if x.get("outcome") == "candidate"]
    pipeline_results = last_pipeline.get("results", []) if isinstance(last_pipeline, dict) else []

    def _pipeline_result_verdict(item: dict[str, Any]) -> str:
        verdict = str(item.get("entry_verdict") or item.get("verdict") or item.get("signal_verdict") or "").strip()
        if verdict:
            return verdict
        score = float(item.get("entry_score", item.get("score", 0)) or 0)
        if score >= 90:
            return "STRONG_BUY"
        if score >= 75:
            return "BUY"
        if score > 0:
            return "WEAK_BUY"
        return "?"

    candidate_entry_verdicts = Counter(
        _pipeline_result_verdict(x)
        for x in pipeline_results
        if isinstance(x, dict)
    )
    candidate_entry_score_avg = (
        sum(float(x.get("entry_score", x.get("score", 0)) or 0) for x in pipeline_results if isinstance(x, dict)) / len(pipeline_results)
        if pipeline_results else 0.0
    )
    candidate_entry_unknown_count = sum(
        1 for x in pipeline_results
        if isinstance(x, dict) and _pipeline_result_verdict(x) == "?"
    )
    filter_strategy = _filter_strategy_diagnostics(filter_stats)

    active_symbol_cooldowns: List[Dict[str, Any]] = []
    for key, entry in (symbol_cooldowns.get("entries", {}) or {}).items():
        if isinstance(entry, str):
            until = entry
            reason = ""
        else:
            until = str(entry.get("until", ""))
            reason = str(entry.get("reason", ""))
        dt = _parse_ts(until)
        if dt and dt > now_dt:
            market, _, code = key.partition(":")
            active_symbol_cooldowns.append({
                "market": market,
                "code": code,
                "until": until,
                "reason": reason,
            })

    combined_counts = Counter(agent_counts) + Counter(stock_counts)
    notable_errors = {
        k: combined_counts.get(k, 0)
        for k in ["rate_limit", "empty_response", "order_fail", "balance_zero", "unsupported_us", "exception"]
    }

    issues: List[str] = []
    notes: List[str] = []
    suggestions: List[str] = []

    if blocked_slot_full:
        notes.append(
            f"슬롯 full 때문에 막힌 후보 {len(blocked_slot_full)}건 누적"
        )
        suggestions.append("blocked_slot_capacity 후보를 우선순위 기준으로 별도 모니터링")

    if filter_strategy.get("notes"):
        notes.extend(filter_strategy["notes"])
    if filter_strategy.get("suggestions"):
        suggestions.extend(filter_strategy["suggestions"])

    if candidate_entry_unknown_count > 0:
        notes.append(f"candidate_entry_verdict 미해결 {candidate_entry_unknown_count}건 — 저장/렌더링 경로 fallback 적용")
        suggestions.append("recommendation_history의 candidate 레코드 저장 포맷을 entry_verdict/entry_score로 정규화")
    if not stock_alive:
        issues.append("주식봇 프로세스가 내려가 있음")
        suggestions.append("watchdog로 즉시 재기동 확인")

    if not dashboard_alive:
        issues.append("대시보드 프로세스가 내려가 있음")
        suggestions.append("대시보드 재기동 또는 포트 8501 충돌 확인")

    log_age_threshold_min = _log_age_warning_threshold_min()
    if agent_age_min is not None and agent_age_min > log_age_threshold_min:
        issues.append(f"agent_system.log가 {agent_age_min:.0f}분 이상 갱신되지 않음")
        suggestions.append("파이프라인이 멈췄는지 run_agents / watchdog 상태 확인")

    if notable_errors["rate_limit"] >= 5:
        issues.append(f"rate limit 감지 {notable_errors['rate_limit']}건")
        suggestions.append("일봉 조회/시세 조회 간격과 재시도 백오프를 조금 더 늘리기")

    if notable_errors["empty_response"] >= 3:
        issues.append(f"빈 응답 감지 {notable_errors['empty_response']}건")
        suggestions.append("해당 시장/종목의 응답 패턴과 거래소 맵 재점검")

    if notable_errors["order_fail"] >= 1:
        issues.append(f"주문 실패 감지 {notable_errors['order_fail']}건")
        suggestions.append("최근 실패 종목의 cooldown / TR ID / 주문 조건을 점검")

    if notable_errors["balance_zero"] >= 1:
        issues.append("잔액 동기화 0 덮어쓰기 징후")
        suggestions.append("last_good_balance 유지 로직이 계속 유효한지 확인")

    if notable_errors["unsupported_us"] >= 1:
        notes.append("미국 잔고/서비스 미지원 응답은 현 모의 환경에서는 예상되는 상태")
        suggestions.append("미국 라이브 전환 시에만 US readiness / 서비스 코드 처리 재점검")

    if consecutive_losses >= 2:
        issues.append(f"연속 손실 {consecutive_losses}회")
        suggestions.append("같은 테마/종목 재진입 제한을 잠시 강화")

    recent_pnl_pct = (recent_realized_pnl / max(total_balance, 1)) * 100.0 if total_balance > 0 else 0.0
    if total_sells >= 5 and recent_win_rate < 50.0 and recent_pnl_pct < -0.5:
        issues.append(f"최근 {len(recent_sells)}건 승률 저하 ({recent_win_rate:.1f}%, PnL {recent_realized_pnl:+.0f})")
        suggestions.append("최근 진입 필터를 더 보수적으로 조정")

    repeat_losers = _repeat_losers(recent_sells)
    if repeat_losers:
        top = repeat_losers[0]
        issues.append(f"반복 손실 종목 감지: {top['code']}({top.get('name')})")
        suggestions.append("해당 종목/테마는 일정 기간 재진입 금지 후보로 올리기")

    max_positions_per_market = int(getattr(config, "MAX_POSITIONS_PER_MARKET", 3) or 3)
    _classify_position_slot_usage(
        open_slot_count=len(open_slots),
        max_positions_per_market=max_positions_per_market,
        issues=issues,
        notes=notes,
        suggestions=suggestions,
    )

    if len(open_slots) >= max_positions_per_market and decongestion_candidates:
        weakest = decongestion_candidates[0]
        notes.append(
            f"정리 우선순위 1순위: {weakest.get('code')}({weakest.get('name')}) score={weakest.get('decongestion_score')}"
        )
        suggestions.append("슬롯 해소는 가장 약한 보유부터 검토하고 신규 진입은 잠시 보수적으로 유지")

    loss_trades = [t for t in sells if float(t.get("pnl", 0) or 0) < 0]
    if loss_trades and not symbol_cooldowns_exists:
        notes.append(
            f"symbol_cooldowns.json 미존재 (손절 {len(loss_trades)}건 발생했으나 저장 기록 없음"
            " — 레거시 경로로 처리되었거나 신규 코드 배포 전의 손실)"
        )

    if valuation_estimate["quality"] == "position_based_estimate":
        notes.append(valuation_estimate["note"])

    _classify_empty_pipeline_candidates(
        pipeline_candidate_count=pipeline_candidate_count,
        open_slot_count=len(open_slots),
        max_positions_per_market=max_positions_per_market,
        issues=issues,
        notes=notes,
        suggestions=suggestions,
    )

    latest_trade_time = _latest_trade_time(trades)
    signature = _build_signature(
        stock_alive=stock_alive,
        dashboard_alive=dashboard_alive,
        agent_age_min=agent_age_min,
        notable_errors=notable_errors,
        consecutive_losses=consecutive_losses,
        recent_win_rate=recent_win_rate,
        open_slots=open_slots,
        max_positions_per_market=max_positions_per_market,
        latest_trade_time=latest_trade_time,
        repeat_losers=repeat_losers,
    )

    snapshot = {
        "generated_at": _now_iso(),
        "health": "ok" if not issues else ("warn" if stock_alive and dashboard_alive else "critical"),
        "signals": {
            "stock_alive": stock_alive,
            "dashboard_alive": dashboard_alive,
            "stock_pid": stock_pid,
            "dashboard_pid": dashboard_pid,
            "agent_log_age_min": round(agent_age_min, 1) if agent_age_min is not None else None,
            "stock_log_age_min": round(stock_age_min, 1) if stock_age_min is not None else None,
            "agent_counts": notable_errors,
        },
        "metrics": {
            "realized_pnl": round(realized_pnl, 2),
            "win_rate_pct": round(win_rate, 1),
            "recent_realized_pnl": round(recent_realized_pnl, 2),
            "recent_win_rate_pct": round(recent_win_rate, 1),
            "open_positions": len(open_slots),
            "position_over_limit": len(open_slots) > max_positions_per_market,
            "market_counts": market_counts,
            "daily_pnl": round(risk_daily_pnl, 2),
            "daily_trades": risk_daily_trades,
            "consecutive_losses": consecutive_losses,
            "total_balance": total_balance,
            "deposit": deposit,
            "invested_amount": round(invested_amount, 2),
            "open_market_value_est": open_market_value_est,
            "unrealized_pnl_est": unrealized_pnl_est,
            "unrealized_return_pct_est": unrealized_return_pct_est,
            "valuation_estimate_quality": valuation_estimate["quality"],
            "exit_reason_stats": exit_reason_stats,
            "pipeline_candidate_count": pipeline_candidate_count,
            "pipeline_theme_count": pipeline_theme_count,
        },
        "issues": issues,
        "notes": notes,
        "suggestions": list(dict.fromkeys(suggestions)),
        "evidence": {
            "latest_trade_time": latest_trade_time,
            "last_pipeline_scanned_at": last_pipeline.get("scanned_at") if isinstance(last_pipeline, dict) else None,
            "recent_loss_symbols": [
                {
                    "code": x.get("code"),
                    "name": x.get("name"),
                    "pnl": round(float(x.get("pnl", 0) or 0), 2),
                    "reason": x.get("reason"),
                }
                for x in recent_sells[-5:]
                if float(x.get("pnl", 0) or 0) < 0
            ],
            "market_runtime_keys": list(market_runtime.keys()) if isinstance(market_runtime, dict) else [],
            "active_symbol_cooldowns": active_symbol_cooldowns[:10],
            "open_positions_detail": open_positions_detail,
            "decongestion_candidates": decongestion_candidates,
            "symbol_performance_top_abs_pnl": symbol_performance,
            "blocked_recommendation_count": len(blocked_recommendations),
            "blocked_recommendation_by_outcome": dict(blocked_recommendation_outcomes),
            "blocked_slot_capacity_count": len(blocked_slot_full),
            "candidate_recommendation_count": len(candidate_recommendations),
            "candidate_entry_verdicts": dict(candidate_entry_verdicts),
            "candidate_entry_unknown_count": candidate_entry_unknown_count,
            "candidate_entry_score_avg": round(candidate_entry_score_avg, 2),
            "filter_strategy_diagnostics": filter_strategy,
            "blocked_recommendation_recent": [
                {
                    "timestamp": x.get("timestamp"),
                    "market": x.get("market"),
                    "code": x.get("code"),
                    "name": x.get("name"),
                    "outcome": x.get("outcome"),
                    "entry_verdict": x.get("entry_verdict"),
                    "entry_score": x.get("entry_score"),
                    "blocked_reason": x.get("blocked_reason"),
                    "theme": x.get("theme"),
                }
                for x in blocked_recommendations_recent
            ],
        },
        "signature": signature,
        "log_offsets": {
            "agent_bytes": agent_log_bytes,
            "stock_bytes": stock_log_bytes,
        },
    }
    return snapshot


def main() -> int:
    snapshot = _build_report()
    now_ts = datetime.now(timezone.utc).timestamp()
    emit = bool(snapshot["issues"]) and _should_emit(snapshot["signature"], now_ts=now_ts)

    _write_snapshot(snapshot)
    _append_history(snapshot)
    _persist_state(snapshot["signature"], emit, snapshot)

    if emit:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
