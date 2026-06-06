"""
circuit_breaker.py -- trading safety gate for Stock Bot.

The breaker never places orders. It tracks persistent risk state and returns an
allow/block decision for the stock pipeline, pending-entry executor, and the
autonomous loop.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import config  # type: ignore
except Exception:  # pragma: no cover
    config = None  # type: ignore

UTC = timezone.utc
DEFAULT_STATE = {
    "manual_halt": False,
    "halt_reason": "",
    "halted_at": None,
    "daily": {"date": "", "realized_pnl": 0.0, "loss_pct": 0.0},
    "consecutive_losses": 0,
    "api_failures": 0,
    "symbol_cooldowns": {},
    "events": [],
}


@dataclass
class BreakerDecision:
    allowed: bool
    reason: str = "ok"
    severity: str = "info"
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["details"] = self.details or {}
        return payload


class CircuitBreaker:
    def __init__(
        self,
        state_path: str | Path | None = None,
        *,
        daily_max_loss_pct: float | None = None,
        consecutive_loss_stop: int | None = None,
        api_failure_stop: int | None = None,
        per_symbol_cooldown_hours: float | None = None,
        log_func: Callable | None = None,
    ):
        root = Path(getattr(config, "ROOT_DIR", Path(__file__).resolve().parent))
        data_dir = Path(getattr(config, "DATA_DIR", root / "data"))
        self.state_path = Path(state_path) if state_path else data_dir / "agents" / "circuit_state.json"
        self.daily_max_loss_pct = float(
            (daily_max_loss_pct if daily_max_loss_pct is not None else getattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)) / 100.0
            if (daily_max_loss_pct if daily_max_loss_pct is not None else getattr(config, "MAX_DAILY_LOSS_PERCENT", 5.0)) > 1
            else (daily_max_loss_pct if daily_max_loss_pct is not None else getattr(config, "MAX_DAILY_LOSS_PERCENT", 0.05))
        )
        self.consecutive_loss_stop = int(
            consecutive_loss_stop if consecutive_loss_stop is not None else getattr(config, "MAX_CONSECUTIVE_LOSSES", 3)
        )
        self.api_failure_stop = int(api_failure_stop if api_failure_stop is not None else getattr(config, "API_DEGRADED_RETRY_LIMIT", 3))
        cooldown_seconds = float(getattr(config, "ORDER_COOLDOWN_SECONDS", 900))
        self.per_symbol_cooldown_hours = float(per_symbol_cooldown_hours if per_symbol_cooldown_hours is not None else cooldown_seconds / 3600.0)
        self.log_func = log_func or self._print_log
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self._roll_daily_if_needed()
        self._save_state()

    def status(self) -> dict[str, Any]:
        self._roll_daily_if_needed()
        self._purge_expired_cooldowns()
        return {"decision": self.check(action="status").to_dict(), "state": self.state, "limits": self.limits()}

    def limits(self) -> dict[str, Any]:
        return {
            "daily_max_loss_pct": self.daily_max_loss_pct,
            "consecutive_loss_stop": self.consecutive_loss_stop,
            "api_failure_stop": self.api_failure_stop,
            "per_symbol_cooldown_hours": self.per_symbol_cooldown_hours,
        }

    def check(self, symbol: str | None = None, *, action: str = "entry", equity: float | None = None) -> BreakerDecision:
        self._roll_daily_if_needed()
        self._purge_expired_cooldowns()
        if self.state.get("manual_halt"):
            return BreakerDecision(False, self.state.get("halt_reason") or "manual_halt", "critical")
        loss_pct = abs(float(self.state.get("daily", {}).get("loss_pct", 0.0)))
        if loss_pct >= self.daily_max_loss_pct:
            return BreakerDecision(False, "daily_max_loss_exceeded", "critical", {"loss_pct": loss_pct})
        if int(self.state.get("consecutive_losses", 0)) >= self.consecutive_loss_stop:
            return BreakerDecision(False, "consecutive_loss_stop", "critical", {"count": self.state.get("consecutive_losses")})
        if int(self.state.get("api_failures", 0)) >= self.api_failure_stop:
            return BreakerDecision(False, "api_failure_stop", "error", {"count": self.state.get("api_failures")})
        if symbol:
            until = float((self.state.get("symbol_cooldowns", {}) or {}).get(symbol, 0) or 0)
            if until > time.time():
                return BreakerDecision(False, "symbol_cooldown", "warning", {"symbol": symbol, "until": self._iso(until)})
        return BreakerDecision(True, "ok", "info", {"action": action, "equity": equity})

    def record_trade_result(self, symbol: str, pnl: float, *, equity: float | None = None, cooldown: bool | None = None) -> dict[str, Any]:
        self._roll_daily_if_needed()
        pnl = float(pnl)
        daily = self.state.setdefault("daily", {"date": self._today(), "realized_pnl": 0.0, "loss_pct": 0.0})
        daily["realized_pnl"] = float(daily.get("realized_pnl", 0.0)) + pnl
        if equity and equity > 0:
            daily["loss_pct"] = min(0.0, float(daily["realized_pnl"]) / float(equity))
        if pnl < 0:
            self.state["consecutive_losses"] = int(self.state.get("consecutive_losses", 0)) + 1
            if cooldown is not False:
                self.set_symbol_cooldown(symbol, self.per_symbol_cooldown_hours, save=False)
        else:
            self.state["consecutive_losses"] = 0
            if cooldown is True:
                self.set_symbol_cooldown(symbol, self.per_symbol_cooldown_hours, save=False)
        self._event("trade_result", {"symbol": symbol, "pnl": pnl, "equity": equity})
        self._save_state()
        return self.status()

    def record_api_success(self) -> dict[str, Any]:
        self.state["api_failures"] = 0
        self._event("api_success", {})
        self._save_state()
        return self.status()

    def record_api_error(self, message: str = "") -> dict[str, Any]:
        self.state["api_failures"] = int(self.state.get("api_failures", 0)) + 1
        self._event("api_error", {"message": message, "count": self.state["api_failures"]})
        self._save_state()
        return self.status()

    def set_symbol_cooldown(self, symbol: str, hours: float | None = None, *, save: bool = True) -> dict[str, Any]:
        until = time.time() + float(hours if hours is not None else self.per_symbol_cooldown_hours) * 3600
        self.state.setdefault("symbol_cooldowns", {})[symbol] = until
        self._event("symbol_cooldown", {"symbol": symbol, "until": self._iso(until)})
        if save:
            self._save_state()
        return {"symbol": symbol, "until": self._iso(until)}

    def trip(self, reason: str = "manual_halt") -> dict[str, Any]:
        self.state["manual_halt"] = True
        self.state["halt_reason"] = reason
        self.state["halted_at"] = self._now_iso()
        self._event("manual_halt", {"reason": reason})
        self._save_state()
        return self.status()

    def reset(self, *, clear_cooldowns: bool = False, clear_manual_halt: bool = True) -> dict[str, Any]:
        if clear_manual_halt:
            self.state["manual_halt"] = False
            self.state["halt_reason"] = ""
            self.state["halted_at"] = None
        self.state["consecutive_losses"] = 0
        self.state["api_failures"] = 0
        if clear_cooldowns:
            self.state["symbol_cooldowns"] = {}
        self._event("reset", {"clear_cooldowns": clear_cooldowns, "clear_manual_halt": clear_manual_halt})
        self._save_state()
        return self.status()

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_path.exists():
                return self._merge_defaults(json.loads(self.state_path.read_text(encoding="utf-8")))
        except Exception as exc:
            self._safe_log("circuit_state_corrupt", f"{self.state_path}: {exc}")
            try:
                self.state_path.rename(self.state_path.with_suffix(self.state_path.suffix + f".broken_{int(time.time())}"))
            except Exception:
                pass
        return self._merge_defaults({})

    def _merge_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        merged = json.loads(json.dumps(DEFAULT_STATE))
        for k, v in (data or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def _roll_daily_if_needed(self) -> None:
        today = self._today()
        if self.state.setdefault("daily", {}).get("date") != today:
            self.state["daily"] = {"date": today, "realized_pnl": 0.0, "loss_pct": 0.0}
            self._event("daily_roll", {"date": today})

    def _purge_expired_cooldowns(self) -> None:
        now = time.time()
        cooldowns = self.state.setdefault("symbol_cooldowns", {})
        for sym in [sym for sym, until in cooldowns.items() if float(until or 0) <= now]:
            cooldowns.pop(sym, None)

    def _event(self, event_type: str, detail: dict[str, Any]) -> None:
        events = self.state.setdefault("events", [])
        events.append({"ts": self._now_iso(), "type": event_type, "detail": detail})
        del events[:-100]
        self._safe_log(f"circuit_{event_type}", json.dumps(detail, ensure_ascii=False)[:500])

    def _safe_log(self, event_type: str, detail: str) -> None:
        try:
            self.log_func(event_type, "circuit breaker", detail)
        except TypeError:
            try:
                self.log_func(f"{event_type}: {detail}")
            except Exception:
                print(f"{event_type}: {detail}")
        except Exception:
            print(f"{event_type}: {detail}")

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(float(ts), UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _print_log(*args) -> None:
        print(*args)


def check_entry(symbol: str, equity: float | None = None, state_path: str | Path | None = None) -> dict[str, Any]:
    return CircuitBreaker(state_path=state_path).check(symbol, action="entry", equity=equity).to_dict()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or control the stock circuit breaker")
    parser.add_argument("action", choices=["status", "trip", "reset", "check"], nargs="?", default="status")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--reason", default="manual_halt")
    parser.add_argument("--state-path", default=None)
    args = parser.parse_args()
    breaker = CircuitBreaker(state_path=args.state_path)
    if args.action == "trip":
        out = breaker.trip(args.reason)
    elif args.action == "reset":
        out = breaker.reset(clear_cooldowns=True)
    elif args.action == "check":
        out = breaker.check(args.symbol).to_dict()
    else:
        out = breaker.status()
    print(json.dumps(out, indent=2, ensure_ascii=False))
