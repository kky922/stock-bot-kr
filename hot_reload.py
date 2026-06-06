"""
hot_reload.py -- params.json based strategy/risk hot reloader.

This module intentionally handles strategy/risk parameters only. Secrets remain in
.env/config and are never copied to params.json or history files.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class HotReloader:
    def __init__(self, params_path: str, config_module, history_dir: str,
                 check_interval: float = 5.0, log_func: Callable | None = None):
        self.params_path = Path(params_path)
        self.config_module = config_module
        self.history_dir = Path(history_dir)
        self.check_interval = float(check_interval)
        self.log_func = log_func or self._print_log
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mtime: float | None = None
        self._lock = threading.RLock()
        self._current_version = 0
        self.history_dir.mkdir(parents=True, exist_ok=True)
        if self.params_path.exists():
            try:
                data = self._read_params()
                self._current_version = int(data.get("_meta", {}).get("version", 0) or 0)
                self._last_mtime = self.params_path.stat().st_mtime
            except Exception as exc:
                self._log("params_reload_failed", f"initial read failed: {exc}")

    def start(self) -> None:
        """Start polling in a daemon background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, name="HotReloader", daemon=True)
        self._thread.start()
        self._log("params_reloader_started", f"path={self.params_path} interval={self.check_interval}")

    def stop(self) -> None:
        """Stop polling."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.check_interval + 1.0))
        self._log("params_reloader_stopped", f"path={self.params_path}")

    def reload_now(self) -> dict:
        """Validate params.json, backup previous file, apply values to config, and return changed params."""
        with self._lock:
            data = self._read_params()
            violations = self._validate_ranges(data)
            if violations:
                detail = "; ".join(violations)
                self._log("params_reload_rejected", detail)
                raise ValueError(f"params range violation: {detail}")

            old_values = self._current_config_values(data)
            flat = self._flatten_params(data)
            changed = {k: v for k, v in flat.items() if old_values.get(k) != v}

            self._backup_current_file()
            applied = self._apply_to_config(flat)
            version = self._bump_file_version(data)
            self._current_version = version
            self._last_mtime = self.params_path.stat().st_mtime
            self._log("params_reloaded", f"version={version} changed={sorted(changed.keys())} applied={sorted(applied.keys())}")
            return changed

    def get_current_version(self) -> int:
        return int(self._current_version or 0)

    def rollback(self, version: int | None = None) -> dict:
        """Rollback params.json to a history version. If version is omitted, use the latest backup."""
        with self._lock:
            target = self._history_file(version)
            if target is None or not target.exists():
                raise FileNotFoundError(f"rollback target not found: version={version}")
            self._backup_current_file(prefix="rollback_from")
            shutil.copy2(target, self.params_path)
            changed = self.reload_now()
            self._log("params_rollback", f"rolled_back_to={target.name}")
            return changed

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.params_path.exists():
                    mtime = self.params_path.stat().st_mtime
                    if self._last_mtime is None:
                        self._last_mtime = mtime
                    elif mtime > self._last_mtime:
                        self.reload_now()
                else:
                    self._log("params_missing", str(self.params_path))
            except Exception as exc:
                self._log("params_reload_failed", str(exc))
                try:
                    if self.params_path.exists():
                        self._last_mtime = self.params_path.stat().st_mtime
                except Exception:
                    pass
            self._stop_event.wait(self.check_interval)

    def _read_params(self) -> dict:
        with self.params_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("params root must be a JSON object")
        for section in ("strategy", "risk", "allowed_range"):
            if section in data and not isinstance(data[section], dict):
                raise ValueError(f"{section} must be an object")
        return data

    def _flatten_params(self, params: dict) -> dict:
        flat: dict[str, Any] = {}
        for section in ("strategy", "risk"):
            flat.update(params.get(section, {}) or {})
        return flat

    def _current_config_values(self, params: dict) -> dict:
        return {k: getattr(self.config_module, k, None) for k in self._flatten_params(params)}

    def _apply_to_config(self, flat: dict) -> dict:
        applied: dict[str, Any] = {}
        for key, value in flat.items():
            current = getattr(self.config_module, key, None)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            setattr(self.config_module, key, value)
            applied[key] = value
        return applied

    def _validate_ranges(self, new_params: dict) -> list:
        violations: list[str] = []
        allowed = new_params.get("allowed_range", {}) or {}
        flat = self._flatten_params(new_params)
        for key, bounds in allowed.items():
            if key not in flat:
                continue
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                violations.append(f"{key}: invalid allowed_range {bounds!r}")
                continue
            value = flat[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, list):
                for item in value:
                    if item < bounds[0] or item > bounds[1]:
                        violations.append(f"{key}: item {item} outside {bounds}")
            elif value < bounds[0] or value > bounds[1]:
                violations.append(f"{key}: {value} outside {bounds}")
        return violations

    def _backup_current_file(self, prefix: str = "params") -> Path | None:
        if not self.params_path.exists():
            return None
        try:
            current = self._read_params()
            version = int(current.get("_meta", {}).get("version", self._current_version or 0) or 0)
        except Exception:
            version = int(time.time())
        candidate = self.history_dir / f"{prefix}_v{version:03d}.json"
        if candidate.exists():
            candidate = self.history_dir / f"{prefix}_v{version:03d}_{int(time.time())}.json"
        shutil.copy2(self.params_path, candidate)
        return candidate

    def _bump_file_version(self, data: dict) -> int:
        meta = dict(data.get("_meta", {}) or {})
        old_version = int(meta.get("version", self._current_version or 0) or 0)
        meta["version"] = old_version + 1
        meta["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        meta.setdefault("updated_by", "hot_reload")
        data["_meta"] = meta
        tmp = self.params_path.with_suffix(self.params_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.params_path)
        return int(meta["version"])

    def _history_file(self, version: int | None) -> Path | None:
        files = sorted(self.history_dir.glob("params_v*.json"))
        if version is None:
            if not files:
                return None
            try:
                current_flat = self._flatten_params(self._read_params())
                for candidate in reversed(files):
                    with candidate.open("r", encoding="utf-8") as f:
                        candidate_flat = self._flatten_params(json.load(f))
                    if candidate_flat != current_flat:
                        return candidate
            except Exception:
                pass
            return files[-1]
        exact = self.history_dir / f"params_v{version:03d}.json"
        if exact.exists():
            return exact
        matches = sorted(self.history_dir.glob(f"params_v{version:03d}_*.json"))
        return matches[-1] if matches else None

    def _log(self, event_type: str, detail: str) -> None:
        try:
            self.log_func(event_type, "params hot reload", detail)
        except TypeError:
            try:
                self.log_func(f"{event_type}: {detail}")
            except Exception:
                print(f"{event_type}: {detail}")
        except Exception:
            print(f"{event_type}: {detail}")

    @staticmethod
    def _print_log(*args) -> None:
        print(*args)
