"""Stock Bot Pro Dashboard - 모바일 프로페셔널 웹 대시보드."""

import json
import logging
import sys
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT_DIR = Path(__file__).parent
sys.path.insert(0, str(ROOT_DIR))

import config
from core.data_store import DataStore
from kis_api import KISAPI

app = Flask(__name__)
logger = logging.getLogger(__name__)

# 데이터 파일 경로
RATIONALE_FILE = config.DATA_DIR / "trade_rationale.json"
EQUITY_HISTORY_FILE = config.DATA_DIR / "equity_history.json"
AGENT_STATE_FILE = config.DATA_DIR / "agents" / "state.json"

# KIS API 인스턴스
_kis = None
_store = None
_kis_api_lock = threading.Lock()


def get_kis() -> KISAPI:
    global _kis
    if _kis is None:
        _kis = KISAPI()
    return _kis


def get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store


# ── 데이터 로드 유틸 ──────────────────────────────────

def load_json(path, default=None):
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            pass
    return default if default is not None else {}


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_state():
    return load_json(config.STATE_FILE, {
        "status": "waiting", "last_scan": None,
        "scan_count": 0, "trades_today": 0, "pnl_today": 0,
    })


def load_trade_history():
    return load_json(config.TRADE_HISTORY_FILE, [])


def normalize_trade(trade):
    """대시보드 표시용으로 거래 필드를 단일 형태로 맞춘다."""
    item = dict(trade)
    side = str(item.get("side") or item.get("action") or "").lower()
    if side in ("매수", "buy"):
        side = "buy"
    elif side in ("매도", "sell"):
        side = "sell"

    price = item.get("price")
    if price in (None, ""):
        price = item.get("sell_price") if side == "sell" else item.get("entry_price")

    item["side"] = side
    item["action"] = side or item.get("action", "")
    item["price"] = price or 0
    item["name"] = item.get("name") or item.get("stock_name") or item.get("code") or "-"
    item["timestamp"] = item.get("timestamp") or item.get("time") or item.get("date") or ""
    return item


def _position_key(position):
    return f"{position.get('market', 'KR')}_{str(position.get('code', '')).strip()}"


def normalize_account_position(position, market, bot_slots):
    item = dict(position)
    item["market"] = market
    key = _position_key(item)
    slot = bot_slots.get(key)
    item["source"] = "matched" if slot else "account"
    if slot:
        account_qty = float(item.get("quantity") or 0)
        slot_qty = float(slot.get("quantity") or 0)
        item["slot_id"] = key
        item["slot_entry_price"] = slot.get("entry_price")
        item["slot_quantity"] = slot.get("quantity")
        item["quantity_mismatch"] = account_qty != slot_qty
    return item


def normalize_slot_position(slot_id, slot):
    item = {
        "slot_id": slot_id,
        "market": slot.get("market", "KR"),
        "code": slot.get("code", ""),
        "name": slot.get("name") or slot.get("code") or "-",
        "quantity": slot.get("quantity", 0),
        "avg_price": slot.get("entry_price", 0),
        "current_price": slot.get("current_price") or slot.get("highest_price") or slot.get("entry_price", 0),
        "pnl": 0,
        "pnl_rate": 0,
        "source": "bot_slot",
        "entry_time": slot.get("entry_time") or slot.get("synced_at"),
        "reason": slot.get("reason"),
    }
    current = float(item["current_price"] or 0)
    entry = float(item["avg_price"] or 0)
    quantity = float(item["quantity"] or 0)
    if current > 0 and entry > 0 and quantity > 0:
        item["pnl"] = (current - entry) * quantity
        item["pnl_rate"] = ((current - entry) / entry) * 100
    return item


def calc_stock_eval(stocks):
    total = 0
    for stock in stocks:
        try:
            total += float(stock.get("current_price", 0) or 0) * float(stock.get("quantity", 0) or 0)
        except Exception:
            continue
    return total


def load_rationale():
    return load_json(RATIONALE_FILE, {"entries": []})


def load_equity_history():
    return load_json(EQUITY_HISTORY_FILE, {"history": []})


def load_agent_state():
    return load_json(AGENT_STATE_FILE, {
        "agents": {
            "market_scout": {"status": "idle", "last_active": None},
            "technical_analyst": {"status": "idle", "last_active": None},
            "risk_manager": {"status": "idle", "last_active": None},
            "trade_executor": {"status": "idle", "last_active": None},
            "monitor": {"status": "idle", "last_active": None},
            "orchestrator": {"status": "idle", "last_active": None},
        }
    })


