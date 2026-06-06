from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import config

logger = logging.getLogger("runtime_policy")


@dataclass(frozen=True)
class RuntimePolicy:
    """Runtime guardrails derived from deterministic stock-bot TODOs.

    Fail-open by design: missing, malformed, or unreadable TODO files must not
    stop trading. Only open P0/P1 TODOs affect live exposure-increasing paths.
    """

    block_new_entries: bool = False
    block_scale_in: bool = False
    conservative_mode: bool = False
    excluded_codes: frozenset[str] = frozenset()
    excluded_names: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()


_OPEN_STATUSES = {"open", "pending", "in_progress", "todo", "doing"}
_KRX_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def todos_path() -> Path:
    return config.DATA_DIR / "agents" / "stock_bot_todos.json"


def _read_todos(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("stock runtime policy TODO read failed; failing open: %s", exc)
        return []

    raw = data.get("todos", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _is_open(item: dict[str, Any]) -> bool:
    status = str(item.get("status", "open")).strip().lower()
    return status in _OPEN_STATUSES


def _normalize_code(value: object) -> str:
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def _candidate_name_index(candidates: Iterable[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _extract_codes(text: str, tradable_codes: set[str] | None) -> set[str]:
    found = {m.group(0) for m in _KRX_CODE_RE.finditer(text)}
    if tradable_codes is not None:
        found &= tradable_codes
    return found


def _extract_names(text: str, known_names: set[str] | None) -> set[str]:
    if not known_names:
        return set()
    return {name for name in known_names if name and name in text}


def load_runtime_policy(candidates: Iterable[dict[str, Any]] | None = None) -> RuntimePolicy:
    """Build runtime policy from data/agents/stock_bot_todos.json.

    Mapping:
    - open P0 TODO: block new entries and scale-in/exposure increases.
    - open P1 TODO: conservative mode; any mentioned 6-digit KRX code or known
      candidate name is excluded from new entries while the TODO remains open.
    - P2 and closed TODOs: observability only.
    """

    candidate_list = list(candidates or [])
    tradable_codes = {
        _normalize_code(item.get("code"))
        for item in candidate_list
        if isinstance(item, dict) and item.get("code")
    } or None
    known_names = _candidate_name_index(candidate_list)

    block_new_entries = False
    block_scale_in = False
    conservative_mode = False
    excluded_codes: set[str] = set()
    excluded_names: set[str] = set()
    reasons: list[str] = []

    for item in _read_todos(todos_path()):
        if not _is_open(item):
            continue
        priority = str(item.get("priority", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        detail = str(item.get("detail", "")).strip()
        text = f"{title} {detail}"
        reason = f"{priority}:{title}" if title else priority

        if priority == "P0":
            block_new_entries = True
            block_scale_in = True
            if reason:
                reasons.append(reason)
        elif priority == "P1":
            conservative_mode = True
            if reason:
                reasons.append(reason)
            excluded_codes |= _extract_codes(text, tradable_codes)
            excluded_names |= _extract_names(text, known_names)

    return RuntimePolicy(
        block_new_entries=block_new_entries,
        block_scale_in=block_scale_in,
        conservative_mode=conservative_mode,
        excluded_codes=frozenset(sorted(excluded_codes)),
        excluded_names=frozenset(sorted(excluded_names)),
        reasons=tuple(reasons),
    )


def runtime_entry_skip_reason(candidate: dict[str, Any], policy: RuntimePolicy) -> str:
    if policy.block_new_entries:
        reason = "; ".join(policy.reasons) or "open P0 TODO"
        return f"runtime_policy_entry_block:{reason}"

    code = _normalize_code(candidate.get("code"))
    name = str(candidate.get("name") or "").strip()
    if code and code in policy.excluded_codes:
        return f"runtime_policy_symbol_excluded:{code}"
    if name and name in policy.excluded_names:
        return f"runtime_policy_symbol_excluded:{name}"
    return ""


def runtime_scale_in_skip_reason(policy: RuntimePolicy) -> str:
    if policy.block_scale_in:
        reason = "; ".join(policy.reasons) or "open P0 TODO"
        return f"runtime_policy_scale_in_block:{reason}"
    return ""