# ── 라우트 ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/overview")
def api_overview():
    """계좌 개요 + 잔고 (국내 + 미국 통합)."""
    try:
        kis = get_kis()
        with _kis_api_lock:
            bal = kis.get_balance()
            if bal.get("balance_status") == "failed":
                raise RuntimeError("KIS 잔고 조회 실패")
            # 미국 잔고 (있다면)
            us_bal = None
            if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
                try:
                    us_bal = kis.get_us_balance()
                except Exception:
                    us_bal = None
            # 환율 (기본값)
            exchange_rate = 1350.0
            try:
                rate = kis.get_exchange_rate()
                if rate and rate > 0:
                    exchange_rate = rate
            except Exception:
                pass
        state = load_state()

        # 국내 잔고: KIS tot_evlu_amt는 예수금 포함 총평가로 취급한다.
        kr_cash = bal.get("cash", bal.get("total_deposit", 0))
        kr_assets = bal.get("total_assets", bal.get("total_eval", 0))
        kr_stock_eval = bal.get("stock_eval") or calc_stock_eval(bal.get("stocks", []))
        kr_pnl = bal.get("total_pnl", 0)
        kr_stocks = bal.get("stocks", [])

        us_eval = 0
        us_pnl = 0
        us_stocks = []
        if us_bal:
            us_eval = us_bal.get("total_eval", us_bal.get("total_usd", 0))
            us_pnl = us_bal.get("total_pnl", us_bal.get("total_pnl_usd", 0))
            us_stocks = us_bal.get("stocks", [])

        # 총자산 (원화 환산)
        total_us_kr = us_eval * exchange_rate
        total_assets = kr_assets + total_us_kr
        total_pnl = kr_pnl + (us_pnl * exchange_rate)
        pnl_base = total_assets - total_pnl

        return jsonify({
            "success": True,
            "mode": kis.mode,
            "account": f"{kis.account_no}-{kis.account_product}",
            "exchange_rate": exchange_rate,
            "kr": {
                "cash": kr_cash,
                "deposit": kr_cash,
                "stock_eval": kr_stock_eval,
                "eval": kr_assets,
                "total_assets": kr_assets,
                "pnl": kr_pnl,
                "stocks_count": len(kr_stocks),
            },
            "us": {
                "eval": us_eval,
                "pnl": us_pnl,
                "stocks_count": len(us_stocks),
            },
            "total": {
                "assets": total_assets,
                "pnl": total_pnl,
                "pnl_pct": round((total_pnl / pnl_base) * 100, 2) if pnl_base > 0 else 0,
            },
            "bot_status": state.get("status", "waiting"),
            "last_scan": state.get("last_scan"),
            "scan_count": state.get("scan_count", 0),
            "trades_today": state.get("trades_today", 0),
            "pnl_today": state.get("pnl_today", 0),
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/positions")
def api_positions():
    """보유 종목 상세 (국내 + 미국)."""
    try:
        kis = get_kis()
        store = get_store()
        bot_slots = store.load_all_slots()
        with _kis_api_lock:
            bal = kis.get_balance()
            if bal.get("balance_status") == "failed":
                raise RuntimeError("KIS 잔고 조회 실패")
        kr_stocks = bal.get("stocks", [])

        positions = []
        account_keys = set()
        for s in kr_stocks:
            item = normalize_account_position(s, "KR", bot_slots)
            account_keys.add(_position_key(item))
            positions.append(item)

        if config.US_STOCK_ENABLED and config.KIS_MODE == "real":
            try:
                with _kis_api_lock:
                    us_bal = kis.get_us_balance()
                for s in us_bal.get("stocks", []):
                    item = normalize_account_position(s, "US", bot_slots)
                    account_keys.add(_position_key(item))
                    positions.append(item)
            except Exception:
                pass

        for slot_id, slot in bot_slots.items():
            if slot and slot.get("code") and slot_id not in account_keys:
                positions.append(normalize_slot_position(slot_id, slot))

        positions.sort(key=lambda x: x.get("pnl_rate", 0), reverse=True)
        return jsonify({"success": True, "positions": positions, "count": len(positions)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "positions": []})


@app.route("/api/trades")
def api_trades():
    """거래 내역."""
    trades = [normalize_trade(t) for t in get_store().get_trades(limit=200)]
    trades.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
    limit = int(request.args.get("limit", 50))
    return jsonify({"success": True, "trades": trades[:limit], "count": len(trades)})


@app.route("/api/rationale")
def api_rationale():
    """매수/매도 사유 (Investment Thesis)."""
    data = load_rationale()
    entries = data.get("entries", [])
    limit = int(request.args.get("limit", 20))
    return jsonify({"success": True, "entries": entries[:limit], "count": len(entries)})


@app.route("/api/market_compare")
def api_market_compare():
    """당일 수익률 vs 시장 지수 비교."""
    try:
        kis = get_kis()
        state = load_state()

        # 봇 당일 수익률
        bot_pnl_today = state.get("pnl_today", 0)
        kr_budget = config.KR_BUDGET
        bot_return_pct = round((bot_pnl_today / kr_budget) * 100, 2) if kr_budget > 0 else 0

        # 시장 지수 조회
        market_data = {"bot": bot_return_pct}

        indices = {
            "KOSPI": "0001",     # 코스피
            "KOSDAQ": "1001",    # 코스닥
        }

        for name, code in indices.items():
            try:
                price = kis.get_stock_price(code)
                if price:
                    change_rate = price.get("change_rate", 0)
                    market_data[name] = round(change_rate, 2)
                else:
                    market_data[name] = 0
            except Exception:
                market_data[name] = 0

        # S&P500 (미국 지수는 전일 종가 기준)
        market_data["S&P500"] = 0  # TODO: 미국장 시간에 실시간

        return jsonify({
            "success": True,
            "data": market_data,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "data": {"bot": 0, "KOSPI": 0, "KOSDAQ": 0}})


@app.route("/api/equity_curve")
def api_equity_curve():
    """자산 추이 데이터."""
    data = load_equity_history()
    history = data.get("history", [])
    days = int(request.args.get("days", 30))
    return jsonify({
        "success": True,
        "history": history[-days:],
        "count": len(history),
    })


@app.route("/api/agents")
def api_agents():
    """에이전트 상태."""
    data = load_agent_state()
    return jsonify({"success": True, "agents": data.get("agents", {})})


@app.route("/api/config")
def api_config():
    """현재 설정."""
    return jsonify({
        "success": True,
        "config": {
            "mode": config.KIS_MODE,
            "account": f"{config.KIS_ACCOUNT_NO}-{config.KIS_ACCOUNT_PRODUCT}",
            "ai_model": config.ZAI_MODEL,
            "stop_loss": config.STOP_LOSS_PERCENT,
            "take_profit": config.TAKE_PROFIT_PERCENT,
            "max_daily_loss": config.MAX_DAILY_LOSS_PERCENT,
            "max_consecutive_losses": config.MAX_CONSECUTIVE_LOSSES,
            "position_size": config.POSITION_SIZE_PERCENT,
            "scan_interval": config.NEWS_SCAN_INTERVAL // 60,
            "us_stock_enabled": config.US_STOCK_ENABLED,
            "kr_budget": config.KR_BUDGET,
            "us_budget": config.US_BUDGET,
            "atr_period": config.ATR_PERIOD,
            "stop_loss_atr_multi": config.STOP_LOSS_ATR_MULTI,
            "take_profit_atr_multi": config.TAKE_PROFIT_ATR_MULTI,
            "trailing_stop_atr_multi": config.TRAILING_STOP_ATR_MULTI,
            "trailing_activate_pct": config.TRAILING_ACTIVATE_PCT,
        }
    })


@app.route("/api/bot/control", methods=["POST"])
def api_bot_control():
    """봇 제어 (시작/정지)."""
    action = request.json.get("action", "")
    if action in ("start", "stop"):
        state = load_state()
        state["status"] = "running" if action == "start" else "stopped"
        save_json(config.STATE_FILE, state)
        return jsonify({"success": True, "status": state["status"]})
    return jsonify({"success": False, "error": "invalid action"})


@app.route("/api/equity_snapshot", methods=["POST"])
def api_equity_snapshot():
    """자산 스냅샷 저장 (Orchestrator에서 호출)."""
    try:
        data = load_equity_history()
        snapshot = request.json
        snapshot["timestamp"] = datetime.now().isoformat()
        data.setdefault("history", []).append(snapshot)
        # 최대 90일 유지
        if len(data["history"]) > 90:
            data["history"] = data["history"][-90:]
        save_json(EQUITY_HISTORY_FILE, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/rationale/save", methods=["POST"])
def api_rationale_save():
    """매수/매도 사유 저장 (Agent에서 호출)."""
    try:
        data = load_rationale()
        entry = request.json
        entry["timestamp"] = datetime.now().isoformat()
        data.setdefault("entries", []).insert(0, entry)
        # 최대 100개 유지
        if len(data["entries"]) > 100:
            data["entries"] = data["entries"][:100]
        save_json(RATIONALE_FILE, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    port = int(os.environ.get("DASHBOARD_PORT", 8501))
    print(f"📊 Stock Bot Pro Dashboard: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
